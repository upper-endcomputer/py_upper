from __future__ import annotations

import pytest

from core.app import run_application


def test_application(capsys):
    run_application()
    assert "py_upper" in capsys.readouterr().out


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
