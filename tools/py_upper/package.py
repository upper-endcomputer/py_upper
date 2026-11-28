"""Packaging: build the launcher, assemble the bundle and record the release manifest."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import (
    APP, BUILD, DIST, LAUNCHER, Target, app_identifier, app_name, entry_source, host_target,
    project_version, publish_dir, resolve_target_python, runtime_executable, staging_dir, target_runtime_dir,
)
from .egg import EGG_DIR, EGG_NAME, egg_loader_source
from .fs import (
    _housekeeping, copy_file_contents, copy_optional_tree, copy_tree_contents, make_executable,
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


def _write_changed(path, text, encoding="utf-8"):
    data = text.encode(encoding)
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)


def build_launcher(target: Target, egg=None):
    sdk = resolve_target_python(target)
    tc = resolve_toolchain(target)
    b = BUILD / "launcher" / target.key
    signature = json.dumps({"toolchain": vars(tc),
                            "sdk": str(sdk.include_dir),
                            "console": os.environ.get("PY_UPPER_LAUNCHER_CONSOLE", "0")}, sort_keys=True, default=str)
    signature = hashlib.sha256(signature.encode()).hexdigest()
    stamp = b / "toolchain.sha256"
    if b.exists() and (not stamp.exists() or stamp.read_text() != signature):
        shutil.rmtree(b)
    b.mkdir(parents=True, exist_ok=True)
    stamp.write_text(signature)
    embedded = write_embedded_source(b, target, egg)
    env = dict(os.environ)
    env.update(tc.env)
    cmd = [
        "cmake", "-S", str(LAUNCHER), "-B", str(b), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DPYSTAND_PYTHON_INCLUDE={_cmake_path(sdk.include_dir)}",
        # Compiled into the launcher so a renamed executable still reports the
        # configured application name instead of its own filename.
        f"-DPY_UPPER_APP_NAME={app_name()}",
        f"-DPY_UPPER_CACHE_NAMESPACE={hashlib.sha256(app_identifier().encode()).hexdigest()[:32]}",
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


def masked_key_arrays(key_hex: str) -> tuple:
    """Split an egg key into a masked byte array and its mask.

    The key has to be in the binary for the loader to decrypt anything, so it
    cannot be hidden. What it must not be is *readable*: a plain hexadecimal
    string is found by ``strings launcher | grep``, which turns "the code is
    encrypted" into a two-second exercise for anyone who tries. Storing
    ``key ^ mask`` and the mask as numeric arrays removes that shortcut.

    The mask is derived from the key itself, so a rebuild of the same key
    produces the same binary and release manifests stay reproducible.
    """
    key = bytes.fromhex(str(key_hex).strip())
    mask = hashlib.shake_256(key + b"py_upper-key-mask").digest(len(key))
    masked = bytes(left ^ right for left, right in zip(key, mask))
    return masked, mask


def _byte_array(values: bytes) -> str:
    return "{" + ", ".join(f"0x{value:02x}" for value in values) + "}"


def write_embedded_source(directory: Path, target: Target, egg=None) -> Path:
    """Write the translation unit that carries the entry programs.

    A packaged launcher runs its application without a sidecar: the bundle used
    to ship the entry twice under two names plus the smoke program, which is
    build-verification data no user installs. Compiling them into the launcher
    leaves one home for the entry, removes the "sidecar deleted or edited"
    failure mode, and makes renaming the executable work without a second copy
    of the same file.

    When the encrypted egg is enabled, the loader and the key are compiled in
    the same way. The key is obfuscation material, not a secret: it has to be
    present in the binary for the loader to decrypt anything at all. It is
    therefore stored masked (see masked_key_arrays) so the shipped binary does
    not contain the key as a readable hexadecimal string.
    """
    entry = _embedded_literal(entry_source(), "the application entry")
    smoke = _embedded_literal(smoke_source(target), "the launcher smoke program")
    if egg is not None and egg.enabled:
        loader = _embedded_literal(egg_loader_source(), "the egg loader")
        loader_definition = f"const char* const kEggLoader = {loader};\n"
        masked, mask = masked_key_arrays(egg.key_hex)
        key_definition = (
            f"const unsigned char kEggKeyMasked[] = {_byte_array(masked)};\n"
            f"const unsigned char kEggKeyMask[] = {_byte_array(mask)};\n"
            f"const unsigned int kEggKeyLength = {len(masked)}u;\n"
        )
        backend_definition = f"const char* const kEggBackend = {_rc_literal(egg.backend)};\n"
    else:
        loader_definition = "const char* const kEggLoader = nullptr;\n"
        key_definition = (
            "const unsigned char kEggKeyMasked[] = {0};\n"
            "const unsigned char kEggKeyMask[] = {0};\n"
            "const unsigned int kEggKeyLength = 0u;\n"
        )
        backend_definition = 'const char* const kEggBackend = "auto";\n'
    path = directory / EMBEDDED_SOURCE_FILE
    _write_changed(path,
        "// Generated by tools/py_upper at packaging time: the programs the\n"
        "// launcher runs, so a packaged bundle ships no .int sidecar. Declared\n"
        "// extern so the definitions keep external linkage.\n"
        "namespace py_upper_embedded {\n"
        "extern const char* const kEntry;\n"
        f"const char* const kEntry = {entry};\n"
        "extern const char* const kSmoke;\n"
        f"const char* const kSmoke = {smoke};\n"
        "extern const char* const kEggLoader;\n"
        + loader_definition +
        "extern const unsigned char kEggKeyMasked[];\n"
        "extern const unsigned char kEggKeyMask[];\n"
        "extern const unsigned int kEggKeyLength;\n"
        + key_definition +
        "extern const char* const kEggBackend;\n"
        + backend_definition +
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
    _write_changed(path, script, encoding="utf-8-sig")
    return path

def _mac_info_plist(name: str) -> str:
    return plistlib.dumps({
        "CFBundleExecutable": name, "CFBundleIdentifier": app_identifier(),
        "CFBundleName": name, "CFBundleDisplayName": name,
        "CFBundlePackageType": "APPL", "CFBundleVersion": project_version(),
        "CFBundleShortVersionString": project_version(),
    }).decode("utf-8")


def _report_pruned(site: Path) -> None:
    removed = prune_excluded_native_files(site)
    if removed:
        print(f"Native exclusions: removed {len(removed)} file(s) matched by [tool.py_upper.native].exclude")


def _optimize_runtime(path: Path, trim=None) -> dict[str, int]:
    """Clean a copied runtime, with trim rules when the build supplied any."""
    if trim is None:
        return optimize_runtime_tree(path)
    return optimize_runtime_tree(path, trim)


def _publish(assembly: Path, source: Path, destination: Path) -> Path:
    """Move a finished bundle to its published path in a single rename.

    ``dist/`` is the path the editor watches: it resolves every ``python`` under
    the workspace by running it, so a bundle assembled there is mutated by a
    foreign process while the build is still writing it, and the cleanup fails
    on the entry that writer recreated. Assembling elsewhere and renaming at the
    end keeps the published path either absent or complete.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous = assembly / f".previous-{destination.name}"
    had_previous = destination.exists()
    if had_previous:
        if not destination.is_dir() or destination.is_symlink():
            raise RuntimeError(f"Not a directory tree: {destination}")
        _housekeeping(lambda: os.replace(destination, previous), path=destination)
    try:
        os.replace(source, destination)
    except OSError:
        if had_previous:
            _housekeeping(lambda: os.replace(previous, destination), path=previous)
        raise
    # Delete the previous bundle only after the new one has been published.
    retire_tree(assembly, trash=BUILD / "retired")
    return destination


def _precompile_linux_stdlib(bundle: Path, target: Target) -> None:
    """Build target bytecode before publication; the launcher stays read-only.

    Only native Linux targets can run their interpreter here. Checked hashes
    keep caches valid after relocation and detect edited sources; -d removes
    the private assembly path from tracebacks.
    """
    if target.os != "linux" or target != host_target():
        return
    executable = runtime_executable(target)
    for stdlib in sorted((bundle / "runtime" / "lib").glob("python*")):
        if not stdlib.is_dir():
            continue
        subprocess.run([
            str(executable), "-I", "-m", "compileall", "-q", "-f",
            "--invalidation-mode", "checked-hash", "-d",
            stdlib.relative_to(bundle).as_posix(), str(stdlib),
        ], check=True)


def package(target: Target, launcher: Path, trim=None, *, publish=True) -> Path:
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
        _optimize_runtime(out / "runtime", trim)
        runtime_data = stage / "runtime-data"
        if runtime_data.exists():
            copy_tree_contents(runtime_data, out / "runtime", replace=False)
        copy_tree_contents(site, out / "site-packages")
        stage_egg = stage / EGG_DIR / EGG_NAME
        if stage_egg.is_file():
            copy_file_contents(stage_egg, out / EGG_DIR / EGG_NAME)
        _report_pruned(out / "site-packages")
        # Resources are optional: a project without app/resources simply ships none.
        copy_optional_tree(APP / "resources", out / "resources")
        bundle_native_dependencies(out, target)
        _precompile_linux_stdlib(out, target)
        return _publish(assembly, out, DIST / name) if publish else out

    app = assembly / f"{name}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    copy_file_contents(launcher, macos / name)
    make_executable(macos / name)
    copy_tree_contents(runtime, resources / "runtime")
    _optimize_runtime(resources / "runtime", trim)
    runtime_data = stage / "runtime-data"
    if runtime_data.exists():
        copy_tree_contents(runtime_data, resources / "runtime", replace=False)
    copy_tree_contents(site, resources / "site-packages")
    stage_egg = stage / EGG_DIR / EGG_NAME
    if stage_egg.is_file():
        copy_file_contents(stage_egg, resources / EGG_DIR / EGG_NAME)
    _report_pruned(resources / "site-packages")
    # Resources are optional: a project without app/resources simply ships none.
    copy_optional_tree(APP / "resources", resources / "resources")
    (contents / "Info.plist").write_text(_mac_info_plist(name), encoding="utf-8")
    bundle_native_dependencies(app, target)
    return _publish(assembly, app, DIST / f"{name}.app") if publish else app


def write_release_manifest(target: Target, output: Path | None = None) -> Path:
    root = output or (DIST / (f"{app_name()}.app" if target.os == "macos" else app_name()))
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
    path = root / "release-manifest.json" if output is not None else root.parent / (root.name + "-release-manifest.json")
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
