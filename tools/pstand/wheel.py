from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .config import APP, BUILD, Target, load_app_config, python_version, require_local_python
from .target_python import resolve_target_python


def _pip_args(target: Target) -> list[str]:
    pyver = python_version()
    if target.os == "windows":
        platform_tag = "win_amd64" if target.arch == "x86_64" else "win_arm64"
    elif target.os == "macos":
        # macOS wheels are versioned by minimum deployment target. 11.0 is the
        # baseline for arm64; x86_64 wheels using 10.9+ remain broadly usable.
        platform_tag = "macosx_11_0_arm64" if target.arch == "arm64" else "macosx_10_15_x86_64"
    elif target.os == "linux":
        platform_tag = "manylinux_2_17_aarch64" if target.arch == "arm64" else "manylinux_2_17_x86_64"
    else:
        raise RuntimeError(f"Unsupported wheel target: {target.key}")
    return [
        "--platform", platform_tag,
        "--python-version", pyver,
        "--implementation", "cp",
    ]


def dependency_specs() -> list[str]:
    return list(load_app_config()["project"].get("dependencies", []))


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(map(str, cmd)))
    subprocess.run(cmd, cwd=cwd, check=True)


def resolve_wheels(target: Target) -> Path:
    """Download only wheels compatible with the target interpreter/platform.

    pip's cross-target resolver is used instead of copying the host venv.
    Platform, Python version, and implementation are explicitly constrained;
    ABI is left to pip so compatible abi3 wheels remain eligible.
    """
    out = BUILD / "wheels" / target.key
    locked = __import__("os").environ.get("PYSTAND_LOCKED") == "1"
    if locked:
        if not out.exists():
            raise RuntimeError(f"Locked build requires existing wheel directory: {out}")
        wheels = sorted(out.glob("*.whl"))
        if not wheels:
            # No dependencies is a valid locked state.
            return out
        return out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    specs = dependency_specs()
    if not specs:
        (out / "manifest.json").write_text(json.dumps({"target": target.key, "wheels": []}, indent=2), encoding="utf-8")
        return out

    host_python = require_local_python()

    cmd = [str(host_python), "-m", "pip", "download", "--only-binary=:all:", "--no-cache-dir", "-d", str(out)]
    cmd += _pip_args(target)
    cmd += specs
    _run(cmd, cwd=APP)

    wheels = sorted(p.name for p in out.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"No compatible wheels resolved for {target.key}")
    (out / "manifest.json").write_text(
        json.dumps({"target": target.key, "python": python_version(), "wheels": wheels}, indent=2),
        encoding="utf-8",
    )
    print(f"Target wheels ready: {out}")
    return out


def install_wheels(target: Target, site: Path) -> Path:
    wheel_dir = resolve_wheels(target)
    wheels = sorted(wheel_dir.glob("*.whl"))
    if not wheels:
        return wheel_dir
    host_python = require_local_python()
    cmd = [str(host_python), "-m", "pip", "install", "--no-index", "--no-deps", "--target", str(site)]
    cmd += [str(w) for w in wheels]
    _run(cmd, cwd=APP)
    return wheel_dir
