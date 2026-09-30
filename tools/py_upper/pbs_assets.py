from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .config import Target

VERSION_RE = re.compile(r"^cpython-(\d+\.\d+\.\d+)")
PBS_RELEASES_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases?per_page=50&page={page}"


def asset_python_version(name: str) -> str | None:
    match = VERSION_RE.match(name)
    return match.group(1) if match else None


def available_python_versions(
    release: dict,
    target: Target,
    *,
    kind: str,
) -> list[str]:
    """Return sorted CPython versions available for a target in a PBS release."""
    versions: set[str] = set()
    for asset in release.get("assets", []):
        name = str(asset.get("name") or "")
        version = asset_python_version(name)
        if not version or target.triple not in name or "freethreaded" in name:
            continue
        if kind == "runtime":
            wanted = "install_only_stripped" in name and name.endswith(".tar.gz")
        elif kind == "sdk":
            wanted = "full" in name and name.endswith(".tar.zst")
        else:
            raise ValueError(f"Unknown PBS asset kind: {kind}")
        if wanted:
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
    """Find the newest release in a supplied GitHub releases list with the exact asset."""
    for release in releases:
        tag = str(release.get("tag_name") or "")
        if not tag or tag == current_tag:
            continue
        for asset in release.get("assets", []) or []:
            name = str(asset.get("name") or "")
            if asset_python_version(name) != pyver or target.triple not in name or "freethreaded" in name:
                continue
            if kind == "runtime":
                if "install_only_stripped" in name and name.endswith(".tar.gz"):
                    return tag
            elif kind == "sdk":
                if "full" in name and name.endswith(".tar.zst"):
                    return tag
            else:
                raise ValueError(f"Unknown PBS asset kind: {kind}")
    return None


def find_matching_release(
    fetch_json: Callable[[str], Any],
    current_tag: str,
    target: Target,
    pyver: str,
    *,
    kind: str,
    max_pages: int = 4,
) -> str | None:
    """Best-effort lookup of a recent PBS release containing an exact asset."""
    for page in range(1, max_pages + 1):
        try:
            data = fetch_json(PBS_RELEASES_API.format(page=page))
        except Exception:
            return None
        if not isinstance(data, list):
            return None
        match = find_matching_release_from_candidates(
            data, current_tag, target, pyver, kind=kind
        )
        if match:
            return match
        if len(data) < 50:
            return None
    return None


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
        "Set [tool.py_upper.pbs].release to a release containing the requested exact Python version; "
        "py_upper will not silently substitute another version."
    )
    return RuntimeError("\n".join(lines))
