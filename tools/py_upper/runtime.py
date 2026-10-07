"""Target runtime: provider dispatch, PBS acquisition and release-safe optimization."""
from __future__ import annotations

import json
import os
import shutil
import tarfile
from pathlib import Path

from .config import (
    CACHE, Target, optimize_config, pbs_release, python_version, runtime_provider,
    runtime_spec, target_runtime_dir,
)
from .fs import copy_tree_contents, sha256
from .manifest import build_manifest, read_manifest, validate_manifest, write_manifest
from .net import download, http_json
from .pbs import PBSInputs, ensure_pbs_sdk, resolve_pbs_inputs, sdk_info as pbs_sdk_info


RUNTIME_MARKER = ".py_upper-runtime.json"


def safe_extract(tar_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if os.path.commonpath([str(root), str(target)]) != str(root):
                raise RuntimeError(f"Unsafe archive path: {member.name}")
        tf.extractall(dest)


def resolve_inputs(target: Target) -> PBSInputs:
    return resolve_pbs_inputs(http_json, pbs_release(), target, python_version())


def ensure_pbs_runtime(target: Target) -> Path:
    out = target_runtime_dir(target)
    marker = out / RUNTIME_MARKER
    if marker.exists():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            expected_tag = str(data.get("tag") or "")
            if (
                data.get("format") == 1
                and data.get("target") == target.key
                and data.get("python") == python_version()
                and (not pbs_release() or expected_tag == pbs_release())
                and out.exists()
            ):
                return out
        except (OSError, ValueError):
            pass

    inputs = resolve_inputs(target)
    archive = CACHE / "pbs" / inputs.tag / target.key / inputs.runtime.name
    download(inputs.runtime.url, archive)
    actual = sha256(archive)
    if inputs.runtime.sha256 and actual.lower() != inputs.runtime.sha256.lower():
        raise RuntimeError(f"PBS runtime SHA256 mismatch: expected {inputs.runtime.sha256}, got {actual}")
    extract = CACHE / "pbs" / inputs.tag / target.key / "runtime-extract"
    if extract.exists():
        shutil.rmtree(extract)
    safe_extract(archive, extract)
    source = extract / "python"
    if not source.exists():
        raise RuntimeError(f"Unexpected PBS runtime archive layout: {extract}")
    if out.exists():
        shutil.rmtree(out)
    copy_tree_contents(source, out)
    marker.write_text(json.dumps({
        "format": 1, "provider": "pbs", "tag": inputs.tag, "asset": inputs.runtime.name,
        "url": inputs.runtime.url, "sha256": actual, "target": target.key, "python": python_version(),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


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
        return ensure_pbs_sdk(target)
    _local_info(target)
    return runtime_spec(target).sdk_root


def ensure_runtime(target: Target) -> Path:
    spec = runtime_spec(target)
    if runtime_provider() == "pbs":
        root = ensure_pbs_runtime(target)
        data = build_manifest(spec, pbs_sdk_info(target))
        validate_manifest(data, target, spec)
        write_manifest(root, data)
        return root
    _local_info(target)
    return spec.root


def sdk_info(target: Target) -> dict:
    if runtime_provider() == "pbs":
        return pbs_sdk_info(target)
    return _local_info(target)


def optimize_runtime_tree(root: Path) -> dict[str, int]:
    """Apply release-safe cleanup to a copied runtime tree, never the source runtime."""
    cfg = optimize_config()
    removed_files = 0
    removed_dirs = 0
    remove_caches = bool(cfg.get("remove_python_caches", True))
    remove_pip = bool(cfg.get("remove_runtime_pip", True))
    if remove_caches:
        for path in sorted(root.rglob("__pycache__"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                shutil.rmtree(path)
                removed_dirs += 1
        for path in sorted(root.rglob("*.pyc")) + sorted(root.rglob("*.pyo")):
            if path.is_file():
                path.unlink()
                removed_files += 1
    if remove_pip:
        candidates = [
            root / "lib" / "python" / "site-packages" / "pip",
            root / "lib" / "python3" / "site-packages" / "pip",
        ]
        for lib_root in root.glob("lib/python*/site-packages"):
            candidates.append(lib_root / "pip")
            candidates.extend(lib_root.glob("pip-*.dist-info"))
        candidates.extend(root.glob("lib/python*/ensurepip"))
        for path in candidates:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed_dirs += 1
            elif path.is_file() or path.is_symlink():
                path.unlink()
                removed_files += 1
    return {"removed_files": removed_files, "removed_dirs": removed_dirs}
