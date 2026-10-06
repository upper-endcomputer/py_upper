from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path


EXECUTE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def copy_file_contents(source: Path, destination: Path, *, replace: bool = True) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not replace:
        raise RuntimeError(f"File collision: {destination}")
    shutil.copyfile(source, destination)
    # Executable status is functional, not disposable metadata: a copied
    # interpreter or tool must stay runnable. Only execute bits are added;
    # restrictive source modes are never replayed, which is what made macOS
    # reject package-time chmod() calls.
    if source.stat().st_mode & EXECUTE_BITS:
        make_executable(destination)


def copy_tree_contents(source: Path, destination: Path, *, replace: bool = True) -> None:
    """Copy bytes, symlinks, and execute bits without replaying filesystem metadata."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_dir():
        raise RuntimeError(f"Copy source directory does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(source):
        src = Path(entry.path)
        dst = destination / entry.name
        if entry.is_symlink():
            if dst.exists() or dst.is_symlink():
                if not replace:
                    raise RuntimeError(f"File collision: {dst}")
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            os.symlink(os.readlink(src), dst, target_is_directory=entry.is_dir())
        elif entry.is_dir(follow_symlinks=False):
            if dst.exists() and not dst.is_dir():
                if not replace:
                    raise RuntimeError(f"File collision: {dst}")
                dst.unlink()
                dst.mkdir(parents=True, exist_ok=True)
            copy_tree_contents(src, dst, replace=replace)
        elif entry.is_file(follow_symlinks=False):
            copy_file_contents(src, dst, replace=replace)


def make_executable(path: Path) -> None:
    """Add execute bits without replaying source metadata."""
    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def sha256(path: Path) -> str:
    """Content hash used by runtime/SDK/wheel/release integrity checks."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
