from slidge import entrypoint
from slidge.util.util import get_version  # noqa: F401

# import everything for automatic subclasses discovery by slidge core
from . import command, config, contact, gateway, group, session


def main():
    entrypoint("slidge_whatsapp")


__all__ = "command", "config", "contact", "gateway", "group", "main", "session"

__version__ = get_version()
