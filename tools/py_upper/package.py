from __future__ import annotations
import os
import shutil
import stat
from pathlib import Path
from .config import APP, DIST, Target, app_identifier, app_name, project_version, staging_dir, target_runtime_dir
from .native.bundle import bundle_native_dependencies


def _write_resource_app_config(path: Path, name: str) -> None:
    if not path.exists():
        return
    path.write_text(f"[app]\nname = {name!r}\n", encoding="utf-8")


def _copy_file_contents(source: Path, destination: Path) -> None:
    """Copy only file bytes; never replay source filesystem metadata.

    A packaged runtime must not inherit read-only modes, ACLs, timestamps,
    extended attributes, or filesystem flags from the PBS extraction tree.
    The launcher is made executable explicitly by the platform-specific
    package path below. Native libraries only need to be readable for dlopen.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)

def _copy_tree_contents(source: Path, destination: Path) -> None:
    """Recursively copy a tree without copying source filesystem metadata."""
    source = Path(source)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(source):
        src = Path(entry.path)
        dst = destination / entry.name
        if entry.is_symlink():
            if dst.exists() or dst.is_symlink():
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            os.symlink(os.readlink(src), dst, target_is_directory=entry.is_dir())
        elif entry.is_dir(follow_symlinks=False):
            _copy_tree_contents(src, dst)
        elif entry.is_file(follow_symlinks=False):
            _copy_file_contents(src, dst)


def _make_executable(path: Path) -> None:
    """Make a packaged launcher executable without copying source metadata."""
    mode = stat.S_IMODE(path.stat().st_mode)
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


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
