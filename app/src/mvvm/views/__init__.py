"""View Layer - 框架层（ViewBase 与两种绑定）。

具体窗口（MainWindow/AboutWindow）各自成模块，Qt 名字在它们真正建窗口的
__init__ 里才 import（见开发指南 §9.4），所以 import 这个包、以及 import 具体
窗口模块本身都不需要 Qt；这个包只导出与 binding 无关的部分，装配处直接按模块
路径取具体窗口，框架层在没有 Qt 的环境里照样可导入、可 lint。
"""
from mvvm.views.base_view import MVVMBinding, CommandBinding, ViewBase

__all__ = ['MVVMBinding', 'CommandBinding', 'ViewBase']
