from __future__ import annotations
import os
import platform
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
CACHE = ROOT / ".cache"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
RUNTIMES = ROOT / "runtimes"
LAUNCHER = ROOT / "launcher"

@dataclass(frozen=True)
class Target:
    os: str
    arch: str
    triple: str
    @property
    def key(self) -> str:
        return f"{self.os}-{self.arch}"

@dataclass(frozen=True)
class RuntimeSpec:
    provider: str
    python: str
    target: Target
    root: Path
    sdk_root: Path
    @property
    def major_minor(self) -> str:
        parts = self.python.split(".")
        return ".".join(parts[:2])
    @property
    def abi_tag(self) -> str:
        return f"cp{self.major_minor.replace('.', '')}"

TARGETS = {
    "windows-x86": Target("windows", "x86", "i686-pc-windows-msvc"),
    "windows-x86_64": Target("windows", "x86_64", "x86_64-pc-windows-msvc"),
    "windows-arm64": Target("windows", "arm64", "aarch64-pc-windows-msvc"),
    "macos-x86_64": Target("macos", "x86_64", "x86_64-apple-darwin"),
    "macos-arm64": Target("macos", "arm64", "aarch64-apple-darwin"),
    "linux-x86_64": Target("linux", "x86_64", "x86_64-unknown-linux-gnu"),
    "linux-arm64": Target("linux", "arm64", "aarch64-unknown-linux-gnu"),
}

def load_app_config() -> dict:
    with (APP / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)

def host_target() -> Target:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        return TARGETS["windows-x86_64"]
    if system == "Windows" and machine in {"arm64", "aarch64"}:
        return TARGETS["windows-arm64"]
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return TARGETS["macos-arm64"]
    if system == "Darwin" and machine in {"x86_64", "amd64"}:
        return TARGETS["macos-x86_64"]
    if system == "Linux" and machine in {"arm64", "aarch64"}:
        return TARGETS["linux-arm64"]
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return TARGETS["linux-x86_64"]
    raise RuntimeError(f"Unsupported host: {system} {machine}")

def validate_target(name: str | None) -> Target:
    if not name:
        return host_target()
    if name not in TARGETS:
        raise SystemExit(f"Unknown target: {name}; available: {', '.join(TARGETS)}")
    return TARGETS[name]

def pstand_config() -> dict:
    return load_app_config()["tool"].get("pstand", {})

def runtime_config() -> dict:
    cfg = pstand_config().get("runtime", {})
    return cfg if isinstance(cfg, dict) else {}

def python_version() -> str:
    cfg = runtime_config()
    value = cfg.get("python") or pstand_config().get("python") or ""
    value = str(value)
    if not value:
        raise RuntimeError("[tool.pstand.runtime].python is required")
    return value

def runtime_spec(target: Target) -> RuntimeSpec:
    provider = runtime_provider()
    if provider == "local":
        root = local_runtime_path(target)
        sdk = local_sdk_path(target)
    else:
        root = target_runtime_dir(target)
        sdk = pbs_sdk_dir(target)
    return RuntimeSpec(provider, python_version(), target, root, sdk)

def runtime_provider() -> str:
    provider = str(runtime_config().get("provider") or "pbs").lower()
    if provider not in {"pbs", "local"}:
        raise RuntimeError(f"Unsupported runtime provider: {provider!r}; use 'pbs' or 'local'")
    return provider

def local_runtime_path(target: Target) -> Path:
    value = runtime_config().get("runtime")
    if not value:
        raise RuntimeError("[tool.pstand.runtime].runtime is required when provider = 'local'")
    return _expand_target_path(str(value), target)

def local_sdk_path(target: Target) -> Path:
    value = runtime_config().get("sdk")
    if not value:
        raise RuntimeError("[tool.pstand.runtime].sdk is required when provider = 'local'")
    return _expand_target_path(str(value), target)

def _expand_target_path(value: str, target: Target) -> Path:
    return (ROOT / value.format(target=target.key, os=target.os, arch=target.arch, python=python_version())).resolve()

def pbs_release() -> str:
    value = pstand_config().get("pbs", {}).get("release")
    if not value:
        raise RuntimeError("[tool.pstand.pbs].release is required for reproducible builds")
    return str(value)

def cython_config() -> dict:
    return pstand_config().get("cython", {})

def target_runtime_dir(target: Target) -> Path:
    if runtime_provider() == "local":
        return local_runtime_path(target)
    return RUNTIMES / target.key / python_version()

def staging_dir(target: Target) -> Path:
    return BUILD / "staging" / target.key

def wheel_dir(target: Target) -> Path:
    return BUILD / "wheels" / target.key

def pbs_sdk_dir(target: Target) -> Path:
    if runtime_provider() == "local":
        return local_sdk_path(target)
    return CACHE / "pbs-sdk" / target.key / python_version()

def python_executable() -> Path:
    """Return the preferred development Python without requiring a venv.

    Priority: explicit PYSTAND_PYTHON, app/.venv, the interpreter running
    this build tool, then python/python3 on PATH.
    """
    explicit = os.environ.get("PYSTAND_PYTHON")
    if explicit:
        return Path(explicit).expanduser().resolve()

    local = APP / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if local.exists():
        return local

    current = Path(sys.executable).resolve()
    if current.exists():
        return current

    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    raise RuntimeError("No development Python found. Set PYSTAND_PYTHON or install Python.")

def require_local_python() -> Path:
    """Return the configurable host/development Python.

    The host Python is only a build tool: Cython generates C with it and the
    target Python headers/runtime are used for the actual extension build. It
    therefore does *not* need to equal the bundled target Python version.
    This is important for legacy targets such as Python 3.8.x.
    """
    p = python_executable()
    if not p.exists():
        raise RuntimeError(f"Development Python not found: {p}. Set PYSTAND_PYTHON to a valid interpreter.")
    probe = __import__("subprocess").run(
        [str(p), "-c", "import sys; print(sys.version_info[0], sys.version_info[1], sys.executable)"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split()
    if len(probe) < 2:
        raise RuntimeError(f"Could not determine development Python version: {p}")
    major, minor = int(probe[0]), int(probe[1])
    if (major, minor) < (3, 8):
        raise RuntimeError(f"Development Python {major}.{minor} is too old; Python 3.8+ is required: {p}")
    return p
