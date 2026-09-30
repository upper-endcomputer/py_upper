from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from .config import CACHE, Target, pbs_release, pbs_sdk_dir, python_version
from .fs import copy_tree_contents
from .net import download, http_json
from .pbs_assets import PBSInputs, resolve_pbs_inputs

MARKER = ".py_upper-sdk.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_zst(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    tar = shutil.which("tar")
    if not tar:
        raise RuntimeError("Extracting PBS SDK requires tar on PATH")
    result = subprocess.run([tar, "-axf", str(archive), "-C", str(destination)], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return
    zstd = shutil.which("zstd")
    if not zstd:
        raise RuntimeError(f"Unable to extract {archive.name}: tar could not read zstd archive and zstd is not installed")
    raw = archive.with_suffix("")
    subprocess.run([zstd, "-d", "-f", str(archive), "-o", str(raw)], check=True)
    try:
        subprocess.run([tar, "-xf", str(raw), "-C", str(destination)], check=True)
    finally:
        raw.unlink(missing_ok=True)


def _validate_layout(root: Path) -> tuple[Path, Path, dict]:
    python_root = root / "python"
    metadata_path = python_root / "PYTHON.json"
    if not metadata_path.exists():
        raise RuntimeError(f"PBS full SDK has no PYTHON.json: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    install_root = python_root / "install"
    if not install_root.is_dir():
        raise RuntimeError(f"PBS full SDK has no python/install directory: {install_root}")
    matches = sorted(path.parent for path in python_root.rglob("Python.h"))
    if not matches:
        raise RuntimeError(f"PBS full SDK contains no Python.h: {python_root}")
    return python_root, matches[0], metadata


def resolve_inputs(target: Target) -> PBSInputs:
    return resolve_pbs_inputs(http_json, pbs_release(), target, python_version())


def ensure_pbs_sdk(target: Target) -> Path:
    out = pbs_sdk_dir(target)
    marker_path = out / MARKER
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if (
                marker.get("format") == 2
                and marker.get("target") == target.key
                and marker.get("python") == python_version()
                and out.exists()
            ):
                return out
        except (OSError, ValueError):
            pass

    inputs = resolve_inputs(target)
    archive = CACHE / "pbs" / inputs.tag / target.key / inputs.sdk.name
    download(inputs.sdk.url, archive)
    actual = sha256(archive)
    if inputs.sdk.sha256 and actual.lower() != inputs.sdk.sha256.lower():
        raise RuntimeError(f"PBS SDK SHA256 mismatch: expected {inputs.sdk.sha256}, got {actual}")

    extract = CACHE / "pbs" / inputs.tag / target.key / "sdk-extract"
    if extract.exists():
        shutil.rmtree(extract)
    _safe_extract_zst(archive, extract)
    python_root, include_dir, metadata = _validate_layout(extract)
    if out.exists():
        shutil.rmtree(out)
    copy_tree_contents(python_root, out)
    rel_include = include_dir.relative_to(python_root).as_posix()
    marker = {
        "format": 2,
        "provider": "pbs",
        "tag": inputs.tag,
        "asset": inputs.sdk.name,
        "url": inputs.sdk.url,
        "sha256": actual,
        "target": target.key,
        "python": str(metadata.get("python_version") or python_version()),
        "include_dir": rel_include,
    }
    (out / MARKER).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def sdk_info(target: Target) -> dict:
    root = ensure_pbs_sdk(target)
    marker = json.loads((root / MARKER).read_text(encoding="utf-8"))
    include = root / str(marker.get("include_dir") or "")
    if not include.exists() or not (include / "Python.h").exists():
        matches = sorted(path.parent for path in root.rglob("Python.h"))
        if not matches:
            raise RuntimeError(f"Python.h not found in PBS SDK: {root}")
        include = matches[0]

    names = [f"python{python_version()}", f"python{'.'.join(python_version().split('.')[:2])}", "python3", "python"]
    executable = None
    for name in names:
        for candidate in (root / "install" / "bin" / name, root / "bin" / name):
            if candidate.is_file():
                executable = candidate
                break
        if executable:
            break
    if executable is None:
        raise RuntimeError(f"Target Python executable not found in PBS SDK: {root}")

    actual_python = str(marker.get("python") or python_version())
    return {
        "provider": "pbs",
        "root": str(root),
        "tag": marker.get("tag"),
        "asset": marker.get("asset"),
        "url": marker.get("url"),
        "sha256": marker.get("sha256"),
        "python": actual_python,
        "python_executable": str(executable),
        "include_dir": str(include),
        "python_major_minor": ".".join(actual_python.split(".")[:2]),
        "python_tag": "cp" + "".join(actual_python.split(".")[:2]),
        "python_platform_tag": target.primary_wheel_platform,
        "python_implementation_name": "cpython",
        "libpython_link_mode": "none" if target.os != "windows" else "windows-import-library",
    }
