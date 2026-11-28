# 中文维护说明：检查清单、架构与依赖并执行目标 import 和 launcher smoke；成功范围以实际运行步骤为准。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Verification: static package checks plus the target-runtime and launcher smoke tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .config import (
    APP, BUILD, DIST, Target, app_name, cython_merge_mode, entry_module,
    entry_source, host_target, optimize_config, runtime_executable, runtime_spec,
    staging_dir, target_extension_suffix, target_runtime_dir,
)
from .egg import (
    EGG_DIR, EGG_NAME, egg_config, egg_loader_source, open_egg_archive, read_egg_meta,
)
from .egg_loader import MANIFEST_NAME, _safe_member
from .manifest import read_manifest, validate_manifest
from .native.deps import _system_dependency, dependency_names
from .native.inspect import (
    PE_SUBSYSTEM, PE_SUBSYSTEM_CONSOLE, PE_SUBSYSTEM_GUI, inspect,
    native_files, target_format, verify_arch,
)
from .python_build import (
    merged_bundle_relative, merged_groups, module_name, selected_sources,
)
from .runtime import runtime_pip_payloads
from .third_party import WHEEL_MANIFEST, dependency_specs, wheel_dir


def _expected_extension(site: Path, source: Path, suffix: str) -> Path:
    """计算源码在 site-packages 中应有的目标扩展路径。"""
    rel = source.relative_to(APP / "src").with_suffix("")
    return site.joinpath(*rel.parts[:-1], rel.name + suffix)


def _stage_egg_config(stage) -> "EggConfig":
    """优先读取 staging 实际配置；无记录时依据归档存在性选择 egg 或散装验证。

    The egg configuration a stage was actually built with.

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


def _egg_manifest_checks(archive: zipfile.ZipFile, members: set[str]) -> list[tuple[bool, str]]:
    """检查清单覆盖成员且摘要长度符合 SHA-256，返回检查结果列表。

    The archive records a digest per member, so a cache can be validated.

    Without it the plaintext extract cache has no way to tell an untouched
    directory from an edited one; the authentication tag covers the archive but
    not the files a previous run unpacked from it.
    """
    try:
        data = json.loads(archive.read(MANIFEST_NAME)) if MANIFEST_NAME in members else {}
        files = data.get("files") if isinstance(data, dict) else None
        manifest = files if isinstance(files, dict) else {}
    except Exception as exc:
        return [(False, f"egg manifest: {exc}")]
    if not manifest:
        return [(False, "egg manifest records per-member digests")]
    covered = {name for name in members if not name.endswith("/") and name != MANIFEST_NAME}
    missing = sorted(covered - set(manifest))
    checks = [(not missing, "egg manifest covers every member" + (f": missing {missing[0]}" if missing else ""))]
    malformed = sorted(name for name, digest in manifest.items() if len(str(digest)) != 64)
    checks.append((not malformed, "egg manifest digests are SHA-256"))
    return checks


def _verify_egg_bundle(
    archive: zipfile.ZipFile,
    member: str,
    target: Target,
) -> list[tuple[bool, str]]:
    """临时提取合并库并检查存储方法和目标架构；异常转为失败项。

    Check the stored method and architecture of a bundle inside an egg."""
    try:
        info = archive.getinfo(member)
        data = archive.read(info)
    except Exception as exc:
        return [(False, f"merged bundle in egg: {exc}")]
    checks = [(info.compress_type == zipfile.ZIP_STORED, f"merged bundle stored {member}")]
    with tempfile.TemporaryDirectory(prefix="appbuild-verify-") as tmp:
        probe = Path(tmp) / Path(member).name
        probe.write_bytes(data)
        try:
            verify_arch(probe, target)
            checks.append((True, f"arch bundle {member}={target.arch}"))
        except Exception as exc:
            checks.append((False, f"arch bundle {member}: {exc}"))
    return checks


def _verify_egg_native_dependencies(archive: zipfile.ZipFile, target: Target,
                                    runtime: Path) -> list[tuple[bool, str]]:
    """检查 egg 内原生库可独立装载；拒绝依赖包外自定义库的缓存加载布局。

    Packed extensions load from a cache, outside the bundle's native tree.

    Their custom linked libraries cannot use site-packages-relative paths.
    Refuse that unsupported layout explicitly; --no-egg lets the normal
    native closure relocate the extension and its dependencies together.
    """
    checks = []
    preloaded = set()
    if target.os == "windows":
        # load_python loads every python*.dll before any cached application
        # bundle. Only their transitive DLL dependencies are already available;
        # a random DLL elsewhere in site-packages is not on that search path.
        libraries = {path.name.lower(): path for path in runtime.glob("*.dll")}
        pending = [name for name in libraries if name.startswith("python")]
        while pending:
            name = pending.pop()
            if name in preloaded:
                continue
            preloaded.add(name)
            pending.extend(dep.lower() for dep in dependency_names(libraries[name])
                           if dep.lower() in libraries and dep.lower() not in preloaded)
    with tempfile.TemporaryDirectory(prefix="appbuild-egg-check-") as tmp:
        root = Path(tmp)
        resolved_root = root.resolve()
        for member in archive.infolist():
            if member.is_dir():
                continue
            destination = root / _safe_member(member.filename)
            destination.resolve().relative_to(resolved_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
        for binary in native_files(root, target):
            verify_arch(binary, target)
            for name in dependency_names(binary):
                if _system_dependency(name, target):
                    continue
                if target.os == "windows" and name.lower() in preloaded:
                    continue
                checks.append((False,
                    f"egg native dependency {binary.relative_to(root)}: {name}; "
                    "use --no-egg for application extensions with custom linked libraries"))
    if not checks:
        checks.append((True, "egg native dependencies portable"))
    return checks


def _application_payload_checks(
    site: Path,
    sources,
    target: Target,
    prefix: str,
    *,
    archive: zipfile.ZipFile | None = None,
) -> list[tuple[bool, str]]:
    """按合并策略检查应用源码、扩展、归档清单和目标架构。

    Check archive metadata and the application's own files.

    The per-module layout has one extension per source. The merged layout has
    bundles selected by the same grouping logic as the build. Bundles inside an
    egg ship as stored entries; the launcher extracts and registers them before
    ``Py_Initialize``, so site-packages carries no loose copy in that case.
    """
    spec = runtime_spec(target)
    suffix = target_extension_suffix(target, spec.major_minor, spec.abi_tag)
    checks: list[tuple[bool, str]] = []
    mode = cython_merge_mode()
    egg_members = None
    if archive is not None:
        label = f"{prefix or 'encrypted '}egg"
        egg_members = set(archive.namelist())
        checks.extend([(True, f"{label} authenticated"),
                       (bool(egg_members), f"{label} members={len(egg_members)}")])
        checks.extend(_egg_manifest_checks(archive, egg_members))
    if mode == "none":
        for source in sources:
            expected = _expected_extension(site, source, suffix)
            name = module_name(source)
            if egg_members is None:
                checks.append((expected.is_file(), f"{prefix}extension {name}"))
                if expected.is_file():
                    try:
                        verify_arch(expected, target)
                        checks.append((True, f"{prefix}arch {name}={target.arch}"))
                    except Exception as exc:
                        checks.append((False, f"{prefix}arch {name}: {exc}"))
            else:
                member = expected.relative_to(site).as_posix()
                checks.append((member in egg_members, f"{prefix}egg extension {name}"))
                checks.append((not expected.is_file(), f"{prefix}extension not loose {name}"))
            checks.append((not (site / source.relative_to(APP / "src")).exists(), f"{prefix}source removed {name}"))
        return checks

    for group, grouped in merged_groups(sources, mode).items():
        relative = merged_bundle_relative(group, suffix)
        bundle = site / relative
        if egg_members is None:
            checks.append((bundle.is_file(), f"{prefix}merged bundle {group}"))
            if bundle.is_file():
                try:
                    verify_arch(bundle, target)
                    checks.append((True, f"{prefix}arch bundle {group}={target.arch}"))
                except Exception as exc:
                    checks.append((False, f"{prefix}arch bundle {group}: {exc}"))
        else:
            present = relative in egg_members
            checks.append((present, f"{prefix}egg merged bundle {group}"))
            checks.append((not bundle.is_file(), f"{prefix}merged bundle not loose {group}"))
            if present and archive is not None:
                checks.extend(_verify_egg_bundle(archive, relative, target))
        checks.append((bool(grouped), f"{prefix}merged bundle modules {group}={len(grouped)}"))

    for source in sources:
        name = module_name(source)
        relative = source.relative_to(APP / "src")
        expected = _expected_extension(site, source, suffix)
        if source.name == "__init__.py":
            if egg_members is None:
                checks.append(((site / relative).is_file(), f"{prefix}package marker {name}"))
            else:
                checks.append((relative.as_posix() in egg_members, f"{prefix}egg package marker {name}"))
                checks.append((not (site / relative).exists(), f"{prefix}package marker not loose {name}"))
            checks.append((not expected.is_file(), f"{prefix}package marker not compiled {name}"))
        else:
            checks.append((not (site / relative).exists(), f"{prefix}source removed {name}"))
            checks.append((not expected.is_file(), f"{prefix}no per-module extension {name}"))
    return checks


def _checks_for_application(
    site: Path,
    sources: list[Path],
    target: Target,
    *,
    prefix: str = "",
    egg_input: tuple[Path, str] | None = None,
    runtime: Path | None = None,
) -> list[tuple[bool, str]]:
    """统一打开一次应用归档，并汇集应用负载检查结果。"""
    if egg_input is None:
        return _application_payload_checks(site, sources, target, prefix)

    label = f"{prefix or 'encrypted '}egg"
    try:
        archive = open_egg_archive(*egg_input)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return [(False, f"{label}: {exc}")]
    with archive:
        checks = _application_payload_checks(site, sources, target, prefix, archive=archive)
        if runtime is not None:
            checks.extend(_verify_egg_native_dependencies(archive, target, runtime))
    return checks


def _checks_for_stage(target: Target) -> list[tuple[bool, str]]:
    """检查构建 staging 中的输入清单、依赖和应用负载。"""
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
    egg_input = None
    if egg.enabled:
        egg_path = stage / EGG_DIR / EGG_NAME
        exists = egg_path.is_file()
        checks.append((exists, f"encrypted egg {EGG_NAME}"))
        if exists:
            egg_input = (egg_path, egg.key_hex)
        if target.os == "windows" and egg.backend == "memory":
            checks.append(((site / "_memimporter.pyd").is_file(),
                           "egg memory backend helper _memimporter"))

    checks.extend(_checks_for_application(site, sources, target, egg_input=egg_input))

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
    """依据平台生成默认已发布应用包路径。"""
    name = app_name()
    return DIST / f"{name}.app" if target.os == "macos" else DIST / name


def _launcher_subsystem_check(executable: Path) -> tuple[bool, str]:
    """检查 Windows launcher 使用预期 GUI 或调试控制台子系统。

    Assert the launcher is the subsystem Windows will start it as.

    A GUI launcher is the shipped layout, but its subsystem is only implied by
    the build: CMake's WIN32_EXECUTABLE relies on -mwindows, which MSVC honours
    and Zig's clang frontend silently ignores, so a cross-built launcher used to
    come out as a console image that flashes a console window on every start.
    APPBUILD_LAUNCHER_CONSOLE=1 is the documented debugging escape hatch that
    builds the console variant on purpose, and is the only case console is
    expected.
    """
    expected = PE_SUBSYSTEM_CONSOLE if os.environ.get("APPBUILD_LAUNCHER_CONSOLE") == "1" else PE_SUBSYSTEM_GUI
    try:
        subsystem = inspect(executable).subsystem
    except Exception as exc:
        return False, f"launcher subsystem: {exc}"
    if subsystem == expected:
        return True, f"launcher subsystem={PE_SUBSYSTEM[expected]}"
    actual = PE_SUBSYSTEM.get(subsystem, subsystem)
    return False, f"launcher subsystem: {actual}, expected {PE_SUBSYSTEM[expected]}"


def _checks_for_package(target: Target, root: Path | None = None) -> list[tuple[bool, str]]:
    """检查成品目录的 launcher、runtime、资源、应用负载及原生依赖闭包。"""
    root = root or _package_root(target)
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
    binary = executable.read_bytes() if executable.is_file() else None
    if binary is not None:
        # The entry is the one thing the launcher cannot get from anywhere else,
        # and the packaging step compiles it in: reading the bytes back out
        # proves the binary that was built carries the entry the configuration
        # names, which a file-based check silently stopped doing the moment the
        # two sides could disagree.
        embedded = entry_source().encode("utf-8")
        checks.append((embedded in binary, f"embedded entry {entry_module()}"))

    # The package is the contract: an egg that is present must be readable with
    # the key of the build that produced it. Stage verification is what proves
    # an enabled project actually produced one, and package() copies it
    # unconditionally, so an absent archive here means a loose build.
    egg_path = entry_dir / EGG_DIR / EGG_NAME
    egg = read_egg_meta(staging_dir(target)) or egg_config()
    egg_input = (egg_path, egg.key_hex) if egg_path.is_file() else None
    if egg_input is not None and binary is not None:
        # The archive is useless unless the launcher carries its loader.
        loader = egg_loader_source().encode("utf-8")
        checks.append((loader in binary, "embedded egg loader"))
        checks.append((egg.key_hex.encode("ascii") not in binary, "egg key not stored as plain hex"))
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

    checks.extend(_checks_for_application(
        site, selected_sources(), target, prefix="packaged ", egg_input=egg_input, runtime=runtime_root,
    ))

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
        if not info.loadable or not info.format.startswith(target_container):
            continue
        checked += 1
        try:
            verify_arch(path, target)
        except Exception as exc:
            checks.append((False, f"native arch {path.relative_to(root)}: {exc}"))
    checks.append((True, f"native binaries checked={checked}"))
    return checks


def verify(target: Target, root: Path | None = None) -> int:
    """输出 staging 和成品全部静态检查结果；任一失败返回 1。"""
    checks = _checks_for_stage(target) + (_checks_for_package(target, root) if root is not None else _checks_for_package(target))
    failures = 0
    for ok, label in checks:
        print("PASS" if ok else "FAIL", label)
        failures += int(not ok)
    return int(bool(failures))


def runtime_python(target: Target) -> Path:
    """返回可运行的目标 runtime 解释器路径。"""
    return runtime_executable(target)


def direct_dependency_imports(target: Target) -> list[str]:
    """从 wheel 清单读取裁剪前记录的直接依赖导入名。"""
    path = wheel_dir(target) / WHEEL_MANIFEST
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    values = data.get("imports", [])
    return sorted({str(value).strip() for value in values if str(value).strip()}) if isinstance(values, list) else []


def smoke_modules(target: Target, include_entry: bool = True) -> list[str]:
    # Import the application entry point and every directly declared third-party
    # distribution. Imported application modules are exercised transitively by
    # the entry point; importing every Cython module independently would turn
    # optional/lazy application modules into false build failures.
    #
    # The launcher registers merged bundles before Py_Initialize, so its smoke
    # program can import the entry. A plain interpreter started with PYTHONPATH
    # cannot: it has no inittab to extend, so that smoke run covers third-party
    # imports and leaves the application modules to the launcher.
    """生成入口及直接依赖冒烟列表；合并模块需由 launcher 注册后导入。"""
    values = ([entry_module()] if include_entry else []) + direct_dependency_imports(target)
    return sorted({value for value in values if value})


def write_smoke_script(target: Target, destination: Path) -> Path:
    """生成目标解释器测试脚本；存在 egg 时嵌入 loader 并采用实际构建配置。"""
    merged = cython_merge_mode() != "none"
    modules = smoke_modules(target, include_entry=not merged)
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
        "print('SMOKE PASS', len(modules), 'modules (launcher registers merged bundles)' if "
        + repr(merged) + " else '')\n",
        encoding="utf-8",
    )
    return destination


def run_target_python_smoke(target: Target) -> None:
    """仅运行本机目标解释器冒烟，隔离用户 site 并禁止往 staging 写字节码。"""
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
    """显式继承标准流启动打包程序，确保 Windows GUI 子系统输出可被 CI 捕获。

    Run a packaged launcher with this process's own streams attached.

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
    """仅对本机目标运行 launcher 内嵌冒烟；禁用交互错误对话框。"""
    if target != host_target():
        print("SKIP launcher smoke: cross target")
        return
    # The smoke program is compiled into the launcher like the entry is, so the
    # run below is what proves it: there is no file to check first.
    env = dict(os.environ)
    env["APPBUILD_SMOKE"] = "1"
    # The smoke test is non-interactive by definition: a failure must surface as
    # a non-zero exit code, not as a dialog waiting for a click.
    env["APPBUILD_NO_DIALOG"] = "1"
    print("+", launcher, "[launcher smoke]")
    run_packaged_launcher(launcher, cwd=launcher.parent, env=env, check=True)
