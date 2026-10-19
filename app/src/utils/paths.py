from __future__ import annotations

import os
import sys
from pathlib import Path

# Exported by the packaged launcher: the directory that holds `runtime/`,
# `site-packages/` and the entry script. Counting parent directories only works
# for the layout of the day; the launcher knows the real answer.
BUNDLE_HOME_ENV = "PY_UPPER_HOME"

# Exported by the packaged launcher: the application name compiled in at
# packaging time. The launcher cannot read it from its own filename, because
# renaming the executable is supported.
APP_NAME_ENV = "PY_UPPER_APP_NAME"

_APP_NAME_SECTION = "tool.py_upper.app"
# Mirrors the packager's own fallback, for the case where neither a launcher nor
# a pyproject.toml is reachable.
_DEFAULT_APP_NAME = "Upper"


def bundle_home() -> Path | None:
    value = os.environ.get(BUNDLE_HOME_ENV)
    return Path(value).resolve() if value else None


def _module_file() -> Path | None:
    # A merged Cython bundle is registered as a built-in module. CPython gives
    # it the spec origin "built-in" as __file__, which is not a path and has no
    # parent directories to count.
    value = globals().get("__file__")
    if not value:
        return None
    try:
        path = Path(value).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return path if path.is_file() else None


def _project_root() -> Path | None:
    module = _module_file()
    if module is None:
        return None
    try:
        return module.parents[2]
    except IndexError:
        return None


def resource_root() -> Path:
    # In development: app/src/utils -> app/resources.
    # In a package: site-packages/utils -> bundled-app/resources.
    home = bundle_home()
    if home is not None:
        return home / "resources"
    project = _project_root()
    if project is not None:
        return project / "resources"
    # A merged bundle has no source path. The executable still lives in the
    # bundle root, which is the best fallback when the launcher export is lost.
    return Path(sys.executable).resolve().parent / "resources"


def app_name() -> str:
    """The user-facing application name.

    The launcher exports it, so a renamed executable still reports the
    configured name. Running from a source tree (tests, `PYTHONPATH=src python
    src/main.py`) there is no launcher, so the name is read from the same
    `[tool.py_upper.app].name` the packager uses instead of being written twice.
    """
    value = os.environ.get(APP_NAME_ENV, "").strip()
    if value:
        return value
    return _declared_app_name() or _DEFAULT_APP_NAME


def _declared_app_name() -> str | None:
    # app/src/utils -> app/pyproject.toml. Inside a package there is no source
    # path to count from; the launcher's export is the only source there. The
    # read stays hand-rolled because `tomllib` is Python 3.11+ and this
    # application supports 3.8.
    root = _project_root()
    if root is None:
        return None
    project = root / "pyproject.toml"
    if not project.is_file():
        return None
    section = None
    for line in project.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry.startswith("[") and entry.endswith("]"):
            section = entry[1:-1].strip()
        elif section == _APP_NAME_SECTION and "=" in entry:
            key, _, value = entry.partition("=")
            if key.strip() == "name":
                return _toml_string(value.strip())
    return None


def _toml_string(value: str) -> str | None:
    quote = value[:1]
    if quote not in {'"', "'"}:
        return None
    end = value.find(quote, 1)
    if end <= 0:
        return None
    return value[1:end] or None
