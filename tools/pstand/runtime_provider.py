from __future__ import annotations

import json
from pathlib import Path

from .config import RuntimeSpec, Target, pbs_sdk_dir, runtime_provider, runtime_spec, target_runtime_dir, python_version


def _metadata(root: Path) -> dict:
    for candidate in (root / "PYTHON.json", root / "python" / "PYTHON.json", root / "install" / "PYTHON.json"):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _version_from_runtime(root: Path) -> str:
    data = _metadata(root)
    return str(data.get("python_version") or data.get("python") or "")


def _find_python(root: Path, target: Target) -> Path:
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
    data = _metadata(root)
    rel = data.get("python_paths", {}).get("include")
    if rel and (root / rel).exists():
        return root / rel
    raise RuntimeError(f"Python include directory not found under {root}")


def _local_info(target: Target) -> dict:
    spec = runtime_spec(target)
    root, sdk = spec.root, spec.sdk_root
    if not root.exists():
        raise RuntimeError(f"Local runtime does not exist: {root}")
    if not sdk.exists():
        raise RuntimeError(f"Local runtime SDK does not exist: {sdk}")
    exe = _find_python(root, target)
    include = _find_include(sdk)
    version = _version_from_runtime(root) or _version_from_runtime(sdk) or spec.python
    if not version.startswith(spec.python):
        raise RuntimeError(f"Local runtime Python {version} does not match configured target Python {spec.python}")
    data = _metadata(root)
    triple = str(data.get("target_triple") or "")
    if triple and triple != target.triple:
        raise RuntimeError(f"Local runtime target {triple} does not match {target.triple}")
    return {
        "provider": "local",
        "target": target.key,
        "target_triple": target.triple,
        "root": str(root),
        "sdk_root": str(sdk),
        "python_executable": str(exe),
        "include_dir": str(include),
        "python": version,
        "python_major_minor": ".".join(version.split(".")[:2]),
        "libpython_link_mode": None,
        "metadata": data,
    }


def ensure_runtime(target: Target) -> Path:
    if runtime_provider() == "pbs":
        from .runtime import ensure_pbs_runtime
        return ensure_pbs_runtime(target)
    return Path(_local_info(target)["root"])


def ensure_sdk(target: Target) -> Path:
    if runtime_provider() == "pbs":
        from .pbs_sdk import ensure_pbs_sdk
        return ensure_pbs_sdk(target)
    return Path(_local_info(target)["sdk_root"])


def sdk_info(target: Target) -> dict:
    if runtime_provider() == "pbs":
        from .pbs_sdk import sdk_info
        return sdk_info(target)
    return _local_info(target)
