from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator

from slidge import LegacyContact, LegacyRoster
from slidge.util.types import Avatar
from slixmpp.exceptions import XMPPError

from . import config
from .avatar import AvatarMixin
from .generated import whatsapp

if TYPE_CHECKING:
    from .session import Session


class Contact(AvatarMixin, LegacyContact[str]):
    session: "Session"

    CORRECTION = True
    REACTIONS_SINGLE_EMOJI = True

    async def update_presence(
        self, presence: whatsapp.PresenceKind, last_seen_timestamp: int
    ):
        last_seen = (
            datetime.fromtimestamp(last_seen_timestamp, tz=timezone.utc)
            if last_seen_timestamp > 0
            else None
        )
        if presence == whatsapp.PresenceUnavailable:
            self.away(last_seen=last_seen)
        else:
            self.online(last_seen=last_seen)

    async def update_info(self) -> None:
        if whatsapp.IsAnonymousJID(self.legacy_id):
            raise XMPPError(
                "item-not-found", f"LIDs are not valid contact IDs: {self.legacy_id}"
            )
        # If we receive presences, the status will be updated accordingly. But presences do not
        # work reliably, and having contacts offline has annoying side effects, such as contacts not
        # appearing in the participant list of groups.
        self.online()

    async def update_whatsapp_info(self, wa_contact: whatsapp.Contact) -> None:
        self.session.log.debug(
            "User named %s, friend: %s", wa_contact.Name, wa_contact.IsFriend
        )
        self.name = wa_contact.Name
        self.is_friend = wa_contact.IsFriend or config.ADD_GROUP_PARTICIPANTS_TO_ROSTER
        await self.update_whatsapp_avatar()
        self.set_vcard(full_name=self.name, phone=str(self.jid.local))


class Roster(LegacyRoster[str, Contact]):
    session: "Session"

    async def fill(self) -> AsyncIterator[Contact]:
        """
        Retrieve contacts from remote WhatsApp service, subscribing to their presence and adding to
        local roster.
        """
        wa_contacts = self.session.whatsapp.GetContacts(
            refresh=config.ALWAYS_SYNC_ROSTER
        )
        for wa_contact in wa_contacts:
            contact = await self.add_whatsapp_contact(wa_contact)
            if contact is not None:
                yield contact

    async def add_whatsapp_contact(self, data: whatsapp.Contact) -> Contact | None:
        """
        Adds a WhatsApp contact to local roster, filling all required and optional information.
        """
        # Don't attempt to add ourselves to the roster.
        if data.JID == self.user_legacy_id:
            return None
        contact = await self.by_legacy_id(data.JID)
        return await contact.update_whatsapp_info(data)

    async def legacy_id_to_jid_username(self, legacy_id: str) -> str:
        if not "@" in legacy_id:
            raise XMPPError("item-not-found", "Invalid contact ID, not a JID")
        return "+" + legacy_id[: legacy_id.find("@")]

    async def jid_username_to_legacy_id(self, jid_username: str) -> str:
        if jid_username.startswith("#"):
            raise XMPPError("item-not-found", "Invalid contact ID: group ID given")
        if not jid_username.startswith("+"):
            raise XMPPError("item-not-found", "Invalid contact ID, expected '+' prefix")
        return jid_username.removeprefix("+") + "@" + whatsapp.DefaultUserServer
