# DO NOT EDIT
# Generated from .copier-answers.yml

from slidge import entrypoint
from slidge.util.util import get_version  # noqa: F401

# import everything for automatic subclasses discovery by slidge core
from . import command, config, contact, gateway, group, session
from .__version__ import __version__


def main():
    entrypoint("slidge_whatsapp")


__all__ = (
    "__version__",
    "command",
    "config",
    "contact",
    "gateway",
    "group",
    "main",
    "session",
)
