# 中文维护说明：延迟选择可用 Qt binding 并统一 Signal、Slot 与事件循环入口；不在导入适配模块时加载全部 Qt。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Qt binding abstraction: PySide6, PySide2, PyQt6, PyQt5 behind one import.

Applications import Qt classes from here:

    from utils.qt_compat import QtWidgets, QtCore, Signal

``QtWidgets`` and every other ``Qt<Module>`` name is a module-like proxy whose
attribute access resolves against the detected binding, so application code
reads the same as a direct PySide6 import while staying binding-neutral at
runtime. For IDE completion, install PySide6 in the development environment —
the ``TYPE_CHECKING`` block below re-exports the modules the application uses
and editors pick them up automatically.

The binding is detected on first attribute access and cannot change for the
life of the process, because loading two different Qt bindings in one
interpreter crashes immediately. Nothing here imports a binding on module
import, so the shim stays importable — and lintable, and Cython-compilable —
where no Qt is installed. Signal/Slot are re-exported here and resolve at call
time to the spelling the detected binding actually ships (Signal on PySide,
pyqtSignal on PyQt), so application code writes one name across all four. The
blocking event loop is the same kind of difference -- Qt6 binds it as ``exec``,
the Qt5 bindings only as ``exec_`` -- so ``exec_loop`` is the one place that
tells them apart.
"""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import TYPE_CHECKING, Any

# The names the TYPE_CHECKING block resolves for editors. Runtime lookup goes
# through __getattr__ and covers every Qt<Module>, binding or not, so this is
# the IDE surface rather than the set of importable names.
__all__ = ["QtCore", "QtGui", "QtWidgets", "Signal", "Slot", "exec_loop"]

if TYPE_CHECKING:  # IDE/static: editors resolve completion and types here.
    from PySide6 import QtCore as QtCore, QtGui as QtGui, QtWidgets as QtWidgets  # type: ignore # noqa

# The binding is process-global: importing a second one aborts. Cache so
# repeated callers get the same module set, not a new detection walk.
_binding: tuple[str, str] | None = None
_modules: dict[str, ModuleType] = {}

# Load order: newer bindings first. PySide exposes its own version strings;
# PyQt uses PYQT_VERSION_STR. The signal/slot spelling differs per family and is
# probed from the detected module instead of being tabulated here.
_CANDIDATES = (
    ("PySide6", "6"),
    ("PyQt6", "6"),
    ("PySide2", "5"),
    ("PyQt5", "5"),
)


def qt_binding() -> str:
    """返回首次探测选定的 Qt binding 包名，并缓存到进程结束。

    Return the installed binding's top-level package name."""
    return _load_binding()[0]


def qt_major() -> int:
    """返回当前 binding 的 Qt 主版本。"""
    return int(_load_binding()[1])


def _load_binding() -> tuple[str, str]:
    """按候选顺序选择唯一 binding；全部不可导入时抛出 ImportError。"""
    global _binding
    if _binding is not None:
        return _binding
    for name, major in _CANDIDATES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        _modules[name] = module
        _binding = (name, major)
        return _binding
    raise ImportError(
        "No Qt binding found; install one of PySide6, PyQt6, PySide2, PyQt5 "
        "(development: pip install PySide6)"
    )


def _binding_module(submodule: str) -> ModuleType:
    """按需导入并缓存选定 binding 的子模块；缺失时附加明确诊断。"""
    binding, _ = _load_binding()
    key = f"{binding}.{submodule}"
    if key not in _modules:
        try:
            _modules[key] = importlib.import_module(key)
        except ImportError as exc:
            raise ImportError(f"{binding} has no submodule {submodule!r}: {exc}") from exc
    return _modules[key]


class _QtModule:
    """Module proxy: attribute access delegates to the detected binding.

    A missing attribute raises the same ImportError flavor the direct import
    would, so version differences fail loudly instead of returning None.
    """

    __slots__ = ("_submodule",)

    def __init__(self, submodule: str) -> None:
        """保存子模块名并立即验证可导入性，使拼写错误在导入处暴露。"""
        self._submodule = submodule
        # Resolve the probe now rather than at first attribute access: a
        # mistyped module then fails at the import that names it, exactly where
        # a direct binding import would.
        _binding_module(submodule)

    def __getattr__(self, name: str) -> Any:
        """从真实 Qt 模块读取属性；缺少属性转换为带 binding 信息的 ImportError。"""
        module = _binding_module(self._submodule)
        try:
            return getattr(module, name)
        except AttributeError as exc:
            raise ImportError(
                f"{module.__name__} has no {name!r} "
                f"(binding {qt_binding()}, Qt {qt_major()})"
            ) from exc

    def __dir__(self) -> list[str]:
        """返回真实模块成员供补全；binding 不可用时返回空列表。"""
        try:
            return dir(_binding_module(self._submodule))
        except ImportError:
            return []

    def __repr__(self) -> str:
        """显示代理子模块及已解析 binding，便于调试延迟导入。"""
        return f"<qt_compat {self._submodule} via {_binding[0] if _binding else 'unresolved'}>"


def __getattr__(name: str) -> Any:
    """将 Qt 开头的属性解析为子模块代理；其他名称保留普通 AttributeError。

    Forward any ``Qt*`` name to the detected binding as a submodule.

    Submodules are resolved by name instead of being listed, so
    ``from utils.qt_compat import QtNetwork`` works exactly like
    ``from PySide6 import QtNetwork`` with no list to keep in step with the
    bindings. The name is checked against the binding while the proxy is built,
    so a typo fails at the import statement instead of at the first attribute
    it reaches.
    """
    if name.startswith("Qt"):
        return _QtModule(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _qt_factory(*names: str) -> Any:
    """探测 QtCore 实际提供的工厂名称，统一 PySide 和 PyQt 的命名差异。

    The first of ``names`` the detected binding's QtCore actually provides.

    The two binding families spell the signal and slot factories differently
    and neither ships the other's spelling: PySide has ``Signal``/``Slot``, PyQt
    has ``pyqtSignal``/``pyqtSlot``. Asking the module which one it has keeps
    the shim from encoding a family table a future binding could falsify.
    """
    module = _binding_module("QtCore")
    for name in names:
        factory = getattr(module, name, None)
        if factory is not None:
            return factory
    raise ImportError(f"{qt_binding()}.QtCore provides none of {names}")


def Signal(*args: Any, **kwargs: Any) -> Any:
    """调用当前 binding 的 Signal 工厂，参数原样转交。"""
    return _qt_factory("Signal", "pyqtSignal")(*args, **kwargs)


def Slot(*args: Any, **kwargs: Any) -> Any:
    """调用当前 binding 的 Slot 装饰器工厂，参数原样转交。"""
    return _qt_factory("Slot", "pyqtSlot")(*args, **kwargs)


# The names the two Qt majors use for a blocking event loop entry. Qt6 spells it
# exec; the Qt5 wrappers were generated while exec was still a statement and only
# publish exec_. One binding ships one of the two, so probing the object is the
# whole detection -- no version table to falsify.
_EVENT_LOOP_ENTRIES = ("exec", "exec_")


def exec_loop(target: Any, *args: Any, **kwargs: Any) -> Any:
    """调用对象实际提供的阻塞事件循环入口，并原样返回退出结果。

    Run ``target``'s blocking event loop under the name it actually ships.

    Application code that writes either spelling is bound to one Qt major: a
    PySide2 bundle builds its window and then dies with ``AttributeError:
    'QApplication' object has no attribute 'exec'`` before anything is shown.
    This is the same capability probe ``Signal``/``Slot`` use, applied to the
    call that never returns until the application is done.

    ``target`` is a ``QApplication``, a ``QDialog`` or anything else with a
    blocking loop; a missing entry reports the binding it was probed against.
    """
    for name in _EVENT_LOOP_ENTRIES:
        method = getattr(target, name, None)
        if method is not None:
            return method(*args, **kwargs)
    raise AttributeError(
        f"{type(target).__name__} has neither exec nor exec_ "
        f"(binding {qt_binding()}, Qt {qt_major()})"
    )
