from typing import TYPE_CHECKING, Optional

from slidge.command import Command, CommandAccess, Form, FormField
from slidge.util import is_valid_phone_number
from slixmpp import JID
from slixmpp.exceptions import XMPPError

from .generated import whatsapp

if TYPE_CHECKING:
    from .session import Session


class Logout(Command):
    NAME = "🔓 Disconnect from WhatsApp"
    HELP = (
        "Disconnects active WhatsApp session without removing any linked device credentials. "
        "To re-connect, use the 're-login' command."
    )
    NODE = "wa_logout"
    CHAT_COMMAND = "logout"
    ACCESS = CommandAccess.USER_LOGGED

    async def run(
        self,
        session: Optional["Session"],  # type:ignore
        ifrom: JID,
        *args,
    ) -> Form:
        assert session is not None
        try:
            msg = session.shutdown()
        except Exception as e:
            session.send_gateway_status(f"Logout failed: {e}", show="dnd")
            raise XMPPError(
                "internal-server-error",
                etype="wait",
                text=f"Could not logout WhatsApp session: {e}",
            )
        session.send_gateway_message(msg or "Logged out successfully")
        session.send_gateway_status(msg or "Logged out", show="away")
        return msg


class PairPhone(Command):
    NAME = "📱 Complete registration via phone number"
    HELP = (
        "As an alternative to QR code verification, this allows you to complete registration "
        "by inputing a one-time code into the official WhatsApp client; this requires that you "
        "provide the phone number used for the main device, in international format "
        "(e.g. +447700900000). See more information here: https://faq.whatsapp.com/1324084875126592"
    )
    NODE = "wa_pair_phone"
    CHAT_COMMAND = "pair-phone"
    ACCESS = CommandAccess.USER_NON_LOGGED

    async def run(
        self,
        session: Optional["Session"],  # type:ignore
        ifrom: JID,
        *args,
    ) -> Form:
        return Form(
            title="Pair to WhatsApp via phone number",
            instructions="Enter your phone number in international format (e.g. +447700900000)",
            fields=[FormField(var="phone", label="Phone number", required=True)],
            handler=self.finish,  # type:ignore
        )

    @staticmethod
    async def finish(form_values: dict, session: "Session", _ifrom: JID):
        p = form_values.get("phone")
        if not is_valid_phone_number(p):
            raise ValueError("Not a valid phone number", p)
        code = await session.run_in_executor(session.whatsapp.PairPhone, p)
        return f"Please open the official WhatsApp client and input the following code: {code}"


class ChangePresence(Command):
    NAME = "📴 Set WhatsApp web presence"
    HELP = (
        "If you want to receive notifications in the WhatsApp official client,"
        "you need to set your presence to unavailable. As a side effect, you "
        "won't receive receipts and presences from your contacts."
    )
    NODE = "wa_presence"
    CHAT_COMMAND = "presence"
    ACCESS = CommandAccess.USER_LOGGED

    async def run(
        self,
        session: Optional["Session"],  # type:ignore
        ifrom: JID,
        *args,
    ) -> Form:
        return Form(
            title="Set WhatsApp web presence",
            instructions="Choose what type of presence you want to set",
            fields=[
                FormField(
                    var="presence",
                    value="available",
                    type="list-single",
                    options=[
                        {"label": "Available", "value": "available"},
                        {"label": "Unavailable", "value": "unavailable"},
                    ],
                )
            ],
            handler=self.finish,  # type:ignore
        )

    @staticmethod
    async def finish(form_values: dict, session: "Session", _ifrom: JID):
        p = form_values.get("presence")
        if p == "available":
            await session.run_in_executor(
                session.whatsapp.SendPresence, whatsapp.PresenceAvailable, ""
            )
        elif p == "unavailable":
            await session.run_in_executor(
                session.whatsapp.SendPresence, whatsapp.PresenceUnavailable, ""
            )
        else:
            raise ValueError("Not a valid presence kind.", p)
        return f"Presence succesfully set to {p}"


class SubscribeToPresences(Command):
    NAME = "🔔 Subscribe to contacts' presences"
    HELP = (
        "Subscribes to and refreshes contacts' presences; typically this is "
        "done automatically, but re-subscribing might be useful in case contact "
        "presences are stuck or otherwise not updating."
    )
    NODE = "wa_subscribe"
    CHAT_COMMAND = "subscribe"
    ACCESS = CommandAccess.USER_LOGGED

    async def run(
        self,
        session: Optional["Session"],  # type:ignore
        ifrom: JID,
        *args,
    ) -> str:
        assert session is not None
        await session.run_in_executor(session.whatsapp.GetContacts, False)
        return "Looks like no exception was raised. Success, I guess?"
