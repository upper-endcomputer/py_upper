from __future__ import annotations

import os

from services.hello import make_message
from utils.paths import app_name

# Set by the build tool's smoke step and by CI: build the real window, pump the
# event loop once and exit instead of blocking on a window server.
HEADLESS_ENV = "PY_UPPER_HEADLESS"


def run_application() -> None:
    print(make_message())


def run_gui() -> int:
    """Open the Qt main window.

    This is the real Qt check for a packaged application: it creates a
    QApplication and a QMainWindow with the bundled PySide6, so a broken Qt
    payload (missing platform plugin, unresolved Qt framework, unusable
    shiboken6) fails here instead of silently shipping.
    """
    headless = os.environ.get(HEADLESS_ENV) == "1"
    if headless:
        # No window server in CI; the offscreen plugin still exercises the
        # whole Qt stack. An explicit QT_QPA_PLATFORM always wins.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

    application = QApplication.instance() or QApplication([])
    window = QMainWindow()
    window.setWindowTitle(app_name())
    window.setCentralWidget(QLabel(make_message()))
    window.resize(480, 240)
    window.show()
    application.processEvents()

    if headless:
        window.close()
        print("GUI OK", application.platformName())
        return 0
    return application.exec()


def main() -> int:
    """Packaged application entry point."""
    run_application()
    return run_gui()
