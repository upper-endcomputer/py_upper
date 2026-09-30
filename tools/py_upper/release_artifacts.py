from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import DIST, Target, project_version


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_release_manifest(target: Target, output: Path | None = None) -> Path:
    root = output or DIST
    if not root.exists():
        raise RuntimeError(f"Release output does not exist: {root}")
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "release-manifest.json"):
        files.append({
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        })
    data = {
        "format": 1,
        "project": "py_upper",
        "version": project_version(),
        "target": target.key,
        "target_triple": target.triple,
        "files": files,
    }
    path = root / "release-manifest.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
