# 中文维护说明：定义 app/src/mvvm 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""MVVM framework for py_upper applications.

Ported from pcs_upper/mvvm. The core layer (ObservableProperty,
BaseViewModel, RelayCommand) is binding-agnostic; the views layer resolves Qt
classes through utils.qt_compat so it works with PySide6, PySide2, PyQt6 and
PyQt5 without per-binding forks.
"""

from mvvm.views.base_view import MVVMBinding, CommandBinding, ViewBase
from mvvm.mvvm_application import MVVMApplication

__all__ = ["MVVMBinding", "CommandBinding", "ViewBase", "MVVMApplication"]
