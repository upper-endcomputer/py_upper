from __future__ import annotations

from pathlib import Path

from .config import Target, runtime_provider, runtime_spec
from .manifest import build_manifest, read_manifest, validate_manifest, write_manifest


def _local_info(target: Target) -> dict:
    spec = runtime_spec(target)
    if not spec.root.exists() or not spec.sdk_root.exists():
        raise RuntimeError(f"Local runtime/SDK missing: runtime={spec.root} sdk={spec.sdk_root}")
    manifest = read_manifest(spec.root)
    validate_manifest(manifest, target, spec)
    return {
        "provider": "local",
        "target": target.key,
        "target_triple": target.triple,
        "root": str(spec.root),
        "sdk_root": str(spec.sdk_root),
        "python_executable": str((spec.sdk_root / manifest["python_executable"]).resolve()),
        "include_dir": str((spec.sdk_root / manifest["include_dir"]).resolve()),
        "python": manifest["python"],
        "python_major_minor": manifest["python_major_minor"],
        "python_tag": manifest["python_abi"],
        "python_platform_tag": manifest["python_platform_tag"],
        "manifest": manifest,
    }


def ensure_sdk(target: Target) -> Path:
    if runtime_provider() == "pbs":
        from .pbs_sdk import ensure_pbs_sdk
        return ensure_pbs_sdk(target)
    _local_info(target)
    return runtime_spec(target).sdk_root


def ensure_runtime(target: Target) -> Path:
    spec = runtime_spec(target)
    if runtime_provider() == "pbs":
        from .pbs_sdk import sdk_info
        from .runtime import ensure_pbs_runtime
        root = ensure_pbs_runtime(target)
        data = build_manifest(spec, sdk_info(target))
        validate_manifest(data, target, spec)
        write_manifest(root, data)
        return root
    _local_info(target)
    return spec.root


def sdk_info(target: Target) -> dict:
    if runtime_provider() == "pbs":
        from .pbs_sdk import sdk_info as pbs_sdk_info
        return pbs_sdk_info(target)
    return _local_info(target)
