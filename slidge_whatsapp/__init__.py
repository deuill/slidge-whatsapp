# DO NOT EDIT
# Generated from .copier-answers.yml

import sys
from importlib.metadata import PackageNotFoundError, version

from slidge import __version__ as slidge_version
from slidge import entrypoint

from .gateway import Gateway

try:
    __version__ = version("slidge-whatsapp")
except PackageNotFoundError:
    # package is not installed
    __version__ = "dev"


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "--version":
        print("slidge version", slidge_version)
        print("slidge-whatsapp version", __version__)
        exit(0)
    entrypoint("slidge_whatsapp")


__all__ = ("Gateway", "__version__", "main")
