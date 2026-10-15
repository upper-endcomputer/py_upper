"""主窗口：这个应用的参考 View —— 绑定、命令、跟子窗口的联动都在这里。"""
from __future__ import annotations

from typing import Optional

from mvvm.core import PropertyChangedEvent
from mvvm.view_models import MainViewModel
from mvvm.views.about_window import AboutWindow
from mvvm.views.base_view import ViewBase

ABOUT_BUTTON_TEXT = "关于 MVVM 架构"


class MainWindow(ViewBase):
    """主窗口：一句话 + 一个按钮，按钮走 CommandBinding → ViewModel 命令"""

    def __init__(self, viewmodel: MainViewModel, about_window: Optional[AboutWindow] = None) -> None:
        super().__init__(viewmodel)
        from utils.qt_compat import QtWidgets  # 只在真的要建窗口时加载 binding

        self._about_window = about_window
        window = QtWidgets.QMainWindow()
        window.setWindowTitle(viewmodel.title)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        label = QtWidgets.QLabel(viewmodel.message)
        button = QtWidgets.QPushButton(ABOUT_BUTTON_TEXT)
        layout.addWidget(label)
        layout.addWidget(button)
        window.setCentralWidget(central)
        window.resize(480, 240)
        self._window = window
        self._label = label
        self._about_button = button

        # label: 属性 → setter 方法；button: 命令 → clicked 信号
        self.bind_property("message", label, "setText")
        self.bind_command("show_about", button)
        self.viewmodel.subscribe_property_changed(self._on_viewmodel_property_changed)

    def _on_viewmodel_property_changed(self, event: PropertyChangedEvent) -> None:
        """about_visible 变化时开关「关于」窗口；ViewModel 不认识窗口"""
        if event.property_name != "about_visible" or self._about_window is None:
            return
        if event.new_value:
            self._about_window.show()
        else:
            self._about_window.close()

    def show(self) -> None:
        self._window.show()

    def close(self) -> None:
        self._window.close()
