from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from .config import CACHE, Target, pbs_release, pbs_sdk_dir, python_version

RELEASE_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/{tag}"


def http_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "PyStand2/0.14.0", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    print(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "PyStand2/0.14.0"})
    with urllib.request.urlopen(req) as r, dst.open("wb") as f:
        shutil.copyfileobj(r, f)


def resolve_release() -> tuple[str, dict]:
    tag = pbs_release()
    return tag, http_json(RELEASE_API.format(tag=tag))


def _version_key(name: str) -> tuple[int, int, int, str]:
    m = re.search(r"cpython-(\d+)\.(\d+)\.(\d+)([^-]*)-", name)
    if not m:
        return (0, 0, 0, "")
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)


def select_full_asset(release: dict, target: Target, pyver: str) -> dict:
    candidates = []
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if not (
            name.startswith(f"cpython-{pyver}.")
            and target.triple in name
            and "freethreaded" not in name
            and "full" in name
            and name.endswith(".tar.zst")
        ):
            continue
        candidates.append(asset)
    if not candidates:
        raise RuntimeError(f"No PBS full SDK asset for {pyver}/{target.triple}")

    # Prefer the optimized full build. The archive metadata remains authoritative.
    def rank(asset: dict) -> tuple[int, tuple[int, int, int, str]]:
        n = asset["name"]
        optimization = 2 if "pgo+lto" in n else 1 if "pgo" in n else 0
        return optimization, _version_key(n)

    return max(candidates, key=rank)


def _extract_zst(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    # Prefer a native tar implementation that understands zstd. This works with
    # modern bsdtar/GNU tar installations. Fall back to zstd + tar.
    tar = shutil.which("tar")
    if tar:
        p = subprocess.run([tar, "-axf", str(archive), "-C", str(dest)], capture_output=True, text=True)
        if p.returncode == 0:
            return

    zstd = shutil.which("zstd")
    if not zstd or not tar:
        raise RuntimeError(
            "Extracting PBS full SDK requires a tar implementation with zstd support "
            "or both tar and zstd on PATH."
        )
    raw = archive.with_suffix("")
    subprocess.run([zstd, "-d", "-f", str(archive), "-o", str(raw)], check=True)
    try:
        subprocess.run([tar, "-xf", str(raw), "-C", str(dest)], check=True)
    finally:
        raw.unlink(missing_ok=True)


def _safe_member_path(root: Path, member: str) -> None:
    target = (root / member).resolve()
    if os.path.commonpath([str(root.resolve()), str(target)]) != str(root.resolve()):
        raise RuntimeError(f"Unsafe archive path: {member}")


def _validate_layout(root: Path) -> dict:
    python_root = root / "python"
    metadata = python_root / "PYTHON.json"
    if not metadata.exists():
        raise RuntimeError(f"PBS full archive has no PYTHON.json: {metadata}")
    data = json.loads(metadata.read_text(encoding="utf-8"))
    paths = data.get("python_paths", {})
    install_root = python_root / "install"
    include_rel = paths.get("include")
    stdlib_rel = paths.get("stdlib")
    if not include_rel:
        raise RuntimeError("PBS PYTHON.json has no python_paths.include")
    include = python_root / include_rel
    if not include.exists():
        # Some PBS metadata paths are relative to the install directory.
        include = install_root / include_rel
    if not include.exists():
        raise RuntimeError(f"PBS include directory not found: {include_rel}")
    if not install_root.exists():
        raise RuntimeError("PBS full archive has no python/install directory")
    return {
        "metadata": data,
        "python_root": python_root,
        "install_root": install_root,
        "include_dir": include,
        "stdlib_rel": stdlib_rel,
    }


def ensure_pbs_sdk(target: Target) -> Path:
    out = pbs_sdk_dir(target)
    marker = out / ".pystand2-sdk.json"
    if marker.exists():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if (data.get("tag") == pbs_release() and str(data.get("python_major_minor") or data.get("python")) .startswith(python_version())
                    and data.get("target") == target.key):
                return out
        except Exception:
            pass

    tag, release = resolve_release()
    asset = select_full_asset(release, target, python_version())
    archive = CACHE / "pbs" / tag / target.key / asset["name"]
    download(asset["browser_download_url"], archive)
    digest = asset.get("digest")
    actual = sha256(archive)
    if digest and digest.startswith("sha256:") and actual.lower() != digest.split(":", 1)[1].lower():
        raise RuntimeError(f"SHA256 mismatch: expected {digest}, got {actual}")

    extract = CACHE / "pbs" / tag / target.key / "sdk-extract"
    if extract.exists():
        shutil.rmtree(extract)
    _extract_zst(archive, extract)
    layout = _validate_layout(extract)

    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(extract / "python", out)

    metadata = layout["metadata"]
    resolved = {
        "tag": tag,
        "asset": asset["name"],
        "sha256": actual,
        "target": target.key,
        "python": metadata.get("python_version"),
        "python_major_minor": metadata.get("python_major_minor_version"),
        "python_paths": metadata.get("python_paths", {}),
        "libpython_link_mode": metadata.get("libpython_link_mode"),
    }
    marker.write_text(json.dumps(resolved, indent=2), encoding="utf-8")
    return out


def sdk_info(target: Target) -> dict:
    root = ensure_sdk(target)
    marker = root / ".pystand2-sdk.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["root"] = str(root)
    data["python_executable"] = str(target_python_executable(root, target))
    data["include_dir"] = str(target_include_dir(root))
    return data


def target_python_executable(root: Path, target: Target) -> Path:
    if target.os == "windows":
        return root / "install" / "python.exe"
    return root / "install" / "bin" / "python3"


def target_include_dir(root: Path) -> Path:
    # PYTHON.json is authoritative; do not guess the SDK layout.
    marker = root / "PYTHON.json"
    if marker.exists():
        data = json.loads(marker.read_text(encoding="utf-8"))
        rel = data.get("python_paths", {}).get("include")
        if rel:
            p = root / rel
            if p.exists():
                return p
    for p in (root / "include", root / "Include", root / "install" / "include"):
        if p.exists():
            return p
    raise RuntimeError(f"Python include directory not found under {root}")


def ensure_sdk(target: Target) -> Path:
    """Backward-compatible PBS-only entrypoint."""
    return ensure_pbs_sdk(target)
