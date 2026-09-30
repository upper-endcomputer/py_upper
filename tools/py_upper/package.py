from __future__ import annotations

import shutil
from pathlib import Path

from .config import APP, DIST, Target, app_identifier, app_name, entry_module, project_version, staging_dir, target_runtime_dir
from .fs import copy_file_contents, copy_tree_contents, make_executable
from .runtime_optimize import optimize_runtime_tree
from .native.bundle import bundle_native_dependencies
from .smoke import smoke_modules

def _entry_script() -> str:
    module = entry_module()
    return f"from {module} import main\n\nif __name__ == \"__main__\":\n    raise SystemExit(main())\n"



def _write_resource_app_config(path: Path, name: str) -> None:
    if path.exists():
        path.write_text(f"[app]\nname = {name!r}\n", encoding="utf-8")


def _mac_info_plist(name: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict><key>CFBundleExecutable</key><string>{name}</string><key>CFBundleIdentifier</key><string>{app_identifier()}</string><key>CFBundleName</key><string>{name}</string><key>CFBundleDisplayName</key><string>{name}</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleVersion</key><string>{project_version()}</string><key>CFBundleShortVersionString</key><string>{project_version()}</string></dict></plist>\n'''


def package(target: Target, launcher: Path) -> Path:
    stage = staging_dir(target)
    runtime = target_runtime_dir(target)
    site = stage / "site-packages"
    if not site.exists():
        raise RuntimeError(f"Python stage missing: {site}")
    if not runtime.exists():
        raise RuntimeError(f"Runtime missing: {runtime}")
    if DIST.exists():
        shutil.rmtree(DIST)

    name = app_name()
    if target.os in {"linux", "windows"}:
        out = DIST / name
        copy_file_contents(launcher, out / (f"{name}.exe" if target.os == "windows" else name))
        if target.os == "linux":
            make_executable(out / name)
        copy_tree_contents(runtime, out / "runtime")
        optimize_runtime_tree(out / "runtime")
        runtime_data = stage / "runtime-data"
        if runtime_data.exists():
            copy_tree_contents(runtime_data, out / "runtime", replace=False)
        copy_tree_contents(site, out / "site-packages")
        copy_tree_contents(APP / "resources", out / "resources")
        _write_resource_app_config(out / "resources" / "config" / "app.toml", name)
        (out / f"{name}.int").write_text(_entry_script(), encoding="utf-8")
        (out / f"{name}.smoke.int").write_text("import importlib\nmodules = " + repr(smoke_modules(target)) + "\nfor name in modules: importlib.import_module(name)\nprint('SMOKE PASS launcher')\n", encoding="utf-8")
        bundle_native_dependencies(out, target)
        return out

    app = DIST / f"{name}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    copy_file_contents(launcher, macos / name)
    make_executable(macos / name)
    copy_tree_contents(runtime, resources / "runtime")
    optimize_runtime_tree(resources / "runtime")
    runtime_data = stage / "runtime-data"
    if runtime_data.exists():
        copy_tree_contents(runtime_data, resources / "runtime", replace=False)
    copy_tree_contents(site, resources / "site-packages")
    copy_tree_contents(APP / "resources", resources / "resources")
    _write_resource_app_config(resources / "config" / "app.toml", name)
    (resources / f"{name}.int").write_text(_entry_script(), encoding="utf-8")
    (resources / f"{name}.smoke.int").write_text("import importlib\nmodules = " + repr(smoke_modules(target)) + "\nfor name in modules: importlib.import_module(name)\nprint('SMOKE PASS launcher')\n", encoding="utf-8")
    (contents / "Info.plist").write_text(_mac_info_plist(name), encoding="utf-8")
    bundle_native_dependencies(app, target)
    return app
