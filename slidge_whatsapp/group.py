import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator, Optional

from slidge.group import LegacyBookmarks, LegacyMUC, LegacyParticipant, MucType
from slidge.util.archive_msg import HistoryMessage
from slidge.util.types import Avatar, Hat, HoleBound, Mention, MucAffiliation
from slixmpp.exceptions import XMPPError

from .generated import go, whatsapp

if TYPE_CHECKING:
    from .contact import Contact
    from .session import Session


class Participant(LegacyParticipant):
    contact: "Contact"
    muc: "MUC"


class MUC(LegacyMUC[str, str, Participant, str]):
    session: "Session"
    type = MucType.GROUP

    HAS_DESCRIPTION = False
    REACTIONS_SINGLE_EMOJI = True

    async def update_info(self):
        try:
            unique_id = ""
            if self.avatar is not None:
                unique_id = self.avatar.unique_id or ""  # type:ignore[assignment]
            avatar = self.session.whatsapp.GetAvatar(self.legacy_id, unique_id)
            if avatar.URL and unique_id != avatar.ID:
                await self.set_avatar(Avatar(url=avatar.URL, unique_id=avatar.ID))
            elif avatar.URL == "" and avatar.ID == "":
                await self.set_avatar(None)
        except RuntimeError as err:
            self.session.log.error(
                "Failed getting avatar for group %s: %s", self.legacy_id, err
            )

    async def backfill(
        self,
        after: HoleBound | None = None,
        before: HoleBound | None = None,
    ):
        """
        Request history for messages older than the oldest message given by ID and date.
        """

        if before is None:
            return
            # WhatsApp requires a full reference to the last seen message in performing on-demand sync.

        assert isinstance(before.id, str)
        oldest_message = whatsapp.Message(
            ID=before.id,
            IsCarbon=self.session.message_is_carbon(self, before.id),
            Timestamp=int(before.timestamp.timestamp()),
        )
        self.session.whatsapp.RequestMessageHistory(self.legacy_id, oldest_message)

    def get_message_sender(self, legacy_msg_id: str):
        with self.xmpp.store.session() as orm:
            try:
                stored = next(
                    self.xmpp.store.mam.get_messages(
                        orm, self.stored.id, ids=[legacy_msg_id]
                    )
                )
            except StopIteration:
                stored = None
        if not stored:
            raise XMPPError("internal-server-error", "Unable to find message sender")
        msg = HistoryMessage(stored.stanza)
        occupant_id = msg.stanza["occupant-id"]["id"]
        if occupant_id == "slidge-user":
            return self.session.contacts.user_legacy_id
        if "@" in occupant_id:
            jid_username = occupant_id.split("@")[0]
            return jid_username.removeprefix("+") + "@" + whatsapp.DefaultUserServer
        raise XMPPError("internal-server-error", "Unable to find message sender")

    async def update_whatsapp_info(self, info: whatsapp.Group):
        """
        Set MUC information based on WhatsApp group information, which may or may not be partial in
        case of updates to existing MUCs.
        """
        if info.Nickname:
            self.user_nick = info.Nickname
        if info.Name:
            self.name = info.Name
        if info.Subject.Subject:
            self.subject = info.Subject.Subject
            if info.Subject.SetAt:
                set_at = datetime.fromtimestamp(info.Subject.SetAt, tz=timezone.utc)
                self.subject_date = set_at
            if info.Subject.SetBy:
                self.subject_setter = info.Subject.SetBy
        self.session.whatsapp_participants[self.legacy_id] = info.Participants
        self.n_participants = len(info.Participants)
        # Since whatsmeow does always emit a whatsapp.Group event even for participant changes,
        # we need to do that to actually update the participant list.
        if self.participants_filled:
            async for _ in self.fill_participants():
                pass

    async def fill_participants(self) -> AsyncIterator[Participant]:
        await self.session.bookmarks.ready
        try:
            participants = self.session.whatsapp_participants.pop(self.legacy_id)
        except KeyError:
            self.log.warning("No participants!")
            return
        for data in participants:
            if whatsapp.IsAnonymousJID(data.JID):
                participant = await self.get_participant(data.JID)
                if data.Nickname:
                    participant.nickname = data.Nickname
            else:
                participant = await self.get_participant_by_legacy_id(data.JID)
            if data.Action == whatsapp.GroupParticipantActionRemove:
                self.remove_participant(participant)
            else:
                if data.Affiliation == whatsapp.GroupAffiliationAdmin:
                    # Only owners can change the group name according to
                    # XEP-0045, so we make all "WA admins" "XMPP owners"
                    participant.affiliation = "owner"
                    participant.role = "moderator"
                elif data.Affiliation == whatsapp.GroupAffiliationOwner:
                    # The WA owner is in fact the person who created the room
                    participant.set_hats(
                        [Hat("https://slidge.im/hats/slidge-whatsapp/owner", "Owner")]
                    )
                    participant.affiliation = "owner"
                    participant.role = "moderator"
                else:
                    participant.affiliation = "member"
                    participant.role = "participant"
                yield participant

    async def replace_mentions(self, t: str):
        return replace_whatsapp_mentions(
            t,
            participants=(
                {
                    p.contact.jid.username: p.nickname
                    async for p in self.get_participants()
                    if p.contact is not None  # should not happen
                }
                | {self.session.user_phone: self.user_nick}
                if self.session.user_phone  # user_phone *should* be set at this point,
                else {}  # but better safe than sorry
            ),
        )

    async def on_avatar(self, data: Optional[bytes], mime: Optional[str]) -> None:
        return self.session.whatsapp.SetAvatar(
            self.legacy_id,
            go.Slice_byte.from_bytes(data) if data else go.Slice_byte(),
        )

    async def on_set_config(
        self,
        name: Optional[str],
        description: Optional[str],
    ):
        # there are no group descriptions in WA, but topics=subjects
        if self.name != name:
            self.session.whatsapp.SetGroupName(self.legacy_id, name)

    async def on_set_subject(self, subject: str):
        if self.subject != subject:
            self.session.whatsapp.SetGroupTopic(self.legacy_id, subject)

    async def on_set_affiliation(
        self,
        contact: "Contact",  # type:ignore
        affiliation: MucAffiliation,
        reason: Optional[str],
        nickname: Optional[str],
    ):
        if affiliation == "member":
            participant = await self.get_participant_by_contact(contact, create=False)
            if participant is None or participant.affiliation in ("outcast", "none"):
                action = whatsapp.GroupParticipantActionAdd
            elif participant.affiliation == "member":
                return
            else:
                action = whatsapp.GroupParticipantActionDemote
        elif affiliation == "admin":
            action = whatsapp.GroupParticipantActionPromote
        elif affiliation == "outcast" or affiliation == "none":
            action = whatsapp.GroupParticipantActionRemove
        else:
            raise XMPPError(
                "bad-request",
                f"You can't make a participant '{affiliation}' in WhatsApp",
            )
        self.session.whatsapp.UpdateGroupParticipants(
            self.legacy_id,
            whatsapp.Slice_whatsapp_GroupParticipant(
                [whatsapp.GroupParticipant(JID=contact.legacy_id, Action=action)]
            ),
        )


class Bookmarks(LegacyBookmarks[str, MUC]):
    session: "Session"

    def __init__(self, session: "Session"):
        super().__init__(session)
        self.__filled = False

    async def fill(self):
        groups = self.session.whatsapp.GetGroups()
        for group in groups:
            await self.add_whatsapp_group(group)
        self.__filled = True

    async def add_whatsapp_group(self, data: whatsapp.Group):
        muc = await self.by_legacy_id(data.JID)
        await muc.update_whatsapp_info(data)
        await muc.add_to_bookmarks()

    async def legacy_id_to_jid_local_part(self, legacy_id: str):
        return "#" + legacy_id[: legacy_id.find("@")]

    async def jid_local_part_to_legacy_id(self, local_part: str):
        if not local_part.startswith("#"):
            raise XMPPError("bad-request", "Invalid group ID, expected '#' prefix")

        if not self.__filled:
            raise XMPPError(
                "recipient-unavailable", "Still fetching group info, please retry later"
            )

        whatsapp_group_id = (
            local_part.removeprefix("#") + "@" + whatsapp.DefaultGroupServer
        )

        if not await self.by_legacy_id(whatsapp_group_id, create=False):
            raise XMPPError("item-not-found", f"No group found for {whatsapp_group_id}")

        return whatsapp_group_id


def replace_xmpp_mentions(text: str, mentions: list[Mention]):
    offset: int = 0
    result: str = ""
    for m in mentions:
        legacy_id = "@" + m.contact.legacy_id[: m.contact.legacy_id.find("@")]
        result = result + text[offset : m.start] + legacy_id
        offset = m.end
    return result + text[offset:] if offset > 0 else text


def replace_whatsapp_mentions(text: str, participants: dict[str, str]):
    def match(m: re.Match):
        group = m.group(0)
        return participants.get(group.replace("@", "+"), group)

    return re.sub(r"@\d+", match, text)
