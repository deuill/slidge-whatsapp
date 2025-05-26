"""
Config contains plugin-specific configuration for WhatsApp, and is loaded automatically by the
core configuration framework.
"""

from pathlib import Path
from typing import Optional

from slidge import global_config

# workaround because global_config.HOME_DIR is not defined unless
# called by slidge's main(), which is a problem for tests, docs and the
# dedicated slidge-whatsapp setuptools entrypoint
try:
    DB_PATH: Optional[Path] = global_config.HOME_DIR / "whatsapp" / "whatsapp.db"
except AttributeError:
    DB_PATH: Optional[Path] = None  # type:ignore

DB_PATH__DOC = (
    "The path to the database used for the WhatsApp plugin. Default to "
    "${SLIDGE_HOME_DIR}/whatsapp/whatsapp.db"
)

DB_PARAMS = "?_foreign_keys=true"
DB_PARAMS__DOC = "Additional parameters to pass to database connection string."

ALWAYS_SYNC_ROSTER = False
ALWAYS_SYNC_ROSTER__DOC = (
    "Whether or not to perform a full sync of the WhatsApp roster on startup."
)

ENABLE_LINK_PREVIEWS = True
ENABLE_LINK_PREVIEWS__DOC = (
    "Whether or not previews for links (URLs) should be generated on outgoing messages"
)
