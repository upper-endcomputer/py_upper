# 中文维护说明：复制、散列和退役构建目录；复制内容时避免继承来源目录的限制性权限。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
from pathlib import Path


EXECUTE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


# Windows refuses to rename or delete a directory tree while any handle inside
# it is open, and it reports that refusal as a bare "access denied" on the tree
# itself. The holders are ordinary and short-lived -- the packaged application
# the developer is still closing after an earlier ``--run``, an editor or
# indexer that resolved and executed ``dist/**/python``, an antivirus scan of a
# freshly written payload -- so a tree that cannot be retired yet is a retry,
# not a verdict. ``winerror`` is the only place that distinction lives: POSIX
# reports a genuinely undeletable tree with the same ``EACCES``.
_BUSY_WINERRORS = frozenset({5, 32, 33})  # access denied, sharing violation, lock violation
_BUSY_ATTEMPTS = 30
_BUSY_DELAY = 0.25


def _busy(exc: OSError) -> bool:
    """判断异常是否属于 Windows 文件占用错误；不把普通 POSIX 权限错误当成可重试占用。

Whether Windows refused a filesystem operation because another process holds the tree.
    """
    return getattr(exc, "winerror", None) in _BUSY_WINERRORS


def _housekeeping(step, *, path: Path) -> None:
    """执行文件操作；只对占用错误做有界重试，耗尽后抛出包含路径的 RuntimeError。

Run one filesystem step, waiting out a tree another process still holds.

    Failing outright is what a real permission problem deserves, but this
    failure is indistinguishable from it at the call site: Windows answers
    "access denied" whether the file is unreadable or merely open elsewhere.
    Retrying the short-lived holders covers the common case; the attempt count
    keeps a locked tree from stalling the build forever; and the message names
    the only things the user can actually act on.
    """
    for attempt in range(1, _BUSY_ATTEMPTS + 1):
        try:
            step()
            return
        except OSError as exc:
            if not _busy(exc):
                raise
            if attempt == _BUSY_ATTEMPTS:
                raise RuntimeError(
                    f"{path}: Windows still reports this tree in use after {_BUSY_ATTEMPTS} "
                    f"attempts (WinError {exc.winerror}). Another process holds it: close the "
                    "packaged application if it is still running from an earlier build, and "
                    "check for an editor, indexer, or antivirus scan that is reading or "
                    "executing the tree, then run the build again."
                ) from exc
            time.sleep(_BUSY_DELAY)


def copy_file_contents(source: Path, destination: Path, *, replace: bool = True) -> None:
    """复制文件内容并补齐可执行位；replace=False 时拒绝覆盖，不复制来源的限制性权限。"""
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
    """递归复制目录内容、符号链接和执行位；不重放来源元数据，冲突按 replace 策略处理。

Copy bytes, symlinks, and execute bits without replaying filesystem metadata.
    """
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


def copy_optional_tree(source: Path, destination: Path, *, replace: bool = True) -> None:
    """可选资源目录存在时复制；缺失时返回，适用于没有资源目录的应用。

Copy a tree when it exists; an absent source means the project ships no such payload.
    """
    if Path(source).is_dir():
        copy_tree_contents(source, destination, replace=replace)


def retire_tree(path: Path, *, trash: Path) -> None:
    """把目录移入退役位置再清理；平台占用错误交给有界重试处理。

Remove a directory tree that other processes may be using right now.

    Recursive deletion of a live tree races with every writer inside it: a
    writer recreates an entry between the directory scan and the final rmdir, so
    the removal fails with ENOENT on the entry it just deleted or ENOTEMPTY on
    the directory it cannot empty. Renaming the tree away first takes it out of
    the only name those writers know; the ones that lose the race write into a
    path that no longer exists and give up, and the recursive delete that
    follows sees a tree nothing else can reach. ``trash`` must be on the same
    filesystem as ``path`` for the rename to stay atomic.

    A writer that already holds an open handle takes the rename itself away
    instead of the delete that follows, which Windows reports exactly like a
    permissions problem. Those holders are transient, so each step waits for
    them out before the build gives up with a message about what is holding the
    tree (see ``_housekeeping``).
    """
    if not path.exists():
        return
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError(f"Not a directory tree: {path}")
    trash.mkdir(parents=True, exist_ok=True)
    retired = trash / f"{path.name}.{os.getpid()}"
    if retired.exists():
        _housekeeping(lambda: shutil.rmtree(retired), path=retired)
    _housekeeping(lambda: os.replace(path, retired), path=path)
    _housekeeping(lambda: shutil.rmtree(retired), path=path)


def make_executable(path: Path) -> None:
    """为现有权限添加执行位；不修改其他访问权限。

Add execute bits without replaying source metadata.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def sha256(path: Path) -> str:
    """按 1 MiB 分块计算文件 SHA-256，返回十六进制字符串；文件读取错误原样传播。

Content hash used by runtime/SDK/wheel/release integrity checks.
    """
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
