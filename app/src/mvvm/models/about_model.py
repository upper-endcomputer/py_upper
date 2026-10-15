"""关于窗口的内容：属于 Model 层的数据，不含任何界面代码。"""

from mvvm.models.base_model import BaseModel

DEFAULT_TITLE = "关于 MVVM 架构"

# 本应用的分层职责。写在 model 里而不是视图里：这段文字是数据，
# 视图只负责把它显示出来。
LAYERS = (
    ("models/", "数据与业务规则，不 import Qt"),
    ("view_models/", "界面状态与命令：ObservableProperty 通知、RelayCommand 执行"),
    ("views/", "只做渲染与绑定，Qt 名字一律经 utils.qt_compat"),
    ("services/", "被注入的能力（通信、仓库、后端），由容器注册后取用"),
)


def default_body() -> str:
    """默认文案：本应用的分层，以及这个窗口是怎么被打开的"""
    layers = "\n".join(f"  {name}  {duty}" for name, duty in LAYERS)
    return (
        "app/src/mvvm 的分层：\n"
        f"{layers}\n"
        "\n"
        "这个窗口就是例子：主窗口按钮 → CommandBinding →\n"
        "MainViewModel.show_about_command → about_visible 属性变化 → 视图显示/关闭。\n"
    )


class AboutModel(BaseModel):
    """「关于」窗口的数据"""

    def __init__(self, title: str = DEFAULT_TITLE, body: str | None = None) -> None:
        super().__init__()
        self.title: str = title
        self.body: str = default_body() if body is None else body

    def to_dict(self) -> dict:
        return {
            'title': self.title,
            'body': self.body,
        }

    def from_dict(self, data: dict) -> None:
        self.title = data.get('title', DEFAULT_TITLE)
        self.body = data.get('body', default_body())
