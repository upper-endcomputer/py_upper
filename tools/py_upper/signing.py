# 中文维护说明：按镜像类型签署 Mach-O 与嵌套 bundle；扩展入 egg 前签名，应用封签在最后。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Sign loadable Mach-O images, including extension images before egg packing."""
from __future__ import annotations

import shutil
import subprocess
from .native.inspect import inspect


def sign_macos_files(paths, identity):
    tool = shutil.which("codesign")
    if not tool:
        raise RuntimeError("macOS release requires codesign")
    for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not path.is_symlink() and path.suffix in {".framework", ".app", ".xpc"}:
            subprocess.run([tool, "--force", "--timestamp", "--options", "runtime",
                            "--sign", identity, str(path)], check=True)
            continue
        if not path.is_file() or path.is_symlink():
            continue
        try:
            info = inspect(path)
        except (OSError, ValueError):
            continue
        if info.loadable and info.format.startswith("Mach-O"):
            subprocess.run([tool, "--force", "--timestamp", "--options", "runtime",
                            "--sign", identity, str(path)], check=True)
