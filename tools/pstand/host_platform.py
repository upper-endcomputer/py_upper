from __future__ import annotations
import platform as _platform
import subprocess
from pathlib import Path
from .config import Target, host_target


def cmake() -> str:
    return "cmake"


def ninja() -> str:
    return "ninja"


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(map(str, cmd)))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def host_description() -> str:
    return f"{_platform.system()} {_platform.machine()} Python {_platform.python_version()}"
