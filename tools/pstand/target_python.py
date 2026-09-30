from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Target, runtime_spec
from .runtime_provider import ensure_sdk, sdk_info


@dataclass(frozen=True)
class TargetPython:
    target: Target
    root: Path
    executable: Path
    include_dir: Path
    python_version: str
    python_major_minor: str
    abi_tag: str
    libpython_link_mode: str | None

    @property
    def platform_tag(self) -> str:
        if self.target.os == "windows":
            return {"x86": "win32", "x86_64": "win_amd64", "arm64": "win_arm64"}[self.target.arch]
        if self.target.os == "linux":
            return {"x86_64": "manylinux_2_17_x86_64", "arm64": "manylinux_2_17_aarch64"}[self.target.arch]
        return "macosx_11_0_arm64" if self.target.arch == "arm64" else "macosx_10_15_x86_64"


def resolve_target_python(target: Target) -> TargetPython:
    spec = runtime_spec(target)
    root = ensure_sdk(target)
    info = sdk_info(target)
    exe = Path(info["python_executable"])
    include = Path(info["include_dir"])
    if not exe.exists():
        raise RuntimeError(f"Target Python executable not found: {exe}")
    configured = spec.python
    actual = str(info.get("python") or configured)
    if not actual.startswith(configured):
        raise RuntimeError(f"Target Python {actual} does not match configured runtime Python {configured}")
    major_minor = ".".join(actual.split(".")[:2])
    return TargetPython(
        target=target,
        root=root,
        executable=exe,
        include_dir=include,
        python_version=actual,
        python_major_minor=major_minor,
        abi_tag=f"cp{major_minor.replace('.', '')}",
        libpython_link_mode=info.get("libpython_link_mode"),
    )
