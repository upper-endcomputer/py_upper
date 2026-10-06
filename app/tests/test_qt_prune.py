from __future__ import annotations

from pathlib import Path

from py_upper.config import TARGETS
from py_upper.qt_prune import _qt_module_of, prune, used_qt_modules


def _touch(path: Path, payload: bytes = b"\xcf\xfa\xed\xfe") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _fake_pyside(site: Path) -> Path:
    pyside = site / "PySide6"
    for module in ("QtCore", "QtGui", "QtWidgets", "QtQml", "QtWebEngineCore"):
        _touch(pyside / f"{module}.abi3.so")
    for module in ("QtCore", "QtGui", "QtWidgets", "QtQml", "QtWebEngineCore"):
        _touch(pyside / "Qt" / "lib" / f"{module}.framework" / "Versions" / "A" / module)
    _touch(pyside / "Qt" / "plugins" / "platforms" / "libqcocoa.dylib")
    _touch(pyside / "Qt" / "plugins" / "sqldrivers" / "libqsqlite.dylib")
    _touch(pyside / "Qt" / "plugins" / "imageformats" / "libqpdf.dylib")
    _touch(pyside / "Qt" / "qml" / "QtQuick" / "libqtquickplugin.dylib")
    _touch(pyside / "Qt" / "libexec" / "QtWebEngineProcess")
    _touch(pyside / "qmlls")
    _touch(pyside / "Assistant.app" / "Contents" / "MacOS" / "Assistant")
    _touch(pyside / "libpyside6.abi3.6.11.dylib")
    _touch(pyside / "libpyside6qml.abi3.6.11.dylib")
    (pyside / "Qt" / "translations").mkdir(parents=True, exist_ok=True)
    for name in ("qtbase_zh_CN.qm", "qt_zh_CN.qm", "qt_help_zh_CN.qm", "assistant_zh_CN.qm", "qtmultimedia_zh_CN.qm"):
        (pyside / "Qt" / "translations" / name).write_text("", encoding="utf-8")
    return pyside


def test_qt_module_mapping_covers_platform_layouts():
    assert _qt_module_of("@rpath/QtGui.framework/Versions/A/QtGui") == "QtGui"
    assert _qt_module_of("@rpath/libQt6Core.6.dylib") == "QtCore"
    assert _qt_module_of("libQt6Gui.so.6.11.0") == "QtGui"
    assert _qt_module_of("Qt6Widgets.dll") == "QtWidgets"
    assert _qt_module_of("libqcocoa.dylib") is None


def test_qt_module_mapping_ignores_system_frameworks():
    """System frameworks must not look like Qt modules.

    Treating CoreVideo/IOSurface/ColorSync as Qt modules dropped the cocoa
    platform plugin, because it links system frameworks the Qt wrapper closure
    never mentions, and the packaged application then could not start.
    """
    assert _qt_module_of("/System/Library/Frameworks/CoreVideo.framework/Versions/A/CoreVideo") is None
    assert _qt_module_of("/System/Library/Frameworks/IOSurface.framework/Versions/A/IOSurface") is None
    assert _qt_module_of("/System/Library/Frameworks/AppKit.framework/Versions/C/AppKit") is None
    assert _qt_module_of("/usr/lib/libSystem.B.dylib") is None
    assert _qt_module_of("/usr/lib/libobjc.A.dylib") is None


def test_used_qt_modules_reads_application_sources(tmp_path):
    source = tmp_path / "src"
    (source / "core").mkdir(parents=True)
    (source / "core" / "app.py").write_text(
        "from PySide6.QtWidgets import QApplication\nimport PySide6.QtCore\n", encoding="utf-8"
    )
    (source / "main.py").write_text("print('no qt here')\n", encoding="utf-8")
    assert used_qt_modules(source) == {"QtWidgets", "QtCore"}


def test_prune_keeps_only_the_imported_qt_payload(tmp_path, monkeypatch):
    from py_upper import qt_prune

    site = tmp_path / "site-packages"
    pyside = _fake_pyside(site)

    def fake_dependencies(path: Path) -> list[str]:
        name = path.name
        if name == "QtWidgets.abi3.so":
            return ["@rpath/QtWidgets.framework/Versions/A/QtWidgets", "@rpath/QtGui.framework/Versions/A/QtGui"]
        if name == "QtGui.abi3.so":
            return ["@rpath/QtGui.framework/Versions/A/QtGui", "@rpath/QtCore.framework/Versions/A/QtCore"]
        if name == "libqcocoa.dylib":
            # A platform plugin links system frameworks and may even reference
            # an extra Qt module; it must still be kept.
            return [
                "@rpath/QtGui.framework/Versions/A/QtGui",
                "@rpath/QtCore.framework/Versions/A/QtCore",
                "@rpath/QtPdf.framework/Versions/A/QtPdf",
                "/System/Library/Frameworks/CoreVideo.framework/Versions/A/CoreVideo",
                "/System/Library/Frameworks/IOSurface.framework/Versions/A/IOSurface",
            ]
        if name == "libqsqlite.dylib":
            return ["@rpath/QtCore.framework/Versions/A/QtCore"]
        if name == "libqpdf.dylib":
            return ["@rpath/QtGui.framework/Versions/A/QtGui", "@rpath/QtPdf.framework/Versions/A/QtPdf"]
        if name == "QtCore.framework":
            return ["/usr/lib/libSystem.B.dylib"]
        if name == "QtGui.framework":
            return ["@rpath/QtCore.framework/Versions/A/QtCore"]
        if name == "QtWidgets.framework":
            return ["@rpath/QtGui.framework/Versions/A/QtGui"]
        if name == "QtWebEngineCore.framework":
            return ["@rpath/QtQml.framework/Versions/A/QtQml"]
        return []

    monkeypatch.setattr(qt_prune, "dependency_names", fake_dependencies)
    # prune() receives the expanded module set, exactly as configured_modules()
    # hands it over after resolving the runtime closure.
    counters = prune(site, TARGETS["macos-arm64"], {"QtWidgets", "QtGui", "QtCore"})

    assert counters and counters["bytes"] > 0
    # kept: the imported module and its native closure
    assert (pyside / "QtWidgets.abi3.so").exists()
    assert (pyside / "QtGui.abi3.so").exists()
    assert (pyside / "QtCore.abi3.so").exists()
    assert (pyside / "Qt" / "lib" / "QtWidgets.framework").exists()
    assert (pyside / "Qt" / "plugins" / "platforms" / "libqcocoa.dylib").exists()
    # dropped: unimported modules, their libraries, plugins and tooling
    assert not (pyside / "QtWebEngineCore.abi3.so").exists()
    assert not (pyside / "QtQml.abi3.so").exists()
    assert not (pyside / "Qt" / "lib" / "QtWebEngineCore.framework").exists()
    assert not (pyside / "Qt" / "lib" / "QtQml.framework").exists()
    assert not (pyside / "Qt" / "plugins" / "sqldrivers").exists()
    # an image format that needs a Qt module the app never imports goes too
    assert not (pyside / "Qt" / "plugins" / "imageformats" / "libqpdf.dylib").exists()
    assert (pyside / "Qt" / "plugins" / "imageformats").exists()
    assert not (pyside / "Qt" / "qml").exists()
    assert not (pyside / "Qt" / "libexec").exists()
    assert not (pyside / "libpyside6qml.abi3.6.11.dylib").exists()
    assert (pyside / "libpyside6.abi3.6.11.dylib").exists()
    assert not (pyside / "qmlls").exists()
    assert not (pyside / "Assistant.app").exists()
    # translations: Qt's own strings stay, tooling and unused modules go
    assert (pyside / "Qt" / "translations" / "qtbase_zh_CN.qm").exists()
    assert (pyside / "Qt" / "translations" / "qt_zh_CN.qm").exists()
    assert not (pyside / "Qt" / "translations" / "qt_help_zh_CN.qm").exists()
    assert not (pyside / "Qt" / "translations" / "assistant_zh_CN.qm").exists()
    assert not (pyside / "Qt" / "translations" / "qtmultimedia_zh_CN.qm").exists()


def test_configured_modules_expands_explicit_lists_through_the_closure(tmp_path, monkeypatch):
    from py_upper import qt_prune

    site = tmp_path / "site-packages"
    _fake_pyside(site)
    monkeypatch.setattr(qt_prune, "optimize_config", lambda: {"qt": ["QtWidgets"]})
    monkeypatch.setattr(qt_prune, "_runtime_closure", lambda site, target, modules: modules | {"QtGui", "QtCore"})
    assert qt_prune.configured_modules(TARGETS["macos-arm64"], site) == {"QtWidgets", "QtGui", "QtCore"}


def test_configured_modules_rejects_unknown_settings(tmp_path, monkeypatch):
    from py_upper import qt_prune

    monkeypatch.setattr(qt_prune, "optimize_config", lambda: {"qt": "everything"})
    try:
        qt_prune.configured_modules(TARGETS["macos-arm64"], tmp_path)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected a rejection of an unknown qt setting")


def test_prune_is_disabled_by_default(tmp_path, monkeypatch):
    from py_upper import qt_prune

    site = tmp_path / "site-packages"
    pyside = _fake_pyside(site)
    monkeypatch.setattr(qt_prune, "optimize_config", lambda: {})
    monkeypatch.setattr(qt_prune, "dependency_names", lambda path: [])
    assert prune(site, TARGETS["macos-arm64"]) is None
    assert (pyside / "QtWebEngineCore.abi3.so").exists()
