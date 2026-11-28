# 中文维护说明：适配宿主 Python 的标准库差异；宿主版本与被打包的目标 Python 版本独立。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Compatibility helpers for the py_upper build tool.

The build tool supports Python 3.8+ independently of the Python runtime that
will eventually be bundled into the application.
"""
from __future__ import annotations

try:  # Python 3.11+
    import tomllib as tomllib
except ModuleNotFoundError:  # Python 3.8-3.10
    from ._vendor import tomli as tomllib

__all__ = ["tomllib"]
