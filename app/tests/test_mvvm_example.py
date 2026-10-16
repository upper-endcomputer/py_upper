"""The About example: model → service → ViewModel → views → button click.

Qt is faked here on purpose: the point of these tests is the wiring, and the
packaged build runs the same code against the real PySide6 in the launcher
smoke step and in the CI integration run.
"""
from __future__ import annotations

import os

import pytest

from conftest import install_fake_binding

ABOUT_BUTTON = "关于 MVVM 架构"
CLOSE_BUTTON = "关闭"

# Every fake widget registers itself so a test can reach what the view built
# without the view having to expose its widgets for testing.
CREATED: dict[str, list] = {
    "windows": [],
    "dialogs": [],
    "labels": [],
    "buttons": [],
    "browsers": [],
    "layouts": [],
    "applications": [],
    "qt_attributes": [],
}


class FakeSignal:
    def __init__(self) -> None:
        self.handlers = []

    def connect(self, handler) -> None:
        self.handlers.append(handler)

    def emit(self, *args) -> None:
        for handler in list(self.handlers):
            handler(*args)


class FakeWindow:
    """QWidget/QMainWindow/QDialog surface the views touch."""

    def __init__(self) -> None:
        self.visible = False
        self.title = ""
        self.central_widget = None
        self.size = None

    def setWindowTitle(self, title: str) -> None:  # noqa: N802 - Qt spelling
        self.title = title

    def setCentralWidget(self, widget) -> None:  # noqa: N802 - Qt spelling
        self.central_widget = widget

    def resize(self, *size: int) -> None:
        self.size = size

    def show(self) -> None:
        self.visible = True

    def close(self) -> None:
        self.visible = False


class FakeMainWindow(FakeWindow):
    def __init__(self) -> None:
        super().__init__()
        CREATED["windows"].append(self)


class FakeDialog(FakeWindow):
    def __init__(self) -> None:
        super().__init__()
        CREATED["dialogs"].append(self)


class FakeLabel:
    def __init__(self, text: str = "") -> None:
        self.text = text
        CREATED["labels"].append(self)

    def setText(self, value: str) -> None:  # noqa: N802 - Qt spelling
        self.text = value


class FakeButton:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.clicked = FakeSignal()
        CREATED["buttons"].append(self)

    def click(self) -> None:
        self.clicked.emit(False)


class FakeTextBrowser:
    def __init__(self) -> None:
        self.plain_text = ""
        CREATED["browsers"].append(self)

    def setPlainText(self, value: str) -> None:  # noqa: N802 - Qt spelling
        self.plain_text = value


class FakeVBoxLayout:
    def __init__(self, parent=None) -> None:
        self.parent = parent
        self.widgets: list = []
        CREATED["layouts"].append(self)

    def addWidget(self, widget) -> None:  # noqa: N802 - Qt spelling
        self.widgets.append(widget)


class FakeQtFlags:
    """The ``QtCore.Qt`` namespace; only the Qt5 path reads it (high-DPI attr)."""

    AA_EnableHighDpiScaling = 5


class FakeApplicationSurface:
    """Everything the bootstrap and the example touch on a QApplication.

    The blocking-loop spelling is deliberately absent here: Qt6 binds it as
    ``exec``, the Qt5 bindings only as ``exec_``, and one binding ships one of
    the two. ``exec_loop`` in the shim reads whichever exists, so each
    application fake below carries exactly one binding's name.
    """

    def __init__(self, argv=None) -> None:
        self.argv = argv
        self.processed = 0
        self.loops = 0
        CREATED["applications"].append(self)

    @classmethod
    def instance(cls):
        return None

    @classmethod
    def setAttribute(cls, attribute) -> None:  # noqa: N802 - Qt spelling
        # Static in Qt: the application attributes are read while the instance is
        # built, which is why this records on the class and not on an object.
        CREATED["qt_attributes"].append(attribute)

    def processEvents(self) -> None:  # noqa: N802 - Qt spelling
        self.processed += 1

    def platformName(self) -> str:  # noqa: N802 - Qt spelling
        return os.environ.get("QT_QPA_PLATFORM", "unknown")

    def quit(self) -> None:
        pass

    def _enter_loop(self) -> int:
        self.loops += 1
        return 0


class FakeApplication(FakeApplicationSurface):
    """PySide6/PyQt6 shape: the loop entry is ``exec``."""

    def exec(self) -> int:
        return self._enter_loop()


class FakeQt5Application(FakeApplicationSurface):
    """PySide2/PyQt5 shape: the loop entry is ``exec_``, and ``exec`` does not exist.

    This is the shape a packaged application met on Windows: ``AttributeError:
    'PySide2.QtWidgets.QApplication' object has no attribute 'exec'``. Qt5
    bindings were generated while ``exec`` was still a statement, so they only
    publish the trailing-underscore name.

    The constructor also carries Qt5's high-DPI rule: Qt reads those attributes
    while the application is built, so the fake refuses to be constructed before
    one was set, exactly as Qt5 logs a warning and ignores the attribute when it
    is set too late.
    """

    def __init__(self, argv=None) -> None:
        assert CREATED["qt_attributes"], "Qt5 attributes must be set before the application exists"
        super().__init__(argv)

    def exec_(self) -> int:  # noqa: N802 - Qt spelling
        return self._enter_loop()


def _qt_widgets(application) -> dict[str, object]:
    """The QtWidgets members the example's views and the bootstrap touch."""
    return {
        "QApplication": application,
        "QMainWindow": FakeMainWindow,
        "QDialog": FakeDialog,
        "QWidget": FakeWindow,
        "QVBoxLayout": FakeVBoxLayout,
        "QLabel": FakeLabel,
        "QPushButton": FakeButton,
        "QTextBrowser": FakeTextBrowser,
    }


@pytest.fixture
def fake_qt(monkeypatch):
    """The Qt6 shape: PySide6, whose application spells the loop entry ``exec``."""
    for entries in CREATED.values():
        entries.clear()
    install_fake_binding(
        monkeypatch,
        {"PySide6": {"QtCore": {"Qt": FakeQtFlags}, "QtWidgets": _qt_widgets(FakeApplication)}},
    )
    return CREATED


@pytest.fixture
def fake_qt5(monkeypatch):
    """The Qt5 shape: PySide2, whose application only spells the loop entry ``exec_``."""
    for entries in CREATED.values():
        entries.clear()
    install_fake_binding(
        monkeypatch,
        {"PySide2": {"QtCore": {"Qt": FakeQtFlags}, "QtWidgets": _qt_widgets(FakeQt5Application)}},
    )
    return CREATED


def button(text: str) -> FakeButton:
    matches = [item for item in CREATED["buttons"] if item.text == text]
    assert len(matches) == 1, f"expected exactly one {text!r} button, found {len(matches)}"
    return matches[0]


def test_the_viewmodel_reads_its_model_and_service():
    from mvvm.models import AboutModel, ApplicationModel
    from mvvm.services import GreetingService
    from mvvm.view_models import MainViewModel

    model = ApplicationModel()
    model.app_name = "Demo"
    viewmodel = MainViewModel(model, GreetingService(), AboutModel())

    assert viewmodel.title == "Demo"
    assert viewmodel.message == "Hello from py_upper"
    assert viewmodel.about_visible is False

    changed: list[str] = []
    viewmodel.subscribe_property_changed(lambda event: changed.append(event.property_name))

    viewmodel.show_about_command.execute()
    assert viewmodel.about_visible is True
    assert "about_visible" in changed

    viewmodel.hide_about_command.execute()
    assert viewmodel.about_visible is False


def test_about_model_round_trips():
    from mvvm.models import AboutModel

    model = AboutModel(title="T", body="B")
    assert model.to_dict() == {"title": "T", "body": "B"}
    assert "view_models" in AboutModel().body

    copy = AboutModel()
    copy.from_dict(model.to_dict())
    assert (copy.title, copy.body) == ("T", "B")


def test_the_composition_root_registers_every_layer(fake_qt):
    from application import PyUpperApplication
    from mvvm.services import GreetingService

    application = PyUpperApplication()
    application.prepare()

    container = application.mvvm_app
    assert isinstance(container.resolve_service(GreetingService), GreetingService)
    assert container.get_viewmodel("main") is not None
    assert container.get_view("main") is not None
    assert container.get_view("about") is not None
    assert container.is_running is True


def test_the_about_button_opens_and_closes_the_window(fake_qt):
    from application import PyUpperApplication

    application = PyUpperApplication()
    application.prepare()

    viewmodel = application.mvvm_app.get_viewmodel("main")
    window = fake_qt["windows"][0]
    dialog = fake_qt["dialogs"][0]

    # start() 显示了主窗口，但没点按钮就不该弹出「关于」窗口
    assert window.visible is True
    assert dialog.visible is False

    # 绑定生效：窗口标题/文案来自 ViewModel，ViewModel 又来自 model 与 service
    assert window.title == viewmodel.title != ""
    assert fake_qt["labels"][0].text == "Hello from py_upper"
    assert dialog.title == ABOUT_BUTTON
    assert fake_qt["browsers"][0].plain_text == viewmodel.about_body

    button(ABOUT_BUTTON).click()
    assert viewmodel.about_visible is True
    assert dialog.visible is True

    button(CLOSE_BUTTON).click()
    assert viewmodel.about_visible is False
    assert dialog.visible is False


def test_run_gui_headless_uses_the_same_assembly(fake_qt, monkeypatch, capsys):
    from application import run_gui

    monkeypatch.setenv("PY_UPPER_HEADLESS", "1")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)

    assert run_gui() == 0
    assert "GUI OK offscreen" in capsys.readouterr().out
    # stop() 把窗口关掉、容器清空，冒烟路径不会留下半开的窗口
    assert fake_qt["windows"][0].visible is False
    assert fake_qt["dialogs"][0].visible is False


def test_run_gui_enters_the_loop_the_binding_ships(fake_qt5, monkeypatch):
    """The interactive path, on the binding the packaged Windows application used.

    Every other test here and the build smoke step take the headless branch,
    which calls ``processEvents()`` and never opens a loop, and every real-Qt leg
    had PySide6 installed. So the one call that starts the event loop was never
    spelled on Qt5: the window was built and the process died with
    ``AttributeError: 'PySide2.QtWidgets.QApplication' object has no attribute
    'exec'`` before anything was shown.
    """
    from application import run_gui

    monkeypatch.delenv("PY_UPPER_HEADLESS", raising=False)

    assert run_gui() == 0
    assert fake_qt5["applications"][-1].loops == 1


def test_the_qt5_high_dpi_attribute_is_set_before_the_window(fake_qt5):
    """Qt5 only reads the high-DPI attributes while the QApplication is built.

    The old call sat on the instance, after construction, so Qt logged
    ``Attribute Qt::AA_EnableHighDpiScaling must be set before QCoreApplication is
    created`` and the scaling never applied. `FakeQt5Application.__init__` refuses
    to be built before the attribute is recorded, so the order is part of the
    test rather than a comment.
    """
    from application import PyUpperApplication

    PyUpperApplication()

    assert fake_qt5["qt_attributes"] == [FakeQtFlags.AA_EnableHighDpiScaling]
