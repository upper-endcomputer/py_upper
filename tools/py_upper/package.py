"""Packaging: build the launcher, assemble the bundle and record the release manifest."""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import (
    APP, BUILD, DIST, LAUNCHER, Target, app_identifier, app_name, entry_source,
    project_version, publish_dir, resolve_target_python, staging_dir, target_runtime_dir,
)
from .fs import (
    copy_file_contents, copy_optional_tree, copy_tree_contents, make_executable,
    retire_tree, sha256,
)
from .native.bundle import bundle_native_dependencies, prune_excluded_native_files
from .runtime import optimize_runtime_tree
from .toolchain import resolve_toolchain
from .verify import smoke_modules

# CMake spells the Windows processor architecture the way the MSVC toolchain
# does; the packaging targets use the Python platform tag vocabulary instead.
CMAKE_WINDOWS_PROCESSOR = {"x86": "x86", "x86_64": "AMD64", "arm64": "ARM64"}

# The generated translation unit that carries the entry programs, and the C++
# raw string delimiter it wraps them in. The delimiter is chosen to be absent
# from the payload; both are validated together in _embedded_literal().
EMBEDDED_SOURCE_FILE = "py_upper_embedded.cpp"
RAW_STRING_DELIMITER = "pyupper"


def _cmake_path(path: Path) -> str:
    r"""A path spelled the way a ``-D`` value has to be for CMake.

    CMake copies some ``-D`` values verbatim into the cache-reload scripts it
    generates. ``CMAKE_RC_COMPILER`` lands in ``CMakeRCCompiler.cmake`` as
    ``set(CMAKE_RC_COMPILER "...")``, and a Windows path with backslashes is
    then read back as a CMake string literal: ``D:\XY_Project\...`` is the
    invalid escape ``\X``, so configuration dies with "Invalid character
    escape" before the resource compiler is ever invoked. The compiler path
    CMake finds for itself is normalized; a pre-set one is not. Forward slashes
    are the spelling the CMake language accepts on every host.
    """
    return path.as_posix()


def build_launcher(target: Target):
    sdk = resolve_target_python(target)
    tc = resolve_toolchain(target)
    b = BUILD / "launcher" / target.key
    if b.exists():
        shutil.rmtree(b)
    b.mkdir(parents=True)
    embedded = write_embedded_source(b, target)
    env = dict(os.environ)
    env.update(tc.env)
    cmd = [
        "cmake", "-S", str(LAUNCHER), "-B", str(b), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DPYSTAND_PYTHON_INCLUDE={_cmake_path(sdk.include_dir)}",
        # Compiled into the launcher so a renamed executable still reports the
        # configured application name instead of its own filename.
        f"-DPY_UPPER_APP_NAME={app_name()}",
        # The entry programs ride in the launcher, so a packaged bundle needs no
        # script next to the executable.
        f"-DPY_UPPER_EMBEDDED_SOURCE={_cmake_path(embedded)}",
    ]
    if target.os == "windows" and tc.compiler_args:
        # CMake needs to invoke a single executable. Zig's compiler frontend is
        # reached through a wrapper because the compiler itself needs target
        # arguments appended to every invocation.
        if platform.system() == "Windows":
            for name, arg in (("CC", "cc"), ("CXX", "c++")):
                wrapper = b / f"zig-{arg}.cmd"
                frontend = [tc.compiler, "c++" if arg == "c++" else arg, *tc.compiler_args[1:]]
                wrapper.write_text("@echo off\n" + subprocess.list2cmdline(frontend) + " %*\n", encoding="utf-8")
                env[name] = str(wrapper)
        else:
            for name, arg in (("CC", "cc"), ("CXX", "c++")):
                wrapper = b / f"zig-{arg}.sh"
                frontend = [tc.compiler, arg, *tc.compiler_args[1:]]
                wrapper.write_text("#!/bin/sh\nexec " + shlex.join(frontend) + ' "$@"\n', encoding="utf-8")
                make_executable(wrapper)
                env[name] = str(wrapper)
        rc = b / ("zig-rc.cmd" if platform.system() == "Windows" else "zig-rc.sh")
        rc.write_text(("@echo off\n" + subprocess.list2cmdline([tc.compiler, "rc"]) + " %*\n") if platform.system() == "Windows" else ("#!/bin/sh\nexec " + shlex.join([tc.compiler, "rc"]) + ' "$@"\n'), encoding="utf-8")
        if platform.system() != "Windows":
            make_executable(rc)
        # Declaring the cross build keeps CMake from applying Darwin host
        # defaults (notably -arch) to a Windows compiler.
        cmd += [
            "-DCMAKE_SYSTEM_NAME=Windows",
            f"-DCMAKE_SYSTEM_PROCESSOR={CMAKE_WINDOWS_PROCESSOR[target.arch]}",
            f"-DCMAKE_RC_COMPILER={_cmake_path(rc)}",
            # MinGW uses the ANSI WinMain entry unless the Unicode entry point
            # is explicitly selected; MSVC infers it from WIN32_EXECUTABLE.
            # CMake's compiler probe builds a main() executable, which is the
            # wrong entry for MinGW's Unicode launcher; compile-only probing
            # keeps the real launcher's wWinMain link intact.
            "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY",
            "-DCMAKE_CXX_FLAGS=-municode",
            # The GUI subsystem itself is requested by the launcher's
            # CMakeLists, which cannot rely on WIN32_EXECUTABLE alone.
            "-DCMAKE_EXE_LINKER_FLAGS=-municode",
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
        cmd += [f"-DPY_UPPER_RESOURCE_RC={_cmake_path(_windows_version_resource(b))}"]
    subprocess.run(cmd, check=True, env=env)
    subprocess.run(["cmake", "--build", str(b), "--config", "Release"], check=True, env=env)
    exe = b / ("PyUpper.exe" if target.os == "windows" else "PyUpper")
    if not exe.exists() and (b / "Release" / exe.name).exists():
        exe = b / "Release" / exe.name
    if not exe.exists():
        raise RuntimeError(f"Launcher build did not produce {exe}")
    return exe


def smoke_source(target: Target) -> str:
    """The program the launcher runs under ``PY_UPPER_SMOKE=1``.

    It imports the application entry plus every directly declared third-party
    distribution, which is what catches a payload that a pruning pass or a wheel
    layout broke — the reason it exists at all.
    """
    return "import importlib\nmodules = " + repr(smoke_modules(target)) + "\nfor name in modules: importlib.import_module(name)\nprint('SMOKE PASS launcher')\n"


def _embedded_literal(source: str, what: str) -> str:
    """Quote a program as a C++ raw string literal, or refuse to.

    The delimiter is a fixed choice rather than a search for a safe one: a
    payload that contains it is a bug in this generator, and silently switching
    delimiters would hide it. A triple double quote must not appear either,
    because the launcher's bootstrap wraps the same text in a Python literal
    delimited by exactly that.
    """
    if f'){RAW_STRING_DELIMITER}"' in source:
        raise RuntimeError(f"{what} contains the C++ raw string delimiter ){RAW_STRING_DELIMITER}\"")
    if '"""' in source:
        raise RuntimeError(f'{what} contains a triple quote and cannot be embedded in the bootstrap')
    return f'R"{RAW_STRING_DELIMITER}({source}){RAW_STRING_DELIMITER}"'


def write_embedded_source(directory: Path, target: Target) -> Path:
    """Write the translation unit that carries the entry programs.

    A packaged launcher runs its application without a sidecar: the bundle used
    to ship the entry twice under two names plus the smoke program, which is
    build-verification data no user installs. Compiling them into the launcher
    leaves one home for the entry, removes the "sidecar deleted or edited"
    failure mode, and makes renaming the executable work without a second copy
    of the same file.
    """
    entry = _embedded_literal(entry_source(), "the application entry")
    smoke = _embedded_literal(smoke_source(target), "the launcher smoke program")
    path = directory / EMBEDDED_SOURCE_FILE
    path.write_text(
        "// Generated by tools/py_upper at packaging time: the programs the\n"
        "// launcher runs, so a packaged bundle ships no .int sidecar. Declared\n"
        "// extern so the definitions keep external linkage.\n"
        "namespace py_upper_embedded {\n"
        "extern const char* const kEntry;\n"
        f"const char* const kEntry = {entry};\n"
        "extern const char* const kSmoke;\n"
        f"const char* const kSmoke = {smoke};\n"
        "}\n",
        encoding="utf-8",
    )
    return path


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
