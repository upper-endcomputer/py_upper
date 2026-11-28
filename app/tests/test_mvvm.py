# 中文维护说明：验证属性、命令和容器的生命周期；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""MVVM framework port: core, views, container, binding-neutral imports."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conftest import install_fake_binding

SRC = Path(__file__).resolve().parents[1] / "src"


def test_mvvm_core_imports_without_qt():
    """验证不加载 Qt 时核心属性读写与变更通知仍可工作。"""
    import mvvm.core
    import mvvm.models

    for name in ("BaseViewModel", "ObservableProperty", "PropertyChangedEvent",
                 "RelayCommand", "AsyncRelayCommand", "PropertyFactory", "ViewModelFactory"):
        assert hasattr(mvvm.core, name), f"mvvm.core lost {name}"
    for name in ("BaseModel", "ApplicationModel"):
        assert hasattr(mvvm.models, name), f"mvvm.models lost {name}"

    class Demo(mvvm.core.BaseViewModel):
        pass

    vm = Demo()
    prop = mvvm.core.ObservableProperty(42)
    vm._register_property("value", prop)
    assert vm.get_property_value("value") == 42
    vm.set_property_value("value", 7)
    assert vm.get_property_value("value") == 7
    events = []
    vm.subscribe_property_changed(lambda e: events.append(e.property_name))
    vm.set_property_value("value", 8)
    assert "value" in events


def test_mvvm_application_container():
    """验证容器注册、解析、启动和停止的完整对象生命周期。"""
    from mvvm.core import BaseViewModel
    from mvvm.mvvm_application import MVVMApplication
    from mvvm.models import ApplicationModel
    from mvvm.views import ViewBase

    model = ApplicationModel()
    app = MVVMApplication(model)
    assert app.model is model
    assert app.is_running is False

    service = object()
    app.register_service("demo", service)
    assert app.get_service("demo") is service
    assert app.resolve_service(object) is service
    assert app.resolve_service(str) is None

    viewmodel = BaseViewModel()
    view = ViewBase(viewmodel)
    app.register_viewmodel("main", viewmodel)
    app.register_view("main", view)
    assert app.get_viewmodel("main") is viewmodel
    assert app.get_view("main") is view

    app.start()
    assert app.is_running and model.is_running
    assert repr(app) == "MVVMApplication(viewmodels=1, views=1)"

    app.stop()
    assert not app.is_running and not model.is_running
    assert app.get_viewmodel("main") is None
    assert app.get_view("main") is None


def test_services_belong_to_the_application_that_registered_them():
    """验证多个应用容器之间的服务注册互不泄漏。

    A class-level registry hands one application another application's services.

    The container exists to keep dependencies explicit; a process-global
    locator defeats that the moment two MVVMApplication objects exist (tests,
    an embedder, a second window set).
    """
    from mvvm.mvvm_application import MVVMApplication
    from mvvm.models import ApplicationModel

    first = MVVMApplication(ApplicationModel())
    second = MVVMApplication(ApplicationModel())
    first.register_service("demo", object())

    assert first.get_service("demo") is not None
    assert second.get_service("demo") is None


def test_disposing_one_view_preserves_the_shared_viewmodel():
    """验证释放一个视图不会清空共享 ViewModel 或其他视图的绑定。"""
    from types import SimpleNamespace
    from mvvm.core import BaseViewModel, ObservableProperty
    from mvvm.views import ViewBase

    vm = BaseViewModel()
    vm._register_property("value", ObservableProperty(0))
    first, second = ViewBase(vm), ViewBase(vm)
    first_widget, second_widget = SimpleNamespace(value=0), SimpleNamespace(value=0)
    first.bind_property("value", first_widget)
    second.bind_property("value", second_widget)
    events = []
    vm.subscribe_property_changed(lambda event: events.append(event.new_value))

    first.dispose()
    assert vm.set_property_value("value", 7)
    assert first_widget.value == 0
    assert second_widget.value == 7
    assert events == [7]


def test_container_disposes_shared_viewmodel_once_after_all_views():
    """验证先关闭和释放全部视图，再仅释放一次共享 ViewModel。"""
    from mvvm.core import BaseViewModel
    from mvvm.mvvm_application import MVVMApplication
    from mvvm.views import ViewBase

    events = []

    class VM(BaseViewModel):
        def dispose(self):
            """记录 ViewModel 的最终释放时机。"""
            events.append("vm.dispose")
            super().dispose()

    class View(ViewBase):
        def __init__(self, vm, name):
            """为测试视图保存名称和共享 ViewModel。"""
            super().__init__(vm)
            self.name = name

        def close(self):
            """记录当前命名视图的关闭事件。"""
            events.append(self.name + ".close")

        def dispose(self):
            """记录当前命名视图的绑定释放事件。"""
            events.append(self.name + ".dispose")
            super().dispose()

    app = MVVMApplication()
    vm = VM()
    app.register_viewmodel("shared", vm)
    for name in ("first", "second"):
        app.register_view(name, View(vm, name))
    app.stop()
    app.stop()
    assert events == ["first.close", "first.dispose", "second.close", "second.dispose", "vm.dispose"]


def test_a_binding_can_target_a_setter_method():
    """验证属性绑定能调用控件 setter，而不是覆盖该方法。

    ``bind_property(..., 'setText')`` has to call setText.

    setattr(label, 'setText', value) only shadows the method with an attribute:
    the widget never updates and nothing reports a problem.
    """
    from mvvm.core import BaseViewModel, ObservableProperty
    from mvvm.views import ViewBase

    class Label:
        def __init__(self):
            """创建保存显示文本的最小标签桩。"""
            self.text = ""

        def setText(self, value):  # noqa: N802 - Qt spelling
            """记录绑定传入的文本值。"""
            self.text = value

    viewmodel = BaseViewModel()
    viewmodel._register_property("message", ObservableProperty("a"))
    label = Label()
    ViewBase(viewmodel).bind_property("message", label, "setText")

    viewmodel.set_property_value("message", "b")
    assert label.text == "b"


def test_a_binding_can_target_a_plain_attribute():
    """验证绑定也支持直接赋值的普通控件属性。"""
    from mvvm.core import BaseViewModel, ObservableProperty
    from mvvm.views import ViewBase

    class Element:
        value = ""

    viewmodel = BaseViewModel()
    viewmodel._register_property("message", ObservableProperty("a"))
    element = Element()
    ViewBase(viewmodel).bind_property("message", element, "value")

    viewmodel.set_property_value("message", "b")
    assert element.value == "b"


def test_two_way_binding_needs_an_explicit_getter():
    """验证双向绑定通过显式 getter 读取控件状态。

    Guessing the getter from the setter name reads the wrong value in Qt."""
    from mvvm.core import BaseViewModel, ObservableProperty
    from mvvm.views import ViewBase

    class Field:
        text = "from the widget"

    viewmodel = BaseViewModel()
    viewmodel._register_property("message", ObservableProperty("a"))
    view = ViewBase(viewmodel)

    one_way = view.bind_property("message", Field(), "text")
    with pytest.raises(ValueError, match="getter_name"):
        one_way.update_viewmodel_from_ui()

    two_way = view.bind_property("message", Field(), "text", getter_name="text")
    two_way.update_viewmodel_from_ui()
    assert viewmodel.get_property_value("message") == "from the widget"


def test_mvvm_import_does_not_require_qt():
    # The framework itself must not pull any Qt binding at import time.
    """验证导入 MVVM 包不会提前导入任何 Qt binding。"""
    for name in list(sys.modules):
        if name.startswith(("PySide6", "PySide2", "PyQt6", "PyQt5")):
            del sys.modules[name]
    import importlib
    importlib.import_module("mvvm")
    importlib.import_module("mvvm.mvvm_application")
    assert not any(
        name.startswith(("PySide6", "PySide2", "PyQt6", "PyQt5"))
        for name in sys.modules
    )


def test_factories_have_a_single_home():
    """验证工厂仅在统一模块定义并通过公开接口导出一次。

    mvvm.core.factory defines the factories; mvvm.core only re-exports them.

    A second definition under the same name made ``from mvvm.core import
    PropertyFactory`` resolve to whichever module the package imported last,
    which is not a decision anything should be making by accident.
    """
    import mvvm.core
    import mvvm.core.base_view_model as base_view_model
    import mvvm.core.factory as factory
    import mvvm.core.observable as observable

    assert mvvm.core.PropertyFactory is factory.PropertyFactory
    assert mvvm.core.ViewModelFactory is factory.ViewModelFactory
    assert not hasattr(observable, "PropertyFactory")
    assert not hasattr(base_view_model, "ViewModelFactory")
    assert mvvm.core.__all__.count("PropertyFactory") == 1
    assert mvvm.core.__all__.count("ViewModelFactory") == 1
    assert set(mvvm.core.__all__) <= set(dir(mvvm.core))


def test_every_mvvm_module_imports(monkeypatch):
    """遍历导入全部 MVVM 模块，发现循环导入和遗漏依赖。

    Every module in the tree must be importable.

    The port arrived with a models/app_model.py that imported four model modules
    that do not exist in this repository and redefined ApplicationModel. Nothing
    referenced it, so the only symptom was Cython compiling dead source into the
    bundle. mvvm_bootstrap is the one module that needs Qt, hence the fake.
    """
    import importlib

    install_fake_binding(monkeypatch, {"PySide6": {"QtCore": {}, "QtGui": {}, "QtWidgets": {}}})

    imported = []
    for path in sorted((SRC / "mvvm").rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        imported.append(importlib.import_module(".".join(parts)))
    assert len(imported) >= 8
