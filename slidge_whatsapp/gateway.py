from logging import getLevelName, getLogger
from pathlib import Path
from typing import TYPE_CHECKING

from slidge import BaseGateway, FormField, GatewayUser, global_config

from . import config
from .generated import whatsapp

if TYPE_CHECKING:
    from .session import Session

REGISTRATION_INSTRUCTIONS = (
    "Continue and scan the resulting QR codes on your main device, or alternatively, "
    "use the 'pair-phone' command to complete registration. More information at "
    "https://slidge.im/slidge-whatsapp/user.html"
)

WELCOME_MESSAGE = (
    "Thank you for registering! Please scan the following QR code on your main device "
    "or use the 'pair-phone' command to complete registration, or type 'help' to list "
    "other available commands."
)


class Gateway(BaseGateway):
    COMPONENT_NAME = "WhatsApp (slidge)"
    COMPONENT_TYPE = "whatsapp"
    COMPONENT_AVATAR = "https://www.whatsapp.com/apple-touch-icon.png"
    ROSTER_GROUP = "WhatsApp"

    REGISTRATION_INSTRUCTIONS = REGISTRATION_INSTRUCTIONS
    WELCOME_MESSAGE = WELCOME_MESSAGE
    REGISTRATION_FIELDS = []

    SEARCH_FIELDS = [
        FormField(var="phone", label="Phone number", required=True),
    ]

    MARK_ALL_MESSAGES = True
    GROUPS = True
    PROPER_RECEIPTS = True

    def __init__(self):
        super().__init__()
        self.whatsapp = whatsapp.NewGateway()
        self.whatsapp.Name = "Slidge on " + str(global_config.JID)
        self.whatsapp.LogLevel = getLevelName(getLogger().level)

        assert config.DB_PATH is not None
        Path(config.DB_PATH.parent).mkdir(exist_ok=True)
        self.whatsapp.DBPath = str(config.DB_PATH)

        (global_config.HOME_DIR / "tmp").mkdir(exist_ok=True)
        self.whatsapp.TempDir = str(global_config.HOME_DIR / "tmp")
        self.whatsapp.Init()

    async def validate(self, user_jid, registration_form):
        """
        Validate registration form. A no-op for WhatsApp, as actual registration takes place
        after in-band registration commands complete; see :meth:`.Session.login` for more.
        """
        pass

    async def unregister(self, user: GatewayUser):
        """
        Logout from the active WhatsApp session. This will also force a remote log-out, and thus
        require pairing on next login. For simply disconnecting the active session, look at the
        :meth:`.Session.disconnect` function.
        """
        session: "Session" = self.get_session_from_user(user)  # type:ignore
        session.whatsapp.Logout()
        try:
            device_id = session.user.legacy_module_data["device_id"]
            self.whatsapp.CleanupSession(whatsapp.LinkedDevice(ID=device_id))
        except KeyError:
            pass
        except RuntimeError as err:
            log.error("Failed to clean up WhatsApp session: %s", err)


log = getLogger(__name__)
