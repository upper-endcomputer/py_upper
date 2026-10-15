import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "app" / "src"))

QT_BINDINGS = ("PySide6", "PyQt6", "PySide2", "PyQt5")


def install_fake_binding(monkeypatch, bindings: dict[str, dict[str, dict[str, object]]]):
    """Install fake Qt packages in sys.modules for one test.

    Every binding the caller did not fake is shadowed with ``None``: importing a
    module set to None raises ImportError, so detection lands on the fake one
    even on a machine that has a real binding installed. The shim's
    process-global cache is reset too, or the fake leaks into later tests.
    """
    from utils import qt_compat

    for name in QT_BINDINGS:
        if name not in bindings:
            monkeypatch.setitem(sys.modules, name, None)
    for name, submodules in bindings.items():
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
        for submodule, members in submodules.items():
            module = types.ModuleType(f"{name}.{submodule}")
            for member, value in members.items():
                setattr(module, member, value)
            monkeypatch.setitem(sys.modules, f"{name}.{submodule}", module)
    monkeypatch.setattr(qt_compat, "_binding", None)
    monkeypatch.setattr(qt_compat, "_modules", {})
    return qt_compat
