from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Target


@dataclass(frozen=True)
class Toolchain:
    target: Target
    env: dict[str, str]
    compiler: str
    cxx: str
    linker: str
    arch_flags: tuple[str, ...] = ()
    deployment_target: str | None = None


def _vswhere() -> Path | None:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
    ]
    for p in candidates:
        if p.exists():
            return p
    return Path(shutil.which("vswhere") or "") if shutil.which("vswhere") else None


def _vs_install() -> Path | None:
    v = _vswhere()
    if v and v.exists():
        p = subprocess.run([str(v), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], capture_output=True, text=True, check=True)
        value = p.stdout.strip().splitlines()
        if value:
            return Path(value[-1])
    for root in (
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft Visual Studio",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio",
    ):
        if root.exists():
            installs = sorted(root.glob("*/BuildTools")) + sorted(root.glob("*/Community")) + sorted(root.glob("*/Professional")) + sorted(root.glob("*/Enterprise"))
            if installs:
                return installs[-1]
    return None


def _windows_vcvars(target: Target) -> list[str]:
    if target.arch == "x86":
        return ["x86"]
    if target.arch == "x86_64":
        return ["amd64"]
    if target.arch == "arm64":
        # x64-hosted ARM64 cross compiler; this is the conventional vcvarsall target.
        return ["amd64_arm64"]
    raise RuntimeError(target.arch)


def _capture_bat_env(bat: Path, args: list[str]) -> dict[str, str]:
    command = f'call "{bat}" {" ".join(args)} >nul && set'
    p = subprocess.run(["cmd.exe", "/d", "/s", "/c", command], capture_output=True, text=True, check=True)
    env: dict[str, str] = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def resolve_toolchain(target: Target) -> Toolchain:
    host_system = platform.system()
    if target.os == "windows":
        if host_system != "Windows":
            raise RuntimeError("Windows targets require a Windows/MSVC build host in v0.7. Use CI or a Windows build machine for Windows artifacts.")
        install = _vs_install()
        if not install:
            raise RuntimeError("Visual Studio/Build Tools not found. Install Desktop development with C++ and the target ARM64/x64 build tools.")
        bat = install / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
        if not bat.exists():
            raise RuntimeError(f"vcvarsall.bat not found: {bat}")
        env = _capture_bat_env(bat, _windows_vcvars(target))
        compiler = env.get("CC", "cl")
        cxx = env.get("CXX", "cl")
        return Toolchain(target, env, compiler, cxx, "link.exe")

    if target.os == "macos":
        if host_system != "Darwin":
            raise RuntimeError("macOS targets require a macOS/Xcode build host in v0.7.")
        clang = shutil.which("clang")
        if not clang:
            raise RuntimeError("clang not found. Install Xcode Command Line Tools.")
        arch = "arm64" if target.arch == "arm64" else "x86_64"
        env = dict(os.environ)
        env["CC"] = clang
        env["CXX"] = shutil.which("clang++") or "clang++"
        env["ARCHFLAGS"] = f"-arch {arch}"
        env["CFLAGS"] = (env.get("CFLAGS", "") + f" -arch {arch}").strip()
        env["CXXFLAGS"] = (env.get("CXXFLAGS", "") + f" -arch {arch}").strip()
        env["LDFLAGS"] = (env.get("LDFLAGS", "") + f" -arch {arch}").strip()
        # Keep this configurable; PBS wheels/runtime determine the practical minimum.
        deployment = os.environ.get("MACOSX_DEPLOYMENT_TARGET", "11.0" if arch == "arm64" else "10.15")
        env["MACOSX_DEPLOYMENT_TARGET"] = deployment
        return Toolchain(target, env, clang, env["CXX"], clang, ("-arch", arch), deployment)


    if target.os == "linux":
        if host_system != "Linux":
            raise RuntimeError("Linux targets require a Linux build host.")
        if target.arch == "x86_64":
            cc, cxx = shutil.which("gcc"), shutil.which("g++")
        else:
            cc, cxx = shutil.which("aarch64-linux-gnu-gcc"), shutil.which("aarch64-linux-gnu-g++")
        if not cc or not cxx:
            raise RuntimeError("Linux compiler missing. linux-arm64 needs aarch64-linux-gnu-gcc/g++.")
        env = dict(os.environ)
        env["CC"], env["CXX"] = cc, cxx
        return Toolchain(target, env, cc, cxx, cc)
    raise RuntimeError(target.os)


def describe_toolchain(target: Target) -> dict[str, str]:
    tc = resolve_toolchain(target)
    return {
        "target": target.key,
        "compiler": tc.compiler,
        "cxx": tc.cxx,
        "linker": tc.linker,
        "deployment_target": tc.deployment_target or "",
        "vcvars_target": ("amd64_arm64" if target.arch == "arm64" else "amd64" if target.arch == "x86_64" else "x86") if target.os == "windows" else "",
    }
