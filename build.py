# build script for whatsapp extensions

import os
import platform
import shutil
import subprocess
from pathlib import Path

from packaging.tags import sys_tags


def is_musl():
    # taken from https://stackoverflow.com/a/75172415/5902284
    tags = list(sys_tags())
    return "musllinux" in tags[0].platform


def get_correct_lib_suffix():
    system = platform.system().lower()
    machine = platform.machine().lower()
    suffix = "_musl" if is_musl() else ""
    if machine in ("aarch64", "aarch64_be", "armv8b", "armv8l"):
        return system + "_arm64" + suffix
    if machine in ("x86_64", "amd64", "i386", "i686"):
        return system + "_amd64" + suffix
    return None


def main():
    if not shutil.which("go"):
        raise RuntimeError(
            "Cannot find the go executable in $PATH. "
            "Make you sure install golang, via your package manager or https://go.dev/dl/"
        )
    os.environ["PATH"] = os.path.expanduser("~/go/bin") + ":" + os.environ["PATH"]
    os.environ["CGO_LDFLAGS"] = (
        "-lgumbo -lfreetype -ljbig2dec -lharfbuzz -ljpeg -lmujs -lopenjp2"
    )
    subprocess.run(["go", "install", "github.com/go-python/gopy@master"], check=True)
    subprocess.run(
        ["go", "install", "golang.org/x/tools/cmd/goimports@latest"], check=True
    )
    src_path = Path(".") / "slidge_whatsapp"
    subprocess.run(
        [
            "gopy",
            "build",
            "-output=generated",
            "-no-make=true",
            '-build-tags="mupdf extlib static"',
            ".",
        ],
        cwd=src_path,
        check=True,
    )
    suffix = get_correct_lib_suffix()
    if suffix is None:
        return
    # remove the useless prebuilt libmupdf libs for other platforms
    for path in src_path.glob("**/*.a"):
        if not path.stem.endswith(suffix):
            path.unlink()


if __name__ == "__main__":
    main()
