# 中文维护说明：在配置导入前确定应用根目录；支持命令行、环境变量和默认示例路径。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Application root selection for the build tool.

The build tool is deliberately independent of the bundled example application.
An application is a directory containing ``pyproject.toml`` and ``src/``; it
may live at ``<repo>/app`` (the default example), at the repository root, or
anywhere else on disk.

``--app`` is consumed before :mod:`py_upper.config` is imported because config
constants such as ``APP`` and ``APP_CONFIG`` are resolved at import time. This
module therefore has no dependency on the rest of py_upper.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR_ENV = "PY_UPPER_APP_DIR"


def resolve_app_dir(value: str) -> Path:
    """Resolve a user-supplied application directory against the caller's cwd."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def selected_app_dir(default: Path) -> Path:
    """Return the environment-selected app directory, or *default*."""
    value = os.environ.get(APP_DIR_ENV)
    return resolve_app_dir(value) if value else default


def select_from_argv(argv) -> str | None:
    """Apply ``--app DIR`` / ``--app=DIR`` from a command line, before imports.

    The option remains in *argv* for argparse to validate and document. A
    malformed value fails here with a short message instead of surfacing as a
    confusing configuration error later.
    """
    value = None
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument == "--app":
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                raise SystemExit("--app requires an application directory")
            value = argv[index + 1]
            index += 1
        elif argument.startswith("--app="):
            value = argument.split("=", 1)[1]
        index += 1
    if value is not None:
        if not value.strip():
            raise SystemExit("--app requires an application directory")
        os.environ[APP_DIR_ENV] = str(resolve_app_dir(value))
    return value
