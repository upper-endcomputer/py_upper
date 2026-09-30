from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path

from .config import CACHE, Target, pbs_release, python_version, target_runtime_dir
from .fs import copy_tree_contents
from .net import download, http_json
from .pbs_assets import PBSInputs, resolve_pbs_inputs

MARKER = ".py_upper-runtime.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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
    marker = out / MARKER
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


def ensure_runtime(target: Target) -> Path:
    return ensure_pbs_runtime(target)
