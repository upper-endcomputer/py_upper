from __future__ import annotations
import shutil
from pathlib import Path
from .config import APP, DIST, Target, app_identifier, app_name, project_version, staging_dir, target_runtime_dir
from .native.bundle import bundle_native_dependencies
from .fs import (
    copy_file_contents as _copy_file_contents,
    copy_tree_contents as _copy_tree_contents,
    make_executable as _make_executable,
)


def _write_resource_app_config(path: Path, name: str) -> None:
    if not path.exists():
        return
    path.write_text(f"[app]\nname = {name!r}\n", encoding="utf-8")


ENTRY = '''from main import main\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'''


def package(target: Target, launcher: Path) -> Path:
    stage = staging_dir(target)
    runtime = target_runtime_dir(target)
    name = app_name()
    if not (stage / "site-packages").exists():
        raise RuntimeError("Python stage missing")
    if not runtime.exists():
        raise RuntimeError("Runtime missing")
    if DIST.exists():
        shutil.rmtree(DIST)
    if target.os in {"windows", "linux"}:
        out = DIST / name
        (out / "site-packages").mkdir(parents=True)
        (out / "runtime").mkdir(parents=True)
        (out / "resources").mkdir(parents=True)
        executable = f"{name}.exe" if target.os == "windows" else name
        _copy_file_contents(launcher, out / executable)
        if target.os == "linux":
            _make_executable(out / executable)
        _copy_tree_contents(runtime, out / "runtime")
        _copy_tree_contents(stage / "site-packages", out / "site-packages")
        _copy_tree_contents(APP / "resources", out / "resources")
        _write_resource_app_config(out / "resources" / "config" / "app.toml", name)
        (out / f"{name}.int").write_text(ENTRY, encoding="utf-8")
        bundle_native_dependencies(out, target)
        return out
    if target.os == "macos":
        app = DIST / f"{name}.app"
        contents = app / "Contents"
        macos = contents / "MacOS"
        res = contents / "Resources"
        (res / "runtime").mkdir(parents=True)
        (res / "site-packages").mkdir()
        (res / "resources").mkdir()
        macos.mkdir(parents=True)
        _copy_file_contents(launcher, macos / name)
        _make_executable(macos / name)
        _copy_tree_contents(runtime, res / "runtime")
        _copy_tree_contents(stage / "site-packages", res / "site-packages")
        _copy_tree_contents(APP / "resources", res / "resources")
        _write_resource_app_config(res / "resources" / "config" / "app.toml", name)
        (res / f"{name}.int").write_text(ENTRY, encoding="utf-8")
        (contents / "Info.plist").write_text(
            f'''<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict><key>CFBundleExecutable</key><string>{name}</string><key>CFBundleIdentifier</key><string>{app_identifier()}</string><key>CFBundleName</key><string>{name}</string><key>CFBundleDisplayName</key><string>{name}</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleVersion</key><string>{project_version()}</string><key>CFBundleShortVersionString</key><string>{project_version()}</string></dict></plist>\n''',
            encoding="utf-8",
        )
        bundle_native_dependencies(app, target)
        return app
    raise RuntimeError(f"Unsupported target OS: {target.os}")
