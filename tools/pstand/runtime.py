from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
from pathlib import Path

from .config import CACHE, Target, pbs_release, python_version, target_runtime_dir

RELEASE_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/{tag}"


def http_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "PyStand2/0.12.0", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def resolve_release() -> tuple[str, dict]:
    tag = pbs_release()
    return tag, http_json(RELEASE_API.format(tag=tag))


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
    req = urllib.request.Request(url, headers={"User-Agent": "PyStand2/0.12.0"})
    with urllib.request.urlopen(req) as r, dst.open("wb") as f:
        shutil.copyfileobj(r, f)


def safe_extract(tar_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if os.path.commonpath([str(root), str(target)]) != str(root):
                raise RuntimeError(f"Unsafe archive path: {member.name}")
        tf.extractall(dest)


def select_asset(release: dict, target: Target, pyver: str) -> dict:
    candidates = []
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if (
            name.startswith(f"cpython-{pyver}.")
            and target.triple in name
            and "install_only_stripped" in name
            and "freethreaded" not in name
            and name.endswith(".tar.gz")
        ):
            candidates.append(asset)
    if not candidates:
        raise RuntimeError(f"No PBS install_only_stripped asset for {pyver}/{target.triple}")
    return max(candidates, key=lambda a: a["name"])


def ensure_pbs_runtime(target: Target) -> Path:
    out = target_runtime_dir(target)
    marker = out / ".pystand2-runtime.json"
    if marker.exists():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if (data.get("format") == 5 and data.get("tag") == pbs_release()
                    and str(data.get("python")) == python_version()
                    and data.get("target") == target.key):
                return out
        except Exception:
            pass

    tag, release = resolve_release()
    asset = select_asset(release, target, python_version())
    archive = CACHE / "pbs" / tag / target.key / asset["name"]
    download(asset["browser_download_url"], archive)
    digest = asset.get("digest")
    actual = sha256(archive)
    if digest and digest.startswith("sha256:"):
        expected = digest.split(":", 1)[1]
        if actual.lower() != expected.lower():
            raise RuntimeError(f"SHA256 mismatch: expected {expected}, got {actual}")

    extract = CACHE / "pbs" / tag / target.key / "runtime-extract"
    if extract.exists():
        shutil.rmtree(extract)
    safe_extract(archive, extract)
    source = extract / "python"
    if not source.exists():
        raise RuntimeError(f"Unexpected PBS archive layout: {extract}")
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, out)
    marker.write_text(
        json.dumps(
            {
                "format": 5,
                "tag": tag,
                "asset": asset["name"],
                "sha256": actual,
                "target": target.key,
                "python": python_version(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return out


def ensure_runtime(target: Target) -> Path:
    """Backward-compatible PBS-only entrypoint."""
    return ensure_pbs_runtime(target)
