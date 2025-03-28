# build script for whatsapp extensions

import os
import shutil
import subprocess
from pathlib import Path


def main():
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
    subprocess.run(
        [
            "gopy",
            "build",
            "-output=generated",
            "-no-make=true",
            ".",
        ],
        cwd=Path(".") / "slidge_whatsapp",
        check=True,
    )


if __name__ == "__main__":
    main()
