"""「关于」窗口：绑定 MainViewModel，自己不做任何业务判断。"""
from __future__ import annotations

from mvvm.view_models import MainViewModel
from mvvm.views.base_view import ViewBase

CLOSE_BUTTON_TEXT = "关闭"


class AboutWindow(ViewBase):
    """关于窗口：显示 ViewModel 里的 about_title/about_body"""

    def __init__(self, viewmodel: MainViewModel) -> None:
        super().__init__(viewmodel)
        from utils.qt_compat import QtWidgets  # 只在真的要建窗口时加载 binding

        dialog = QtWidgets.QDialog()
        dialog.setWindowTitle(viewmodel.about_title)
        layout = QtWidgets.QVBoxLayout(dialog)
        browser = QtWidgets.QTextBrowser()
        browser.setPlainText(viewmodel.about_body)
        close_button = QtWidgets.QPushButton(CLOSE_BUTTON_TEXT)
        layout.addWidget(browser)
        layout.addWidget(close_button)
        dialog.resize(560, 320)
        self._dialog = dialog
        self._browser = browser
        self._close_button = close_button

        self.bind_property("about_title", dialog, "setWindowTitle")
        self.bind_property("about_body", browser, "setPlainText")
        self.bind_command("hide_about", close_button)

    def show(self) -> None:
        """按 ViewModel 当前的 about_visible 显示窗口"""
        if self.viewmodel.about_visible:
            self._dialog.show()

    def close(self) -> None:
        self._dialog.close()
