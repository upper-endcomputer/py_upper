from __future__ import annotations

from pathlib import Path

import pytest

from core.app import run_application


def test_application(capsys):
    run_application()
    assert "py_upper" in capsys.readouterr().out


def test_resource_root_prefers_the_launcher_exported_home(monkeypatch, tmp_path):
    """The launcher knows the bundle root; counting parent directories guesses."""
    from utils import paths

    monkeypatch.delenv(paths.BUNDLE_HOME_ENV, raising=False)
    assert paths.bundle_home() is None
    assert paths.resource_root() == Path(paths.__file__).resolve().parents[2] / "resources"

    monkeypatch.setenv(paths.BUNDLE_HOME_ENV, str(tmp_path))
    assert paths.bundle_home() == tmp_path.resolve()
    assert paths.resource_root() == tmp_path.resolve() / "resources"


def test_app_name_agrees_with_the_packaging_configuration(monkeypatch):
    """The name is written once, in app/pyproject.toml, and read from there."""
    from py_upper.config import app_name as packaged_app_name
    from utils import paths

    monkeypatch.delenv(paths.APP_NAME_ENV, raising=False)
    assert paths.app_name() == packaged_app_name()

    # The launcher wins: a renamed executable still reports the configured name.
    monkeypatch.setenv(paths.APP_NAME_ENV, "Renamed Tool")
    assert paths.app_name() == "Renamed Tool"


def test_declared_app_name_ignores_the_project_section(tmp_path, monkeypatch):
    from utils import paths

    project = tmp_path / "app" / "pyproject.toml"
    project.parent.mkdir(parents=True)
    project.write_text(
        '[project]\nname = "not-the-app-name"\n\n[tool.py_upper.app]\nname = "Real App"  # inline comment\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "__file__", str(project.parent / "src" / "utils" / "paths.py"))

    assert paths._declared_app_name() == "Real App"


def test_gui_opens_a_window_headlessly(monkeypatch, capsys):
    """The Qt window is the real check that a bundled PySide6 payload works.

    The development interpreter may not have PySide6 installed, so this is
    skipped there; the packaged application exercises the same code path in the
    build's launcher smoke and in the CI integration run.
    """
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is only required for the packaged application")

    from core.app import run_gui

    monkeypatch.setenv("PY_UPPER_HEADLESS", "1")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)

    assert run_gui() == 0
    assert "GUI OK offscreen" in capsys.readouterr().out
