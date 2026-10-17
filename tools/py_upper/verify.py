"""Verification: static package checks plus the target-runtime and launcher smoke tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .config import (
    APP, BUILD, DIST, Target, app_name, entry_module, entry_source, host_target,
    optimize_config, runtime_executable, runtime_spec, staging_dir,
    target_extension_suffix, target_runtime_dir,
)
from .egg import (
    EGG_DIR, EGG_NAME, egg_config, egg_loader_source, read_egg_members, read_egg_meta,
)
from .egg_loader import MAGIC
from .manifest import read_manifest, validate_manifest
from .native.inspect import (
    PE_SUBSYSTEM, PE_SUBSYSTEM_CONSOLE, PE_SUBSYSTEM_GUI, inspect,
    target_format, verify_arch,
)
from .python_build import module_name, selected_sources
from .runtime import runtime_pip_payloads
from .third_party import WHEEL_MANIFEST, dependency_specs, wheel_dir


def _expected_extension(site: Path, source: Path, target: Target) -> Path:
    spec = runtime_spec(target)
    suffix = target_extension_suffix(target, spec.major_minor, spec.abi_tag)
    rel = source.relative_to(APP / "src").with_suffix("")
    return site.joinpath(*rel.parts[:-1], rel.name + suffix)


def _stage_egg_config(stage) -> "EggConfig":
    """The egg configuration a stage was actually built with.

    The stage record wins because --egg-key and --egg-backend need not match
    the project file. Without a record, a stage that carries an archive is
    still read with the project defaults, while a stage without one is a
    --no-egg build even when the project enables the egg: an absent archive is
    the evidence, and requiring one there would make verify reject a valid
    loose build.
    """
    egg = read_egg_meta(stage)
    if egg is not None:
        return egg
    return egg_config(enabled=(stage / EGG_DIR / EGG_NAME).is_file())


def _checks_for_stage(target: Target) -> list[tuple[bool, str]]:
    stage = staging_dir(target)
    site = stage / "site-packages"
    checks: list[tuple[bool, str]] = [
        (stage.is_dir(), "staging"),
        (site.is_dir(), "application site-packages"),
    ]
    try:
        manifest = read_manifest(runtime_spec(target).root)
        validate_manifest(manifest, target, runtime_spec(target))
        checks.append((True, f"runtime manifest format={manifest['format']}"))
    except Exception as exc:
        checks.append((False, f"runtime manifest: {exc}"))

    sources = selected_sources()
    selected_modules = {module_name(source) for source in sources}
    checks.append((entry_module() in selected_modules, f"entry module Cythonized: {entry_module()}"))

    egg = _stage_egg_config(stage)
    egg_members = None
    if egg.enabled:
        egg_path = stage / EGG_DIR / EGG_NAME
        checks.append((egg_path.is_file(), f"encrypted egg {EGG_NAME}"))
        if egg_path.is_file():
            try:
                checks.append((egg_path.read_bytes()[:len(MAGIC)] == MAGIC, "encrypted egg magic"))
                egg_members = set(read_egg_members(egg_path, egg.key_hex))
                checks.append((bool(egg_members), f"encrypted egg members={len(egg_members)}"))
            except Exception as exc:
                checks.append((False, f"encrypted egg members: {exc}"))
        if target.os == "windows" and egg.backend == "memory":
            checks.append(((site / "_memimporter.pyd").is_file(),
                           "egg memory backend helper _memimporter"))

    for source in sources:
        expected = _expected_extension(site, source, target)
        name = module_name(source)
        if egg_members is None:
            checks.append((expected.is_file(), f"extension {name}"))
            if expected.is_file():
                try:
                    verify_arch(expected, target)
                    checks.append((True, f"arch {name}={target.arch}"))
                except Exception as exc:
                    checks.append((False, f"arch {name}: {exc}"))
        else:
            member = expected.relative_to(site).as_posix()
            checks.append((member in egg_members, f"egg extension {name}"))
            checks.append((not expected.is_file(), f"extension not loose {name}"))
        checks.append((not (site / source.relative_to(APP / "src")).exists(), f"source removed {name}"))

    leftovers = [path for path in site.rglob("*") if path.is_file() and path.suffix.lower() in {".c", ".cpp", ".pyc", ".pyo"}]
    checks.append((not leftovers, "no generated source/cache files in site-packages"))

    if dependency_specs():
        manifest_path = wheel_dir(target) / WHEEL_MANIFEST
        checks.append((manifest_path.is_file(), "third-party wheel manifest"))
        if manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                checks.append((data.get("requirements") == dependency_specs(), "third-party requirements match"))
                imports = data.get("imports", [])
                checks.append((bool(imports), "third-party direct imports recorded"))
            except Exception as exc:
                checks.append((False, f"third-party manifest: {exc}"))
    return checks


def _package_root(target: Target) -> Path:
    name = app_name()
    return DIST / f"{name}.app" if target.os == "macos" else DIST / name


def _launcher_subsystem_check(executable: Path) -> tuple[bool, str]:
    """Assert the launcher is the subsystem Windows will start it as.

    A GUI launcher is the shipped layout, but its subsystem is only implied by
    the build: CMake's WIN32_EXECUTABLE relies on -mwindows, which MSVC honours
    and Zig's clang frontend silently ignores, so a cross-built launcher used to
    come out as a console image that flashes a console window on every start.
    PY_UPPER_LAUNCHER_CONSOLE=1 is the documented debugging escape hatch that
    builds the console variant on purpose, and is the only case console is
    expected.
    """
    expected = PE_SUBSYSTEM_CONSOLE if os.environ.get("PY_UPPER_LAUNCHER_CONSOLE") == "1" else PE_SUBSYSTEM_GUI
    try:
        subsystem = inspect(executable).subsystem
    except Exception as exc:
        return False, f"launcher subsystem: {exc}"
    if subsystem == expected:
        return True, f"launcher subsystem={PE_SUBSYSTEM[expected]}"
    actual = PE_SUBSYSTEM.get(subsystem, subsystem)
    return False, f"launcher subsystem: {actual}, expected {PE_SUBSYSTEM[expected]}"


def _checks_for_package(target: Target) -> list[tuple[bool, str]]:
    root = _package_root(target)
    name = app_name()
    if target.os == "macos":
        executable = root / "Contents" / "MacOS" / name
        resources = root / "Contents" / "Resources"
        site = resources / "site-packages"
        entry_dir = resources
    else:
        executable = root / (f"{name}.exe" if target.os == "windows" else name)
        site = root / "site-packages"
        entry_dir = root
    checks: list[tuple[bool, str]] = [
        (root.is_dir(), "package"),
        (executable.is_file(), "launcher"),
    ]

    # The launcher carries the entry programs, so the bundle ships no script
    # next to the executable at all. A leftover .int is either the old layout or
    # a stray hand-written entry, and the launcher would not read it either way:
    # saying so here is what keeps the package from growing a second, unread
    # copy of the entry.
    sidecars = sorted(path.relative_to(root).as_posix() for path in entry_dir.glob("*.int"))
    checks.append((not sidecars, "no entry sidecar next to the launcher" + (f": {', '.join(sidecars)}" if sidecars else "")))
    if executable.is_file():
        # The entry is the one thing the launcher cannot get from anywhere else,
        # and the packaging step compiles it in: reading the bytes back out
        # proves the binary that was built carries the entry the configuration
        # names, which a file-based check silently stopped doing the moment the
        # two sides could disagree.
        embedded = entry_source().encode("utf-8")
        checks.append((embedded in executable.read_bytes(), f"embedded entry {entry_module()}"))

    # The package is the contract: an egg that is present must be readable with
    # the key of the build that produced it. Stage verification is what proves
    # an enabled project actually produced one, and package() copies it
    # unconditionally, so an absent archive here means a loose build.
    egg_path = entry_dir / EGG_DIR / EGG_NAME
    egg_members = None
    if egg_path.is_file():
        egg = read_egg_meta(staging_dir(target)) or egg_config()
        try:
            checks.append((egg_path.read_bytes()[:len(MAGIC)] == MAGIC, "packaged egg magic"))
            egg_members = set(read_egg_members(egg_path, egg.key_hex))
            checks.append((bool(egg_members), f"packaged egg members={len(egg_members)}"))
        except Exception as exc:
            checks.append((False, f"packaged egg members: {exc}"))
        if executable.is_file():
            # The archive is useless without the loader, and the loader is only
            # reachable when it was compiled into this launcher.
            loader = egg_loader_source().encode("utf-8")
            checks.append((loader in executable.read_bytes(), "embedded egg loader"))
    runtime_root = resources / "runtime" if target.os == "macos" else root / "runtime"
    if bool(optimize_config().get("remove_runtime_pip", True)):
        checks.append((not runtime_pip_payloads(runtime_root), "runtime pip/ensurepip removed"))
    if target.os == "windows" and runtime_spec(target).provider == "pbs":
        digits = runtime_spec(target).major_minor.replace(".", "")
        for filename in ("python3.dll", f"python{digits}.dll", f"python{digits}.zip", f"python{digits}._pth"):
            checks.append(((runtime_root / filename).is_file(), f"Windows embed runtime {filename}"))
        checks.append((not (runtime_root / "Lib").exists() and not (runtime_root / "DLLs").exists(),
                       "Windows embed runtime is flat"))
    if executable.is_file():
        try:
            verify_arch(executable, target)
            checks.append((True, f"launcher arch={target.arch}"))
        except Exception as exc:
            checks.append((False, f"launcher arch: {exc}"))
        if target.os == "windows":
            checks.append(_launcher_subsystem_check(executable))

    for source in selected_sources():
        expected = _expected_extension(site, source, target)
        name_ = module_name(source)
        if egg_members is None:
            checks.append((expected.is_file(), f"packaged extension {name_}"))
        else:
            member = expected.relative_to(site).as_posix()
            checks.append((member in egg_members, f"packaged egg extension {name_}"))
            checks.append((not expected.is_file(), f"packaged extension not loose {name_}"))
        checks.append((not (site / source.relative_to(APP / "src")).exists(), f"packaged source removed {name_}"))

    # Only binaries the target OS can actually load are part of the native ABI
    # contract. Bundled runtimes and third-party packages ship inert
    # foreign-format data files (for example the Windows setuptools launcher
    # stubs inside a macOS CPython runtime); their architecture is irrelevant
    # and must not fail an otherwise correct package.
    #
    # The same reasoning covers Windows executables. Nothing loads a .exe, the
    # OS starts it as its own process, and the only process this bundle starts
    # is the launcher, which is checked on its own above. Runtimes and wheels
    # deliberately ship the launcher stubs of every architecture side by side
    # (setuptools' easy_install cli-*.exe, distlib's t64.exe, the venv
    # templates), so their architecture is data, not contract.
    target_container = target_format(target)
    checked = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() == ".exe":
            continue
        try:
            info = inspect(path)
        except (OSError, ValueError):
            continue
        if not info.format.startswith(target_container):
            continue
        checked += 1
        try:
            verify_arch(path, target)
        except Exception as exc:
            checks.append((False, f"native arch {path.relative_to(root)}: {exc}"))
    checks.append((True, f"native binaries checked={checked}"))
    return checks


def verify(target: Target) -> int:
    checks = _checks_for_stage(target) + _checks_for_package(target)
    failures = 0
    for ok, label in checks:
        print("PASS" if ok else "FAIL", label)
        failures += int(not ok)
    return int(bool(failures))


def runtime_python(target: Target) -> Path:
    return runtime_executable(target)


def direct_dependency_imports(target: Target) -> list[str]:
    path = wheel_dir(target) / WHEEL_MANIFEST
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    values = data.get("imports", [])
    return sorted({str(value).strip() for value in values if str(value).strip()}) if isinstance(values, list) else []


def smoke_modules(target: Target) -> list[str]:
    # Import the application entry point and every directly declared third-party
    # distribution. Imported application modules are exercised transitively by
    # the entry point; importing every Cython module independently would turn
    # optional/lazy application modules into false build failures.
    values = [entry_module()] + direct_dependency_imports(target)
    return sorted({value for value in values if value})


def write_smoke_script(target: Target, destination: Path) -> Path:
    modules = smoke_modules(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # The target interpreter is started with the stage on PYTHONPATH, not
    # through the launcher, so it has to install the egg itself before the
    # imports below. The loader is plain ASCII and safe to inline.
    egg_path = staging_dir(target) / EGG_DIR / EGG_NAME
    prelude = ""
    if egg_path.is_file():
        egg = read_egg_meta(staging_dir(target)) or egg_config(enabled=True)
        prelude = (
            egg_loader_source() + "\n\n"
            "install(" + repr(str(egg_path))
            + ", key_hex=" + repr(egg.key_hex)
            + ", backend=" + repr(egg.backend) + ")\n"
        )
    destination.write_text(
        prelude
        + "import importlib\n"
        f"modules = {modules!r}\n"
        "for name in modules:\n"
        "    importlib.import_module(name)\n"
        "print('SMOKE PASS', len(modules))\n",
        encoding="utf-8",
    )
    return destination


def run_target_python_smoke(target: Target) -> None:
    if target != host_target():
        print("SKIP target runtime smoke: cross target")
        return
    stage_root = BUILD / "staging" / target.key
    site = stage_root / "site-packages"
    script = write_smoke_script(target, BUILD / "smoke" / target.key / "target.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site)
    env["PYTHONHOME"] = str(target_runtime_dir(target))
    env["PYTHONNOUSERSITE"] = "1"
    # Importing the package markers must not leave __pycache__ inside the
    # staging tree; a later --verify reports those as leftover build artifacts.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if target.os == "windows" and runtime_spec(target).provider == "pbs":
        # The official embed interpreter ignores PYTHONPATH via pythonXY._pth.
        # Check staged modules with the matching PBS SDK interpreter, then check
        # the actual embeddable runtime with the packaged launcher below.
        from .runtime import sdk_info

        py = Path(sdk_info(target)["python_executable"])
        # SDK embeds its own complete stdlib; PYTHONHOME must not point at the
        # flat embeddable ZIP (which contains no Lib directory).
        env.pop("PYTHONHOME", None)
    else:
        py = runtime_python(target)
    print("+", py, script)
    subprocess.run([str(py), str(script)], env=env, cwd=stage_root, check=True)


def run_packaged_launcher(
    executable: Path,
    *,
    cwd: Path,
    env: dict | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run a packaged launcher with this process's own streams attached.

    The Windows launcher is a GUI-subsystem binary: Windows attaches no console
    to such a child, and ``subprocess`` started with the default
    ``stdout=None`` hands it the caller's handle *values* without the handles
    themselves. The child then writes into dangling handles and everything it
    prints disappears while the exit code stays 0, which is exactly the output
    the smoke step and ``--run`` assert on. Passing the streams explicitly
    makes ``subprocess`` duplicate them as inheritable handles and start the
    child with ``STARTF_USESTDHANDLES``, which is what a GUI-subsystem child
    needs. Console-subsystem children and POSIX inherit the streams either way,
    so this needs no platform branch.
    """
    return subprocess.run(
        [str(executable)],
        cwd=cwd,
        env=env,
        check=check,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def run_launcher_smoke(target: Target, launcher: Path) -> None:
    if target != host_target():
        print("SKIP launcher smoke: cross target")
        return
    # The smoke program is compiled into the launcher like the entry is, so the
    # run below is what proves it: there is no file to check first.
    env = dict(os.environ)
    env["PY_UPPER_SMOKE"] = "1"
    # The smoke test is non-interactive by definition: a failure must surface as
    # a non-zero exit code, not as a dialog waiting for a click.
    env["PY_UPPER_NO_DIALOG"] = "1"
    print("+", launcher, "[launcher smoke]")
    run_packaged_launcher(launcher, cwd=launcher.parent, env=env, check=True)
