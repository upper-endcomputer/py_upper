"""ViewModel Layer - 界面状态与命令，不 import Qt"""
from __future__ import annotations

from typing import Any, Optional

from mvvm.core import BaseViewModel, ObservableProperty, PropertyFactory, RelayCommand
from mvvm.models import AboutModel, ApplicationModel
from mvvm.services import GreetingService


class MainViewModel(BaseViewModel):
    """主窗口的状态：标题、问候语，以及「关于」窗口的显示开关与内容。

    「关于」窗口和主窗口共用这一个 ViewModel：about_visible 只有一个持有者，
    再给子窗口配一个 VM 就得让两边同步同一个布尔值。
    """

    def __init__(
        self,
        model: Optional[ApplicationModel] = None,
        greeting: Optional[GreetingService] = None,
        about: Optional[AboutModel] = None,
    ) -> None:
        # 属性在 super().__init__() 里初始化，_initialize_properties 要读这些字段，
        # 所以先存字段再调父类构造函数。
        self.model = model or ApplicationModel()
        self._greeting = greeting or GreetingService()
        self._about = about or AboutModel()
        super().__init__()

    def _initialize_properties(self) -> None:
        self.title_property = PropertyFactory.create(self.model.app_name, "title")
        self._register_property("title", self.title_property)

        self.message_property = PropertyFactory.create(self._greeting.message(), "message")
        self._register_property("message", self.message_property)

        self.about_title_property = PropertyFactory.create(self._about.title, "about_title")
        self._register_property("about_title", self.about_title_property)

        self.about_body_property = PropertyFactory.create(self._about.body, "about_body")
        self._register_property("about_body", self.about_body_property)

        self.about_visible_property = ObservableProperty(False)
        self._register_property("about_visible", self.about_visible_property)

        self.show_about_command = RelayCommand(execute=self.show_about)
        self.hide_about_command = RelayCommand(execute=self.hide_about)

    @property
    def title(self) -> str:
        return self.title_property.get_value()

    @property
    def message(self) -> str:
        return self.message_property.get_value()

    @property
    def about_title(self) -> str:
        return self.about_title_property.get_value()

    @property
    def about_body(self) -> str:
        return self.about_body_property.get_value()

    @property
    def about_visible(self) -> bool:
        return bool(self.about_visible_property.get_value())

    def show_about(self, parameter: Any = None) -> None:
        """打开「关于」窗口（视图监听 about_visible 变化，VM 不碰窗口）"""
        self.about_visible_property.set_value(True)

    def hide_about(self, parameter: Any = None) -> None:
        """关闭「关于」窗口"""
        self.about_visible_property.set_value(False)
