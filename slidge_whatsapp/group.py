from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from slidge.db.meta import JSONSerializable
from slidge.group import LegacyBookmarks, LegacyMUC, LegacyParticipant, MucType
from slidge.util.types import (
    Hat,
    HoleBound,
    MucAffiliation,
    MUCMessageProtocol,
    XMPPMessageProtocol,
)
from slixmpp.exceptions import XMPPError

from .generated import go, whatsapp
from .mixins import RecipientMixin, strip_quote_prefix

if TYPE_CHECKING:
    from .contact import Contact
    from .session import Session


class Participant(LegacyParticipant["Contact"]):
    muc: "MUC"
    session: "Session"

    def online(
        self,
        status: str | None = None,
        last_seen: datetime | None = None,
    ) -> None:
        if self.is_user:
            # "user participant" presences are not something we want to bridge
            # on joining a MUC, slidge sends a basic "online" presence for the user,
            # and we have no reason to ever send another one.
            return
        if self.contact is None:
            super().online(status, last_seen)
        else:
            self.contact.online(status, last_seen)

    def update_info(self, data: whatsapp.GroupParticipant) -> None:
        if data.Action == whatsapp.GroupParticipantActionRemove:
            self.muc.remove_participant(self)
            return
        match data.Affiliation:
            case whatsapp.GroupAffiliationAdmin:
                self.affiliation = "owner"
                self.role = "moderator"
            case whatsapp.GroupAffiliationOwner:
                self.set_hats(
                    [Hat("https://slidge.im/hats/slidge-whatsapp/owner", "Owner")]
                )
                self.affiliation = "owner"
                self.role = "moderator"
            case _:
                self.affiliation = "member"
                self.role = "participant"

    async def on_set_affiliation(  # type:ignore[override]  # ty:ignore[invalid-method-override]
        self,
        contact: "Contact",
        affiliation: MucAffiliation,
        reason: str | None,
        nickname: str | None,
    ) -> None:
        match affiliation:
            case "member":
                participant = await self.muc.get_participant_by_contact(  # type:ignore[call-overload]  # ty:ignore
                    contact, create=False
                )
                if participant is None or participant.affiliation in (
                    "outcast",
                    "none",
                ):
                    action = whatsapp.GroupParticipantActionAdd
                elif participant.affiliation == "member":
                    return
                else:
                    action = whatsapp.GroupParticipantActionDemote
            case "admin" | "owner":
                action = whatsapp.GroupParticipantActionPromote
            case "outcast" | "none":
                action = whatsapp.GroupParticipantActionRemove
        self.session.whatsapp.UpdateGroupParticipants(
            whatsapp.ParseAddress(self.muc.legacy_id),  # type:ignore[no-untyped-call]
            whatsapp.Slice_whatsapp_GroupParticipant(  # type:ignore[no-untyped-call]
                [
                    whatsapp.GroupParticipant(  # type:ignore[no-untyped-call]
                        Address=whatsapp.ParseAddress(contact.legacy_id),  # type:ignore[no-untyped-call]
                        Action=action,
                    )
                ]
            ),
        )


class MUC(RecipientMixin, LegacyMUC[Participant]):
    session: "Session"

    HAS_DESCRIPTION = False
    REACTIONS_SINGLE_EMOJI = True
    _ALL_INFO_FILLED_ON_STARTUP = True

    _history_requested: bool = False

    @property
    def history_requested(self) -> bool:
        return self._history_requested

    @history_requested.setter
    def history_requested(self, flag: bool) -> None:
        if self._history_requested == flag:
            return
        self._history_requested = flag
        self.commit()

    def serialize_extra_attributes(self) -> JSONSerializable:
        return {"history_requested": self._history_requested}

    def deserialize_extra_attributes(self, data: JSONSerializable) -> None:
        self._history_requested = bool(data.get("history_requested", False))

    async def update_info(self, group: whatsapp.Group | None = None) -> None:
        """
        Set MUC information based on WhatsApp group information, which may or may not be partial in
        case of updates to existing MUCs.
        """
        if group is None:
            return
        self.type = MucType.GROUP
        if group.Nickname:
            self.user_nick = group.Nickname
        if group.Name:
            self.name = group.Name
        if group.Subject.Subject:
            self.subject = group.Subject.Subject
            if group.Subject.SetAt:
                set_at = datetime.fromtimestamp(group.Subject.SetAt, tz=UTC)
                self.subject_date = set_at
            if not group.Subject.SetBy.IsEmpty():
                self.subject_setter = await self.get_participant_by_legacy_id(
                    str(group.Subject.SetBy)
                )
        self.session.request_avatar(
            self.legacy_id, self.avatar.unique_id if self.avatar else None
        )
        if group.Participants:
            self.n_participants = len(group.Participants)
            for part in group.Participants:
                participant = await self.get_participant_by_legacy_id(str(part.Address))
                participant.update_info(part)

    async def backfill(
        self,
        after: HoleBound | None = None,
        before: HoleBound | None = None,
    ) -> None:
        """
        Request history for messages older than the oldest message given by ID and date.
        """

        if before is None or self.history_requested:
            return
        oldest_message = whatsapp.Message(  # type:ignore[no-untyped-call]
            ID=before.id,
            IsCarbon=self.session.message_is_carbon(self, before.id),
            Timestamp=int(before.timestamp.timestamp()),
        )
        self.session.whatsapp.RequestMessageHistory(  # type:ignore[no-untyped-call]
            whatsapp.ParseAddress(self.legacy_id),  # type:ignore[no-untyped-call]
            oldest_message,
        )
        self.history_requested = True

    def replace_mentions(
        self, body: str, mentions: whatsapp.Slice_whatsapp_Mention
    ) -> str:
        # TODO: Fix handling of mentions.
        return body

    async def on_avatar(self, data: bytes | None, mime: str | None) -> None:
        self.session.whatsapp.SetAvatar(
            whatsapp.ParseAddress(self.legacy_id),  # type:ignore[no-untyped-call]
            go.Slice_byte.from_bytes(data) if data else go.Slice_byte(),  # type:ignore[no-untyped-call]
        )

    async def on_set_config(
        self,
        name: str | None,
        description: str | None,
    ) -> None:
        if self.name != name:
            self.session.whatsapp.SetGroupName(  # type:ignore[no-untyped-call]
                whatsapp.ParseAddress(self.legacy_id),  # type:ignore[no-untyped-call]
                name,
            )

    async def on_set_subject(self, subject: str) -> None:
        if self.subject != subject:
            self.session.whatsapp.SetGroupTopic(  # type:ignore[no-untyped-call]
                whatsapp.ParseAddress(self.legacy_id),  # type:ignore[no-untyped-call]
                subject,
            )

    async def on_moderate(self, legacy_msg_id: str, reason: str | None) -> None:
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageRevoke,
            ID=legacy_msg_id,
            Chat=whatsapp.ParseAddress(self.legacy_id),  # type:ignore[no-untyped-call]
            Origin=whatsapp.ParseAddress(self.get_message_sender(legacy_msg_id)),  # type:ignore[no-untyped-call]
            IsCarbon=self.session.message_is_carbon(self, legacy_msg_id),
        )
        self.session.whatsapp.SendMessage(message)  # type:ignore[no-untyped-call]
        participant = await self.get_user_participant()
        participant.moderate(legacy_msg_id)

    async def on_leave_group(self, legacy_muc_id: str) -> None:
        """
        Removes own user from given WhatsApp group.
        """
        self.session.whatsapp.LeaveGroup(whatsapp.ParseAddress(legacy_muc_id))  # type:ignore[no-untyped-call]

    def get_message_sender(self, legacy_msg_id: str) -> str:
        for message in self.get_archived_messages(legacy_msg_id):
            break
        else:
            raise XMPPError(
                "internal-server-error", f"Message {legacy_msg_id} is not in archive."
            )
        if message.occupant_id == "slidge-user":
            return self.session.contacts.user_legacy_id  # type:ignore
        if message.occupant_id and "@" in message.occupant_id:
            return message.occupant_id.split("@")[0]
        raise XMPPError("internal-server-error", "Unable to find message sender")

    def set_reply_to(
        self,
        xmpp_msg: XMPPMessageProtocol[Participant],
        wa_msg: whatsapp.Message,
    ) -> None:
        xmpp_msg = cast(MUCMessageProtocol[Participant], xmpp_msg)
        if not xmpp_msg.reply:
            return
        wa_msg.ReplyID = xmpp_msg.reply.msg_id
        if xmpp_msg.reply.fallback:
            wa_msg.ReplyBody = strip_quote_prefix(xmpp_msg.reply.fallback)
            wa_msg.Body = wa_msg.Body.lstrip()
        if xmpp_msg.reply.to.contact:
            wa_msg.Origin = whatsapp.ParseAddress(xmpp_msg.reply.to.contact.legacy_id)  # type:ignore[no-untyped-call]
            wa_msg.IsCarbon = xmpp_msg.reply.to.is_user
        elif xmpp_msg.reply.to:
            pass  # TODO: Handle cases for participants with no contact, which shouldn't generally exist.
        else:
            wa_msg.Origin = whatsapp.ParseAddress(self.session.contacts.user_legacy_id)  # type:ignore[no-untyped-call]
            wa_msg.IsCarbon = False


class Bookmarks(LegacyBookmarks[MUC]):
    session: "Session"

    async def fill(self) -> None:
        groups = self.session.whatsapp.GetGroups()  # type:ignore[no-untyped-call]
        for group in groups:
            muc = await self.by_legacy_id(str(group.Address), group)
            await muc.add_to_bookmarks()

    async def legacy_id_to_jid_local_part(self, legacy_id: str) -> str:
        addr = whatsapp.ParseAddress(legacy_id)  # type:ignore[no-untyped-call]
        if addr.Kind() is not whatsapp.AddressGroup:
            raise XMPPError("item-not-found", "Invalid group ID")
        return "#" + str(addr.Bare())

    async def jid_local_part_to_legacy_id(self, local_part: str) -> str:
        if not local_part.startswith("#"):
            raise XMPPError("item-not-found", "Invalid group ID")
        addr = whatsapp.NewAddress(local_part.removeprefix("#"), whatsapp.AddressGroup)  # type:ignore[no-untyped-call]
        return str(addr)
