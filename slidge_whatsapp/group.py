import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from slidge.group import LegacyBookmarks, LegacyMUC, LegacyParticipant, MucType
from slidge.util.types import Mention
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

    REACTIONS_SINGLE_EMOJI = True
    type = MucType.GROUP

    _ALL_INFO_FILLED_ON_STARTUP = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent = dict[str, str]()

    async def update_info(self):
        try:
            avatar = self.session.whatsapp.GetAvatar(self.legacy_id, self.avatar or "")
        except RuntimeError:
            # no avatar
            await self.set_avatar(None)
        else:
            if avatar.URL:
                await self.set_avatar(avatar.URL, avatar.ID)

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
        if info.Subject.Subject or info.Subject.SetAt:
            self.subject = info.Subject.Subject
        if info.Subject.SetAt:
            set_at = datetime.fromtimestamp(info.Subject.SetAt, tz=timezone.utc)
            self.subject_date = set_at
        if info.Subject.SetByJID:
            participant = await self.get_participant_by_legacy_id(info.Subject.SetByJID)
            if name := participant.nickname:
                self.subject_setter = name
        for data in info.Participants:
            participant = await self.get_participant_by_legacy_id(data.JID)
            if data.Action == whatsapp.GroupParticipantActionRemove:
                self.remove_participant(participant)
            else:
                participant.affiliation = "member"
                if data.Affiliation == whatsapp.GroupAffiliationAdmin:
                    participant.affiliation = "admin"
                elif data.Affiliation == whatsapp.GroupAffiliationOwner:
                    participant.affiliation = "owner"

    def replace_mentions(self, t: str):
        return replace_whatsapp_mentions(
            t,
            participants={
                c.jid_username: c.name
                for c, p in self._participants_by_contacts.items()
            }
            | {self.session.user_phone: self.user_nick}
            if self.session.user_phone  # user_phone *should* be set at this point,
            else {},  # but better safe than sorry
        )

    async def on_avatar(self, data: Optional[bytes], mime: Optional[str]) -> None:
        return self.session.whatsapp.SetAvatar(
            self.legacy_id, await get_bytes_temp(data) if data else ""
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
