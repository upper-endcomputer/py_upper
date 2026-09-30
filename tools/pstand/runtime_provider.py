from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import Target, pbs_sdk_dir, runtime_provider, target_runtime_dir, python_version


def _version_from_runtime(root: Path) -> str:
    for candidate in (root / "PYTHON.json", root / "python" / "PYTHON.json"):
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                return str(data.get("python_version") or data.get("python") or "")
            except Exception:
                pass
    return ""


def _find_python(root: Path, target: Target) -> Path:
    candidates = []
    if target.os == "windows":
        candidates = [root / "python.exe", root / "install" / "python.exe"]
    else:
        candidates = [root / "bin" / "python3", root / "install" / "bin" / "python3"]
    for p in candidates:
        if p.exists():
            return p
    raise RuntimeError(f"Target Python executable not found under {root}")


def _find_include(root: Path) -> Path:
    for p in (root / "include", root / "Include", root / "install" / "include"):
        if p.exists():
            return p
    metadata = root / "PYTHON.json"
    if metadata.exists():
        data = json.loads(metadata.read_text(encoding="utf-8"))
        rel = data.get("python_paths", {}).get("include")
        if rel and (root / rel).exists():
            return root / rel
    raise RuntimeError(f"Python include directory not found under {root}")


def _local_info(target: Target) -> dict:
    root = target_runtime_dir(target)
    sdk = pbs_sdk_dir(target)
    if not root.exists():
        raise RuntimeError(f"Local runtime does not exist: {root}")
    if not sdk.exists():
        raise RuntimeError(f"Local runtime SDK does not exist: {sdk}")
    exe = _find_python(root, target)
    include = _find_include(sdk)
    version = _version_from_runtime(root) or _version_from_runtime(sdk)
    if not version:
        # Ask the target executable only for native/local targets. Cross targets
        # are expected to provide PYTHON.json so no foreign binary is executed.
        version = python_version()
    return {
        "provider": "local",
        "target": target.key,
        "root": str(root),
        "sdk_root": str(sdk),
        "python_executable": str(exe),
        "include_dir": str(include),
        "python": version,
        "python_major_minor": ".".join(version.split(".")[:2]) if version.count(".") >= 1 else version,
        "libpython_link_mode": None,
    }


def ensure_runtime(target: Target) -> Path:
    if runtime_provider() == "pbs":
        from .runtime import ensure_pbs_runtime
        return ensure_pbs_runtime(target)
    info = _local_info(target)
    return Path(info["root"])


def ensure_sdk(target: Target) -> Path:
    if runtime_provider() == "pbs":
        from .pbs_sdk import ensure_pbs_sdk
        return ensure_pbs_sdk(target)
    info = _local_info(target)
    return Path(info["sdk_root"])


def sdk_info(target: Target) -> dict:
    if runtime_provider() == "pbs":
        from .pbs_sdk import sdk_info
        return sdk_info(target)
    return _local_info(target)
