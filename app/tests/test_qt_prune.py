from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


def _fake_windows_pyside(site: Path) -> Path:
    """A PySide6 tree with the Windows wheel layout.

    The Windows wheels keep no ``Qt`` directory: the Qt DLLs, the extension
    modules and the tooling sit in the package root, and only ``plugins``,
    ``qml``, ``translations`` and ``metatypes`` are beside them.
    """
    pyside = site / "PySide6"
    for module in ("QtCore", "QtGui", "QtWidgets", "QtQml", "QtWebEngineCore"):
        _touch(pyside / f"{module}.pyd")
    for module in ("QtCore", "QtGui", "QtWidgets", "QtQml", "QtWebEngineCore", "QtPdf", "QtSql", "QtMultimedia"):
        _touch(pyside / f"Qt6{module[2:]}.dll")
    _touch(pyside / "pyside6.abi3.dll")
    _touch(pyside / "pyside6qml.abi3.dll")
    _touch(pyside / "msvcp140.dll")
    # Loaded by name at runtime, never through an import table.
    _touch(pyside / "opengl32sw.dll")
    _touch(pyside / "plugins" / "platforms" / "qwindows.dll")
    _touch(pyside / "plugins" / "sqldrivers" / "qsqlite.dll")
    _touch(pyside / "plugins" / "imageformats" / "qpdf.dll")
    _touch(pyside / "plugins" / "imageformats" / "qsvg.dll")
    _touch(pyside / "plugins" / "multimedia" / "ffmpegmediaplugin.dll")
    _touch(pyside / "qml" / "QtQuick" / "qtquickplugin.dll")
    _touch(pyside / "QtWebEngineProcess.exe")
    _touch(pyside / "assistant.exe")
    _touch(pyside / "qmlls.exe")
    _touch(pyside / "metatypes" / "qt6core_metatypes.json")
    (pyside / "translations").mkdir(parents=True, exist_ok=True)
    for name in ("qtbase_zh_CN.qm", "qt_zh_CN.qm", "qt_help_zh_CN.qm", "assistant_zh_CN.qm", "qtmultimedia_zh_CN.qm"):
        (pyside / "translations" / name).write_text("", encoding="utf-8")
    return pyside


def _windows_dependencies(path: Path) -> list[str]:
    """The import graph of the fake Windows tree, keyed by file name."""
    return {
        "QtWidgets.pyd": ["Qt6Widgets.dll", "Qt6Gui.dll", "pyside6.abi3.dll"],
        "QtGui.pyd": ["Qt6Gui.dll", "Qt6Core.dll", "pyside6.abi3.dll"],
        "QtCore.pyd": ["Qt6Core.dll", "pyside6.abi3.dll"],
        "Qt6Widgets.dll": ["Qt6Gui.dll", "msvcp140.dll"],
        "Qt6Gui.dll": ["Qt6Core.dll", "msvcp140.dll"],
        "Qt6Core.dll": ["msvcp140.dll"],
        "qwindows.dll": ["Qt6Gui.dll", "Qt6Core.dll"],
        "qsvg.dll": ["Qt6Gui.dll", "Qt6Core.dll"],
        "qpdf.dll": ["Qt6Gui.dll", "Qt6Pdf.dll"],
        "qsqlite.dll": ["Qt6Core.dll", "Qt6Sql.dll"],
        "ffmpegmediaplugin.dll": ["Qt6Multimedia.dll"],
    }.get(path.name, [])


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


def test_prune_keeps_only_the_imported_qt_payload_on_windows(tmp_path, monkeypatch):
    """The Windows wheel has no Qt/ directory, so every path has to be derived.

    Reading only the POSIX layout left the whole Windows payload in place: the
    Qt DLLs, the SQL drivers, the multimedia stack and the QML tree all stayed
    in the bundle, and the native closure then failed on the optional payload
    those extra modules link (the database clients, the ffmpeg libraries).
    """
    from py_upper import qt_prune

    site = tmp_path / "site-packages"
    pyside = _fake_windows_pyside(site)
    monkeypatch.setattr(qt_prune, "dependency_names", _windows_dependencies)

    counters = prune(site, TARGETS["windows-x86_64"], {"QtWidgets", "QtGui", "QtCore"})

    assert counters and counters["bytes"] > 0
    # kept: the imported modules, the Qt libraries they link and the runtime
    # images that closure needs
    assert (pyside / "QtCore.pyd").exists()
    assert (pyside / "QtGui.pyd").exists()
    assert (pyside / "QtWidgets.pyd").exists()
    assert (pyside / "Qt6Core.dll").exists()
    assert (pyside / "Qt6Gui.dll").exists()
    assert (pyside / "Qt6Widgets.dll").exists()
    assert (pyside / "pyside6.abi3.dll").exists()
    assert (pyside / "msvcp140.dll").exists()
    assert (pyside / "plugins" / "platforms" / "qwindows.dll").exists()
    assert (pyside / "plugins" / "imageformats" / "qsvg.dll").exists()
    # kept although nothing imports it: QtGui loads it by name
    assert (pyside / "opengl32sw.dll").exists()
    # dropped: unimported modules and everything that belongs to them
    assert not (pyside / "QtQml.pyd").exists()
    assert not (pyside / "QtWebEngineCore.pyd").exists()
    assert not (pyside / "Qt6Qml.dll").exists()
    assert not (pyside / "Qt6WebEngineCore.dll").exists()
    assert not (pyside / "Qt6Sql.dll").exists()
    assert not (pyside / "Qt6Multimedia.dll").exists()
    assert not (pyside / "plugins" / "sqldrivers").exists()
    assert not (pyside / "plugins" / "multimedia").exists()
    # an image format that needs a Qt module the app never imports goes too
    assert not (pyside / "plugins" / "imageformats" / "qpdf.dll").exists()
    assert not (pyside / "Qt6Pdf.dll").exists()
    assert not (pyside / "qml").exists()
    assert not (pyside / "pyside6qml.abi3.dll").exists()
    assert not (pyside / "metatypes").exists()
    # tooling: the Windows wheels append .exe to every name
    assert not (pyside / "assistant.exe").exists()
    assert not (pyside / "qmlls.exe").exists()
    # the WebEngine helper process follows the payload it belongs to
    assert not (pyside / "QtWebEngineProcess.exe").exists()
    # translations: Qt's own strings stay, tooling and unused modules go
    assert (pyside / "translations" / "qtbase_zh_CN.qm").exists()
    assert (pyside / "translations" / "qt_zh_CN.qm").exists()
    assert not (pyside / "translations" / "qt_help_zh_CN.qm").exists()
    assert not (pyside / "translations" / "assistant_zh_CN.qm").exists()
    assert not (pyside / "translations" / "qtmultimedia_zh_CN.qm").exists()


def test_windows_pruning_never_touches_the_package_itself(tmp_path, monkeypatch):
    """Only DLLs are payload; the package's own files must survive the sweep.

    On Windows the Qt libraries live in the package root next to the importable
    extension modules and the packaging metadata, so a reachability sweep that
    treated every entry as a library would delete the modules it just kept.
    """
    from py_upper import qt_prune

    site = tmp_path / "site-packages"
    pyside = _fake_windows_pyside(site)
    (pyside / "__init__.py").write_text("", encoding="utf-8")
    (pyside / "QtCore.pyi").write_text("", encoding="utf-8")
    (pyside / "support").mkdir()
    (pyside / "support" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(qt_prune, "dependency_names", _windows_dependencies)

    prune(site, TARGETS["windows-x86_64"], {"QtWidgets", "QtGui", "QtCore"})

    assert (pyside / "__init__.py").exists()
    assert (pyside / "QtCore.pyi").exists()
    assert (pyside / "support" / "__init__.py").exists()


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


def test_configured_modules_reports_a_runtime_that_cannot_import_the_payload(tmp_path, monkeypatch):
    """A failed closure probe is an error, never a narrower payload.

    Falling back to the requested modules deletes everything they link
    indirectly, because importing QtWidgets alone never mentions QtCore and
    QtGui; the bundle then only breaks in the packaged smoke run.
    """
    from py_upper import qt_prune

    site = tmp_path / "site-packages"
    _fake_pyside(site)
    # A real wheel ships PySide6/__init__.py. Without it the probe would import
    # whichever PySide6 the test interpreter happens to have installed instead
    # of the staged payload.
    (site / "PySide6" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(qt_prune, "optimize_config", lambda: {"qt": "imports"})
    monkeypatch.setattr(qt_prune, "used_qt_modules", lambda *args, **kwargs: {"QtWidgets"})
    monkeypatch.setattr(qt_prune, "_native_runtime_python", lambda target: Path(sys.executable))
    with pytest.raises(RuntimeError, match="QtWidgets"):
        qt_prune.configured_modules(TARGETS["macos-arm64"], site)


def test_configured_modules_keeps_the_payload_when_the_target_cannot_run(tmp_path, monkeypatch):
    """Without the target interpreter the closure is unknown, so nothing goes."""
    from py_upper import qt_prune

    site = tmp_path / "site-packages"
    _fake_pyside(site)
    monkeypatch.setattr(qt_prune, "optimize_config", lambda: {"qt": "imports"})
    monkeypatch.setattr(qt_prune, "used_qt_modules", lambda *args, **kwargs: {"QtWidgets"})
    monkeypatch.setattr(qt_prune, "_native_runtime_python", lambda target: None)
    assert qt_prune.configured_modules(TARGETS["macos-arm64"], site) is None


def test_prune_ignores_a_site_without_a_qt_payload(tmp_path, monkeypatch):
    """A staged site without PySide6 has nothing to prune, so nothing is probed."""
    from py_upper import qt_prune

    monkeypatch.setattr(qt_prune, "optimize_config", lambda: {"qt": "imports"})

    def fail(*args, **kwargs):
        raise AssertionError("the module closure must not be probed without a Qt payload")

    monkeypatch.setattr(qt_prune, "_runtime_closure", fail)
    assert prune(tmp_path / "site-packages", TARGETS["macos-arm64"]) is None


def test_prune_is_disabled_by_default(tmp_path, monkeypatch):
    from py_upper import qt_prune

    site = tmp_path / "site-packages"
    pyside = _fake_pyside(site)
    monkeypatch.setattr(qt_prune, "optimize_config", lambda: {})
    monkeypatch.setattr(qt_prune, "dependency_names", lambda path: [])
    assert prune(site, TARGETS["macos-arm64"]) is None
    assert (pyside / "QtWebEngineCore.abi3.so").exists()
