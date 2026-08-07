from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from slidge.contact import LegacyContact, LegacyRoster
from slidge.util.types import XMPPMessageProtocol
from slixmpp.exceptions import XMPPError

from . import config
from .generated import whatsapp
from .group import Participant
from .mixins import RecipientMixin, strip_quote_prefix

if TYPE_CHECKING:
    from .session import Session


class Contact(RecipientMixin, LegacyContact):
    session: "Session"

    CORRECTION = True
    REACTIONS_SINGLE_EMOJI = True

    async def update_presence(
        self, presence: whatsapp.PresenceKind, last_seen_timestamp: int
    ) -> None:
        last_seen = (
            datetime.fromtimestamp(last_seen_timestamp, tz=UTC)
            if last_seen_timestamp > 0
            else None
        )
        if presence == whatsapp.PresenceKind.PresenceUnavailable:
            self.away(last_seen=last_seen)
        else:
            self.online(last_seen=last_seen)

    async def update_info(self, contact: whatsapp.Contact | None = None) -> None:
        """
        Update contact info, and force-set presence to 'online', to work around issues with
        unreliable presence propagation.
        """
        if contact:
            if contact.Name:
                self.name = contact.Name
            if contact.PhoneNumber:
                self.set_vcard(full_name=self.name, phone=contact.PhoneNumber)
            self.is_friend = bool(
                contact.IsFriend
                or self.session.user.preferences.get("roster_add_non_friends", True)
            )
        self.session.request_avatar(
            self.legacy_id, self.avatar.unique_id if self.avatar else None
        )
        self.online()

    def get_message_sender(self, legacy_msg_id: str) -> str:
        """
        Return the legacy ID of the sender of the message given.
        """
        assert self.session.contacts.user_legacy_id is not None
        is_carbon = self.session.message_is_carbon(self, legacy_msg_id)
        return self.session.contacts.user_legacy_id if is_carbon else self.legacy_id

    def set_reply_to(
        self, xmpp_msg: XMPPMessageProtocol[Participant], wa_msg: whatsapp.Message
    ) -> None:
        if not xmpp_msg.reply:
            return
        wa_msg.ReplyID = xmpp_msg.reply.msg_id
        if xmpp_msg.reply.fallback:
            wa_msg.ReplyBody = strip_quote_prefix(xmpp_msg.reply.fallback)
            wa_msg.Body = wa_msg.Body.lstrip()
        if xmpp_msg.reply.to == "self":
            wa_msg.Origin = whatsapp.ParseAddress(self.session.contacts.user_legacy_id)  # type:ignore[no-untyped-call]
            wa_msg.IsCarbon = True
        else:
            wa_msg.Origin = whatsapp.ParseAddress(self.legacy_id)  # type:ignore[no-untyped-call]


class Roster(LegacyRoster[Contact]):
    session: "Session"

    async def fill(self) -> AsyncIterator[Contact]:
        """
        Retrieve contacts from remote WhatsApp service, subscribing to their presence and adding to
        local roster.
        """
        contacts = self.session.whatsapp.GetContacts(  # type:ignore[no-untyped-call]
            refresh=config.ALWAYS_SYNC_ROSTER
        )
        for data in contacts:
            yield await self.by_legacy_id(str(data.Address), data)
        self.session.whatsapp.SubscribeToPresences()  # type:ignore[no-untyped-call]

    async def legacy_id_to_jid_username(self, legacy_id: str) -> str:
        addr = whatsapp.ParseAddress(legacy_id)  # type:ignore[no-untyped-call]
        match addr.Kind():
            case whatsapp.AddressPhoneNumber:
                return "+" + str(addr.Bare())
            case whatsapp.AddressHidden:
                return "-" + str(addr.Bare())
            case _:
                raise XMPPError("item-not-found", "Invalid contact ID")

    async def jid_username_to_legacy_id(self, jid_username: str) -> str:
        if jid_username.startswith("+"):
            kind = whatsapp.AddressPhoneNumber
            jid_username = jid_username.removeprefix("+")
        elif jid_username.startswith("-"):
            kind = whatsapp.AddressHidden
            jid_username = jid_username.removeprefix("-")
        else:
            raise XMPPError("item-not-found", "Invalid contact ID")
        return str(whatsapp.NewAddress(jid_username, kind))  # type:ignore[no-untyped-call]
