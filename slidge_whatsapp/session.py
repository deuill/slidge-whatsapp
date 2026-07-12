from __future__ import annotations

import asyncio
import warnings
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar, cast
from urllib.parse import quote as url_quote

import sqlalchemy
from slidge import BaseSession
from slidge.command import FormField, SearchResult
from slidge.command.user import SyncContacts
from slidge.contact.roster import ContactIsUser
from slidge.core.mixins.attachment import is_temp_path
from slidge.db.models import ArchivedMessage, GatewayUser
from slidge.util import is_valid_phone_number
from slidge.util.types import (
    Avatar,
    LegacyAttachment,
    MessageReference,
    PseudoPresenceShow,
)
from slidge.util.types import LinkPreview as SlidgeLinkPreview
from slixmpp.exceptions import XMPPError
from slixmpp.types import ResourceDict

from .contact import Contact, Roster
from .gateway import Gateway
from .generated import go, whatsapp
from .group import MUC, Bookmarks, Participant

MESSAGE_PAIR_SUCCESS = (
    "Pairing successful! You might need to repeat this process in the future if the"
    " Linked Device is re-registered from your main device."
)

MESSAGE_LOGGED_OUT = (
    "You have been logged out, please use the re-login adhoc command "
    "and re-scan the QR code on your main device."
)


Recipient = Contact | MUC


P = ParamSpec("P")
T = TypeVar("T")


WrappedSessionMethod = Callable[Concatenate["Session", P], Coroutine[Any, Any, T]]


def ignore_contact_is_user(
    func: WrappedSessionMethod[P, T],
) -> WrappedSessionMethod[P, T | None]:
    @wraps(func)
    async def wrapped(self: Session, /, *a: P.args, **k: P.kwargs) -> T | None:
        try:
            return await func(self, *a, **k)
        except ContactIsUser as e:
            self.log.debug("A wild ContactIsUser has been raised!", exc_info=e)
            return None

    return wrapped


class Session(BaseSession[Contact]):
    xmpp: Gateway
    contacts: Roster
    bookmarks: Bookmarks

    def __init__(self, user: GatewayUser) -> None:
        super().__init__(user)
        try:
            device = whatsapp.LinkedDevice(ID=self.user.legacy_module_data["device_id"])  # type:ignore[no-untyped-call]
        except KeyError:
            device = whatsapp.LinkedDevice()  # type:ignore[no-untyped-call]
        self.__presence_status: str = ""
        self.user_phone: str | None = None
        self.whatsapp: whatsapp.Session = self.xmpp.whatsapp.NewSession(device)
        self.__handle_event = make_sync(self.handle_event, self.xmpp.loop)
        self.whatsapp.SetEventHandler(self.__handle_event)
        self.__reset_connected()

    async def login(self) -> str:
        """
        Initiate login process and connect session to WhatsApp. Depending on existing state, login
        might either return having initiated the Linked Device registration process in the background,
        or will re-connect to a previously existing Linked Device session.
        """
        self.__reset_connected()
        self.whatsapp.Login()  # type:ignore[no-untyped-call]
        return await self.__connected

    async def logout(self) -> None:
        """
        Disconnect the active WhatsApp session. This will not remove any local or remote state, and
        will thus allow previously authenticated sessions to re-authenticate without needing to pair.
        """
        self.whatsapp.Disconnect()  # type:ignore[no-untyped-call]
        self.logged = False

    @ignore_contact_is_user
    async def handle_event(self, event_kind: int, ptr: int) -> None:
        """
        Handle incoming event, as propagated by the WhatsApp adapter. Typically, events carry all
        state required for processing by the Gateway itself, and will do minimal processing themselves.
        """
        if event_kind == whatsapp.EventUnknown:
            return
        if event_kind not in (
            whatsapp.EventQRCode,
            whatsapp.EventPairDeviceID,
            whatsapp.EventConnect,
            whatsapp.EventLoggedOut,
        ):
            await self.contacts.ready
            await self.bookmarks.ready
        event = whatsapp.EventPayload(handle=ptr)  # type:ignore[no-untyped-call]
        match event_kind:
            case whatsapp.EventQRCode:
                await self.on_wa_qr(event.QRCode)
            case whatsapp.EventConnect:
                await self.on_wa_connect(event.Connect)
            case whatsapp.EventPairDeviceID:
                await self.on_wa_pair(event.PairDeviceID)
            case whatsapp.EventLoggedOut:
                await self.on_wa_logged_out(event.LoggedOut)
            case whatsapp.EventContact:
                await self.on_wa_contact(event.Contact)
            case whatsapp.EventPresence:
                await self.on_wa_presence(event.Presence)
            case whatsapp.EventMessage:
                await self.on_wa_message(event.Message)
            case whatsapp.EventChatState:
                await self.on_wa_chat_state(event.ChatState)
            case whatsapp.EventReceipt:
                await self.on_wa_receipt(event.Receipt)
            case whatsapp.EventGroup:
                await self.on_wa_group(event.Group)
            case whatsapp.EventCall:
                await self.on_wa_call(event.Call)
            case whatsapp.EventAvatar:
                await self.on_wa_avatar(event.Avatar)
            case _:
                self.log.warning("No handler for event of kind %s", event_kind)

    async def on_wa_qr(self, qr: str) -> None:
        self.send_gateway_status("QR Scan Needed", show="dnd")
        await self.send_qr(qr)

    async def on_wa_pair(self, device_id: str) -> None:
        self.send_gateway_message(MESSAGE_PAIR_SUCCESS)
        self.legacy_module_data_set({"device_id": device_id})

    async def on_wa_connect(self, connect: whatsapp.Connect) -> None:
        # On re-pair, Session.login() is not called by slidge core, so the status message is
        # not updated.
        if self.__connected.done():
            if connect.Error != "":
                self.send_gateway_status("Connection error", show="dnd")
                self.send_gateway_message(connect.Error)
            else:
                self.send_gateway_status(
                    self.__get_connected_status_message(), show="chat"
                )
        elif connect.Error != "":
            self.xmpp.loop.call_soon_threadsafe(
                self.__connected.set_exception,
                XMPPError("internal-server-error", connect.Error),
            )
        else:
            self.contacts.user_legacy_id = connect.JID
            self.user_phone = "+" + connect.JID.split("@")[0]
            self.xmpp.loop.call_soon_threadsafe(
                self.__connected.set_result, self.__get_connected_status_message()
            )

    async def on_wa_logged_out(self, logged_out: whatsapp.LoggedOut) -> None:
        self.logged = False
        message = MESSAGE_LOGGED_OUT
        if logged_out.Reason:
            message += f"\nReason: {logged_out.Reason}"
        self.send_gateway_message(message)
        self.send_gateway_status("Logged out", show="away")
        for muc in self.bookmarks:
            # When we are logged out, the initial history sync may not completely
            # cover the "hole" between logout and re-pair, so we want to request
            # more history.
            muc.history_requested = False

    async def on_wa_contact(self, wa_contact: whatsapp.Contact) -> None:
        if wa_contact.Actor.JID:
            contact = await self.contacts.add_whatsapp_contact(wa_contact)
            if contact is not None and contact.is_friend:
                # slidge core would do that automatically if the is_friend flag
                # was set in update_info(), but it actually happens in
                # update_whatsapp_info()
                await contact.add_to_roster()
        elif wa_contact.Actor.LID:
            await self.bookmarks.rename_anonymous_participants(wa_contact)

    async def on_wa_group(self, group: whatsapp.Group) -> None:
        await self.bookmarks.add_whatsapp_group(group)

    async def on_wa_presence(self, presence: whatsapp.Presence) -> None:
        if presence.Actor.JID:
            contact = await self.contacts.by_legacy_id(presence.Actor.JID)
            await contact.update_presence(presence.Kind, presence.LastSeen)
        # TODO: LID participant presence update?

    async def on_wa_chat_state(self, state: whatsapp.ChatState) -> None:
        if not state.Chat.IsGroup and not state.Actor.JID:
            # For unknown/new contacts, we receive 1:1 *LID* chat states.
            # We currently have no way to map those, so let's ignore them.
            return

        contact, _muc = await self.__get_contact_or_participant(state.Chat, state.Actor)
        if state.Kind == whatsapp.ChatStateComposing:
            contact.composing()
            contact.online(last_seen=datetime.now())
        elif state.Kind == whatsapp.ChatStatePaused:
            contact.paused()

    async def on_wa_receipt(self, receipt: whatsapp.Receipt) -> None:
        """
        Handle incoming delivered/read receipt, as propagated by the WhatsApp adapter.
        """
        try:
            contact, _muc = await self.__get_contact_or_participant(
                receipt.Chat, receipt.Actor
            )
        except ValueError:
            self.log.warning("What do with this receipt? %s", receipt)
            return
        for message_id in receipt.MessageIDs:
            if receipt.Kind == whatsapp.ReceiptDelivered:
                contact.received(message_id)
            elif receipt.Kind == whatsapp.ReceiptRead:
                contact.displayed(legacy_msg_id=message_id, carbon=receipt.Actor.IsMe)
                contact.online(last_seen=datetime.now())

    async def on_wa_call(self, call: whatsapp.Call) -> None:
        if not call.Actor.JID:
            warnings.warn(f"Ignoring a call: {call}")
            return
        contact = await self.contacts.by_legacy_id(call.Actor.JID)
        text = f"from {contact.name or 'tel:' + str(contact.jid.local)} (xmpp:{contact.jid.bare})"
        if call.State == whatsapp.CallIncoming:
            text = "Incoming call " + text
        elif call.State == whatsapp.CallMissed:
            text = "Missed call " + text
        else:
            text = "Call " + text
        if call.Timestamp > 0:
            call_at = datetime.fromtimestamp(call.Timestamp, tz=UTC)
            text = text + f" at {call_at}"
        self.send_gateway_message(text)

    async def on_wa_message(self, message: whatsapp.Message) -> None:
        """
        Handle incoming message, as propagated by the WhatsApp adapter. Messages can be one of many
        types, including plain-text messages, media messages, reactions, etc., and may also include
        other aspects such as references to other messages for the purposes of quoting or correction.
        """
        # Skip handing message that's already in our message archive.
        if (
            message.Chat.IsGroup
            and message.IsHistory
            and await self.__is_message_in_archive(message.ID)
        ):
            # FIXME: this only works for messages with a body
            # Messages without body have no "legacy_msg_id" attached to them. In practice, this means
            # we fill our MAM table with (hopefully just a few) duplicate rows for all reactions, receipts,
            # displayed markers, retractions and corrections.
            return
        actor, muc = await self.__get_contact_or_participant(
            message.Chat, message.Actor
        )
        actor.online(last_seen=datetime.now())
        if message.GroupInvite.JID:
            muc = await self.bookmarks.by_legacy_id(message.GroupInvite.JID)
            muc_name = f"{muc.name} xmpp:{url_quote(muc.jid.user)}@{muc.jid.server}"
            self.send_gateway_message(
                f"Received group invite for {muc_name} from {actor.name}, auto-joining…"
            )

        match message.Kind:
            case whatsapp.MessagePlain:
                await self.on_wa_msg_plain(message, actor, muc)
            case whatsapp.MessageEdit:
                await self.on_wa_msg_edit(message, actor, muc)
            case whatsapp.MessageRevoke:
                await self.on_wa_msg_revoke(message, actor, muc)
            case whatsapp.MessageReaction:
                await self.on_wa_msg_reaction(message, actor, muc)
            case whatsapp.MessageAttachment:
                await self.on_wa_msg_attachment(message, actor, muc)
            case whatsapp.MessagePoll:
                await self.on_wa_msg_poll(message, actor, muc)

        for receipt in message.Receipts:
            await self.on_wa_receipt(receipt)
        for reaction in message.Reactions:
            await self.on_wa_message(reaction)

    def __get_timestamp(self, message: whatsapp.Message) -> datetime | None:
        return (
            datetime.fromtimestamp(message.Timestamp, tz=UTC)
            if message.Timestamp > 0
            else None
        )

    async def on_wa_msg_plain(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        actor.send_text(
            body=await self.__get_body(message, muc),
            legacy_msg_id=message.ID,
            when=self.__get_timestamp(message),
            reply_to=await self.__get_reply_to(message, muc),
            carbon=message.Actor.IsMe,
            link_previews=_get_link_previews(message.Preview),
        )

    async def on_wa_msg_attachment(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        attachments = await Attachment.convert_list(message.Attachments, muc)
        await actor.send_files(
            attachments=attachments,
            legacy_msg_id=message.ID,
            reply_to=await self.__get_reply_to(message, muc),
            when=self.__get_timestamp(message),
            carbon=message.Actor.IsMe,
        )
        for attachment in attachments:
            if attachment.path is None:
                continue
            assert isinstance(attachment.path, Path)
            if is_temp_path(attachment.path):
                self.log.debug("Removing '%s' from disk", attachment.path)
                Path(attachment.path).unlink(missing_ok=True)

    async def on_wa_msg_edit(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        actor.correct(
            legacy_msg_id=message.ReferenceID,
            new_text=message.Body,
            reply_to=await self.__get_reply_to(message, muc),
            when=self.__get_timestamp(message),
            carbon=message.Actor.IsMe,
            correction_event_id=message.ID,
        )

    async def on_wa_msg_revoke(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        if muc is None or message.OriginActor.JID == message.Actor.JID:
            actor.retract(legacy_msg_id=message.ID, carbon=message.Actor.IsMe)
        else:
            assert isinstance(actor, Participant)
            actor.moderate(legacy_msg_id=message.ID)

    async def on_wa_msg_reaction(
        self, message: whatsapp.Message, actor: Contact | Participant, _muc: MUC | None
    ) -> None:
        emojis = [message.Body] if message.Body else []
        actor.react(legacy_msg_id=message.ID, emojis=emojis, carbon=message.Actor.IsMe)

    async def on_wa_msg_poll(
        self, message: whatsapp.Message, actor: Contact | Participant, muc: MUC | None
    ) -> None:
        body = f"🗳 {message.Poll.Title}"
        for option in message.Poll.Options:
            body = body + f"\n☐ {option.Title}"
        actor.send_text(
            body=body,
            legacy_msg_id=message.ID,
            reply_to=await self.__get_reply_to(message, muc),
            when=self.__get_timestamp(message),
            carbon=message.Actor.IsMe,
        )

    async def on_wa_avatar(self, avatar: whatsapp.Avatar) -> None:
        if avatar.IsGroup:
            chat: MUC | Contact = await self.bookmarks.by_legacy_id(avatar.ResourceID)
        else:
            chat = await self.contacts.by_legacy_id(avatar.ResourceID)
        chat.avatar = Avatar(url=avatar.URL or None, unique_id=avatar.ID or None)

    async def on_presence(
        self,
        resource: str,
        show: PseudoPresenceShow,
        status: str,
        resources: dict[str, ResourceDict],
        merged_resource: ResourceDict | None,
    ) -> None:
        """
        Send outgoing availability status (i.e. presence) based on combined status of all connected
        XMPP clients.
        """
        if not merged_resource:
            self.whatsapp.SendPresence(whatsapp.PresenceUnavailable, "")  # type:ignore[no-untyped-call]
        else:
            presence = (
                whatsapp.PresenceAvailable
                if merged_resource["show"] in ["chat", ""]
                else whatsapp.PresenceUnavailable
            )
            status = (
                merged_resource["status"]
                if self.__presence_status != merged_resource["status"]
                else ""
            )
            if status:
                self.__presence_status = status
            self.whatsapp.SendPresence(presence, status)  # type:ignore[no-untyped-call]

    async def on_avatar(
        self,
        bytes_: bytes | None,
        hash_: str | None,
        type_: str | None,
        width: int | None,
        height: int | None,
    ) -> None:
        """
        Update profile picture in WhatsApp for corresponding avatar change in XMPP.
        """
        self.whatsapp.SetAvatar(
            "",
            go.Slice_byte.from_bytes(bytes_) if bytes_ else go.Slice_byte(),  # type:ignore[no-untyped-call]
        )

    async def on_create_group(self, name: str, contacts: list[Contact]) -> str:
        """
        Creates a WhatsApp group for the given human-readable name and participant list.
        """
        group = self.whatsapp.CreateGroup(
            name,
            go.Slice_string([c.legacy_id for c in contacts]),  # type:ignore[no-untyped-call]
        )
        muc = await self.bookmarks.by_legacy_id(group.JID)
        return muc.legacy_id

    async def on_search(self, form_values: dict[str, str]) -> SearchResult | None:
        """
        Searches for, and automatically adds, WhatsApp contact based on phone number. Phone numbers
        not registered on WhatsApp will be ignored with no error.
        """
        phone = form_values.get("phone")
        if not is_valid_phone_number(phone):
            raise ValueError("Not a valid phone number", phone)

        data: whatsapp.Contact = self.whatsapp.FindContact(phone)  # type:ignore[no-untyped-call]
        if not data.Actor.JID:
            return None

        contact = await self.contacts.add_whatsapp_contact(data)
        assert contact is not None
        await contact.add_to_roster()

        return SearchResult(
            fields=[FormField("phone"), FormField("jid", type="jid-single")],
            items=[{"phone": cast(str, phone), "jid": contact.jid.bare}],
        )

    async def on_preferences(
        self, previous: dict[str, Any], new: dict[str, Any]
    ) -> None:
        if previous.get("roster_add_non_friends") != new.get("roster_add_non_friends"):
            self.log.debug(
                "Running contact sync after group contacts in roster policy change"
            )
            # This updates the "friend" status of contacts
            for wa_contact in self.whatsapp.GetContacts(refresh=True):  # type:ignore[no-untyped-call]
                await self.contacts.add_whatsapp_contact(wa_contact)
            # This works but is really hacky, slidge core should expose this more cleanly
            await SyncContacts.sync(self, self, self.user_jid)  # type:ignore

    def message_is_carbon(self, c: Recipient, legacy_msg_id: str) -> bool:
        with self.xmpp.store.session() as orm:
            return bool(
                self.xmpp.store.id_map.get_xmpp(
                    orm, c.stored.id, legacy_msg_id, c.is_group
                )
            )

    def __reset_connected(self) -> None:
        if hasattr(self, "__connected") and not self.__connected.done():
            self.xmpp.loop.call_soon_threadsafe(self.__connected.cancel)
        self.__connected: asyncio.Future[str] = self.xmpp.loop.create_future()

    def __get_connected_status_message(self) -> str:
        return f"Connected as {self.user_phone}"

    async def __get_body(
        self, message: whatsapp.Message, muc: MUC | None = None
    ) -> str:
        body: str = message.Body
        if muc:
            body = await muc.replace_mentions(body)
        if message.Location.Latitude != 0 or message.Location.Longitude != 0:
            body = f"geo:{message.Location.Latitude:f},{message.Location.Longitude:f}"
            if message.Location.Accuracy > 0:
                body += ";u={message.Location.Accuracy:d}"
        if message.IsForwarded:
            body = "↱ Forwarded message:\n " + add_quote_prefix(body)
        if message.Album.IsAlbum:
            body += "Album: "
            if message.Album.ImageCount > 0:
                body += f"{message.Album.ImageCount} photos, "
            if message.Album.VideoCount > 0:
                body += f"{message.Album.VideoCount} videos"
            body = body.rstrip(" ,:")
        return body

    async def __get_reply_to(
        self, message: whatsapp.Message, muc: MUC | None = None
    ) -> MessageReference | None:
        if not message.ReplyID:
            return None
        reply_to = MessageReference(
            legacy_id=message.ReplyID,
            body=(
                message.ReplyBody
                if muc is None
                else await muc.replace_mentions(message.ReplyBody)
            ),
        )
        if self.contacts.user_legacy_id == message.OriginActor.JID:
            reply_to.author = "user"
        else:
            reply_to.author, _muc = await self.__get_contact_or_participant(
                message.Chat, message.OriginActor
            )
        return reply_to

    async def __is_message_in_archive(self, legacy_msg_id: str) -> bool:
        with self.xmpp.store.session() as orm:
            return bool(
                orm.scalar(
                    sqlalchemy.exists()
                    .where(ArchivedMessage.legacy_id == legacy_msg_id)
                    .select()
                )
            )

    async def __get_contact_or_participant(
        self, chat: whatsapp.Chat, actor: whatsapp.Actor
    ) -> tuple[Contact | Participant, MUC | None]:
        """
        Return either a Contact or a Participant instance for the given contact and group JIDs.
        """
        if chat.IsGroup:
            muc = await self.bookmarks.by_legacy_id(chat.JID)
            if actor.IsMe:
                return (
                    await muc.get_user_participant(occupant_id=actor.LID or None),
                    muc,
                )
            elif actor.JID:
                return (
                    await muc.get_participant_by_legacy_id(
                        actor.JID, occupant_id=actor.LID or None
                    ),
                    muc,
                )
            else:
                assert actor.LID
                return await muc.get_participant(occupant_id=actor.LID), muc
        elif not actor.JID:
            raise ValueError("Contact for anonymous JID")
        else:
            return await self.contacts.by_legacy_id(chat.JID), None


class Attachment(LegacyAttachment):
    @staticmethod
    async def convert_list(
        attachments: list[whatsapp.Attachment], muc: MUC | None = None
    ) -> list[Attachment]:
        return [await Attachment.convert(attachment, muc) for attachment in attachments]

    @staticmethod
    async def convert(
        wa_attachment: whatsapp.Attachment, muc: MUC | None = None
    ) -> Attachment:
        return Attachment(
            content_type=wa_attachment.MIME,
            data=bytes(wa_attachment.Data),
            caption=(
                wa_attachment.Caption
                if muc is None
                else await muc.replace_mentions(wa_attachment.Caption)
            ),
            name=wa_attachment.Filename,
        )


def add_quote_prefix(text: str) -> str:
    """
    Return multi-line text with leading quote marks (i.e. the ">" character).
    """
    return "\n".join(("> " + x).strip() for x in text.split("\n")).strip()


def make_sync(
    func: Callable[P, Coroutine[Any, Any, T]], loop: asyncio.AbstractEventLoop
) -> Callable[P, T]:
    """
    Wrap async function in synchronous operation, running against the given loop in thread-safe mode.
    """

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        result = func(*args, **kwargs)
        future = asyncio.run_coroutine_threadsafe(result, loop)
        return future.result()

    return wrapper


def _get_link_previews(preview: whatsapp.Preview) -> list[SlidgeLinkPreview]:
    if preview:
        return [
            SlidgeLinkPreview(
                about=preview.URL,
                title=preview.Title or None,
                description=preview.Description or None,
                url=None,
                image=_bytes_conversion(preview.Thumbnail),
                type=None,
                site_name=None,
            )
        ]
    return []


def _bytes_conversion(slice_byte: go.Slice_byte) -> bytes | None:
    if len(slice_byte) == 0:
        # if we call bytes() on an empty one, we get panic:
        # panic: runtime error: index out of range [0] with length 0
        return None
    return bytes(slice_byte)
