"""Greeting service: an injected capability, not a global."""


class GreetingService:
    """主窗口显示的那句话的来源。

    ViewModel 不自己 new 依赖：装配处把一个实例注册到 MVVMApplication，
    ViewModel 通过构造参数拿到它——换成真后端时只改装配处。
    """

    def message(self) -> str:
        return "Hello from py_upper"
