from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Target
from .pbs_sdk import ensure_sdk, sdk_info, target_include_dir, target_python_executable


@dataclass(frozen=True)
class TargetPython:
    target: Target
    root: Path
    executable: Path
    include_dir: Path
    python_version: str
    python_major_minor: str
    libpython_link_mode: str | None


def resolve_target_python(target: Target) -> TargetPython:
    root = ensure_sdk(target)
    info = sdk_info(target)
    exe = target_python_executable(root, target)
    include = target_include_dir(root)
    if not exe.exists():
        raise RuntimeError(f"PBS target Python executable not found: {exe}")
    return TargetPython(
        target=target,
        root=root,
        executable=exe,
        include_dir=include,
        python_version=str(info.get("python") or ""),
        python_major_minor=str(info.get("python_major_minor") or ""),
        libpython_link_mode=info.get("libpython_link_mode"),
    )
