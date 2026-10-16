"""utils.qt_compat: one binding-neutral import surface, no binding at import time.

The shim exists so application code never names PySide6 or PyQt directly. That
only holds while nothing else imports a binding, so the last test here is an
architecture check over the whole application source tree.
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

from conftest import QT_BINDINGS, install_fake_binding

SHIM = "utils.qt_compat"


def _parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.fixture
def pyside(monkeypatch):
    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.Qt = object()
    qtcore.Signal = lambda *args, **kwargs: ("Signal", args, kwargs)
    qtcore.Slot = lambda *args, **kwargs: ("Slot", args, kwargs)
    members = {"Qt": qtcore.Qt, "Signal": qtcore.Signal, "Slot": qtcore.Slot}
    return install_fake_binding(monkeypatch, {"PySide6": {"QtCore": members}})


def test_proxy_attributes_resolve_against_the_detected_binding(pyside):
    assert pyside.qt_binding() == "PySide6"
    assert pyside.qt_major() == 6
    assert pyside.QtCore.Qt is sys.modules["PySide6.QtCore"].Qt
    assert repr(pyside.QtCore) == "<qt_compat QtCore via PySide6>"


def test_signal_and_slot_follow_the_binding_spelling(pyside):
    assert pyside.Signal(3) == ("Signal", (3,), {})
    assert pyside.Slot() == ("Slot", (), {})


def test_pyqt_factory_spelling_is_resolved_at_call_time(monkeypatch):
    """PyQt ships pyqtSignal/pyqtSlot and no Signal/Slot at all."""
    qtcompat = install_fake_binding(
        monkeypatch,
        {
            "PyQt6": {
                "QtCore": {
                    "pyqtSignal": lambda *a, **k: ("pyqtSignal", a, k),
                    "pyqtSlot": lambda *a, **k: ("pyqtSlot", a, k),
                }
            }
        },
    )
    assert qtcompat.qt_binding() == "PyQt6"
    assert qtcompat.Signal(1) == ("pyqtSignal", (1,), {})
    assert qtcompat.Slot() == ("pyqtSlot", (), {})


def test_a_mistyped_module_fails_at_the_import_that_names_it(pyside):
    with pytest.raises(ImportError, match="QtBogus"):
        pyside.QtBogus


def test_a_missing_attribute_is_an_import_error_not_a_none(pyside):
    with pytest.raises(ImportError, match="NoSuchThing"):
        pyside.QtCore.NoSuchThing


def test_non_qt_names_are_still_attribute_errors(pyside):
    with pytest.raises(AttributeError):
        pyside.not_a_qt_name


def test_exec_loop_uses_the_spelling_the_binding_ships(pyside):
    """Qt6 names the blocking loop ``exec``; the Qt5 wrappers only have ``exec_``."""

    class Qt6Application:
        def exec(self) -> str:
            return "qt6"

    class Qt5Application:
        def exec_(self) -> str:  # noqa: N802 - Qt spelling
            return "qt5"

    assert pyside.exec_loop(Qt6Application()) == "qt6"
    assert pyside.exec_loop(Qt5Application()) == "qt5"


def test_exec_loop_forwards_arguments_and_returns_the_exit_code(pyside):
    class Dialog:
        def exec(self, *args) -> tuple:
            return args, "code"

    assert pyside.exec_loop(Dialog(), 1, 2) == ((1, 2), "code")


def test_exec_loop_reports_an_object_with_no_loop_entry(pyside):
    with pytest.raises(AttributeError, match="neither exec nor exec_"):
        pyside.exec_loop(object())


def test_importing_the_shim_loads_no_binding(monkeypatch):
    for name in [n for n in sys.modules if n.startswith(QT_BINDINGS)]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    import importlib

    shim = importlib.import_module(SHIM)
    assert not [name for name in sys.modules if name.startswith(QT_BINDINGS)]
    assert shim.qt_binding.__module__ == SHIM


def test_only_the_shim_imports_a_qt_binding():
    """One entry point for Qt: a second direct import breaks the abstraction.

    Application modules reach Qt through utils.qt_compat so the binding stays
    swappable and the Qt pruning scanner has a single shape to read. The
    detection table inside the shim names the bindings as strings, which is why
    this looks at import statements only.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path.relative_to(src) == Path(*SHIM.split(".")).with_suffix(".py"):
            continue
        for node in ast.walk(_parsed(path)):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = [node.module.split(".", 1)[0]]
            else:
                continue
            offenders.extend(f"{path.relative_to(src)}: {root}" for root in roots if root in QT_BINDINGS)

    assert not offenders, f"import Qt from {SHIM}, not from a binding: {offenders}"


def test_only_the_shim_names_the_event_loop_entry():
    """``exec`` is Qt6, ``exec_`` is Qt5 -- one call site knows the difference.

    A PySide2 bundle built the window and then died with ``AttributeError:
    'PySide2.QtWidgets.QApplication' object has no attribute 'exec'`` because the
    bootstrap spelled the loop entry itself. Anything that blocks on a loop
    (``QApplication.exec``, ``QDialog.exec_``) goes through the shim's
    ``exec_loop``, or it binds the application to one Qt major.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    shim = src / Path(*SHIM.split(".")).with_suffix(".py")
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path == shim:
            continue
        for node in ast.walk(_parsed(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("exec", "exec_")
            ):
                offenders.append(f"{path.relative_to(src)}:{node.lineno}")

    assert not offenders, f"call the loop through {SHIM}.exec_loop: {offenders}"
