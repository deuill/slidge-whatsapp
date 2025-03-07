import asyncio
from datetime import datetime, timezone
from functools import wraps
from os.path import basename
from pathlib import Path
from re import search
from typing import Any, Optional, Union, cast

from aiohttp import ClientSession
from linkpreview import Link, LinkPreview
from slidge import BaseSession, FormField, GatewayUser, SearchResult, global_config
from slidge.contact.roster import ContactIsUser
from slidge.util import is_valid_phone_number
from slidge.util.types import (
    LegacyAttachment,
    Mention,
    MessageReference,
    PseudoPresenceShow,
    ResourceDict,
)
from slixmpp.exceptions import XMPPError

from . import config
from .contact import Contact, Roster
from .gateway import Gateway
from .generated import go, whatsapp
from .group import MUC, Bookmarks, replace_xmpp_mentions

MESSAGE_PAIR_SUCCESS = (
    "Pairing successful! You might need to repeat this process in the future if the"
    " Linked Device is re-registered from your main device."
)

MESSAGE_LOGGED_OUT = (
    "You have been logged out, please use the re-login adhoc command "
    "and re-scan the QR code on your main device."
)

URL_SEARCH_REGEX = r"(?P<url>https?://[^\s]+)"
GEO_URI_SEARCH_REGEX = (
    r"geo:(?P<lat>-?\d+(\.\d*)?),(?P<lon>-?\d+(\.\d*)?)(;u=(?P<acc>-?\d+(\.\d*)?))?"
)

VIDEO_PREVIEW_DOMAINS = (
    "https://youtube.com/watch",
    "https://m.youtube.com/watch",
    "https://youtu.be",
)


Recipient = Union[Contact, MUC]


def ignore_contact_is_user(func):
    @wraps(func)
    async def wrapped(self, *a, **k):
        try:
            return await func(self, *a, **k)
        except ContactIsUser as e:
            self.log.debug("A wild ContactIsUser has been raised!", exc_info=e)

    return wrapped


class Session(BaseSession[str, Recipient]):
    xmpp: Gateway
    contacts: Roster
    bookmarks: Bookmarks

    def __init__(self, user: GatewayUser):
        super().__init__(user)
        self.migrate()
        try:
            device = whatsapp.LinkedDevice(ID=self.user.legacy_module_data["device_id"])
        except KeyError:
            device = whatsapp.LinkedDevice()
        self.__presence_status: str = ""
        self.user_phone: Optional[str] = None
        self.whatsapp_participants = dict[str, list[whatsapp.GroupParticipant]]()
        self.whatsapp = self.xmpp.whatsapp.NewSession(device)
        self.__handle_event = make_sync(self.handle_event, self.xmpp.loop)
        self.whatsapp.SetEventHandler(self.__handle_event)
        self.__reset_connected()

    def migrate(self):
        user_shelf_path = (
            global_config.HOME_DIR / "whatsapp" / (self.user_jid.bare + ".shelf")
        )
        if not user_shelf_path.exists():
            return
        import shelve

        with shelve.open(str(user_shelf_path)) as shelf:
            try:
                device_id = shelf["device_id"]
            except KeyError:
                pass
            else:
                self.log.info(
                    "Migrated data from %s to the slidge main DB", user_shelf_path
                )
                self.legacy_module_data_set({"device_id": device_id})
        user_shelf_path.unlink()

    async def login(self):
        """
        Initiate login process and connect session to WhatsApp. Depending on existing state, login
        might either return having initiated the Linked Device registration process in the background,
        or will re-connect to a previously existing Linked Device session.
        """
        self.__reset_connected()
        self.whatsapp.Login()
        return await self.__connected

    async def logout(self):
        """
        Disconnect the active WhatsApp session. This will not remove any local or remote state, and
        will thus allow previously authenticated sessions to re-authenticate without needing to pair.
        """
        self.whatsapp.Disconnect()
        self.logged = False

    @ignore_contact_is_user
    async def handle_event(self, event, ptr):
        """
        Handle incoming event, as propagated by the WhatsApp adapter. Typically, events carry all
        state required for processing by the Gateway itself, and will do minimal processing themselves.
        """
        data = whatsapp.EventPayload(handle=ptr)
        if event == whatsapp.EventQRCode:
            self.send_gateway_status("QR Scan Needed", show="dnd")
            await self.send_qr(data.QRCode)
        elif event == whatsapp.EventPair:
            self.send_gateway_message(MESSAGE_PAIR_SUCCESS)
            self.legacy_module_data_set({"device_id": data.PairDeviceID})
        elif event == whatsapp.EventConnect:
            # On re-pair, Session.login() is not called by slidge core, so the status message is
            # not updated.
            if self.__connected.done():
                if data.Connect.Error != "":
                    self.send_gateway_status("Connection error", show="dnd")
                    self.send_gateway_message(data.Connect.Error)
                else:
                    self.send_gateway_status(
                        self.__get_connected_status_message(), show="chat"
                    )
            elif data.Connect.Error != "":
                self.xmpp.loop.call_soon_threadsafe(
                    self.__connected.set_exception,
                    XMPPError("internal-server-error", data.Connect.Error),
                )
            else:
                self.contacts.user_legacy_id = data.Connect.JID
                self.user_phone = "+" + data.Connect.JID.split("@")[0]
                self.xmpp.loop.call_soon_threadsafe(
                    self.__connected.set_result, self.__get_connected_status_message()
                )
        elif event == whatsapp.EventLoggedOut:
            self.logged = False
            self.send_gateway_message(MESSAGE_LOGGED_OUT)
            self.send_gateway_status("Logged out", show="away")
        elif event == whatsapp.EventContact:
            contact = await self.contacts.add_whatsapp_contact(data.Contact)
            if contact is not None:
                await contact.add_to_roster()
        elif event == whatsapp.EventGroup:
            await self.bookmarks.add_whatsapp_group(data.Group)
        elif event == whatsapp.EventPresence:
            contact = await self.contacts.by_legacy_id(data.Presence.JID)
            await contact.update_presence(data.Presence.Kind, data.Presence.LastSeen)
        elif event == whatsapp.EventChatState:
            await self.handle_chat_state(data.ChatState)
        elif event == whatsapp.EventReceipt:
            await self.handle_receipt(data.Receipt)
        elif event == whatsapp.EventCall:
            await self.handle_call(data.Call)
        elif event == whatsapp.EventMessage:
            await self.handle_message(data.Message)

    async def handle_chat_state(self, state: whatsapp.ChatState):
        contact = await self.__get_contact_or_participant(state.JID, state.GroupJID)
        if state.Kind == whatsapp.ChatStateComposing:
            contact.composing()
        elif state.Kind == whatsapp.ChatStatePaused:
            contact.paused()

    async def handle_receipt(self, receipt: whatsapp.Receipt):
        """
        Handle incoming delivered/read receipt, as propagated by the WhatsApp adapter.
        """
        contact = await self.__get_contact_or_participant(receipt.JID, receipt.GroupJID)
        for message_id in receipt.MessageIDs:
            if receipt.Kind == whatsapp.ReceiptDelivered:
                contact.received(message_id)
            elif receipt.Kind == whatsapp.ReceiptRead:
                contact.displayed(legacy_msg_id=message_id, carbon=receipt.IsCarbon)

    async def handle_call(self, call: whatsapp.Call):
        contact = await self.contacts.by_legacy_id(call.JID)
        text = f"from {contact.name or 'tel:' + str(contact.jid.local)} (xmpp:{contact.jid.bare})"
        if call.State == whatsapp.CallIncoming:
            text = "Incoming call " + text
        elif call.State == whatsapp.CallMissed:
            text = "Missed call " + text
        else:
            text = "Call " + text
        if call.Timestamp > 0:
            call_at = datetime.fromtimestamp(call.Timestamp, tz=timezone.utc)
            text = text + f" at {call_at}"
        self.send_gateway_message(text)

    async def handle_message(self, message: whatsapp.Message):
        """
        Handle incoming message, as propagated by the WhatsApp adapter. Messages can be one of many
        types, including plain-text messages, media messages, reactions, etc., and may also include
        other aspects such as references to other messages for the purposes of quoting or correction.
        """
        contact = await self.__get_contact_or_participant(message.JID, message.GroupJID)
        muc = getattr(contact, "muc", None)
        reply_to = await self.__get_reply_to(message, muc)
        message_timestamp = (
            datetime.fromtimestamp(message.Timestamp, tz=timezone.utc)
            if message.Timestamp > 0
            else None
        )
        if message.Kind == whatsapp.MessagePlain:
            body = await self.__get_body(message, muc)
            contact.send_text(
                body=body,
                legacy_msg_id=message.ID,
                when=message_timestamp,
                reply_to=reply_to,
                carbon=message.IsCarbon,
            )
        elif message.Kind == whatsapp.MessageAttachment:
            attachments = await Attachment.convert_list(message.Attachments, muc)
            await contact.send_files(
                attachments=attachments,
                legacy_msg_id=message.ID,
                reply_to=reply_to,
                when=message_timestamp,
                carbon=message.IsCarbon,
            )
            for attachment in attachments:
                if global_config.NO_UPLOAD_METHOD != "symlink":
                    self.log.debug("Removing '%s' from disk", attachment.path)
                    if attachment.path is None:
                        continue
                    Path(attachment.path).unlink(missing_ok=True)
        elif message.Kind == whatsapp.MessageEdit:
            contact.correct(
                legacy_msg_id=message.ID,
                new_text=message.Body,
                when=message_timestamp,
                reply_to=reply_to,
                carbon=message.IsCarbon,
            )
        elif message.Kind == whatsapp.MessageRevoke:
            if muc is None or message.OriginJID == message.JID:
                contact.retract(legacy_msg_id=message.ID, carbon=message.IsCarbon)
            else:
                contact.moderate(legacy_msg_id=message.ID)
        elif message.Kind == whatsapp.MessageReaction:
            emojis = [message.Body] if message.Body else []
            contact.react(
                legacy_msg_id=message.ID, emojis=emojis, carbon=message.IsCarbon
            )
        elif message.Kind == whatsapp.MessagePoll:
            body = "🗳 %s" % message.Poll.Title
            for option in message.Poll.Options:
                body = body + "\n☐ %s" % option.Title
            contact.send_text(
                body=body,
                legacy_msg_id=message.ID,
                when=message_timestamp,
                reply_to=reply_to,
                carbon=message.IsCarbon,
            )
        for receipt in message.Receipts:
            await self.handle_receipt(receipt)
        for reaction in message.Reactions:
            await self.handle_message(reaction)

    async def on_text(
        self,
        chat: Recipient,
        text: str,
        *,
        reply_to_msg_id: Optional[str] = None,
        reply_to_fallback_text: Optional[str] = None,
        reply_to=None,
        mentions: Optional[list[Mention]] = None,
        **_,
    ):
        """
        Send outgoing plain-text message to given WhatsApp contact.
        """
        message_id = self.whatsapp.GenerateMessageID()
        message_preview = await self.__get_preview(text) or whatsapp.Preview()
        message_location = await self.__get_location(text) or whatsapp.Location()
        message = whatsapp.Message(
            ID=message_id,
            JID=chat.legacy_id,
            Body=replace_xmpp_mentions(text, mentions) if mentions else text,
            Preview=message_preview,
            Location=message_location,
            MentionJIDs=go.Slice_string([m.contact.legacy_id for m in mentions or []]),
        )
        set_reply_to(chat, message, reply_to_msg_id, reply_to_fallback_text, reply_to)
        self.whatsapp.SendMessage(message)
        return message_id

    async def on_file(
        self,
        chat: Recipient,
        url: str,
        http_response,
        reply_to_msg_id: Optional[str] = None,
        reply_to_fallback_text: Optional[str] = None,
        reply_to=None,
        **_,
    ):
        """
        Send outgoing media message (i.e. audio, image, document) to given WhatsApp contact.
        """
        data = await get_url_bytes(self.http, url)
        if not data:
            raise XMPPError(
                "internal-server-error",
                "Unable to retrieve file from XMPP server, try again",
            )
        message_id = self.whatsapp.GenerateMessageID()
        message_attachment = whatsapp.Attachment(
            MIME=http_response.content_type,
            Filename=basename(url),
            Data=go.Slice_byte.from_bytes(data),
        )
        message = whatsapp.Message(
            Kind=whatsapp.MessageAttachment,
            ID=message_id,
            JID=chat.legacy_id,
            ReplyID=reply_to_msg_id if reply_to_msg_id else "",
            Attachments=whatsapp.Slice_whatsapp_Attachment([message_attachment]),
        )
        set_reply_to(chat, message, reply_to_msg_id, reply_to_fallback_text, reply_to)
        self.whatsapp.SendMessage(message)
        return message_id

    async def on_presence(
        self,
        resource: str,
        show: PseudoPresenceShow,
        status: str,
        resources: dict[str, ResourceDict],
        merged_resource: Optional[ResourceDict],
    ):
        """
        Send outgoing availability status (i.e. presence) based on combined status of all connected
        XMPP clients.
        """
        if not merged_resource:
            self.whatsapp.SendPresence(whatsapp.PresenceUnavailable, "")
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
            self.whatsapp.SendPresence(presence, status)

    async def on_active(self, c: Recipient, thread=None):
        """
        WhatsApp has no equivalent to the "active" chat state, so calls to this function are no-ops.
        """
        pass

    async def on_inactive(self, c: Recipient, thread=None):
        """
        WhatsApp has no equivalent to the "inactive" chat state, so calls to this function are no-ops.
        """
        pass

    async def on_composing(self, c: Recipient, thread=None):
        """
        Send "composing" chat state to given WhatsApp contact, signifying that a message is currently
        being composed.
        """
        state = whatsapp.ChatState(JID=c.legacy_id, Kind=whatsapp.ChatStateComposing)
        self.whatsapp.SendChatState(state)

    async def on_paused(self, c: Recipient, thread=None):
        """
        Send "paused" chat state to given WhatsApp contact, signifying that an (unsent) message is no
        longer being composed.
        """
        state = whatsapp.ChatState(JID=c.legacy_id, Kind=whatsapp.ChatStatePaused)
        self.whatsapp.SendChatState(state)

    async def on_displayed(self, c: Recipient, legacy_msg_id: str, thread=None):
        """
        Send "read" receipt, signifying that the WhatsApp message sent has been displayed on the XMPP
        client.
        """
        receipt = whatsapp.Receipt(
            MessageIDs=go.Slice_string([legacy_msg_id]),
            JID=(
                c.get_message_sender(legacy_msg_id)
                if isinstance(c, MUC)
                else c.legacy_id
            ),
            GroupJID=c.legacy_id if c.is_group else "",
        )
        self.whatsapp.SendReceipt(receipt)

    async def on_react(
        self, c: Recipient, legacy_msg_id: str, emojis: list[str], thread=None
    ):
        """
        Send or remove emoji reaction to existing WhatsApp message.
        Slidge core makes sure that the emojis parameter is always empty or a
        *single* emoji.
        """
        is_carbon = self.message_is_carbon(c, legacy_msg_id)
        message_sender_id = (
            c.get_message_sender(legacy_msg_id)
            if not is_carbon and isinstance(c, MUC)
            else ""
        )
        message = whatsapp.Message(
            Kind=whatsapp.MessageReaction,
            ID=legacy_msg_id,
            JID=c.legacy_id,
            OriginJID=message_sender_id,
            Body=emojis[0] if emojis else "",
            IsCarbon=is_carbon,
        )
        self.whatsapp.SendMessage(message)

    async def on_retract(self, c: Recipient, legacy_msg_id: str, thread=None):
        """
        Request deletion (aka retraction) for a given WhatsApp message.
        """
        message = whatsapp.Message(
            Kind=whatsapp.MessageRevoke, ID=legacy_msg_id, JID=c.legacy_id
        )
        self.whatsapp.SendMessage(message)

    async def on_moderate(
        self,
        muc: MUC,  # type:ignore
        legacy_msg_id: str,
        reason: Optional[str],
    ):
        message = whatsapp.Message(
            Kind=whatsapp.MessageRevoke,
            ID=legacy_msg_id,
            JID=muc.legacy_id,
            OriginJID=muc.get_message_sender(legacy_msg_id),
        )
        self.whatsapp.SendMessage(message)
        # Apparently, no revoke event is received by whatsmeow after sending
        # the revoke message, so we need to "echo" it here.
        part = await muc.get_user_participant()
        part.moderate(legacy_msg_id)

    async def on_correct(
        self,
        c: Recipient,
        text: str,
        legacy_msg_id: str,
        thread=None,
        link_previews=(),
        mentions=None,
    ):
        """
        Request correction (aka editing) for a given WhatsApp message.
        """
        message = whatsapp.Message(
            Kind=whatsapp.MessageEdit, ID=legacy_msg_id, JID=c.legacy_id, Body=text
        )
        self.whatsapp.SendMessage(message)

    async def on_avatar(
        self,
        bytes_: Optional[bytes],
        hash_: Optional[str],
        type_: Optional[str],
        width: Optional[int],
        height: Optional[int],
    ) -> None:
        """
        Update profile picture in WhatsApp for corresponding avatar change in XMPP.
        """
        self.whatsapp.SetAvatar(
            "", go.Slice_byte.from_bytes(bytes_) if bytes_ else go.Slice_byte()
        )

    async def on_create_group(
        self,
        name: str,
        contacts: list[Contact],  # type:ignore
    ):
        """
        Creates a WhatsApp group for the given human-readable name and participant list.
        """
        group = self.whatsapp.CreateGroup(
            name, go.Slice_string([c.legacy_id for c in contacts])
        )
        muc = await self.bookmarks.by_legacy_id(group.JID)
        return muc.legacy_id

    async def on_leave_group(self, legacy_muc_id: str):  # type:ignore
        """
        Removes own user from given WhatsApp group.
        """
        self.whatsapp.LeaveGroup(legacy_muc_id)

    async def on_search(self, form_values: dict[str, str]):
        """
        Searches for, and automatically adds, WhatsApp contact based on phone number. Phone numbers
        not registered on WhatsApp will be ignored with no error.
        """
        phone = form_values.get("phone")
        if not is_valid_phone_number(phone):
            raise ValueError("Not a valid phone number", phone)

        data = self.whatsapp.FindContact(phone)
        if not data.JID:
            return

        await self.contacts.add_whatsapp_contact(data)
        contact = await self.contacts.by_legacy_id(data.JID)

        return SearchResult(
            fields=[FormField("phone"), FormField("jid", type="jid-single")],
            items=[{"phone": cast(str, phone), "jid": contact.jid.bare}],
        )

    def message_is_carbon(self, c: Recipient, legacy_msg_id: str):
        stored: Any
        if c.is_group:
            assert isinstance(c, MUC)
            assert c.pk is not None
            stored = self.xmpp.store.sent.get_group_xmpp_id(c.pk, legacy_msg_id)
        else:
            stored = self.xmpp.store.sent.get_xmpp_id(self.user_pk, legacy_msg_id)
        return stored is not None

    def __reset_connected(self):
        if hasattr(self, "_connected") and not self.__connected.done():
            self.xmpp.loop.call_soon_threadsafe(self.__connected.cancel)
        self.__connected: asyncio.Future[str] = self.xmpp.loop.create_future()

    def __get_connected_status_message(self):
        return f"Connected as {self.user_phone}"

    async def __get_body(
        self, message: whatsapp.Message, muc: Optional["MUC"] = None
    ) -> str:
        body = message.Body
        if muc:
            body = await muc.replace_mentions(body)
        if message.Location.Latitude != 0 or message.Location.Longitude != 0:
            body = "geo:%f,%f" % (message.Location.Latitude, message.Location.Longitude)
            if message.Location.Accuracy > 0:
                body = body + ";u=%d" % message.Location.Accuracy
        if message.IsForwarded:
            body = "↱ Forwarded message:\n " + add_quote_prefix(body)
        if message.Album.IsAlbum:
            body = body + "Album: "
            if message.Album.ImageCount > 0:
                body = body + "%d photos, " % message.Album.ImageCount
            if message.Album.VideoCount > 0:
                body = body + "%d videos" % message.Album.VideoCount
            body = body.rstrip(" ,:")
        return body

    async def __get_reply_to(
        self, message: whatsapp.Message, muc: Optional["MUC"] = None
    ) -> Optional[MessageReference]:
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
        if message.OriginJID == self.contacts.user_legacy_id:
            reply_to.author = "user"
        else:
            reply_to.author = await self.__get_contact_or_participant(
                message.OriginJID, message.GroupJID
            )
        return reply_to

    async def __get_preview(self, text: str) -> Optional[whatsapp.Preview]:
        if not config.ENABLE_LINK_PREVIEWS:
            return None
        match = search(URL_SEARCH_REGEX, text)
        if not match:
            return None
        url = match.group("url")
        try:
            async with self.http.get(url) as resp:
                if resp.status != 200:
                    self.log.debug(
                        "Could not generate a preview for %s because response status was %s",
                        url,
                        resp.status,
                    )
                    return None
                if resp.content_type != "text/html":
                    self.log.debug(
                        "Could not generate a preview for %s because content type is %s",
                        url,
                        resp.content_type,
                    )
                    return None
                try:
                    html = await resp.text()
                except Exception as e:
                    self.log.debug(
                        "Could not generate a preview for %s", url, exc_info=e
                    )
                    return None
                preview = LinkPreview(Link(url, html))
                if not preview.title:
                    return None
                thumbnail = (
                    await get_url_bytes(self.http, preview.image)
                    if preview.image
                    else None
                )
                kind = (
                    whatsapp.PreviewVideo
                    if url.startswith(VIDEO_PREVIEW_DOMAINS)
                    else whatsapp.PreviewPlain
                )
                return whatsapp.Preview(
                    Kind=kind,
                    Title=preview.title,
                    Description=preview.description or "",
                    URL=url,
                    Thumbnail=(
                        go.Slice_byte.from_bytes(thumbnail)
                        if thumbnail
                        else go.Slice_byte()
                    ),
                )
        except Exception as e:
            self.log.debug("Could not generate a preview for %s", url, exc_info=e)
            return None

    async def __get_location(self, text: str) -> Optional[whatsapp.Location]:
        match = search(GEO_URI_SEARCH_REGEX, text)
        if not match:
            return None
        latitude = match.group("lat")
        longitude = match.group("lon")
        if latitude == "" or longitude == "":
            return None
        return whatsapp.Location(
            Latitude=float(latitude),
            Longitude=float(longitude),
            Accuracy=int(match.group("acc") or 0),
        )

    async def __get_contact_or_participant(
        self, legacy_contact_id: str, legacy_group_jid: str
    ):
        """
        Return either a Contact or a Participant instance for the given contact and group JIDs.
        """
        if legacy_group_jid:
            muc = await self.bookmarks.by_legacy_id(legacy_group_jid)
            return await muc.get_participant_by_legacy_id(legacy_contact_id)
        else:
            return await self.contacts.by_legacy_id(legacy_contact_id)


class Attachment(LegacyAttachment):
    @staticmethod
    async def convert_list(
        attachments: list, muc: Optional["MUC"] = None
    ) -> list["Attachment"]:
        return [await Attachment.convert(attachment, muc) for attachment in attachments]

    @staticmethod
    async def convert(
        wa_attachment: whatsapp.Attachment, muc: Optional["MUC"] = None
    ) -> "Attachment":
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


def add_quote_prefix(text: str):
    """
    Return multi-line text with leading quote marks (i.e. the ">" character).
    """
    return "\n".join(("> " + x).strip() for x in text.split("\n")).strip()


def strip_quote_prefix(text: str):
    """
    Return multi-line text without leading quote marks (i.e. the ">" character).
    """
    return "\n".join(x.lstrip(">").strip() for x in text.split("\n")).strip()


def set_reply_to(
    chat: Recipient,
    message: whatsapp.Message,
    reply_to_msg_id: Optional[str] = None,
    reply_to_fallback_text: Optional[str] = None,
    reply_to=None,
):
    if reply_to_msg_id:
        message.ReplyID = reply_to_msg_id
    if reply_to:
        message.OriginJID = (
            reply_to.contact.legacy_id if chat.is_group else chat.legacy_id
        )
    if reply_to_fallback_text:
        message.ReplyBody = strip_quote_prefix(reply_to_fallback_text)
        message.Body = message.Body.lstrip()
    return message


async def get_url_bytes(client: ClientSession, url: str) -> Optional[bytes]:
    async with client.get(url) as resp:
        if resp.status == 200:
            return await resp.read()
    return None


def make_sync(func, loop):
    """
    Wrap async function in synchronous operation, running against the given loop in thread-safe mode.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        if asyncio.iscoroutine(result):
            future = asyncio.run_coroutine_threadsafe(result, loop)
            return future.result()
        return result

    return wrapper
