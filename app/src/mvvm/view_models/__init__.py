# 中文维护说明：定义 app/src/mvvm/view_models 包的导出边界；保留导出对象身份，避免在包初始化时引入额外运行副作用。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""ViewModel Layer - 界面状态与命令，不依赖 Qt。

这里只放这个应用的 ViewModel；框架基类在 mvvm/core/，别混。
"""
from mvvm.view_models.main_vm import MainViewModel

__all__ = ['MainViewModel']
