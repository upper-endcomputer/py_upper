from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote

from .config import CACHE, Target

PBS_RELEASE_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/{tag}"
PBS_RUNTIME_METADATA_URL = "https://raw.githubusercontent.com/astral-sh/uv/main/crates/uv-python/download-metadata.json"
_METADATA_CACHE = CACHE / "pbs" / "uv-download-metadata.json"


@dataclass(frozen=True)
class PBSAsset:
    name: str
    url: str
    sha256: str | None = None


@dataclass(frozen=True)
class PBSInputs:
    tag: str
    runtime: PBSAsset
    sdk: PBSAsset
    source: str


def metadata_key(target: Target, pyver: str) -> str:
    platform = {
        ("macos", "arm64"): "darwin-aarch64-none",
        ("macos", "x86_64"): "darwin-x86_64-none",
        ("windows", "x86"): "windows-i686-none",
        ("windows", "x86_64"): "windows-x86_64-none",
        ("windows", "arm64"): "windows-aarch64-none",
        ("linux", "x86_64"): "linux-x86_64-gnu",
        ("linux", "arm64"): "linux-aarch64-gnu",
    }[(target.os, target.arch)]
    return f"cpython-{pyver.strip()}-{platform}"


def asset_python_version(name: str) -> str | None:
    match = re.match(r"^cpython-(\d+\.\d+\.\d+)(?:\+|-)", str(name).strip())
    return match.group(1) if match else None


def _asset_from_release(asset: dict[str, Any]) -> PBSAsset:
    name = str(asset.get("name") or "").strip()
    url = str(asset.get("browser_download_url") or asset.get("url") or "").strip()
    return PBSAsset(name=name, url=url, sha256=str(asset.get("sha256") or "").strip() or None)


def is_matching_asset(name: str, target: Target, pyver: str, *, kind: str) -> bool:
    name = str(name).strip()
    if asset_python_version(name) != pyver.strip() or target.triple not in name:
        return False
    if "freethreaded" in name or "debug" in name:
        return False
    if kind == "runtime":
        return name.endswith("-install_only_stripped.tar.gz")
    if kind == "sdk":
        return name.endswith(("-pgo+lto-full.tar.zst", "-pgo-full.tar.zst", "-full.tar.zst"))
    raise ValueError(f"Unknown PBS asset kind: {kind}")


def matching_assets(release: dict[str, Any], target: Target, pyver: str, *, kind: str) -> list[dict[str, Any]]:
    return [
        asset for asset in (release.get("assets") or [])
        if isinstance(asset, dict) and is_matching_asset(asset.get("name") or "", target, pyver, kind=kind)
    ]


def available_python_versions(release: dict[str, Any], target: Target, *, kind: str) -> list[str]:
    values = {
        version
        for asset in (release.get("assets") or [])
        if isinstance(asset, dict)
        for version in [asset_python_version(str(asset.get("name") or ""))]
        if version and is_matching_asset(str(asset.get("name") or ""), target, version, kind=kind)
    }
    return sorted(values, key=lambda x: tuple(int(part) for part in x.split(".")))


def available_metadata_python_versions(data: dict[str, Any], target: Target) -> list[str]:
    suffix = metadata_key(target, "0.0.0").split("0.0.0-", 1)[1]
    prefix = "cpython-"
    values: set[str] = set()
    for key, value in data.items():
        if not isinstance(key, str) or not key.startswith(prefix) or not key.endswith("-" + suffix):
            continue
        version = key[len(prefix): -len(suffix) - 1]
        if re.fullmatch(r"\d+\.\d+\.\d+", version) and isinstance(value, dict) and value.get("url"):
            values.add(version)
    return sorted(values, key=lambda x: tuple(int(part) for part in x.split(".")))


def _load_metadata(fetch_json: Callable[[str], Any], *, force: bool = False) -> dict[str, Any]:
    if not force and _METADATA_CACHE.exists():
        try:
            value = json.loads(_METADATA_CACHE.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            pass
    value = fetch_json(PBS_RUNTIME_METADATA_URL)
    if not isinstance(value, dict):
        raise RuntimeError("Invalid PBS runtime metadata index")
    _METADATA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _METADATA_CACHE.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return value


def metadata_asset(data: dict[str, Any], target: Target, pyver: str) -> PBSAsset | None:
    entry = data.get(metadata_key(target, pyver))
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or "").strip()
    if not url:
        return None
    return PBSAsset(
        name=unquote(url.rsplit("/", 1)[-1]),
        url=url,
        sha256=str(entry.get("sha256") or "").strip() or None,
    )


def _best_sdk(assets: list[dict[str, Any]]) -> dict[str, Any]:
    return max(assets, key=lambda asset: ("pgo+lto-full" in str(asset.get("name") or ""), str(asset.get("name") or "")))


def no_asset_error(release_tag: str, target: Target, pyver: str, release: dict[str, Any], *, missing_runtime: bool, missing_sdk: bool) -> RuntimeError:
    missing = []
    if missing_runtime:
        missing.append("install_only_stripped runtime")
    if missing_sdk:
        missing.append("full SDK")
    return RuntimeError(
        f"No PBS {' / '.join(missing)} asset for Python {pyver} / {target.triple} in release {release_tag}.\n"
        f"Requested Python: {pyver}\n"
        f"PBS release: {release_tag}\n"
        f"Target: {target.triple}\n"
        f"Available runtime Python versions: {', '.join(available_python_versions(release, target, kind='runtime')) or 'none'}\n"
        f"Available SDK Python versions: {', '.join(available_python_versions(release, target, kind='sdk')) or 'none'}\n"
        "Set [tool.py_upper.pbs].release to a release containing the requested exact Python version; "
        "py_upper will not silently substitute another version."
    )


def resolve_pbs_inputs(fetch_json: Callable[[str], Any], configured_tag: str, target: Target, pyver: str) -> PBSInputs:
    """Resolve one exact PBS runtime + SDK pair.

    Automatic mode uses the uv-generated exact runtime metadata to determine the
    build tag, then requests only that tagged GitHub release to locate the matching
    full SDK. It never enumerates the GitHub /releases collection.
    """
    if configured_tag:
        release = fetch_json(PBS_RELEASE_API.format(tag=configured_tag))
        if not isinstance(release, dict):
            raise RuntimeError(f"Unexpected PBS release response for {configured_tag}")
        runtime_assets = matching_assets(release, target, pyver, kind="runtime")
        sdk_assets = matching_assets(release, target, pyver, kind="sdk")
        if not runtime_assets or not sdk_assets:
            raise no_asset_error(configured_tag, target, pyver, release, missing_runtime=not runtime_assets, missing_sdk=not sdk_assets)
        runtime = _asset_from_release(runtime_assets[0])
        sdk = _asset_from_release(_best_sdk(sdk_assets))
        return PBSInputs(configured_tag, runtime, sdk, "release")

    data = _load_metadata(fetch_json)
    runtime = metadata_asset(data, target, pyver)
    if runtime is None:
        available = available_metadata_python_versions(data, target)
        if available and pyver not in available:
            raise RuntimeError(
                f"No PBS runtime metadata for exact Python {pyver} / {target.triple}.\n"
                f"Requested Python: {pyver}\n"
                f"Target: {target.triple}\n"
                f"Available Python versions from PBS metadata for this target: {', '.join(available)}\n"
                "py_upper will not silently substitute another Python version."
            )
        data = _load_metadata(fetch_json, force=True)
        runtime = metadata_asset(data, target, pyver)
    if runtime is None:
        raise RuntimeError(f"No PBS runtime metadata for exact Python {pyver} / {target.triple}")

    match = re.search(r"/releases/download/([^/]+)/", runtime.url)
    tag = unquote(match.group(1)) if match else ""
    if not tag:
        raise RuntimeError(f"PBS runtime metadata has no release build tag: {runtime.url}")

    release = fetch_json(PBS_RELEASE_API.format(tag=tag))
    if not isinstance(release, dict):
        raise RuntimeError(f"Unexpected PBS release response for {tag}")
    sdk_assets = matching_assets(release, target, pyver, kind="sdk")
    if not sdk_assets:
        raise no_asset_error(tag, target, pyver, release, missing_runtime=False, missing_sdk=True)
    sdk = _asset_from_release(_best_sdk(sdk_assets))
    return PBSInputs(tag, runtime, sdk, "metadata")
