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
