from __future__ import annotations

import re
from typing import Any, Callable

from .config import Target

VERSION_RE = re.compile(r"^cpython-(\d+\.\d+\.\d+)(?:\+|-)")
PBS_RELEASES_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=100&page={page}"
PBS_RELEASE_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/{tag}"
# Astral's uv publishes a generated index mapping exact managed-Python versions
# and targets to python-build-standalone release builds. We use it to avoid the
# very large paginated GitHub Releases response during automatic resolution.
PBS_RUNTIME_METADATA_URL = "https://raw.githubusercontent.com/astral-sh/uv/main/crates/uv-python/download-metadata.json"

_RELEASE_RESPONSE_CACHE: dict[tuple[int, str], dict[str, Any]] = {}




def _metadata_key(target: Target, pyver: str) -> str:
    platform = {
        ("macos", "arm64"): "darwin-aarch64-none",
        ("macos", "x86_64"): "darwin-x86_64-none",
        ("windows", "x86"): "windows-i686-none",
        ("windows", "x86_64"): "windows-x86_64-none",
        ("windows", "arm64"): "windows-aarch64-none",
        ("linux", "x86_64"): "linux-x86_64-gnu",
        ("linux", "arm64"): "linux-aarch64-gnu",
    }.get((target.os, target.arch))
    if not platform:
        raise RuntimeError(f"No PBS metadata mapping for target {target.key}")
    return f"cpython-{str(pyver).strip()}-{platform}"


def find_release_via_runtime_metadata(
    fetch_json: Callable[[str], Any], target: Target, pyver: str
) -> tuple[str, dict[str, Any]] | None:
    """Return the PBS release/build metadata for an exact runtime version.

    This uses Astral uv's generated download metadata as a lightweight index. The
    actual runtime and SDK assets are still selected from the PBS release itself.
    """
    data = fetch_json(PBS_RUNTIME_METADATA_URL)
    if not isinstance(data, dict):
        return None
    entry = data.get(_metadata_key(target, pyver))
    if not isinstance(entry, dict):
        return None
    build = str(entry.get("build") or "").strip()
    if not build:
        url = str(entry.get("url") or "")
        match = re.search(r"/releases/download/([^/]+)/", url)
        build = match.group(1) if match else ""
    if not build:
        return None
    return build, entry


def _release_from_tag(fetch_json: Callable[[str], Any], tag: str) -> dict[str, Any]:
    cache_key = (id(fetch_json), tag)
    if cache_key in _RELEASE_RESPONSE_CACHE:
        return _RELEASE_RESPONSE_CACHE[cache_key]
    release = fetch_json(PBS_RELEASE_API.format(tag=tag))
    if not isinstance(release, dict):
        raise RuntimeError(f"Unexpected PBS release API response for tag {tag}")
    _RELEASE_RESPONSE_CACHE[cache_key] = release
    return release

def asset_python_version(name: str) -> str | None:
    match = VERSION_RE.match(name)
    return match.group(1) if match else None


def _is_matching_asset(name: str, target: Target, pyver: str, *, kind: str) -> bool:
    name = str(name).strip()
    pyver = str(pyver).strip()
    if asset_python_version(name) != pyver or target.triple not in name or "freethreaded" in name:
        return False
    if kind == "runtime":
        return "install_only_stripped" in name and name.endswith(".tar.gz")
    if kind == "sdk":
        return "full" in name and name.endswith(".tar.zst")
    raise ValueError(f"Unknown PBS asset kind: {kind}")


def matching_assets(
    release: dict[str, Any], target: Target, pyver: str, *, kind: str
) -> list[dict[str, Any]]:
    """Return exactly the PBS assets accepted for a Python/target/kind tuple."""
    return [
        asset
        for asset in release.get("assets", []) or []
        if _is_matching_asset(str(asset.get("name") or ""), target, pyver, kind=kind)
    ]


def release_has_asset(release: dict[str, Any], target: Target, pyver: str, *, kind: str) -> bool:
    return bool(matching_assets(release, target, pyver, kind=kind))


def available_python_versions(release: dict[str, Any], target: Target, *, kind: str) -> list[str]:
    """Return sorted CPython versions available for a target in a PBS release."""
    versions: set[str] = set()
    for asset in release.get("assets", []) or []:
        name = str(asset.get("name") or "").strip()
        version = asset_python_version(name)
        if version and _is_matching_asset(name, target, version, kind=kind):
            versions.add(version)

    def key(value: str) -> tuple[int, int, int]:
        return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]

    return sorted(versions, key=key)


def find_matching_release_from_candidates(
    releases: list[dict[str, Any]],
    current_tag: str,
    target: Target,
    pyver: str,
    *,
    kind: str,
) -> str | None:
    """Find the newest release in a supplied GitHub releases list with an exact asset."""
    for release in releases:
        tag = str(release.get("tag_name") or "")
        if not tag or tag == current_tag:
            continue
        if release_has_asset(release, target, pyver, kind=kind):
            return tag
    return None


def find_matching_release(
    fetch_json: Callable[[str], Any],
    current_tag: str,
    target: Target,
    pyver: str,
    *,
    kind: str,
    max_pages: int = 20,
) -> str | None:
    """Best-effort lookup of a recent PBS release containing an exact asset."""
    for page in range(1, max_pages + 1):
        try:
            data = fetch_json(PBS_RELEASES_API.format(page=page))
        except Exception:
            return None
        if not isinstance(data, list):
            return None
        match = find_matching_release_from_candidates(data, current_tag, target, pyver, kind=kind)
        if match:
            return match
        if len(data) < 100:
            return None
    return None


def resolve_pbs_release(
    fetch_json: Callable[[str], Any],
    configured_tag: str,
    target: Target,
    pyver: str,
    *,
    max_pages: int = 20,
) -> tuple[str, dict[str, Any]]:
    """Resolve a PBS release containing both the exact runtime and full SDK assets.

    With an explicit configured release, only that release is accepted. Without
    one, search newest-to-oldest releases and select the newest release containing
    both required artifacts for the requested exact Python/target pair.
    """
    if configured_tag:
        return configured_tag, _release_from_tag(fetch_json, configured_tag)

    # Prefer the lightweight exact-version index. The GitHub Releases list endpoint
    # returns every asset for every release on a page and can be very large.
    metadata_match = find_release_via_runtime_metadata(fetch_json, target, pyver)
    if metadata_match:
        tag, _entry = metadata_match
        release = _release_from_tag(fetch_json, tag)
        if release_has_asset(release, target, pyver, kind="runtime") and release_has_asset(
            release, target, pyver, kind="sdk"
        ):
            return tag, release
        # Metadata identified the exact historical build but the release payload is
        # incomplete/unexpected. Fall through to the legacy exhaustive search.

    for page in range(1, max_pages + 1):
        data = fetch_json(PBS_RELEASES_API.format(page=page))
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected PBS releases API response on page {page}")
        for release in data:
            tag = str(release.get("tag_name") or "")
            if not tag:
                continue
            if release_has_asset(release, target, pyver, kind="runtime") and release_has_asset(
                release, target, pyver, kind="sdk"
            ):
                return tag, release
        if len(data) < 100:
            break
    raise RuntimeError(
        f"No PBS release contains both install_only_stripped runtime and full SDK "
        f"for Python {pyver} / {target.triple}."
    )


def no_asset_error(
    *,
    release_tag: str,
    target: Target,
    pyver: str,
    kind: str,
    release: dict,
    suggested_release: str | None = None,
) -> RuntimeError:
    label = "full SDK" if kind == "sdk" else "install_only_stripped runtime"
    available = available_python_versions(release, target, kind=kind)
    lines = [
        f"No PBS {label} asset for Python {pyver} / {target.triple} in release {release_tag}.",
        f"Requested Python: {pyver}",
        f"PBS release: {release_tag}",
        f"Target: {target.triple}",
    ]
    if available:
        lines.append("Available Python versions for this target in this release: " + ", ".join(available))
    else:
        lines.append(f"No {label} assets were found for {target.triple} in this release.")
    if suggested_release:
        lines.append(f"Suggested exact-match PBS release: {suggested_release}")

    lines.append(
        "Pin [tool.py_upper.pbs].release only when you want an explicit PBS release; "
        "otherwise omit that setting and py_upper will auto-select a release containing the exact Python version."
    )
    return RuntimeError("\n".join(lines))
