# build script for whatsapp extensions
import hashlib
import os
import platform
import shutil
import subprocess
from pathlib import Path

from packaging.tags import sys_tags

SRC_PATH = Path(".") / "slidge_whatsapp"


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
    build_go()
    suffix = get_correct_lib_suffix()
    if suffix is None:
        return
    # remove the useless prebuilt libmupdf libs for other platforms
    for path in SRC_PATH.glob("**/*.a"):
        if not path.stem.endswith(suffix):
            path.unlink()


def build_go():
    current_sum = ""
    for p in sorted(list(SRC_PATH.glob("**/*.go"))):
        p_rel = p.relative_to(SRC_PATH)
        if p_rel.parents[0].name == "generated":
            continue
        h = hashlib.sha512(p.read_text().encode()).hexdigest()
        current_sum += f"{p_rel}: {h}\n"
    known_sum_path = SRC_PATH / ".gopy.sum"
    previous_sum = known_sum_path.read_text() if known_sum_path.exists() else None
    if current_sum == previous_sum:
        print("Go files have not changed, no need to build")
        return

    if not shutil.which("go"):
        raise RuntimeError(
            "Cannot find the go executable in $PATH. "
            "Make you sure install golang, via your package manager or https://go.dev/dl/"
        )
    os.environ["PATH"] = os.path.expanduser("~/go/bin") + ":" + os.environ["PATH"]
    subprocess.run(["go", "install", "github.com/go-python/gopy@master"], check=True)
    subprocess.run(
        ["go", "install", "golang.org/x/tools/cmd/goimports@latest"], check=True
    )

    print("Building go parts…")
    subprocess.run(
        [
            "gopy",
            "build",
            "-output=generated",
            "-no-make=true",
            '-build-tags="mupdf extlib"',
            ".",
        ],
        cwd=SRC_PATH,
        check=True,
    )
    known_sum_path.write_text(current_sum)


if __name__ == "__main__":
    main()
