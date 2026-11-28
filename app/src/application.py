# 中文维护说明：组装示例的 Model、Service、ViewModel 与 View；应用入口负责创建和释放对象图。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""装配处与入口：model、service、ViewModel、View 都在这一个文件里接起来。

上面四个包彼此不 import 上层的具体类，只有这里同时认识它们——换掉其中一个
实现（后端、窗口、文案来源）只需要改这个文件。
"""
from __future__ import annotations

import os

from mvvm.models import AboutModel
from mvvm.mvvm_bootstrap import MVVMQtApplication
from mvvm.services import GreetingService
from mvvm.view_models import MainViewModel
from mvvm.views.about_window import AboutWindow
from mvvm.views.main_window import MainWindow
from utils.paths import app_name

# Set by the build tool's smoke step and by CI: build the real window, pump the
# event loop once and exit instead of blocking on a window server.
HEADLESS_ENV = "APPBUILD_HEADLESS"


def run_application() -> None:
    """输出示例服务的问候语，供命令行入口和基础回归验证。"""
    print(GreetingService().message())


class PyUpperApplication(MVVMQtApplication):
    """这个应用的装配：谁是谁的依赖，全在这里说清楚"""

    def initialize_viewmodels(self) -> None:
        """装配应用配置、业务模型与服务，并在容器内注册主 ViewModel。"""
        application = self.mvvm_app
        # 数据来自配置的读取只发生在装配处；model 只持有值。
        application.model.app_name = app_name()
        application.register_service("greeting", GreetingService())
        application.register_viewmodel(
            "main",
            MainViewModel(
                application.model,
                application.resolve_service(GreetingService),
                AboutModel(),
            ),
        )

    def initialize_views(self) -> None:
        """为同一个主 ViewModel 注册关于窗口和主窗口；关于窗口先注册以供主窗口引用。"""
        application = self.mvvm_app
        viewmodel = application.get_viewmodel("main")
        # 「关于」窗口由主窗口按 about_visible 开关，所以先注册它。
        application.register_view("about", AboutWindow(viewmodel))
        application.register_view("main", MainWindow(viewmodel, application.get_view("about")))


def run_gui() -> int:
    """创建真实 Qt 对象图并运行；headless 处理一轮事件后退出，不验证桌面首帧。

    Open the Qt main window.

    This is the real Qt check for a packaged application: it builds the whole
    MVVM object graph on top of the detected Qt binding, so a broken Qt payload
    (missing platform plugin, unresolved Qt framework, unusable binding runtime)
    fails here instead of silently shipping.
    """
    headless = os.environ.get(HEADLESS_ENV) == "1"
    if headless:
        # No window server in CI; the offscreen plugin still exercises the
        # whole Qt stack. An explicit QT_QPA_PLATFORM always wins.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    application = PyUpperApplication()
    result = application.run(headless=headless)
    if headless:
        print("GUI OK", application.qt_app.platformName())
    return result


def main() -> int:
    """运行命令行示例和 GUI，返回事件循环退出码。

    Packaged application entry point."""
    run_application()
    return run_gui()
