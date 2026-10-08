"""Packaging: build the launcher, assemble the bundle and record the release manifest."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import (
    APP, BUILD, DIST, LAUNCHER, STATIC_ENTRY, Target, app_identifier, app_name,
    entry_module, project_version, publish_dir, resolve_target_python, staging_dir,
    target_runtime_dir,
)
from .fs import (
    copy_file_contents, copy_optional_tree, copy_tree_contents, make_executable,
    retire_tree, sha256,
)
from .native.bundle import bundle_native_dependencies, prune_excluded_native_files
from .runtime import optimize_runtime_tree
from .toolchain import resolve_toolchain
from .verify import smoke_modules


def build_launcher(target: Target):
    sdk = resolve_target_python(target)
    tc = resolve_toolchain(target)
    b = BUILD / "launcher" / target.key
    if b.exists():
        shutil.rmtree(b)
    b.mkdir(parents=True)
    env = dict(os.environ)
    env.update(tc.env)
    cmd = [
        "cmake", "-S", str(LAUNCHER), "-B", str(b), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DPYSTAND_PYTHON_INCLUDE={sdk.include_dir}",
        # Compiled into the launcher so a renamed executable still reports the
        # configured application name instead of its own filename.
        f"-DPY_UPPER_APP_NAME={app_name()}",
    ]
    if target.os == "macos":
        cmd += [f"-DCMAKE_OSX_ARCHITECTURES={target.arch}"]
        if tc.deployment_target:
            cmd += [f"-DCMAKE_OSX_DEPLOYMENT_TARGET={tc.deployment_target}"]
    if target.os == "windows":
        if os.environ.get("PY_UPPER_LAUNCHER_CONSOLE") == "1":
            # Debugging escape hatch: a console-subsystem launcher keeps stdout
            # and stderr attached unconditionally, at the cost of a console
            # window for every user.
            cmd += ["-DPY_UPPER_LAUNCHER_CONSOLE=ON"]
        cmd += [f"-DPY_UPPER_RESOURCE_RC={_windows_version_resource(b)}"]
    subprocess.run(cmd, check=True, env=env)
    subprocess.run(["cmake", "--build", str(b), "--config", "Release"], check=True, env=env)
    exe = b / ("PyUpper.exe" if target.os == "windows" else "PyUpper")
    if not exe.exists() and (b / "Release" / exe.name).exists():
        exe = b / "Release" / exe.name
    if not exe.exists():
        raise RuntimeError(f"Launcher build did not produce {exe}")
    return exe


def _entry_script() -> str:
    module = entry_module()
    return f"from {module} import main\n\nif __name__ == \"__main__\":\n    raise SystemExit(main())\n"


def _smoke_script(target: Target) -> str:
    return "import importlib\nmodules = " + repr(smoke_modules(target)) + "\nfor name in modules: importlib.import_module(name)\nprint('SMOKE PASS launcher')\n"


def write_entry_scripts(root: Path, name: str, target: Target) -> None:
    """Write the application entry scripts next to the packaged launcher.

    Both names carry the same generated entry: the static one survives renaming
    the executable, the executable-derived one is the layout the rest of the
    tooling and hand-made bundles expect.
    """
    entry = _entry_script()
    (root / f"{name}.int").write_text(entry, encoding="utf-8")
    (root / STATIC_ENTRY).write_text(entry, encoding="utf-8")
    (root / f"{name}.smoke.int").write_text(_smoke_script(target), encoding="utf-8")


def _rc_literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _windows_version_resource(directory: Path) -> Path:
    """Generate the Windows version resource for the launcher.

    Without it a packaged executable has no product name, no version and no
    description in the file properties, which makes a finished application look
    like an anonymous binary.
    """
    name = app_name()
    version = project_version()
    numbers = [int(part) for part in re.findall(r"\d+", version)][:4]
    numbers += [0] * (4 - len(numbers))
    numeric = ",".join(str(value) for value in numbers)
    script = f"""#include <winver.h>

VS_VERSION_INFO VERSIONINFO
 FILEVERSION {numeric}
 PRODUCTVERSION {numeric}
 FILEFLAGSMASK 0x3fL
 FILEFLAGS 0x0L
 FILEOS 0x40004L
 FILETYPE 0x1L
 FILESUBTYPE 0x0L
BEGIN
    BLOCK "StringFileInfo"
    BEGIN
        BLOCK "040904b0"
        BEGIN
            VALUE "FileDescription", {_rc_literal(name)}
            VALUE "FileVersion", {_rc_literal(version)}
            VALUE "InternalName", {_rc_literal(name)}
            VALUE "OriginalFilename", {_rc_literal(name + ".exe")}
            VALUE "ProductName", {_rc_literal(name)}
            VALUE "ProductVersion", {_rc_literal(version)}
        END
    END
    BLOCK "VarFileInfo"
    BEGIN
        VALUE "Translation", 0x409, 1200
    END
END
"""
    path = directory / "py_upper_resource.rc"
    # rc.exe reads a resource script with the ANSI code page unless the file is
    # marked, so a UTF-8 byte order mark is what keeps a non-ASCII product name
    # intact. A plain ASCII script is unaffected by the mark.
    path.write_text(script, encoding="utf-8-sig")
    return path

def _mac_info_plist(name: str) -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict><key>CFBundleExecutable</key><string>{name}</string><key>CFBundleIdentifier</key><string>{app_identifier()}</string><key>CFBundleName</key><string>{name}</string><key>CFBundleDisplayName</key><string>{name}</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleVersion</key><string>{project_version()}</string><key>CFBundleShortVersionString</key><string>{project_version()}</string></dict></plist>\n'''


def _report_pruned(site: Path) -> None:
    removed = prune_excluded_native_files(site)
    if removed:
        print(f"Native exclusions: removed {len(removed)} file(s) matched by [tool.py_upper.native].exclude")


def _publish(assembly: Path, source: Path, destination: Path) -> Path:
    """Move a finished bundle to its published path in a single rename.

    ``dist/`` is the path the editor watches: it resolves every ``python`` under
    the workspace by running it, so a bundle assembled there is mutated by a
    foreign process while the build is still writing it, and the cleanup fails
    on the entry that writer recreated. Assembling elsewhere and renaming at the
    end keeps the published path either absent or complete.
    """
    retire_tree(destination, trash=BUILD / "retired")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    retire_tree(assembly, trash=BUILD / "retired")
    return destination


def package(target: Target, launcher: Path) -> Path:
    stage = staging_dir(target)
    runtime = target_runtime_dir(target)
    site = stage / "site-packages"
    if not site.exists():
        raise RuntimeError(f"Python stage missing: {site}")
    if not runtime.exists():
        raise RuntimeError(f"Runtime missing: {runtime}")

    name = app_name()
    assembly = publish_dir(target)
    retire_tree(assembly, trash=BUILD / "retired")
    assembly.mkdir(parents=True)
    if target.os in {"linux", "windows"}:
        out = assembly / name
        copy_file_contents(launcher, out / (f"{name}.exe" if target.os == "windows" else name))
        if target.os == "linux":
            make_executable(out / name)
        copy_tree_contents(runtime, out / "runtime")
        optimize_runtime_tree(out / "runtime")
        runtime_data = stage / "runtime-data"
        if runtime_data.exists():
            copy_tree_contents(runtime_data, out / "runtime", replace=False)
        copy_tree_contents(site, out / "site-packages")
        _report_pruned(out / "site-packages")
        # Resources are optional: a project without app/resources simply ships none.
        copy_optional_tree(APP / "resources", out / "resources")
        write_entry_scripts(out, name, target)
        bundle_native_dependencies(out, target)
        return _publish(assembly, out, DIST / name)

    app = assembly / f"{name}.app"
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
    _report_pruned(resources / "site-packages")
    # Resources are optional: a project without app/resources simply ships none.
    copy_optional_tree(APP / "resources", resources / "resources")
    write_entry_scripts(resources, name, target)
    (contents / "Info.plist").write_text(_mac_info_plist(name), encoding="utf-8")
    bundle_native_dependencies(app, target)
    return _publish(assembly, app, DIST / f"{name}.app")


def write_release_manifest(target: Target, output: Path | None = None) -> Path:
    root = output or DIST
    if not root.exists():
        raise RuntimeError(f"Release output does not exist: {root}")
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "release-manifest.json"):
        files.append({
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        })
    data = {
        "format": 1,
        "project": "py_upper",
        "version": project_version(),
        "target": target.key,
        "target_triple": target.triple,
        "files": files,
    }
    path = root / "release-manifest.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
