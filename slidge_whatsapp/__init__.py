# DO NOT EDIT
# Generated from .copier-answers.yml

import sys
from importlib.metadata import PackageNotFoundError, version

from slidge import entrypoint

# import everything for automatic subclasses discovery by slidge core
from . import command, config, contact, gateway, group, session

try:
    __version__ = version("slidge-whatsapp")
except PackageNotFoundError:
    # package is not installed
    __version__ = "dev"


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--version":
        print("slidge-whatsapp version", __version__)
        exit(0)
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
