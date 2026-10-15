"""Qt application bootstrap using the detected Qt binding.

Replaces the pcs_upper utils/qt fork: all Qt names resolve through
utils.qt_compat, so the same bootstrap runs on PySide6/PySide2/PyQt6/PyQt5.
"""
from __future__ import annotations
import sys
from typing import Optional

from mvvm.mvvm_application import MVVMApplication
from mvvm.models import ApplicationModel


def create_application(argv: list[str] | None = None):
    """Create or reuse the QApplication for the detected binding.

    The Qt names are imported here rather than at module level: importing this
    module must not require a binding, or every importer of the application
    entry point (tests, lint, Cython) needs Qt installed.
    """
    from utils.qt_compat import QtCore, QtWidgets, qt_major

    QApplication = QtWidgets.QApplication
    Qt = QtCore.Qt
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    # High-DPI attributes are Qt5-era; Qt6 enables them by default.
    if qt_major() < 6:
        app.setAttribute(Qt.AA_EnableHighDpiScaling)
    return app


class MVVMQtApplication:
    """把 Qt 事件循环接到 MVVMApplication 上的适配器。

    子类实现 initialize_viewmodels() / initialize_views() 来搭具体的对象图；
    这个类只负责 Qt 应用、容器和两者的启动/退出顺序。
    """

    def __init__(self, argv: list[str] | None = None) -> None:
        self.qt_app = create_application(argv)
        self.mvvm_app: Optional[MVVMApplication] = None

    def setup(self, model: Optional[ApplicationModel] = None) -> MVVMApplication:
        """创建 MVVM 容器；root model 由参数提供，缺省时建一个空的"""
        self.mvvm_app = MVVMApplication(model or ApplicationModel())
        return self.mvvm_app

    def initialize_viewmodels(self) -> None:
        """创建并注册 ViewModel（由子类实现）"""

    def initialize_views(self) -> None:
        """创建并注册 View（由子类实现）"""

    def prepare(self) -> MVVMApplication:
        """装配并启动：建容器、造 ViewModel/View、start()（start 会显示 View）"""
        application = self.mvvm_app or self.setup()
        self.initialize_viewmodels()
        self.initialize_views()
        application.start()
        return application

    def run(self) -> int:
        """prepare() 之后进入 Qt 事件循环"""
        self.prepare()
        return self.qt_app.exec()

    def quit(self) -> None:
        """停止应用：停容器、退事件循环"""
        if self.mvvm_app is not None:
            self.mvvm_app.stop()
        self.qt_app.quit()
