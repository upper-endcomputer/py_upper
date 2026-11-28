# 中文维护说明：通过操作系统文件锁拒绝并发写入；退出后由系统释放锁，锁文件本身可以保留。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""OS-owned workspace lock; process exit releases it, including after a crash."""
from contextlib import contextmanager
from pathlib import Path
import os


# 锁由 OS 持有而非 PID 文本判断；进程崩溃后无需清理“陈旧 PID”才能恢复构建。
@contextmanager
def workspace_lock(root: Path):
    """持有工作区独占文件锁直到退出上下文；冲突抛出 RuntimeError，异常退出也释放句柄。"""
    directory = root / '.tmp'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'build.lock').open('a+b') as stream:
        stream.seek(0)
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another build is using this workspace') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
