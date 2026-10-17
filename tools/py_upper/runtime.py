"""Target runtime: provider dispatch, PBS acquisition and release-safe optimization."""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import urllib.error
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from .config import (
    CACHE, Target, optimize_config, pbs_release, python_version, runtime_provider,
    runtime_spec, target_runtime_dir,
)
from .fs import copy_tree_contents, sha256
from .manifest import build_manifest, read_manifest, validate_manifest, write_manifest
from .net import download, http_json
from .pbs import PBSInputs, ensure_pbs_sdk, resolve_pbs_inputs, sdk_info as pbs_sdk_info, windows_embed_asset


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


def ensure_windows_embed_runtime(target: Target) -> Path:
    """Cache the unmodified python.org Windows embeddable ZIP as runtime.

    The ZIP contains a stdlib ZIP and flat .pyd files rather than Lib/DLLs.
    Keep its _pth file: the packaged launcher sets its paths explicitly, while
    invoking python.exe during a build needs isolation from host site-packages.
    """
    if target.os != "windows":
        raise ValueError(f"No Windows embeddable runtime for {target.key}")
    out = target_runtime_dir(target)
    asset = windows_embed_asset(target, python_version())
    marker = out / RUNTIME_MARKER
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if (data.get("format") == 2 and data.get("source") == "python.org"
                    and data.get("asset") == asset.name and data.get("target") == target.key
                    and data.get("python") == python_version()):
                return out
        except (OSError, ValueError):
            pass

    archive = CACHE / "python.org" / python_version() / target.key / asset.name
    try:
        download(asset.url, archive)
    except urllib.error.HTTPError as exc:
        # The pinned version has to exist as an official ZIP for every Windows
        # architecture; PBS covers more versions than python.org does.
        if exc.code == 404:
            raise RuntimeError(
                f"Python {python_version()} has no official embeddable ZIP for {target.key} "
                f"({asset.url}); pin a version python.org publishes an embeddable ZIP for"
            ) from exc
        raise
    actual = sha256(archive)
    required = {"python.exe", "python3.dll", "LICENSE.txt"}
    digits = "".join(python_version().split(".")[:2])
    required.update({f"python{digits}.dll", f"python{digits}.zip", f"python{digits}._pth"})
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        names = {entry.filename for entry in entries}
        if len(names) != len(entries):
            raise RuntimeError(f"Duplicate member in Python embeddable ZIP: {asset.name}")
        for entry in entries:
            name = entry.filename
            if (not name or name.endswith("/") or "/" in name or "\\" in name
                    or PureWindowsPath(name).drive or PurePosixPath(name).is_absolute()
                    or name in {".", ".."}):
                raise RuntimeError(f"Unsafe embed archive path: {name}")
        if not required <= names:
            raise RuntimeError(f"Incomplete Python embeddable ZIP {asset.name}: missing {sorted(required - names)}")
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        for entry in entries:
            with source.open(entry) as stream, (out / entry.filename).open("wb") as destination:
                shutil.copyfileobj(stream, destination)
    marker.write_text(json.dumps({
        "format": 2, "provider": "pbs", "source": "python.org", "asset": asset.name,
        "url": asset.url, "sha256": actual, "target": target.key, "python": python_version(),
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
        "runtime_root": str(spec.root),
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
        root = ensure_windows_embed_runtime(target) if target.os == "windows" else ensure_pbs_runtime(target)
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


def runtime_site_packages(root: Path) -> list[Path]:
    """``site-packages`` directories of a runtime tree, for either layout.

    A POSIX runtime nests them under ``lib/python3.X``; the Windows runtime has
    no version directory and keeps them under ``Lib``.
    """
    found = [path for path in sorted(root.glob("lib/python*/site-packages")) if path.is_dir()]
    windows = root / "Lib" / "site-packages"
    if windows.is_dir():
        found.append(windows)
    return found


def runtime_pip_payloads(root: Path) -> list[Path]:
    """``pip`` and ``ensurepip`` payloads inside a runtime tree.

    The cleanup below and the package check in ``verify`` share this function
    on purpose. A Windows runtime keeps its pip because the check only knew the
    POSIX layout is exactly the failure this prevents: the build removes the
    payload, the check looks somewhere else, and both report success.
    """
    found: list[Path] = []
    for site in runtime_site_packages(root):
        found.extend(sorted(site.glob("pip")))
        found.extend(sorted(site.glob("pip-*.dist-info")))
    found.extend(path for path in sorted(root.glob("lib/python*/ensurepip")) if path.is_dir())
    windows_ensurepip = root / "Lib" / "ensurepip"
    if windows_ensurepip.is_dir():
        found.append(windows_ensurepip)
    return found


def optimize_runtime_tree(root: Path, trim=None) -> dict[str, int]:
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
        for path in runtime_pip_payloads(root):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed_dirs += 1
            elif path.is_file() or path.is_symlink():
                path.unlink()
                removed_files += 1
    if trim is not None and trim.has_runtime_rules:
        from .trim import trim_tree

        report = trim_tree(root, trim.runtime, trim.runtime_keep)
        if report:
            print(report.describe("runtime"))
            removed_files += report.removed_files
            removed_dirs += report.removed_dirs
    return {"removed_files": removed_files, "removed_dirs": removed_dirs}
