# 中文维护说明：通过统一 Qt 适配层创建应用并运行事件循环；复用现存实例时保持对象归属。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
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
    """通过统一 binding 创建或复用 QApplication；Qt5 DPI 属性在新实例创建前设置。

    Create or reuse the QApplication for the detected binding.

    The Qt names are imported here rather than at module level: importing this
    module must not require a binding, or every importer of the application
    entry point (tests, lint, Cython) needs Qt installed.
    """
    from utils.qt_compat import QtCore, QtWidgets, qt_major

    QApplication = QtWidgets.QApplication
    Qt = QtCore.Qt
    # High-DPI attributes are Qt5-era; Qt6 enables them by default. Qt5 reads
    # them while the application is constructed, so setting one on an existing
    # instance is a no-op: Qt logs "Attribute Qt::AA_EnableHighDpiScaling must be
    # set before QCoreApplication is created" and the scaling never applies.
    if qt_major() < 6:
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    return app


class MVVMQtApplication:
    """把 Qt 事件循环接到 MVVMApplication 上的适配器。

    子类实现 initialize_viewmodels() / initialize_views() 来搭具体的对象图；
    这个类只负责 Qt 应用、容器和两者的启动/退出顺序。
    """

    def __init__(self, argv: list[str] | None = None) -> None:
        """准备 Qt 应用并初始化容器和异步退出状态。"""
        self.qt_app = create_application(argv)
        self.mvvm_app: Optional[MVVMApplication] = None
        self.async_lifecycle = None
        self._async_stop = None

    def setup(self, model: Optional[ApplicationModel] = None) -> MVVMApplication:
        """创建 MVVM 容器；root model 由参数提供，缺省时建一个空的"""
        self.mvvm_app = MVVMApplication(model or ApplicationModel())
        return self.mvvm_app

    def initialize_viewmodels(self) -> None:
        """创建并注册 ViewModel（由子类实现）"""

    def initialize_views(self) -> None:
        """创建并注册 View（由子类实现）"""

    def prepare(self) -> MVVMApplication:
        """装配并启动；已运行的容器直接复用，避免重复创建窗口与订阅。"""
        application = self.mvvm_app or self.setup()
        if application.is_running:
            return application
        self.initialize_viewmodels()
        self.initialize_views()
        application.start()
        return application

    def run(self, *, headless: bool = False) -> int:
        """统一装配、事件处理与清理；headless 只处理一轮事件。"""
        from utils.qt_compat import exec_loop

        try:
            self.prepare()
            if headless:
                self.qt_app.processEvents()
                return 0
            return exec_loop(self.qt_app)
        finally:
            if self.mvvm_app is not None:
                self.mvvm_app.stop()

    def run_async(self, *, headless: bool = False) -> int:
        """启用可选 qasync 循环；关闭受管任务和服务后再释放 MVVM 容器。

        Opt-in qasync loop; add qasync to the application's dependencies."""
        import asyncio
        from qasync import QEventLoop
        from mvvm.async_lifecycle import AsyncLifecycle

        self.async_lifecycle = AsyncLifecycle()
        stop = asyncio.Event()
        self._async_stop = stop
        previous_quit = self.qt_app.quitOnLastWindowClosed()
        self.qt_app.setQuitOnLastWindowClosed(False)
        self.qt_app.lastWindowClosed.connect(stop.set)
        loop = QEventLoop(self.qt_app)

        async def main():
            """驱动异步装配与退出等待；无论正常退出或异常，都按顺序清理应用资源。"""
            try:
                self.prepare()
                if headless:
                    await asyncio.sleep(0)
                else:
                    await stop.wait()
            finally:
                try:
                    await self.async_lifecycle.aclose()
                finally:
                    if self.mvvm_app is not None:
                        self.mvvm_app.stop()
            return 0

        try:
            asyncio.set_event_loop(loop)
            with loop:
                return loop.run_until_complete(main())
        finally:
            self.qt_app.lastWindowClosed.disconnect(stop.set)
            self.qt_app.setQuitOnLastWindowClosed(previous_quit)
            self._async_stop = None
            asyncio.set_event_loop(None)

    def quit(self) -> None:
        """停止应用：停容器、退事件循环"""
        if self._async_stop is not None:
            self._async_stop.set()
            return
        if self.mvvm_app is not None:
            self.mvvm_app.stop()
        self.qt_app.quit()
