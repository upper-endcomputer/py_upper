from __future__ import annotations

import shutil
from pathlib import Path

from .config import optimize_config


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
