"""Keep only the Qt payload the application actually imports.

A PySide6 install ships every Qt module, and QtWebEngineCore alone is roughly
450 MB of Chromium. Other packagers look smaller because they only bundle the Qt
modules the application imports; py_upper does the same here:

1. read the ``PySide6.Qt*`` modules referenced by ``app/src``;
2. ask the target runtime which modules that pulls in transitively;
3. drop every other Qt wrapper module, Qt library and Qt plugin category.

The result is validated by the build's own Qt smoke test, which imports the
application and opens a real window in the packaged bundle.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .config import APP, Target, host_target, optimize_config, runtime_executable
from .native.deps import dependency_names

WRAPPER_SUFFIXES = {".so", ".pyd"}
IMPORT_PATTERN = re.compile(r"PySide6(?:\.|\s+import\s+)(Qt[A-Za-z0-9_]+)")

# Plugin categories every Qt application needs at runtime.
BASE_PLUGIN_CATEGORIES = {
    "platforms", "styles", "imageformats", "iconengines",
    "platforminputcontexts", "generic", "networkinformation",
}
# Plugin categories that only matter for specific Qt modules.
MODULE_PLUGIN_CATEGORIES = {
    "QtNetwork": {"tls"},
    "QtSql": {"sqldrivers"},
    "QtMultimedia": {"multimedia"},
    "QtLocation": {"position", "geoservices"},
    "QtSerialBus": {"canbus"},
    "QtSensors": {"sensors"},
    "QtTextToSpeech": {"texttospeech"},
    "QtScxml": {"scxmldatamodel"},
    "QtDesigner": {"designer"},
    "QtWebEngineCore": {"webview"},
    "QtQuick": {"renderplugins"},
    "Qt3DRender": {"sceneparsers", "geometryloaders", "renderers", "assetimporters"},
    "QtQuick3DRuntimeRender": {"sceneparsers", "geometryloaders", "renderers", "assetimporters"},
}
# Developer tooling that is never part of a runtime payload. Missing entries
# are ignored, so the list covers the union of the supported platforms.
TOOLING_ENTRIES = {
    "Assistant.app", "Designer.app", "Linguist.app",
    "assistant", "designer", "linguist",
    "qmlls", "qmllint", "qmlformat", "qmlimportscanner", "qmltyperegistrar",
    "qmlcachegen", "qmlscene", "qmltestrunner", "qmlprofiler", "qmlpreview",
    "qmlplugindump", "lupdate", "lrelease", "lconvert",
    "balsam", "balsamui", "qsb", "svgtoqml", "uic", "rcc", "qtdiag", "qtpaths",
    "typesystems",
}
QML_MODULE_PREFIXES = ("QtQml", "QtQuick")
# Plugins worth keeping even though they pull in an extra Qt module: SVG icons
# are ubiquitous in modern applications.
PLUGIN_KEEP_EXCEPTIONS = ("qsvg", "qsvgicon")
# Translation families that belong to developer tooling, never to a running app.
TOOLING_TRANSLATION_PREFIXES = ("assistant_", "designer_", "linguist_", "qt_help_")


def used_qt_modules(source_root: Path | None = None) -> set[str]:
    """Qt modules referenced by the application's own Python sources."""
    root = source_root or (APP / "src")
    modules: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        modules.update(IMPORT_PATTERN.findall(path.read_text(encoding="utf-8", errors="replace")))
    return modules


def _runtime_closure(site: Path, target: Target, modules: set[str]) -> set[str] | None:
    """Import the modules with the target runtime and return what it loaded."""
    python = _native_runtime_python(target)
    if python is None:
        return None
    script = (
        "import importlib, json, sys\n"
        f"for name in {sorted(modules)!r}:\n"
        "    importlib.import_module('PySide6.' + name)\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('PySide6.'))))\n"
    )
    env = {"PYTHONPATH": str(site), "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"}
    probe = subprocess.run([str(python), "-c", script], capture_output=True, text=True, check=False, env=env)
    if probe.returncode != 0:
        reason = (probe.stderr.strip().splitlines() or ["unknown error"])[-1]
        print(f"Qt pruning: module closure unavailable ({reason}); keeping the imported modules only")
        return None
    try:
        loaded = json.loads(probe.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return {name.split(".", 1)[1] for name in loaded if name.count(".") == 1}


def _native_runtime_python(target: Target) -> Path | None:
    """The target interpreter, when this host can execute it."""
    if target != host_target():
        return None
    try:
        return runtime_executable(target)
    except RuntimeError:
        return None


def configured_modules(target: Target, site: Path) -> set[str] | None:
    """Qt modules to keep, or None when pruning is disabled."""
    value = optimize_config().get("qt", "all")
    if isinstance(value, str):
        setting = value.strip().lower()
        if setting in {"all", ""}:
            return None
        if setting != "imports":
            raise RuntimeError("[tool.py_upper.optimize].qt must be 'all', 'imports', or an array of Qt module names")
        imported = used_qt_modules()
        if not imported:
            print("Qt pruning: app/src does not reference PySide6, keeping the full Qt payload")
            return None
        return _runtime_closure(site, target, imported) or imported
    if isinstance(value, list):
        modules = {str(item).strip() for item in value if str(item).strip()}
        if not modules:
            raise RuntimeError("[tool.py_upper.optimize].qt must not be an empty array")
        return _runtime_closure(site, target, modules) or modules
    raise RuntimeError("[tool.py_upper.optimize].qt must be 'all', 'imports', or an array of Qt module names")


def _wrapper_modules(pyside: Path) -> dict[str, Path]:
    return {
        path.name.split(".", 1)[0]: path
        for path in sorted(pyside.iterdir())
        if path.is_file() and path.suffix in WRAPPER_SUFFIXES and path.name.startswith("Qt")
    }


def _library_dirs(pyside: Path) -> list[Path]:
    return [path for path in (pyside / "Qt" / "lib", pyside / "Qt" / "bin") if path.is_dir()]


def _framework_binary(entry: Path) -> Path | None:
    if not entry.is_dir() or not entry.name.endswith(".framework"):
        return None
    binary = entry / "Versions" / "A" / entry.name[: -len(".framework")]
    return binary if binary.exists() else None


def _library_index(pyside: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for directory in _library_dirs(pyside):
        for entry in sorted(directory.iterdir()):
            binary = _framework_binary(entry)
            if binary is not None:
                index.setdefault(binary.name.lower(), []).append(binary)
                continue
            if entry.is_file() or entry.is_symlink():
                index.setdefault(entry.name.lower(), []).append(entry)
    return index


def _qt_module_of(dependency: str) -> str | None:
    """Map a load path back to its Qt module name, or None when it is not Qt."""
    if ".framework/" in dependency:
        return dependency.split(".framework/", 1)[0].rsplit("/", 1)[-1]
    base = Path(dependency).name
    match = re.match(r"^(?:lib)?(Qt6?[A-Za-z0-9_]+?)(?:\.[0-9.]+)?(?:\.so.*|\.dylib|\.dll)?$", base)
    if not match:
        return None
    name = match.group(1)
    if name.startswith("Qt") and not name.startswith("Qt6"):
        return name
    if name.startswith("Qt6"):
        return "Qt" + name[3:]
    return None


def _reachable_libraries(pyside: Path, roots: list[Path]) -> tuple[set[Path], set[str]]:
    """Qt libraries reachable from ``roots``, plus the modules they belong to."""
    index = _library_index(pyside)
    kept: set[Path] = set()
    modules: set[str] = set()
    queue = list(roots)
    while queue:
        current = queue.pop()
        for name in dependency_names(current):
            module = _qt_module_of(name)
            if module:
                modules.add(module)
            for candidate in index.get(Path(name).name.lower(), ()):
                if candidate not in kept:
                    kept.add(candidate)
                    queue.append(candidate)
    return kept, modules


def prune(site: Path, target: Target, modules: set[str] | None = None) -> dict[str, int] | None:
    """Drop Qt payload the application does not need; returns removal counters.

    ``modules`` defaults to the configured policy (``[tool.py_upper.optimize].qt``).
    """
    if modules is None:
        modules = configured_modules(target, site)
    if modules is None:
        return None
    pyside = site / "PySide6"
    if not pyside.is_dir():
        return None

    counters = {"files": 0, "dirs": 0, "bytes": 0}

    def size_of(path: Path) -> int:
        if path.is_file() or path.is_symlink():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())

    def drop(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        counters["bytes"] += size_of(path)
        counters["dirs" if path.is_dir() and not path.is_symlink() else "files"] += 1
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    for name in TOOLING_ENTRIES:
        drop(pyside / name)
    drop(pyside / "Qt" / "metatypes")

    wrappers = _wrapper_modules(pyside)
    keep_wrappers = [path for name, path in wrappers.items() if name in modules]
    for name, path in wrappers.items():
        if name not in modules:
            drop(path)

    keep_categories = set(BASE_PLUGIN_CATEGORIES)
    for name in modules:
        keep_categories.update(MODULE_PLUGIN_CATEGORIES.get(name, set()))
    plugins = pyside / "Qt" / "plugins"
    if plugins.is_dir():
        for category in sorted(plugins.iterdir()):
            if category.name not in keep_categories:
                drop(category)

    # A plugin is only usable when every Qt module it links is still present.
    # This drops the virtual-keyboard input context (QtQml/QtQuick) and the PDF
    # image format (QtPdf) without guessing categories by hand.
    wrapper_libraries, allowed_modules = _reachable_libraries(pyside, keep_wrappers)
    keep_plugins: list[Path] = []
    if plugins.is_dir():
        for plugin in sorted(path for path in plugins.rglob("*") if path.is_file()):
            needed = {name for dep in dependency_names(plugin) if (name := _qt_module_of(dep))}
            if needed <= allowed_modules or any(token in plugin.name for token in PLUGIN_KEEP_EXCEPTIONS):
                keep_plugins.append(plugin)
            else:
                drop(plugin)

    plugin_libraries, _ = _reachable_libraries(pyside, keep_plugins)
    reachable = wrapper_libraries | plugin_libraries
    for directory in _library_dirs(pyside):
        for entry in sorted(directory.iterdir()):
            binary = _framework_binary(entry)
            if binary is not None:
                if binary not in reachable:
                    drop(entry)
            elif entry not in reachable:
                drop(entry)

    if not any(name.startswith(QML_MODULE_PREFIXES) for name in modules):
        drop(pyside / "Qt" / "qml")
        # shiboken's QML helper library is only used by the QML wrapper modules.
        for helper in sorted(pyside.glob("libpyside6qml*")):
            drop(helper)
    drop(pyside / "Qt" / "libexec")

    # Only Qt's own widget strings are useful to a running application; tooling
    # and unused-module translations are dead weight.
    translations = pyside / "Qt" / "translations"
    if translations.is_dir():
        for entry in sorted(translations.iterdir()):
            name = entry.name
            keep = name.startswith("qtbase_") or (name.startswith("qt_") and not name.startswith("qt_help_"))
            if not keep or name.startswith(TOOLING_TRANSLATION_PREFIXES):
                drop(entry)

    return counters
