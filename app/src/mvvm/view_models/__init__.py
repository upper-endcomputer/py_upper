"""ViewModel Layer - 界面状态与命令，不依赖 Qt。

这里只放这个应用的 ViewModel；框架基类在 mvvm/core/，别混。
"""
from mvvm.view_models.main_vm import MainViewModel

__all__ = ['MainViewModel']
