# 中文维护说明：定义 app/src/mvvm/core 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""导出可观察属性、命令和 ViewModel 基类；对象装配由应用入口负责。"""
from mvvm.core.observable import ObservableProperty, PropertyChangedEvent
from mvvm.core.command import RelayCommand, AsyncRelayCommand
from mvvm.core.base_view_model import BaseViewModel

__all__ = [
    'ObservableProperty', 'PropertyChangedEvent',
    'RelayCommand', 'AsyncRelayCommand', 'BaseViewModel',
]
