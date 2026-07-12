from __future__ import annotations

import abc
import logging
import time
from os.path import basename
from re import search
from typing import TYPE_CHECKING

import aiohttp
from linkpreview import Link, LinkPreview
from slidge.core.mixins import AvatarMixin as BaseAvatarMixin
from slidge.util import replace_mentions
from slidge.util.types import ChatState, Mention, XMPPAttachmentMessage, XMPPMessage
from slixmpp.exceptions import XMPPError

from .generated import go, whatsapp

if TYPE_CHECKING:
    from .session import Session


URL_SEARCH_REGEX = r"(?P<url>https?://[^\s]+)"
GEO_URI_SEARCH_REGEX = (
    r"geo:(?P<lat>-?\d+(\.\d*)?),(?P<lon>-?\d+(\.\d*)?)(;u=(?P<acc>-?\d+(\.\d*)?))?"
)


VIDEO_PREVIEW_DOMAINS = (
    "https://youtube.com/watch",
    "https://m.youtube.com/watch",
    "https://youtu.be",
)


class AvatarMixin(BaseAvatarMixin):
    legacy_id: str
    session: Session

    async def update_whatsapp_avatar(self) -> None:
        unique_id = ""
        if self.avatar is not None:
            # assert=workaround for poor type annotations in slidge core
            assert not isinstance(self.avatar.unique_id, int)
            unique_id = self.avatar.unique_id or ""
        self.session.whatsapp.RequestAvatar(self.legacy_id, unique_id)  # type:ignore[no-untyped-call]


class RecipientMixin(abc.ABC):
    session: Session
    log: logging.Logger
    legacy_id: str

    @property
    def wa(self) -> whatsapp.Session:
        return self.session.whatsapp

    @abc.abstractmethod
    def get_wa_chat(self) -> whatsapp.Chat: ...

    @abc.abstractmethod
    async def get_wa_actor(self, legacy_msg_id: str) -> whatsapp.Actor: ...

    @abc.abstractmethod
    def _set_reply_to(
        self, xmpp_msg: XMPPMessage, wa_msg: whatsapp.Message
    ) -> None: ...

    async def on_message(self, message: XMPPMessage) -> str | None:
        if message.attachments:
            return await self._on_file(message)
        if message.body:
            if message.replace:
                await self._on_correct(message)
                return None
            else:
                return await self._on_text(message)
        raise XMPPError("internal-server-error", "This should not happen!")

    async def _on_text(self, xmpp_msg: XMPPMessage) -> str:
        """
        Send outgoing plain-text message to given WhatsApp contact.
        """
        assert xmpp_msg.body
        message_id: str = self.wa.GenerateMessageID()  # type:ignore[no-untyped-call]
        message_preview = await self.__get_preview(xmpp_msg.body) or whatsapp.Preview()  # type:ignore[no-untyped-call]
        message_location = (
            await self.__get_location(xmpp_msg.body) or whatsapp.Location()  # type:ignore[no-untyped-call]
        )
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            ID=message_id,
            Chat=self.get_wa_chat(),
            Body=replace_mentions(xmpp_msg.body, xmpp_msg.mentions, mention_map),
            Preview=message_preview,
            Location=message_location,
            MentionJIDs=go.Slice_string(  # type:ignore[no-untyped-call]
                [m.contact.legacy_id for m in xmpp_msg.mentions]
            ),
        )
        self._set_reply_to(xmpp_msg, message)
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]
        return message_id

    async def _on_file(self, xmpp_msg: XMPPAttachmentMessage) -> str:
        """
        Send outgoing media message (i.e. audio, image, document) to given WhatsApp contact.
        """
        # TODO: handle multiple attachments when we support that in slidge core
        att = xmpp_msg.attachments[0]
        async with att.get() as resp:
            if resp.status == 200:
                data = await resp.read()
            else:
                raise XMPPError(
                    "internal-server-error",
                    "Unable to retrieve file from XMPP server, try again",
                )
            content_type = resp.content_type
        message_id: str = self.wa.GenerateMessageID()  # type:ignore[no-untyped-call]
        message_attachment = whatsapp.Attachment(  # type:ignore[no-untyped-call]
            MIME=content_type,
            Filename=basename(att.url),
            Data=go.Slice_byte.from_bytes(data),  # type:ignore[no-untyped-call]
            Caption=xmpp_msg.body or "",
        )
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageAttachment,
            ID=message_id,
            Chat=self.get_wa_chat(),
            ReplyID=xmpp_msg.reply.msg_id if xmpp_msg.reply else "",
            Attachments=whatsapp.Slice_whatsapp_Attachment([message_attachment]),  # type:ignore[no-untyped-call]
        )
        self._set_reply_to(xmpp_msg, message)
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]
        return message_id

    async def _on_correct(self, xmpp_msg: XMPPMessage) -> None:
        """
        Request correction (aka editing) for a given WhatsApp message.
        """
        assert xmpp_msg.body
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageEdit,
            ID=xmpp_msg.replace,
            Chat=self.get_wa_chat(),
            Body=replace_mentions(xmpp_msg.body, xmpp_msg.mentions, mention_map),
        )
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]

    async def __get_preview(self, text: str) -> whatsapp.Preview | None:
        enable_previews = self.session.user.preferences.get(
            "enable_link_previews", True
        )
        if not enable_previews:
            return None
        match = search(URL_SEARCH_REGEX, text)
        if not match:
            return None
        url = match.group("url")
        try:
            async with self.session.http.get(url) as resp:
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
                    await get_url_bytes(self.session.http, preview.image)
                    if preview.image
                    else None
                )
                kind = (
                    whatsapp.PreviewVideo
                    if url.startswith(VIDEO_PREVIEW_DOMAINS)
                    else whatsapp.PreviewPlain
                )
                return whatsapp.Preview(  # type:ignore[no-untyped-call]
                    Kind=kind,
                    Title=preview.title,
                    Description=preview.description or "",
                    URL=url,
                    Thumbnail=(
                        go.Slice_byte.from_bytes(thumbnail)  # type:ignore[no-untyped-call]
                        if thumbnail
                        else go.Slice_byte()  # type:ignore[no-untyped-call]
                    ),
                )
        except Exception as e:
            self.log.debug("Could not generate a preview for %s", url, exc_info=e)
            return None

    async def __get_location(self, text: str) -> whatsapp.Location | None:
        match = search(GEO_URI_SEARCH_REGEX, text)
        if not match:
            return None
        latitude = match.group("lat")
        longitude = match.group("lon")
        if latitude == "" or longitude == "":
            return None
        return whatsapp.Location(  # type:ignore[no-untyped-call]
            Latitude=float(latitude),
            Longitude=float(longitude),
            Accuracy=int(match.group("acc") or 0),
        )

    async def on_chat_state(self, chat_state: ChatState, thread: str | None) -> None:
        match chat_state:
            case "composing":
                self.__send_state(whatsapp.ChatStateComposing)
            case "paused":
                self.__send_state(whatsapp.ChatStatePaused)

    def __send_state(self, kind: int) -> None:
        state = whatsapp.ChatState(Chat=self.get_wa_chat(), Kind=kind)  # type:ignore[no-untyped-call]
        self.wa.SendChatState(state)  # type:ignore[no-untyped-call]

    async def on_displayed(self, legacy_msg_id: str, thread: str | None) -> None:
        receipt = whatsapp.Receipt(  # type:ignore[no-untyped-call]
            MessageIDs=go.Slice_string([legacy_msg_id]),  # type:ignore[no-untyped-call]
            Chat=self.get_wa_chat(),
            OriginActor=await self.get_wa_actor(legacy_msg_id),
            Timestamp=round(int(time.time())),
        )
        self.wa.SendReceipt(receipt)  # type:ignore[no-untyped-call]

    async def on_react(
        self, legacy_msg_id: str, emojis: list[str], thread: str | None
    ) -> None:
        """
        Send or remove emoji reaction to existing WhatsApp message.
        Slidge core makes sure that the emojis parameter is always empty or a
        *single* emoji.
        """
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageReaction,
            ID=legacy_msg_id,
            Chat=self.get_wa_chat(),
            Body=emojis[0] if emojis else "",
            OriginActor=await self.get_wa_actor(legacy_msg_id),
        )
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]

    async def on_retract(self, legacy_msg_id: str, thread: str | None) -> None:
        """
        Request deletion (aka retraction) for a given WhatsApp message.
        """
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageRevoke,
            ID=legacy_msg_id,
            Chat=self.get_wa_chat(),
        )
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]


def mention_map(mention: Mention) -> str:
    # mentions are @phonenumber, without the @s.whatsapp.net or @lid suffix
    return f"@{mention.contact.phone}"  # type:ignore


def strip_quote_prefix(text: str) -> str:
    """
    Return multi-line text without leading quote marks (i.e. the ">" character).
    """
    return "\n".join(x.lstrip(">").strip() for x in text.split("\n")).strip()


async def get_url_bytes(client: aiohttp.ClientSession, url: str) -> bytes | None:
    async with client.get(url) as resp:
        if resp.status == 200:
            return await resp.read()
    return None
