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
