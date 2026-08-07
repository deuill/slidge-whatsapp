from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast
from urllib.parse import quote as url_quote

import sqlalchemy
from slidge import BaseSession
from slidge.command import FormField, SearchResult
from slidge.command.user import SyncContacts
from slidge.contact.roster import ContactIsUser
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
from .generated import go, whatsapp
from .group import MUC, Bookmarks, Participant

if TYPE_CHECKING:
    from .gateway import Gateway

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


def ignore_contact_is_user[**P, T](
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


class Session(BaseSession[Roster, Bookmarks]):
    xmpp: Gateway

    def __init__(self, user: GatewayUser) -> None:
        super().__init__(user)
        self.sent_msg_date_store = AutoExpiryStore()
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
    async def handle_event(self, event_kind: whatsapp.EventKind, ptr: int) -> None:
        """
        Handle incoming event, as propagated by the WhatsApp adapter. Typically, events carry all
        state required for processing by the Gateway itself, and will do minimal processing themselves.
        """
        if event_kind == whatsapp.EventUnknown:
            return
        if event_kind not in (
            whatsapp.EventLogin,
            whatsapp.EventConnect,
            whatsapp.EventLogout,
        ):
            await self.contacts.ready
            await self.bookmarks.ready
        event = whatsapp.EventPayload(handle=ptr)  # type:ignore[no-untyped-call]
        match event_kind:
            case whatsapp.EventLogin:
                await self.__handle_login(event.Login)
            case whatsapp.EventConnect:
                await self.__handle_connect(event.Connect)
            case whatsapp.EventLogout:
                await self.__handle_logout(event.Logout)
            case whatsapp.EventContact:
                await self.__handle_contact(event.Contact)
            case whatsapp.EventPresence:
                await self.__handle_presence(event.Presence)
            case whatsapp.EventMessage:
                await self.__handle_message(event.Message)
            case whatsapp.EventChatState:
                await self.__handle_chat_state(event.ChatState)
            case whatsapp.EventReceipt:
                await self.__handle_receipt(event.Receipt)
            case whatsapp.EventGroup:
                await self.__handle_group(event.Group)
            case whatsapp.EventCall:
                await self.__handle_call(event.Call)
            case whatsapp.EventAvatar:
                await self.__handle_avatar(event.Avatar)
            case _:
                self.log.warning("No handler for event of kind %s", event_kind)

    async def __handle_login(self, login: whatsapp.Login) -> None:
        if login.QRCode:
            self.send_gateway_status("QR Scan Needed", show="dnd")
            await self.send_qr(login.QRCode)
        elif login.PairID:
            self.send_gateway_message(MESSAGE_PAIR_SUCCESS)
            self.legacy_module_data_set({"device_id": login.PairID})

    async def __handle_connect(self, connect: whatsapp.Connect) -> None:
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
            self.contacts.user_legacy_id = str(connect.Address)
            self.user_phone = str(connect.Address)
            self.xmpp.loop.call_soon_threadsafe(
                self.__connected.set_result, self.__get_connected_status_message()
            )

    async def __handle_logout(self, logout: whatsapp.Logout) -> None:
        self.logged = False
        message = MESSAGE_LOGGED_OUT
        if logout.Reason:
            message += f"\nReason: {logout.Reason}"
        self.send_gateway_message(message)
        self.send_gateway_status("Logged out", show="away")
        for muc in self.bookmarks:
            # When we are logged out, the initial history sync may not completely cover the "hole"
            # between logout and re-pair, so we want to request more history.
            muc.history_requested = False

    async def __handle_contact(self, contact: whatsapp.Contact) -> None:
        # TODO: Figure out if we still want to keep doing AddressHidden to occupant ID mapping for MUCs.
        await self.contacts.by_legacy_id(str(contact.Address), contact)

    async def __handle_group(self, group: whatsapp.Group) -> None:
        muc = await self.bookmarks.by_legacy_id(str(group.Address), group)
        await muc.add_to_bookmarks()

    async def __handle_presence(self, presence: whatsapp.Presence) -> None:
        contact = await self.contacts.by_legacy_id(str(presence.Sender))
        await contact.update_presence(presence.Kind, presence.LastSeen)

    async def __handle_chat_state(self, state: whatsapp.ChatState) -> None:
        recipient = await self.__get_recipient(state.Sender, state.Chat)
        if state.Kind == whatsapp.ChatStateComposing:
            recipient.composing()
            recipient.online(last_seen=datetime.now(tz=UTC))
        elif state.Kind == whatsapp.ChatStatePaused:
            recipient.paused()

    async def __handle_receipt(self, receipt: whatsapp.Receipt) -> None:
        """
        Handle incoming delivered/read receipt, as propagated by the WhatsApp adapter.
        """
        recipient = await self.__get_recipient(receipt.Sender, receipt.Chat)
        for message_id in receipt.MessageIDs:
            if receipt.Kind == whatsapp.ReceiptDelivered:
                recipient.received(message_id)
            elif receipt.Kind == whatsapp.ReceiptRead:
                recipient.displayed(legacy_msg_id=message_id, carbon=receipt.IsCarbon)
                recipient.online(last_seen=datetime.now(tz=UTC))

    async def __handle_call(self, call: whatsapp.Call) -> None:
        contact = await self.contacts.by_legacy_id(str(call.Sender))
        text = (
            f"from {contact.name or str(contact.jid.local)} (xmpp:{contact.jid.bare})"
        )
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

    async def __handle_message(self, message: whatsapp.Message) -> None:
        """
        Handle incoming message, as propagated by the WhatsApp adapter. Messages can be one of many
        types, including plain-text messages, media messages, reactions, etc., and may also include
        other aspects such as references to other messages for the purposes of quoting or correction.
        """
        # Skip handing message that's already in our message archive. This only works for messages
        # with a body -- messages without body have no "legacy_msg_id" attached to them. In
        # practice, this means we fill our MAM table with (hopefully just a few) duplicate rows for
        # all reactions, receipts, displayed markers, retractions and corrections.
        if (
            message.IsHistory
            and message.Chat.Kind() == whatsapp.AddressGroup
            and await self.__is_message_in_archive(message.ID)
        ):
            return
        recipient = await self.__get_recipient(message.Sender, message.Chat)
        if isinstance(recipient, Participant):
            muc = recipient.muc
        recipient.online(last_seen=datetime.now(tz=UTC))
        if not message.GroupInvite.Address.IsEmpty():
            invite_muc = await self.bookmarks.by_legacy_id(
                str(message.GroupInvite.Address)
            )
            invite_muc_name = f"{invite_muc.name} xmpp:{url_quote(invite_muc.jid.user)}@{invite_muc.jid.server}"
            self.send_gateway_message(
                f"Received group invite for {invite_muc_name} from {recipient.name}, auto-joining…"
            )
        match message.Kind:
            case whatsapp.MessagePlain:
                await self.__handle_message_plain(message, recipient, muc)
            case whatsapp.MessageEdit:
                await self.__handle_message_edit(message, recipient, muc)
            case whatsapp.MessageRevoke:
                await self.__handle_message_revoke(message, recipient, muc)
            case whatsapp.MessageReaction:
                await self.__handle_message_reaction(message, recipient)
            case whatsapp.MessageAttachment:
                await self.__handle_message_attachment(message, recipient, muc)
            case whatsapp.MessagePoll:
                await self.__handle_message_poll(message, recipient, muc)
        for receipt in message.Receipts:
            await self.__handle_receipt(receipt)
        for reaction in message.Reactions:
            await self.__handle_message(reaction)

    def __get_timestamp(self, message: whatsapp.Message) -> datetime | None:
        return (
            datetime.fromtimestamp(message.Timestamp, tz=UTC)
            if message.Timestamp > 0
            else None
        )

    async def __handle_message_plain(
        self,
        message: whatsapp.Message,
        recipient: Contact | Participant,
        muc: MUC | None,
    ) -> None:
        recipient.send_text(
            body=await self.__get_body(message, muc),
            legacy_msg_id=message.ID,
            when=self.__get_timestamp(message),
            reply_to=await self.__get_reply_to(message, muc),
            carbon=message.IsCarbon,
            link_previews=_get_link_previews(message.Preview),
        )

    async def __handle_message_attachment(
        self,
        message: whatsapp.Message,
        recipient: Contact | Participant,
        muc: MUC | None,
    ) -> None:
        try:
            await recipient.send_files(
                attachments=self.__get_message_attachments(message, muc),
                legacy_msg_id=message.ID,
                reply_to=await self.__get_reply_to(message, muc),
                when=self.__get_timestamp(message),
                carbon=message.IsCarbon,
            )
        finally:
            for attachment in message.Attachments:
                if path := attachment.Path:
                    self.log.debug("Unlinking %s", path)
                    try:
                        os.unlink(path)
                    except Exception:
                        self.log.exception("Unlinking attachment tempfile failed.")

    async def __handle_message_edit(
        self,
        message: whatsapp.Message,
        recipient: Contact | Participant,
        muc: MUC | None,
    ) -> None:
        recipient.correct(
            legacy_msg_id=message.ReferenceID,
            new_text=message.Body,
            reply_to=await self.__get_reply_to(message, muc),
            when=self.__get_timestamp(message),
            carbon=message.IsCarbon,
            correction_event_id=message.ID,
        )

    async def __handle_message_revoke(
        self,
        message: whatsapp.Message,
        recipient: Contact | Participant,
        muc: MUC | None,
    ) -> None:
        if muc is None or str(message.Origin.Address) == str(message.Sender.Address):
            recipient.retract(legacy_msg_id=message.ID, carbon=message.IsCarbon)
        else:
            assert isinstance(recipient, Participant)
            recipient.moderate(legacy_msg_id=message.ID)

    async def __handle_message_reaction(
        self,
        message: whatsapp.Message,
        recipient: Contact | Participant,
    ) -> None:
        emojis = [message.Body] if message.Body else []
        recipient.react(
            legacy_msg_id=message.ID, emojis=emojis, carbon=message.IsCarbon
        )

    async def __handle_message_poll(
        self,
        message: whatsapp.Message,
        recipient: Contact | Participant,
        muc: MUC | None,
    ) -> None:
        body = f"🗳 {message.Poll.Title}"
        for option in message.Poll.Options:
            body = body + f"\n☐ {option.Title}"
        recipient.send_text(
            body=body,
            legacy_msg_id=message.ID,
            reply_to=await self.__get_reply_to(message, muc),
            when=self.__get_timestamp(message),
            carbon=message.IsCarbon,
        )

    async def __handle_avatar(self, avatar: whatsapp.Avatar) -> None:
        if avatar.Address.Kind() == whatsapp.AddressGroup:
            chat: MUC | Contact = await self.bookmarks.by_legacy_id(str(avatar.Address))
        else:
            chat = await self.contacts.by_legacy_id(str(avatar.Address))
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
            whatsapp.Address(),  # type:ignore[no-untyped-call]
            go.Slice_byte.from_bytes(bytes_) if bytes_ else go.Slice_byte(),  # type:ignore[no-untyped-call]
        )

    async def on_create_group(self, name: str, contacts: list[Contact]) -> str:
        """
        Creates a WhatsApp group for the given human-readable name and participant list.
        """
        group = self.whatsapp.CreateGroup(
            whatsapp.Group(  # type:ignore[no-untyped-call]
                Name=name,
                Participants=whatsapp.Slice_whatsapp_GroupParticipant(  # type:ignore[no-untyped-call]
                    [
                        whatsapp.GroupParticipant(  # type:ignore[no-untyped-call]
                            Address=whatsapp.ParseAddress(c.legacy_id),  # type:ignore[no-untyped-call]
                        )
                        for c in contacts
                    ]
                ),
            ),
        )
        muc = await self.bookmarks.by_legacy_id(str(group.Address))
        return muc.legacy_id

    async def on_search(self, form_values: dict[str, str]) -> SearchResult | None:
        """
        Searches for, and automatically adds, WhatsApp contact based on phone number. Phone numbers
        not registered on WhatsApp will be ignored with no error.
        """
        phone = form_values.get("phone")
        if not is_valid_phone_number(phone):
            raise ValueError("Not a valid phone number", phone)
        data = self.whatsapp.FindContact(phone)  # type:ignore[no-untyped-call]
        if data.Address.Kind() != whatsapp.AddressPhoneNumber:
            return None
        contact = await self.contacts.by_legacy_id(str(data.Address), data)
        await contact.add_to_roster()
        return SearchResult(
            fields=[FormField("phone"), FormField("jid", type="jid-single")],
            items=[{"phone": cast(str, phone), "jid": contact.jid.bare}],
        )

    async def on_preferences(
        self, previous: dict[str, Any], new: dict[str, Any]
    ) -> None:
        if previous.get("roster_add_non_friends") != new.get("roster_add_non_friends"):
            for data in self.whatsapp.GetContacts(refresh=True):  # type:ignore[no-untyped-call]
                await self.contacts.by_legacy_id(str(data.Address))
            await SyncContacts.sync(self, self, self.user_jid)  # type:ignore

    def message_is_carbon(self, c: Recipient, legacy_msg_id: str) -> bool:
        with self.xmpp.store.session() as orm:
            return bool(
                self.xmpp.store.id_map.get_xmpp(
                    orm, c.stored.id, legacy_msg_id, c.is_group
                )
            )

    def request_avatar(self, legacy_id: str, avatar_unique_id: str | None) -> None:
        self.whatsapp.RequestAvatar(  # type:ignore[no-untyped-call]
            addr=whatsapp.Address(legacy_id),  # type:ignore[no-untyped-call]
            avatarID=avatar_unique_id or "",
            goRun=True,
        )

    def __reset_connected(self) -> None:
        if hasattr(self, "__connected") and not self.__connected.done():
            self.xmpp.loop.call_soon_threadsafe(self.__connected.cancel)
        self.__connected: asyncio.Future[str] = self.xmpp.loop.create_future()

    def __get_connected_status_message(self) -> str:
        return f"Connected as {self.user_phone}"

    def __get_message_attachments(
        self, message: whatsapp.Message, muc: MUC | None
    ) -> list[LegacyAttachment]:
        return [
            LegacyAttachment(
                content_type=attachment.MIME,
                caption=(
                    attachment.Caption
                    if muc is None
                    else muc.replace_mentions(attachment.Caption, message.Mentions)
                ),
                name=attachment.Filename,
                data=bytes(attachment.Data) if attachment.Data else None,
                path=attachment.TempFilePath if attachment.TempFilePath else None,
            )
            for attachment in message.Attachments
        ]

    async def __get_body(
        self, message: whatsapp.Message, muc: MUC | None = None
    ) -> str:
        body: str = message.Body
        if muc:
            body = muc.replace_mentions(body, message.Mentions)
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
        if muc and message.Mentions:
            body = muc.replace_mentions(message.ReplyBody, message.Mentions)
        reply_to = MessageReference(
            legacy_id=message.ReplyID,
            body=body or message.ReplyBody,
        )
        if self.contacts.user_legacy_id == str(message.Origin):
            reply_to.author = "user"
        else:
            reply_to.author = await self.__get_recipient(message.Origin, message.Chat)
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

    async def __get_recipient(
        self, sender: whatsapp.Address, chat: whatsapp.Address
    ) -> Contact | Participant:
        """
        Return either a Contact or a Participant instance for the given contact and group JIDs.
        """
        if chat.Kind() == whatsapp.AddressGroup:  # type:ignore[no-untyped-call]
            muc = await self.bookmarks.by_legacy_id(str(chat))
            return await muc.get_participant_by_legacy_id(str(sender))
        return await self.contacts.by_legacy_id(str(chat))


class AutoExpiryStore:
    _AUTO_CLEANUP_INTERVAL = 60 * 60  # once per hour
    _MAXIMUM_EDIT_TIME = timedelta(minutes=20)
    _MAXIMUM_RETRACT_TIME = timedelta(days=2)

    def __init__(self) -> None:
        self._store: dict[str, datetime] = {}
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def add(self, msg_id: str) -> None:
        self._store[msg_id] = datetime.now(tz=UTC)

    def is_editable(self, msg_id: str) -> bool:
        return self._is_newer_than(msg_id, self._MAXIMUM_EDIT_TIME)

    def is_retractable(self, msg_id: str) -> bool:
        return self._is_newer_than(msg_id, self._MAXIMUM_RETRACT_TIME)

    def _is_newer_than(self, msg_id: str, delta: timedelta) -> bool:
        created_at = self._store.get(msg_id)
        if created_at is None:
            return False
        return datetime.now(tz=UTC) < created_at + delta

    def _cleanup(self) -> None:
        now = datetime.now(tz=UTC)
        to_remove: list[str] = []
        for msg_id, created_at in self._store.items():
            if now > created_at + self._MAXIMUM_RETRACT_TIME:
                to_remove.append(msg_id)
        for msg_id in to_remove:
            del self._store[msg_id]

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._AUTO_CLEANUP_INTERVAL)
            self._cleanup()


def add_quote_prefix(text: str) -> str:
    """
    Return multi-line text with leading quote marks (i.e. the ">" character).
    """
    return "\n".join(("> " + x).strip() for x in text.split("\n")).strip()


def make_sync[**P, T](
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
