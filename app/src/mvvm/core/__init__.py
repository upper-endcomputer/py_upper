# 中文维护说明：定义 app/src/mvvm/core 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""MVVM Core Framework.

The factories are re-exported from mvvm.core.factory, which is their only
home: one name, one implementation, so ``PropertyFactory`` and
``ViewModelFactory`` resolve to the same objects whether a caller imports them
from this package or from the defining module. Object-graph singletons belong
to ``mvvm.mvvm_application.ServiceLocator``, not to a second factory.
"""
from mvvm.core.observable import ObservableProperty, PropertyChangedEvent
from mvvm.core.command import RelayCommand, AsyncRelayCommand
from mvvm.core.base_view_model import BaseViewModel
from mvvm.core.factory import PropertyFactory, ViewModelFactory

__all__ = [
    'ObservableProperty', 'PropertyChangedEvent', 'PropertyFactory',
    'RelayCommand', 'AsyncRelayCommand', 'BaseViewModel', 'ViewModelFactory',
]
