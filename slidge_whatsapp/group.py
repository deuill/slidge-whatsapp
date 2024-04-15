import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from slidge.group import LegacyBookmarks, LegacyMUC, LegacyParticipant, MucType
from slidge.util.types import Hat, Mention, MucAffiliation
from slixmpp.exceptions import XMPPError

from .generated import whatsapp
from .util import get_bytes_temp

if TYPE_CHECKING:
    from .contact import Contact
    from .session import Session


class Participant(LegacyParticipant):
    contact: "Contact"
    muc: "MUC"

    def send_text(self, body, legacy_msg_id, **kw):
        res = super().send_text(body, legacy_msg_id, **kw)
        self._store(legacy_msg_id)
        return res

    async def send_file(self, file_path, legacy_msg_id, **kw):
        res = await super().send_file(file_path, legacy_msg_id, **kw)
        self._store(legacy_msg_id)
        return res

    def _store(self, legacy_msg_id: str):
        if self.is_user:
            self.muc.sent[legacy_msg_id] = str(self.session.contacts.user_legacy_id)
        else:
            self.muc.sent[legacy_msg_id] = self.contact.legacy_id


class MUC(LegacyMUC[str, str, Participant, str]):
    session: "Session"
    type = MucType.GROUP

    REACTIONS_SINGLE_EMOJI = True
    _ALL_INFO_FILLED_ON_STARTUP = True

    HAS_DESCRIPTION = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent = dict[str, str]()

    async def update_info(self):
        try:
            avatar = await self.session.xmpp.run_in_executor(
                self.session.whatsapp.GetAvatar, self.legacy_id, self.avatar or ""
            )
        except RuntimeError:
            # no avatar
            await self.set_avatar(None)
        else:
            if avatar.URL:
                await self.set_avatar(avatar.URL, avatar.ID)

    async def backfill(
        self,
        oldest_message_id: Optional[str] = None,
        oldest_message_date: Optional[datetime] = None,
    ):
        """
        Request history for messages older than the oldest message given by ID and date.
        """
        if (
            oldest_message_id is not None
            and oldest_message_id not in self.session.muc_sent_msg_ids
        ):
            # WhatsApp requires a full reference to the last seen message in performing on-demand sync.
            return
        oldest_message = whatsapp.Message(
            ID=oldest_message_id or "",
            IsCarbon=(
                self.session.message_is_carbon(self, oldest_message_id)
                if oldest_message_id
                else False
            ),
            Timestamp=(
                int(oldest_message_date.timestamp()) if oldest_message_date else 0
            ),
        )
        await self.session.xmpp.run_in_executor(
            self.session.whatsapp.RequestMessageHistory, self.legacy_id, oldest_message
        )

    def get_message_sender(self, legacy_msg_id: str):
        sender_legacy_id = self.sent.get(legacy_msg_id)
        if sender_legacy_id is None:
            raise XMPPError("internal-server-error", "Unable to find message sender")
        return sender_legacy_id

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
            if info.Subject.SetByJID:
                participant = await self.get_participant_by_legacy_id(
                    info.Subject.SetByJID
                )
                if name := participant.nickname:
                    self.subject_setter = name
        for data in info.Participants:
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

    def replace_mentions(self, t: str):
        return replace_whatsapp_mentions(
            t,
            participants=(
                {
                    c.jid_username: p.nickname
                    for c, p in self._participants_by_contacts.items()
                }
                | {self.session.user_phone: self.user_nick}
                if self.session.user_phone  # user_phone *should* be set at this point,
                else {}  # but better safe than sorry
            ),
        )

    async def on_avatar(self, data: Optional[bytes], mime: Optional[str]) -> None:
        return await self.session.xmpp.run_in_executor(
            self.session.whatsapp.SetAvatar,
            self.legacy_id,
            await get_bytes_temp(data) if data else "",
        )

    async def on_set_config(
        self,
        name: Optional[str],
        description: Optional[str],
    ):
        # there are no group descriptions in WA, but topics=subjects
        if self.name != name:
            await self.session.xmpp.run_in_executor(
                self.session.whatsapp.SetGroupName, self.legacy_id, name
            )

    async def on_set_subject(self, subject: str):
        if self.subject != subject:
            await self.session.xmpp.run_in_executor(
                self.session.whatsapp.SetGroupTopic, self.legacy_id, subject
            )

    async def on_set_affiliation(
        self,
        contact: "Contact",  # type:ignore
        affiliation: MucAffiliation,
        reason: Optional[str],
        nickname: Optional[str],
    ):
        if affiliation == "member":
            if contact in self._participants_by_contacts:
                change = "demote"
            else:
                change = "add"
        elif affiliation == "admin":
            change = "promote"
        elif affiliation == "outcast" or affiliation == "none":
            change = "remove"
        else:
            raise XMPPError(
                "bad-request",
                f"You can't make a participant '{affiliation}' in whatsapp",
            )
        await self.session.xmpp.run_in_executor(
            self.session.whatsapp.SetAffiliation,
            self.legacy_id,
            contact.legacy_id,
            change,
        )


class Bookmarks(LegacyBookmarks[str, MUC]):
    session: "Session"

    def __init__(self, session: "Session"):
        super().__init__(session)
        self.__filled = False

    async def fill(self):
        groups = await self.session.xmpp.run_in_executor(
            self.session.whatsapp.GetGroups
        )
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

        if whatsapp_group_id not in self._mucs_by_legacy_id:
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
