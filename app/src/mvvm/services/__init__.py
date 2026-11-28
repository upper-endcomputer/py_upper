# 中文维护说明：定义 app/src/mvvm/services 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Service Layer - Injected capabilities (communication, repositories, backends).

Services are plain Python: they hold no Qt import and no ViewModel reference, so
the ViewModel that receives them stays testable without a window.
"""
from mvvm.services.greeting import GreetingService

__all__ = ['GreetingService']
