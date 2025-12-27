import os
import warnings
from logging import getLevelName, getLogger
from pathlib import Path
from typing import TYPE_CHECKING

from slidge import BaseGateway, FormField, global_config

from . import config
from .generated import whatsapp

if TYPE_CHECKING:
    from .session import Session

REGISTRATION_INSTRUCTIONS = (
    "Continue and scan the resulting QR codes on your main device, or alternatively, "
    "use the 'pair-phone' command to complete registration. More information at "
    "https://slidge.im/docs/slidge-whatsapp/main/user/registration.html"
)

WELCOME_MESSAGE = (
    "Thank you for registering! Please scan the following QR code on your main device "
    "or use the 'pair-phone' command to complete registration, or type 'help' to list "
    "other available commands."
)

if os.cpu_count() is None:
    warnings.warn(
        "Could not determine the CPU count, assuming 1. "
        "Consider launching slidge_whatsapp with 'python -X cpu_count=n -m slidge_whatsapp ...' "
        "if you run into crashes related to the DB QueuePool."
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

    # This should be higher than the number of maximum concurrent threads or goroutines
    # trying to acquire an SQLAlchemy ORM session. The default value is 5, and is not
    # enough in practice, leading to timeouts, cf <https://docs.sqlalchemy.org/en/20/errors.html#error-3o7r>.
    # The +31 bit is a margin that proved empirically necessary on my (nicoco) production
    # slidge-whatsapp instance. It is a workaround for some potential underlying design
    # flaws in slidge core, cf <https://codeberg.org/slidge/slidge-whatsapp/pulls/103#issuecomment-9237104>.
    DB_POOL_SIZE = (os.cpu_count() or 1) + 31

    def __init__(self):
        super().__init__()
        self.whatsapp = whatsapp.NewGateway()
        self.whatsapp.Name = "Slidge on " + str(global_config.JID)
        self.whatsapp.LogLevel = getLevelName(getLogger().level)

        assert config.DB_PATH is not None
        Path(config.DB_PATH.parent).mkdir(exist_ok=True)
        self.whatsapp.DBPath = str(config.DB_PATH) + config.DB_PARAMS

        (global_config.HOME_DIR / "tmp").mkdir(exist_ok=True)
        self.whatsapp.TempDir = str(global_config.HOME_DIR / "tmp")
        self.whatsapp.Init()

    async def validate(self, user_jid, registration_form):
        """
        Validate registration form. A no-op for WhatsApp, as actual registration takes place
        after in-band registration commands complete; see :meth:`.Session.login` for more.
        """
        pass

    async def unregister(self, session: "Session"):  # type:ignore[override]
        """
        Logout from the active WhatsApp session. This will also force a remote log-out, and thus
        require pairing on next login. For simply disconnecting the active session, look at the
        :meth:`.Session.disconnect` function.
        """
        session.whatsapp.Logout()
        try:
            device_id = session.user.legacy_module_data["device_id"]
            self.whatsapp.CleanupSession(whatsapp.LinkedDevice(ID=device_id))
        except KeyError:
            pass
        except RuntimeError as err:
            log.error("Failed to clean up WhatsApp session: %s", err)


log = getLogger(__name__)

# workaround until slidge core does something for anon/nonanon mixed troups
warnings.filterwarnings("ignore", ".*Private group but.*")
