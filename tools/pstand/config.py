from __future__ import annotations
import os
import platform
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

TARGETS = {
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

def python_version() -> str:
    return str(load_app_config()["tool"]["pstand"]["python"])

def pstand_config() -> dict:
    return load_app_config()["tool"].get("pstand", {})

def cython_config() -> dict:
    return pstand_config().get("cython", {})

def target_runtime_dir(target: Target) -> Path:
    return RUNTIMES / target.key / python_version()

def staging_dir(target: Target) -> Path:
    return BUILD / "staging" / target.key

def wheel_dir(target: Target) -> Path:
    return BUILD / "wheels" / target.key

def pbs_sdk_dir(target: Target) -> Path:
    return CACHE / "pbs-sdk" / target.key / python_version()

def python_executable() -> Path:
    return APP / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")

def require_local_python() -> Path:
    p = python_executable()
    if not p.exists():
        raise RuntimeError(f"Development venv not found: {p}. Create app/.venv first.")
    return p
