from __future__ import annotations

import os
import platform
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
    deployment_target: str | None = None


# MSVC host/target pairs as vcvarsall.bat spells them. The host half matters:
# "amd64" is only correct when the build host itself is x64, and an ARM64
# runner has ARM64 host tools rather than x64 ones.
VCVARSALL_HOST_TARGET = {
    ("x86", "x86"): "x86",
    ("x86", "x86_64"): "x86_amd64",
    ("x86", "arm64"): "x86_arm64",
    ("x86_64", "x86"): "amd64_x86",
    ("x86_64", "x86_64"): "amd64",
    ("x86_64", "arm64"): "amd64_arm64",
    ("arm64", "x86"): "arm64_x86",
    ("arm64", "x86_64"): "arm64_amd64",
    ("arm64", "arm64"): "arm64",
}

# The Visual Studio component that installs the host toolset above.
VC_HOST_COMPONENT = {
    "x86": "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
    "x86_64": "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
    "arm64": "Microsoft.VisualStudio.Component.VC.Tools.ARM64",
}


def _host_arch() -> str:
    """Normalize ``platform.machine()`` to the architecture names used here."""
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86"


def _vswhere() -> Path | None:
    for value in (
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe",
    ):
        if value.exists():
            return value
    return None


def _vs_install(host_arch: str) -> Path | None:
    vswhere = _vswhere()
    if vswhere:
        result = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires", VC_HOST_COMPONENT[host_arch], "-property", "installationPath"], capture_output=True, text=True, check=True)
        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if values:
            return Path(values[-1])
    return None


def resolve_toolchain(target: Target) -> Toolchain:
    host_os = platform.system()
    host_arch = _host_arch()
    if target.os == "windows":
        if host_os != "Windows":
            raise RuntimeError("Windows targets require a Windows/MSVC build host")
        install = _vs_install(host_arch)
        if not install:
            raise RuntimeError(
                "Visual Studio Build Tools with C++ are required; vswhere could not find "
                f"{VC_HOST_COMPONENT[host_arch]}"
            )
        bat = install / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
        if not bat.exists():
            raise RuntimeError(f"vcvarsall.bat not found: {bat}")
        vc_target = VCVARSALL_HOST_TARGET.get((host_arch, target.arch))
        if not vc_target:
            raise RuntimeError(f"Unsupported MSVC host/target pair: {host_arch} -> {target.arch}")
        command = f'call "{bat}" {vc_target} >nul && set'
        result = subprocess.run(["cmd.exe", "/d", "/s", "/c", command], capture_output=True, text=True, check=True)
        env = {k: v for line in result.stdout.splitlines() if "=" in line for k, v in [line.split("=", 1)]}
        return Toolchain(target, env, env.get("CC", "cl"), env.get("CXX", "cl"), "link.exe")

    if target.os == "macos":
        if host_os != "Darwin":
            raise RuntimeError("macOS targets require a macOS build host")
        clang = shutil.which("clang")
        clangxx = shutil.which("clang++")
        if not clang or not clangxx:
            raise RuntimeError("Xcode Command Line Tools with clang/clang++ are required")
        arch = target.arch
        env = dict(os.environ)
        env.update({"CC": clang, "CXX": clangxx, "ARCHFLAGS": f"-arch {arch}"})
        env["MACOSX_DEPLOYMENT_TARGET"] = os.environ.get("MACOSX_DEPLOYMENT_TARGET", "11.0" if arch == "arm64" else "10.15")
        for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            env[key] = (env.get(key, "") + f" -arch {arch}").strip()
        return Toolchain(target, env, clang, clangxx, clang, env["MACOSX_DEPLOYMENT_TARGET"])

    if target.os == "linux":
        if host_os != "Linux":
            raise RuntimeError("Linux targets require a Linux build host")
        native_arm = host_arch == "arm64"
        native_x64 = host_arch == "x86_64"
        if target.arch == "x86_64" and native_x64:
            cc, cxx = shutil.which("gcc"), shutil.which("g++")
        elif target.arch == "arm64" and native_arm:
            cc, cxx = shutil.which("gcc"), shutil.which("g++")
        elif target.arch == "x86_64":
            cc, cxx = shutil.which("x86_64-linux-gnu-gcc"), shutil.which("x86_64-linux-gnu-g++")
        else:
            cc, cxx = shutil.which("aarch64-linux-gnu-gcc"), shutil.which("aarch64-linux-gnu-g++")
        if not cc or not cxx:
            raise RuntimeError(f"Linux {target.arch} compiler is unavailable for host {host_arch}")
        env = dict(os.environ)
        env.update({"CC": cc, "CXX": cxx})
        return Toolchain(target, env, cc, cxx, cc)
    raise RuntimeError(f"Unsupported target: {target.key}")


def describe_toolchain(target: Target) -> dict[str, str]:
    tc = resolve_toolchain(target)
    return {
        "target": target.key,
        "compiler": tc.compiler,
        "cxx": tc.cxx,
        "linker": tc.linker,
        "deployment_target": tc.deployment_target or "",
    }
