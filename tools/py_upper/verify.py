from __future__ import annotations

import json
from pathlib import Path

from .config import APP, BUILD, DIST, Target, app_name, entry_module, optimize_config, runtime_spec, staging_dir, target_extension_suffix
from .manifest import read_manifest, validate_manifest
from .native.inspect import inspect, target_format, verify_arch
from .python_build import module_name, selected_sources
from .third_party import WHEEL_MANIFEST, dependency_specs, wheel_dir


def _expected_extension(site: Path, source: Path, target: Target) -> Path:
    spec = runtime_spec(target)
    suffix = target_extension_suffix(target, spec.major_minor, spec.abi_tag)
    rel = source.relative_to(APP / "src").with_suffix("")
    return site.joinpath(*rel.parts[:-1], rel.name + suffix)


def _checks_for_stage(target: Target) -> list[tuple[bool, str]]:
    stage = staging_dir(target)
    site = stage / "site-packages"
    checks: list[tuple[bool, str]] = [
        (stage.is_dir(), "staging"),
        (site.is_dir(), "application site-packages"),
    ]
    try:
        manifest = read_manifest(target_runtime_dir := runtime_spec(target).root)
        validate_manifest(manifest, target, runtime_spec(target))
        checks.append((True, f"runtime manifest format={manifest['format']}"))
    except Exception as exc:
        checks.append((False, f"runtime manifest: {exc}"))

    sources = selected_sources()
    selected_modules = {module_name(source) for source in sources}
    checks.append((entry_module() in selected_modules, f"entry module Cythonized: {entry_module()}"))
    for source in sources:
        expected = _expected_extension(site, source, target)
        name = module_name(source)
        checks.append((expected.is_file(), f"extension {name}"))
        checks.append((not (site / source.relative_to(APP / "src")).exists(), f"source removed {name}"))
        if expected.is_file():
            try:
                verify_arch(expected, target)
                checks.append((True, f"arch {name}={target.arch}"))
            except Exception as exc:
                checks.append((False, f"arch {name}: {exc}"))

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


def _checks_for_package(target: Target) -> list[tuple[bool, str]]:
    root = _package_root(target)
    name = app_name()
    if target.os == "macos":
        executable = root / "Contents" / "MacOS" / name
        resources = root / "Contents" / "Resources"
        site = resources / "site-packages"
        entry = resources / f"{name}.int"
    else:
        executable = root / (f"{name}.exe" if target.os == "windows" else name)
        site = root / "site-packages"
        entry = root / f"{name}.int"
    checks: list[tuple[bool, str]] = [
        (root.is_dir(), "package"),
        (executable.is_file(), "launcher"),
        (entry.is_file(), "application entry"),
    ]
    if bool(optimize_config().get("remove_runtime_pip", True)):
        runtime_site = resources / "runtime" / "lib" if target.os == "macos" else root / "runtime" / "lib"
        pip_payloads = []
        for candidate in runtime_site.glob("python*/site-packages/pip*") if runtime_site.exists() else []:
            pip_payloads.append(candidate)
        for candidate in runtime_site.glob("python*/ensurepip") if runtime_site.exists() else []:
            pip_payloads.append(candidate)
        checks.append((not pip_payloads, "runtime pip/ensurepip removed"))
    if executable.is_file():
        try:
            verify_arch(executable, target)
            checks.append((True, f"launcher arch={target.arch}"))
        except Exception as exc:
            checks.append((False, f"launcher arch: {exc}"))

    for source in selected_sources():
        expected = _expected_extension(site, source, target)
        name_ = module_name(source)
        checks.append((expected.is_file(), f"packaged extension {name_}"))
        checks.append((not (site / source.relative_to(APP / "src")).exists(), f"packaged source removed {name_}"))

    # Only binaries the target OS can actually load are part of the native ABI
    # contract. Bundled runtimes and third-party packages ship inert
    # foreign-format data files (for example the Windows setuptools launcher
    # stubs inside a macOS CPython runtime); their architecture is irrelevant
    # and must not fail an otherwise correct package.
    target_container = target_format(target)
    checked = 0
    for path in root.rglob("*"):
        if not path.is_file():
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
