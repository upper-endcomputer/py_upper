from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


def copy_file_contents(source: Path, destination: Path) -> None:
    """Copy file bytes without replaying source filesystem metadata."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def copy_tree_contents(source: Path, destination: Path) -> None:
    """Recursively copy content while preserving symlink structure only."""
    source = Path(source)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(source):
        src = Path(entry.path)
        dst = destination / entry.name
        if entry.is_symlink():
            if dst.exists() or dst.is_symlink():
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            os.symlink(os.readlink(src), dst, target_is_directory=entry.is_dir())
        elif entry.is_dir(follow_symlinks=False):
            copy_tree_contents(src, dst)
        elif entry.is_file(follow_symlinks=False):
            copy_file_contents(src, dst)


def make_executable(path: Path) -> None:
    """Add execute bits without replaying any source file metadata."""
    mode = stat.S_IMODE(path.stat().st_mode)
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
