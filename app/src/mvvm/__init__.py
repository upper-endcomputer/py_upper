"""MVVM framework for py_upper applications.

Ported from pcs_upper/mvvm. The core layer (ObservableProperty,
BaseViewModel, RelayCommand) is binding-agnostic; the views layer resolves Qt
classes through utils.qt_compat so it works with PySide6, PySide2, PyQt6 and
PyQt5 without per-binding forks.
"""

from mvvm.views.base_view import MVVMBinding, CommandBinding, ViewBase
from mvvm.mvvm_application import MVVMApplication

__all__ = ["MVVMBinding", "CommandBinding", "ViewBase", "MVVMApplication"]
