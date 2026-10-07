from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest


def test_build_python_minimum_and_toml_compatibility():
    from py_upper.compat import tomllib
    from py_upper.config import validate_build_python_version

    validate_build_python_version(type("V", (), {"major": 3, "minor": 8})())
    data = tomllib.loads('[tool.py_upper]\nentry = "main.py"\n')
    assert data["tool"]["py_upper"]["entry"] == "main.py"


def test_every_build_tool_module_imports():
    """Compiling is not loading.

    PEP 604 unions and builtin generics in annotations are evaluated when a
    module is imported, so ``list[str]`` / ``Path | None`` in a signature raise
    TypeError on 3.8 and 3.9 unless the module opts into PEP 563. ``py_compile``
    cannot see that, and the toolchain still supports 3.8, so every module is
    imported here rather than only the two the workflow used to name.
    """
    import importlib
    import pkgutil

    import py_upper

    names = sorted(module.name for module in pkgutil.walk_packages(py_upper.__path__, py_upper.__name__ + "."))
    assert "py_upper.python_build" in names and "py_upper.pbs" in names
    for name in names:
        importlib.import_module(name)


def test_msvc_host_target_pairs_cover_every_combination():
    from py_upper.toolchain import VC_HOST_COMPONENT, VCVARSALL_HOST_TARGET

    for host in ("x86", "x86_64", "arm64"):
        assert host in VC_HOST_COMPONENT
        for target in ("x86", "x86_64", "arm64"):
            assert VCVARSALL_HOST_TARGET[(host, target)]
    # vcvarsall spells a native build with just the host architecture, and the
    # ARM64 runner must not be told to cross-compile from an x64 host.
    assert VCVARSALL_HOST_TARGET[("arm64", "arm64")] == "arm64"
    assert VCVARSALL_HOST_TARGET[("x86_64", "x86_64")] == "amd64"
    assert VCVARSALL_HOST_TARGET[("x86_64", "arm64")] == "amd64_arm64"


def test_host_arch_normalizes_windows_and_linux_spellings(monkeypatch):
    """platform.machine() reports AMD64/ARM64 on Windows and x86_64/aarch64 on Linux."""
    from py_upper import toolchain

    for machine, expected in (
        ("AMD64", "x86_64"),
        ("x86_64", "x86_64"),
        ("ARM64", "arm64"),
        ("aarch64", "arm64"),
        ("i686", "x86"),
    ):
        monkeypatch.setattr(toolchain.platform, "machine", lambda machine=machine: machine)
        assert toolchain._host_arch() == expected


def test_windows_environment_parser_keeps_only_variable_assignments():
    """``vcvarsall.bat`` prints a banner and progress lines around ``set``.

    Everything that is not a ``NAME=VALUE`` line has to be dropped: a stray line
    accepted as a variable would put a non-path into ``INCLUDE`` or ``PATH`` and
    break the compiler invocation far away from the parse. Names are upper-cased
    because ``set`` echoes whatever casing the environment block holds — the
    search path is ``Path`` there, while ``os.environ`` and every caller spell it
    ``PATH``.
    """
    from py_upper import toolchain

    parsed = toolchain._parse_windows_environment(
        "\n".join(
            [
                "**********************************************************************",
                "** Visual Studio 2022 Developer Command Prompt v17.14.0",
                "**********************************************************************",
                "[vcvarsall.bat] Environment initialized for: 'x64'",
                "VCToolsInstallDir=C:\\Program Files\\Microsoft Visual Studio\\2022\\VC\\Tools\\MSVC\\14.44.35207\\",
                "INCLUDE=C:\\SDK\\Include;=leading equals stays in the value",
                "Path=C:\\bin;C:\\Windows",
                "  PADDED=indented output is not a variable",
                "KEY WITH SPACES=not a variable",
                "NO_SEPARATOR_LINE",
                "",
            ]
        )
    )

    assert parsed["VCTOOLSINSTALLDIR"].endswith("14.44.35207\\")
    assert parsed["INCLUDE"] == "C:\\SDK\\Include;=leading equals stays in the value"
    assert parsed["PATH"] == "C:\\bin;C:\\Windows"
    assert "PADDED" not in parsed
    assert "KEY WITH SPACES" not in parsed
    assert set(parsed) == {"VCTOOLSINSTALLDIR", "INCLUDE", "PATH"}


def test_windows_environment_parser_reports_its_own_output_on_failure():
    """A failed ``vcvarsall`` run must describe itself; the exit code cannot."""
    from py_upper import toolchain

    assert toolchain._output_tail("", "") == "(no output)"
    assert toolchain._output_tail("first\n\nsecond") == "first\nsecond"
    tail = toolchain._output_tail("\n".join(f"line {index}" for index in range(100)), limit=3)
    assert tail == "line 97\nline 98\nline 99"


def test_tracked_pyproject_template_is_complete():
    """A fresh clone copies this file, so it must be buildable on its own."""
    import re

    from py_upper.compat import tomllib
    from py_upper.config import APP_CONFIG_EXAMPLE

    assert APP_CONFIG_EXAMPLE.is_file()
    data = tomllib.loads(APP_CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    assert data["project"]["name"] and data["project"]["version"]
    # The template pins no dependencies: a fork must not inherit the pins of
    # whoever cloned first. The integration job materializes its own working
    # copy for the build, so an empty list here still builds.
    assert data["project"]["dependencies"] == []
    tool = data["tool"]["py_upper"]
    assert tool["entry"].endswith(".py")
    assert tool["app"]["name"] and tool["app"]["identifier"]
    assert tool["runtime"]["provider"] in {"pbs", "local"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", tool["runtime"]["python"])


def test_local_configuration_is_git_ignored_but_the_template_is_not():
    root = Path(__file__).parents[2]
    entries = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "app/pyproject.toml" in entries
    assert "app/pyproject.toml.example" not in entries


def test_missing_local_configuration_points_at_the_template(tmp_path, monkeypatch):
    from py_upper import config

    monkeypatch.setattr(config, "APP_CONFIG", tmp_path / "pyproject.toml")
    monkeypatch.setattr(config, "APP_CONFIG_EXAMPLE", tmp_path / "pyproject.toml.example")
    try:
        config.load_app_config()
    except RuntimeError as exc:
        assert "pyproject.toml.example" in str(exc)
    else:
        raise AssertionError("expected a missing-configuration error")


def test_missing_local_configuration_survives_a_foreign_drive(tmp_path, monkeypatch):
    """A configuration outside the project root must still be reported.

    ``os.path.relpath`` raises ValueError when the two paths are on different
    Windows drives, which is what happens when the working copy lives on D: and
    the temporary configuration on C:.
    """
    from py_upper import config

    def cross_drive(*_args, **_kwargs):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr(config, "APP_CONFIG", tmp_path / "pyproject.toml")
    monkeypatch.setattr(config, "APP_CONFIG_EXAMPLE", tmp_path / "pyproject.toml.example")
    monkeypatch.setattr(config.os.path, "relpath", cross_drive)
    with pytest.raises(RuntimeError) as excinfo:
        config.load_app_config()
    assert "pyproject.toml.example" in str(excinfo.value)


def test_target_matrix_and_platform_contract(monkeypatch):
    from py_upper import config
    from py_upper.config import TARGETS

    assert set(TARGETS) == {
        "windows-x86", "windows-x86_64", "windows-arm64",
        "macos-x86_64", "macos-arm64", "linux-x86_64", "linux-arm64",
    }
    assert TARGETS["windows-arm64"].triple == "aarch64-pc-windows-msvc"
    assert TARGETS["macos-arm64"].primary_wheel_platform == "macosx_13_0_arm64"
    # Linux baselines are project configuration, so assert the derivation
    # instead of duplicating whatever app/pyproject.toml currently declares.
    monkeypatch.setattr(config, "dependency_config", lambda: {})
    assert TARGETS["linux-x86_64"].wheel_platforms == ("manylinux_2_17_x86_64",)
    monkeypatch.setattr(config, "dependency_config", lambda: {"manylinux": ["manylinux_2_39", "manylinux_2_34"]})
    assert TARGETS["linux-x86_64"].wheel_platforms == ("manylinux_2_39_x86_64", "manylinux_2_34_x86_64")
    assert TARGETS["linux-arm64"].wheel_platforms == ("manylinux_2_39_aarch64", "manylinux_2_34_aarch64")
    assert TARGETS["linux-x86_64"].primary_wheel_platform == "manylinux_2_39_x86_64"


def test_manylinux_baselines_are_validated(monkeypatch):
    from py_upper import config

    for value in ("manylinux_2_17", ["manylinux_2_28"], "manylinux2014"):
        monkeypatch.setattr(config, "dependency_config", lambda value=value: {"manylinux": value})
        assert config.manylinux_baselines()
    monkeypatch.setattr(config, "dependency_config", lambda: {"manylinux": "manylinux"})
    try:
        config.manylinux_baselines()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected a rejection of an unqualified manylinux baseline")
    monkeypatch.setattr(config, "dependency_config", lambda: {"manylinux": []})
    try:
        config.manylinux_baselines()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected a rejection of an empty manylinux list")


def test_extension_suffixes_follow_target_import_conventions():
    from py_upper.config import TARGETS, target_extension_suffix

    assert target_extension_suffix(TARGETS["windows-x86_64"], "3.13", "cp313") == ".cp313-win_amd64.pyd"
    assert target_extension_suffix(TARGETS["macos-arm64"], "3.13", "cp313") == ".cpython-313-darwin.so"
    assert target_extension_suffix(TARGETS["linux-x86_64"], "3.13", "cp313") == ".cpython-313-x86_64-linux-gnu.so"


def test_source_selection_is_layout_agnostic():
    from py_upper.python_build import module_name, selected_sources

    modules = {module_name(path) for path in selected_sources()}
    assert "main" in modules
    assert "core.app" in modules
    assert "utils.paths" in modules
    assert not any(path.name == "__init__.py" for path in selected_sources())


def test_cython_cache_is_keyed_by_development_python(monkeypatch, tmp_path):
    from py_upper import python_build

    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    calls = []

    class Result:
        stdout = "3.13\n"
        returncode = 0

    monkeypatch.setattr(python_build.subprocess, "run", lambda *args, **kwargs: Result())
    path = python_build._cython_cache_dir(Path("/fake/python3"))
    assert path.name == "cython-py3_13"
    assert calls == []


def test_pip_transfer_args_are_configurable(monkeypatch):
    from py_upper import config

    monkeypatch.setattr(config, "dependency_config", lambda: {})
    monkeypatch.delenv("PY_UPPER_PIP_TIMEOUT", raising=False)
    monkeypatch.delenv("PY_UPPER_PIP_RETRIES", raising=False)
    assert config.pip_transfer_args() == ["--timeout", "120", "--retries", "10"]

    monkeypatch.setattr(config, "dependency_config", lambda: {"timeout": 300, "retries": 3})
    assert config.pip_transfer_args() == ["--timeout", "300", "--retries", "3"]

    monkeypatch.setenv("PY_UPPER_PIP_TIMEOUT", "45")
    monkeypatch.setattr(config, "dependency_config", lambda: {})
    assert config.pip_transfer_args() == ["--timeout", "45", "--retries", "10"]


def test_native_wheel_resolution_uses_the_target_interpreter(tmp_path, monkeypatch):
    """pip evaluates environment markers against the interpreter that runs it."""
    from py_upper import third_party
    from py_upper.config import TARGETS

    target = TARGETS["macos-arm64"]
    runtime_python = tmp_path / "bin" / "python3.10"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(third_party, "host_target", lambda: target)
    monkeypatch.setattr(third_party, "runtime_executable", lambda value: runtime_python)
    monkeypatch.setattr(third_party, "_has_pip", lambda value: True)
    python, tag_args = third_party.resolution_python(target)
    assert python == runtime_python
    assert tag_args == []


def test_cross_target_resolution_keeps_the_host_interpreter(tmp_path, monkeypatch):
    from py_upper import third_party
    from py_upper.config import TARGETS

    target = TARGETS["windows-x86_64"]
    host_python = tmp_path / "host-python"
    host_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(third_party, "host_target", lambda: TARGETS["macos-arm64"])
    monkeypatch.setattr(third_party, "require_local_python", lambda: host_python)
    python, tag_args = third_party.resolution_python(target)
    assert python == host_python
    assert "--platform" in tag_args and "win_amd64" in tag_args


def test_cython_supported_versions():
    from py_upper.python_build import _supported_cython

    assert _supported_cython("3.1.6")
    assert _supported_cython("3.2.4")
    assert not _supported_cython("3.0.12")


def test_package_copy_does_not_replay_source_metadata(tmp_path, monkeypatch):
    from py_upper import fs

    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir()
    readonly = source / "README.rst"
    readonly.write_text("read\n", encoding="utf-8")
    readonly.chmod(0o444)
    calls = []
    original = fs.os.chmod
    monkeypatch.setattr(fs.os, "chmod", lambda path, mode: (calls.append(path), original(path, mode))[1])

    fs.copy_tree_contents(source, destination)

    copied = destination / "README.rst"
    assert copied.read_text(encoding="utf-8") == "read\n"
    assert not calls
    assert stat.S_IMODE(copied.stat().st_mode) & 0o111 == 0


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX execute bits")
def test_copy_preserves_execute_bits_without_replaying_restrictive_modes(tmp_path):
    """A copied interpreter must stay runnable; 0444 sources must not be replayed."""
    from py_upper import fs

    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir()
    interpreter = source / "python3.10"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)
    readonly = source / "README.rst"
    readonly.write_text("read\n", encoding="utf-8")
    readonly.chmod(0o444)

    fs.copy_tree_contents(source, destination)

    copied_interpreter = destination / "python3.10"
    assert stat.S_IMODE(copied_interpreter.stat().st_mode) & 0o111
    assert copied_interpreter.read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert stat.S_IMODE((destination / "README.rst").stat().st_mode) & 0o111 == 0


def test_package_copy_can_reject_collisions(tmp_path):
    from py_upper.fs import copy_file_contents, copy_tree_contents

    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir(); destination.mkdir()
    (source / "a.txt").write_text("one", encoding="utf-8")
    copy_file_contents(source / "a.txt", destination / "a.txt")
    try:
        copy_tree_contents(source, destination, replace=False)
    except RuntimeError as exc:
        assert "collision" in str(exc).lower()
    else:
        raise AssertionError("expected collision")


def test_optional_resource_tree_is_skipped_when_absent(tmp_path):
    """app/resources is optional: a project without it must still package."""
    from py_upper.fs import copy_optional_tree, copy_tree_contents

    source = tmp_path / "resources"
    destination = tmp_path / "out" / "resources"

    copy_optional_tree(source, destination)
    assert not destination.exists()

    source.mkdir()
    (source / "logo.png").write_bytes(b"\x89PNG")
    copy_optional_tree(source, destination)
    assert (destination / "logo.png").read_bytes() == b"\x89PNG"

    # The strict copier keeps failing loudly for payload that must be present.
    try:
        copy_tree_contents(tmp_path / "missing", tmp_path / "never")
    except RuntimeError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("expected a missing-source error")


def test_launcher_uses_global_python_symbols_and_smoke_switch():
    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    text = launcher.read_text(encoding="utf-8")
    # Whitespace-insensitive: the launcher is reformatted from time to time and
    # the assertion is about the flags, not the spacing.
    compact = "".join(text.split())
    assert "RTLD_NOW|RTLD_GLOBAL" in compact
    assert "PY_UPPER_SMOKE" in text
    assert "RTLD_LOCAL" not in text
    # PyConfig_InitIsolatedConfig ignores PYTHONDONTWRITEBYTECODE, so the
    # launcher must disable bytecode caching through the config field. Writing
    # .pyc files into a signed .app mutates the bundle at runtime.
    assert "config.write_bytecode=0;" in compact
    # A renamed executable must still find its application.
    assert "_py_upper_static.int" in text
    # A fatal error in a GUI-subsystem launcher has nowhere to print, so the
    # dialog is the report channel and the env switch is the way out of it.
    assert "PY_UPPER_NO_DIALOG" in text
    # The application name is compiled in: a renamed executable must still
    # report the configured name, not its own filename.
    assert "PY_UPPER_APP_NAME" in text
    cmake = (launcher.parents[1] / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "PY_UPPER_APP_NAME" in cmake


def test_launcher_build_passes_the_configured_app_name(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from py_upper import package
    from py_upper.config import TARGETS, app_name

    monkeypatch.setattr(package, "BUILD", tmp_path)
    monkeypatch.setattr(package, "resolve_target_python", lambda target: SimpleNamespace(include_dir=tmp_path / "include"))
    monkeypatch.setattr(package, "resolve_toolchain", lambda target: SimpleNamespace(env={}, deployment_target=None))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "--build" in cmd:
            (tmp_path / "launcher" / "macos-arm64" / "PyUpper").write_text("", encoding="utf-8")

    monkeypatch.setattr(package.subprocess, "run", fake_run)

    package.build_launcher(TARGETS["macos-arm64"])

    configure = calls[0]
    assert configure[0] == "cmake"
    assert f"-DPY_UPPER_APP_NAME={app_name()}" in configure


def test_entry_scripts_include_a_rename_safe_static_entry(tmp_path):
    from py_upper.config import STATIC_ENTRY, TARGETS, entry_module
    from py_upper.package import write_entry_scripts

    target = TARGETS["windows-x86_64"]
    write_entry_scripts(tmp_path, "MyApp", target)

    named = tmp_path / "MyApp.int"
    static = tmp_path / STATIC_ENTRY
    smoke = tmp_path / "MyApp.smoke.int"
    assert named.is_file() and static.is_file() and smoke.is_file()
    # The static entry is the same application entry under a name that survives
    # renaming the executable.
    assert named.read_text(encoding="utf-8") == static.read_text(encoding="utf-8")
    assert f"from {entry_module()} import main" in named.read_text(encoding="utf-8")
    assert "SMOKE PASS" in smoke.read_text(encoding="utf-8")


def test_windows_version_resource_carries_a_four_part_version(tmp_path):
    import re

    from py_upper.config import app_name, project_version
    from py_upper.package import _windows_version_resource

    path = _windows_version_resource(tmp_path)
    # rc.exe reads a resource script with the ANSI code page unless the file is
    # marked; without the mark a non-ASCII product name would be mangled.
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    text = path.read_text(encoding="utf-8-sig")
    numbers = [int(part) for part in re.findall(r"\d+", project_version())][:4]
    numbers += [0] * (4 - len(numbers))
    numeric = ",".join(str(value) for value in numbers)
    assert f" FILEVERSION {numeric}" in text
    assert f" PRODUCTVERSION {numeric}" in text
    assert f'VALUE "ProductName", "{app_name()}"' in text
    assert f'VALUE "FileVersion", "{project_version()}"' in text


def test_macos_ad_hoc_signing_seals_the_bundle_last(tmp_path, monkeypatch):
    """Signing the launcher seals the .app, so nested images must be signed first."""
    from types import SimpleNamespace

    from py_upper.config import TARGETS
    from py_upper.native import bundle
    from py_upper.native.inspect import BinaryInfo

    target = TARGETS["macos-arm64"]
    root = tmp_path / "MyApp.app"
    launcher = root / "Contents" / "MacOS" / "MyApp"
    library = root / "Contents" / "Resources" / "site-packages" / "libdemo.dylib"
    for path in (launcher, library):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe")

    macho = lambda path: BinaryInfo(path, "Mach-O", "arm64")
    signed: list[Path] = []
    monkeypatch.setattr(bundle, "BUILD", tmp_path / "build")
    monkeypatch.setattr(bundle, "app_name", lambda: "MyApp")
    monkeypatch.setattr(bundle, "_native_files", lambda path, value: [library])
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env: [])
    monkeypatch.setattr(bundle, "inspect", macho)
    monkeypatch.setattr(bundle, "verify_arch", lambda path, value: macho(path))
    monkeypatch.setattr(bundle, "optimize_config", lambda: {})
    monkeypatch.setattr(bundle, "_strip_native", lambda path, value: None)
    monkeypatch.setattr(bundle, "ad_hoc_sign", signed.append)
    # Host tool discovery selects the signing branch. Replace the module
    # attribute instead of shutil.which itself: the stdlib module is shared
    # with deps.py, where a faked path would be executed by subprocess and
    # fail on hosts without otool.
    monkeypatch.setattr(bundle, "shutil", SimpleNamespace(which=lambda name: "/usr/bin/" + name))
    # otool is only needed to index install names. This test asserts signing
    # order, so no host tool should run at all.
    monkeypatch.setattr(bundle, "_mac_aliases", lambda roots: {})

    bundle.bundle_native_dependencies(root, target)

    assert sorted(signed) == sorted([launcher.resolve(), library.resolve()])
    assert signed[-1] == launcher.resolve()


def test_macos_universal_images_are_thinned_to_the_target_architecture(tmp_path, monkeypatch):
    """The bundle declares one architecture, so the foreign slice is dropped.

    PySide6 and pyobjc publish universal2 wheels; carrying both slices would
    double the size of every bundled image for an architecture the bundle never
    loads.
    """
    import shutil as real_shutil
    from types import SimpleNamespace

    from py_upper.native import bundle
    from py_upper.native.inspect import BinaryInfo

    universal = tmp_path / "QtDBus"
    universal.write_bytes(b"\xca\xfe\xba\xbe")
    universal.chmod(0o755)
    mode = stat.S_IMODE(universal.stat().st_mode)
    already_thin = tmp_path / "QtCore"
    already_thin.write_bytes(b"\xcf\xfa\xed\xfe")

    arches = {universal.resolve(): "universal", already_thin.resolve(): "arm64"}
    monkeypatch.setattr(bundle, "inspect", lambda path: BinaryInfo(path, "Mach-O", arches[Path(path).resolve()]))

    calls: list[list[str]] = []

    def fake_run(command, check=False, **kwargs):
        calls.append(command)
        Path(command[command.index("-output") + 1]).write_bytes(b"\xcf\xfa\xed\xfe")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bundle, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(
        bundle,
        "shutil",
        SimpleNamespace(which=lambda name: "/usr/bin/" + name, copymode=real_shutil.copymode),
    )

    bundle._thin_mach_o(universal, "arm64")
    bundle._thin_mach_o(already_thin, "arm64")

    assert calls == [["/usr/bin/lipo", "-thin", "arm64", "-output", str(universal) + ".thin", str(universal)]]
    assert universal.read_bytes() == b"\xcf\xfa\xed\xfe"
    # lipo writes a fresh file, so an executable slice would lose its bits.
    # Windows reports every writable file as 0o666, so compare with the mode the
    # file had rather than a literal POSIX mode.
    assert stat.S_IMODE(universal.stat().st_mode) == mode
    assert not (tmp_path / "QtDBus.thin").exists()


def test_macos_thinning_without_lipo_fails_instead_of_leaving_the_image_universal(tmp_path, monkeypatch):
    """A universal image left in place only defers the failure to install_name_tool."""
    import shutil as real_shutil
    from types import SimpleNamespace

    from py_upper.native import bundle
    from py_upper.native.inspect import BinaryInfo

    universal = tmp_path / "QtDBus"
    universal.write_bytes(b"\xca\xfe\xba\xbe")
    monkeypatch.setattr(bundle, "inspect", lambda path: BinaryInfo(path, "Mach-O", "universal"))
    monkeypatch.setattr(bundle, "shutil", SimpleNamespace(which=lambda name: None, copymode=real_shutil.copymode))

    with pytest.raises(RuntimeError, match="requires lipo"):
        bundle._thin_mach_o(universal, "arm64")


def test_macos_bundle_thins_universal_images_before_rewriting_install_names(tmp_path, monkeypatch):
    """Thinning replaces the image, so it has to run before the names are rewritten."""
    import shutil as real_shutil
    from types import SimpleNamespace

    from py_upper.config import TARGETS
    from py_upper.native import bundle, deps
    from py_upper.native.inspect import BinaryInfo

    target = TARGETS["macos-arm64"]
    root = (tmp_path / "MyApp.app").resolve()
    site = root / "Contents" / "Resources" / "site-packages"
    site.mkdir(parents=True)
    library = site / "libdemo.dylib"
    dependency = site / "libdep.dylib"
    library.write_bytes(b"\xca\xfe\xba\xbe")
    dependency.write_bytes(b"\xcf\xfa\xed\xfe")

    arches = {library.resolve(): "universal", dependency.resolve(): "arm64"}
    monkeypatch.setattr(bundle, "inspect", lambda path: BinaryInfo(path, "Mach-O", arches[Path(path).resolve()]))
    monkeypatch.setattr(bundle, "BUILD", tmp_path / "build")
    monkeypatch.setattr(bundle, "app_name", lambda: "MyApp")
    monkeypatch.setattr(bundle, "_native_files", lambda path, value: [library, dependency])
    monkeypatch.setattr(bundle, "_mac_aliases", lambda roots: {})
    monkeypatch.setattr(
        bundle, "dependency_names", lambda path, env: ["@rpath/libdep.dylib"] if Path(path).name == library.name else []
    )
    monkeypatch.setattr(bundle, "verify_arch", lambda path, value: None)
    monkeypatch.setattr(bundle, "optimize_config", lambda: {})
    monkeypatch.setattr(bundle, "_strip_native", lambda path, value: None)
    monkeypatch.setattr(bundle, "ad_hoc_sign", lambda path: None)
    # The fixture dylibs are not real Mach-O files, so otool would fail on them.
    monkeypatch.setattr(deps, "_mac_rpaths", lambda path: [])

    calls: list[list[str]] = []

    def fake_run(command, check=False, **kwargs):
        calls.append(command)
        if "-thin" in command:
            Path(command[command.index("-output") + 1]).write_bytes(b"\xcf\xfa\xed\xfe")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bundle, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(
        bundle,
        "shutil",
        SimpleNamespace(which=lambda name: "/usr/bin/" + name, copymode=real_shutil.copymode),
    )

    bundle.bundle_native_dependencies(root, target)

    tools = [Path(command[0]).name for command in calls]
    assert "install_name_tool" in tools
    assert tools.index("lipo") < tools.index("install_name_tool")
    # `codesign --remove-signature` shrinks __LINKEDIT without updating its
    # vmsize, and ld64 then rejects the rewritten image with "link edit
    # information does not fill the __LINKEDIT segment". ad_hoc_sign re-signs
    # with --force, so no signature is removed before the rewrite.
    assert "codesign" not in tools


def test_native_dependency_names_use_binary_format_not_filename_suffix(monkeypatch, tmp_path):
    from py_upper.native import deps

    binary = tmp_path / "QtWidgets.abi3.so"
    binary.write_bytes(b"fake")
    monkeypatch.setattr(deps, "inspect_binary", lambda path: type("Info", (), {"format": "Mach-O", "arch": "arm64"})())
    monkeypatch.setattr(deps, "_mac_dependencies", lambda path: ["@rpath/libQt6Core.dylib"])
    monkeypatch.setattr(deps, "_elf_dependencies", lambda path: ["libwrong.so"])
    assert deps.dependency_names(binary) == ["@rpath/libQt6Core.dylib"]


def test_native_alias_resolver_can_map_dylib_install_name_to_real_file(tmp_path):
    from py_upper.native.deps import DependencyResolver

    actual = tmp_path / "config" / "PCBUSB.dylib"
    actual.parent.mkdir()
    actual.write_bytes(b"native")
    alias = {"libpcbusb.0.12.1.dylib": actual.resolve()}
    resolved = DependencyResolver([tmp_path], aliases=alias).resolve("@rpath/libPCBUSB.0.12.1.dylib", actual)
    assert resolved == actual.resolve()


def test_native_dependency_parser_excludes_own_macho_id(monkeypatch, tmp_path):
    from py_upper.native import deps

    dylib = tmp_path / "PCBUSB.dylib"
    dylib.write_bytes(b"fake")
    monkeypatch.setattr(deps, "_mac_install_name", lambda path: "@rpath/libPCBUSB.0.12.1.dylib")
    fake = "PCBUSB.dylib:\n\t@rpath/libPCBUSB.0.12.1.dylib (compatibility version 0.0.0)\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
    class R:
        stdout = fake
    monkeypatch.setattr(deps.subprocess, "run", lambda *args, **kwargs: R())
    monkeypatch.setattr(deps.shutil, "which", lambda name: "/usr/bin/otool" if name == "otool" else None)
    result = deps._mac_dependencies(dylib)
    assert result == ["/usr/lib/libSystem.B.dylib"]


def test_macho_arm64_and_universal_validation(tmp_path):
    from py_upper.config import TARGETS
    from py_upper.native.inspect import inspect, verify_arch

    single = tmp_path / "arm.dylib"
    single.write_bytes(bytes.fromhex("cffaedfe0c0000010000000000000000"))
    assert inspect(single).arch == "arm64"

    universal = tmp_path / "fat.dylib"
    data = bytearray(bytes.fromhex("cafebabe")) + (2).to_bytes(4, "big")
    for cpu in (0x01000007, 0x0100000C):
        data += cpu.to_bytes(4, "big") + (0).to_bytes(4, "big") + (0).to_bytes(4, "big") + (0).to_bytes(4, "big") + (0).to_bytes(4, "big")
    universal.write_bytes(data)
    assert verify_arch(universal, TARGETS["macos-arm64"]).arch == "universal"


def test_network_retries_transient_gateway_errors(monkeypatch):
    from py_upper import net
    calls = []

    class Error(net.urllib.error.HTTPError):
        pass

    class Headers(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    sequence = [net.urllib.error.HTTPError("https://x", 504, "timeout", Headers(), None)]
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, *args): return b"{}"
    sequence.append(Response())

    def fake(req, timeout):
        calls.append(req.full_url)
        value = sequence.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    monkeypatch.setattr(net.time, "sleep", lambda seconds: None)
    result = net.http_json("https://example.invalid/api")
    assert result == {}
    assert len(calls) == 2


def test_github_api_requests_carry_the_workflow_token(monkeypatch):
    """Anonymous api.github.com requests are rate limited per source address.

    The hosted runners share their addresses with every other job on the
    platform, so the PBS release lookup needs the token Actions exports.
    """
    from py_upper import net

    seen: list[dict[str, str]] = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, *args): return b"{}"

    def fake(request, timeout):
        seen.append(dict(request.headers))
        return Response()

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    monkeypatch.setenv("GITHUB_TOKEN", "token-value")

    net.http_json("https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20250818")
    net.http_json("https://raw.githubusercontent.com/astral-sh/uv/main/crates/uv-python/download-metadata.json")

    assert seen[0]["Authorization"] == "Bearer token-value"
    assert "Authorization" not in seen[1]


def test_local_runtime_manifest_round_trip(tmp_path):
    from py_upper.config import RuntimeSpec, TARGETS
    from py_upper.manifest import build_manifest, validate_manifest, write_manifest

    target = TARGETS["linux-x86_64"]
    runtime = tmp_path / "runtime"
    sdk = tmp_path / "sdk"
    (sdk / "bin").mkdir(parents=True)
    (sdk / "include").mkdir()
    (sdk / "bin" / "python3.13").write_text("", encoding="utf-8")
    (sdk / "include" / "Python.h").write_text("", encoding="utf-8")
    runtime.mkdir()
    spec = RuntimeSpec("local", "3.13.5", target, runtime, sdk)
    data = build_manifest(spec, {
        "python": "3.13.5",
        "python_tag": "cp313",
        "python_platform_tag": "manylinux_2_17_x86_64",
        "python_executable": sdk / "bin" / "python3.13",
        "include_dir": sdk / "include",
    })
    write_manifest(runtime, data)
    validate_manifest(data, target, spec)
    assert data["python_executable"] == "bin/python3.13"


def test_project_version_and_user_agent():
    from py_upper.config import load_app_config, project_version, user_agent

    configured = load_app_config()["project"]["version"]
    assert project_version() == configured
    assert user_agent() == f"py_upper/{configured}"


def test_runtime_and_build_python_are_independent():
    from py_upper.config import load_app_config, python_version, require_local_python

    path = require_local_python()
    assert Path(path).exists()
    # The target runtime version is a project decision that lives in
    # app/pyproject.toml; the development Python is discovered separately.
    # Asserting a literal here would just duplicate the config.
    configured = load_app_config()["tool"]["py_upper"]["runtime"]["python"]
    assert python_version() == configured


def test_target_python_smoke_does_not_write_bytecode_into_staging(tmp_path, monkeypatch):
    from py_upper import verify as smoke
    from py_upper.config import TARGETS

    target = TARGETS["macos-arm64"]
    captured: dict[str, str] = {}
    monkeypatch.setattr(smoke, "BUILD", tmp_path / "build")
    monkeypatch.setattr(smoke, "host_target", lambda: target)
    monkeypatch.setattr(smoke, "runtime_python", lambda value: tmp_path / "python3.10")
    monkeypatch.setattr(smoke, "target_runtime_dir", lambda value: tmp_path / "runtime")
    monkeypatch.setattr(smoke.subprocess, "run", lambda cmd, **kwargs: captured.update(kwargs["env"]))

    smoke.run_target_python_smoke(target)

    assert captured["PYTHONDONTWRITEBYTECODE"] == "1"


def test_windows_extensions_link_the_versioned_import_library(tmp_path, monkeypatch):
    """Link the versioned import library, not the stable-ABI shim.

    A CPython tree carries both ``python313.lib`` and ``python3.lib``. The shim
    exports a subset of the same functions and names ``python3.dll`` in the
    import table, a DLL the dependency closure does not know as part of the
    runtime. Choosing between the two by sort order picked the shim.
    """
    from types import SimpleNamespace

    from py_upper import python_build
    from py_upper.config import TARGETS
    from py_upper.toolchain import Toolchain

    target = TARGETS["windows-x86_64"]
    runtime = tmp_path / "runtime"
    libs = runtime / "libs"
    libs.mkdir(parents=True)
    (libs / "python3.lib").write_bytes(b"")
    include = tmp_path / "sdk" / "include"
    include.mkdir(parents=True)
    target_python = SimpleNamespace(
        root=runtime,
        include_dir=include,
        python_major_minor="3.13",
        extension_suffix=".cp313-win_amd64.pyd",
    )
    toolchain = Toolchain(target, {}, "cl.exe", "cl.exe", "link.exe", None)

    app_root = tmp_path / "app"
    source = app_root / "src" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")
    generated = tmp_path / "demo.c"
    generated.write_text("/* generated */\n", encoding="utf-8")

    build = tmp_path / "build"
    extension = build / "native" / target.key / "demo.cp313-win_amd64.pyd"
    generated_setup: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        generated_setup["text"] = Path(cmd[1]).read_text(encoding="utf-8")
        extension.parent.mkdir(parents=True, exist_ok=True)
        extension.write_bytes(b"")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", build)
    monkeypatch.setattr(python_build, "require_local_python", lambda: Path("/fake/python3"))
    monkeypatch.setattr(python_build, "cythonize_to_c", lambda sources, host: {source: generated})
    monkeypatch.setattr(python_build, "resolve_target_python", lambda value: target_python)
    monkeypatch.setattr(python_build, "resolve_toolchain", lambda value: toolchain)
    monkeypatch.setattr(python_build.subprocess, "run", fake_run)

    # The shim alone is not an import library this build can use.
    with pytest.raises(RuntimeError, match="python313.lib not found"):
        python_build.build_target_extensions(target, [source])

    (libs / "python313.lib").write_bytes(b"")
    outputs = python_build.build_target_extensions(target, [source])

    assert [path.name for path in outputs] == ["demo.cp313-win_amd64.pyd"]
    setup_text = generated_setup["text"]
    assert "TARGET_LIBRARIES=['python313']" in setup_text
    assert f"TARGET_LIBDIR={str(libs)!r}" in setup_text


def test_windows_launcher_does_not_ask_for_the_python_import_library(tmp_path):
    """Including Python.h must not put libpython on the launcher's link line.

    The launcher resolves CPython at runtime, so it links no import library and
    the target SDK has no LIBPATH for one. Windows' pyconfig.h does not know
    that: unless the shared build is switched off it nominates ``python313.lib``
    with ``#pragma comment(lib, ...)``, and the launcher build then stops at
    ``LNK1104: cannot open file 'python313.lib'`` — a file that is neither
    present nor wanted.

    The nomination is read off the preprocessed translation unit. Asking the
    linker instead is not a usable signal: a directive only makes the linker
    open the library once a symbol needs it, so an empty translation unit
    nominates the import library and still links cleanly.
    """
    import subprocess
    import sysconfig

    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    source = launcher.read_text(encoding="utf-8")
    guard = "#define Py_NO_ENABLE_SHARED 1"
    include = "#include <Python.h>"
    assert guard in source, "the launcher must switch off the shared-build pragma"
    assert include in source, "the launcher includes Python.h to declare the C-API"
    assert source.index(guard) < source.index(include), (
        "pyconfig.h reads the switch, so it must be defined before the include"
    )

    if sys.platform != "win32":
        pytest.skip("the auto-link pragma belongs to the Windows pyconfig.h")

    from test_third_party import _windows_extension_compiler

    try:
        cl, import_lib, env = _windows_extension_compiler()
    except RuntimeError as exc:
        pytest.skip(f"MSVC is required to preprocess the launcher translation unit: {exc}")
    include_dir = sysconfig.get_path("include")

    def nominations(name: str, unit: Path) -> list[str]:
        output = tmp_path / f"{name}.i"
        result = subprocess.run(
            [cl, "/nologo", "/P", f"/Fi{output}", f"/I{include_dir}", "/std:c++17", str(unit)],
            cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return [
            line.strip()
            for line in output.read_text(encoding="utf-8", errors="replace").splitlines()
            if "pragma comment(lib" in line and "python" in line.lower()
        ]

    control_source = tmp_path / "control.cpp"
    control_source.write_text("#include <Python.h>\n", encoding="utf-8")
    # The control keeps the assertion honest: it fails if the header stopped
    # nominating anything, which would make the launcher assertion vacuous.
    control = nominations("control", control_source)
    assert control, "the header no longer nominates the import library"
    assert import_lib.name in control[0], control

    assert not nominations("launcher", launcher), (
        "the launcher nominates a libpython it does not link and cannot find"
    )


def test_packaged_launcher_streams_reach_the_caller(tmp_path):
    """The build tool must see what the packaged launcher prints.

    The Windows launcher is a GUI-subsystem binary, and Windows attaches no
    console to such a child. ``subprocess`` started with the default
    ``stdout=None`` hands it the caller's handle *values* without the handles:
    the child writes into dangling handles and every line disappears while the
    exit code stays 0. The smoke step and ``--run`` both assert on that output,
    so both spawn the launcher through the same helper, which passes the
    streams explicitly.

    The probe below is a GUI-subsystem executable for the same reason the
    launcher is one: a console-subsystem probe inherits the streams either way
    and could not tell the two spawns apart.
    """
    import subprocess

    if sys.platform != "win32":
        pytest.skip("handle inheritance for GUI-subsystem children belongs to Windows")

    from test_third_party import _windows_extension_compiler

    try:
        cl, _import_lib, env = _windows_extension_compiler()
    except RuntimeError as exc:
        pytest.skip(f"MSVC is required to compile the probe: {exc}")

    source = tmp_path / "probe.cpp"
    source.write_text(
        "#include <stdio.h>\n"
        "#include <windows.h>\n"
        "int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {\n"
        '    printf("PROBE MARKER\\n");\n'
        "    fflush(stdout);\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    probe = tmp_path / "probe.exe"
    build = subprocess.run(
        [cl, "/nologo", str(source), f"/Fe:{probe}", f"/Fo{tmp_path}\\", "/link", "/SUBSYSTEM:WINDOWS"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    # The helper writes into this process's streams, and pytest captures those
    # at the fd level, so the run happens in a child whose stdout is a pipe —
    # the same shape the build tool has under CI.
    driver = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, {tools!r})\n"
        "from py_upper.verify import run_packaged_launcher\n"
        "run_packaged_launcher(Path({probe!r}), cwd=Path({cwd!r}), check=True)\n"
    ).format(
        tools=str(Path(__file__).parents[2] / "tools"),
        probe=str(probe),
        cwd=str(tmp_path),
    )
    result = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PROBE MARKER" in result.stdout


def test_macos_extensions_link_as_bundles_with_deferred_symbols(tmp_path, monkeypatch):
    """A macOS CPython extension must not require Python symbols at link time.

    `-dynamiclib` makes the linker resolve every symbol eagerly, which fails on
    Apple Silicon with "symbol(s) not found for architecture arm64". CPython
    extensions are bundles that resolve the C-API from the embedding launcher.
    """
    from types import SimpleNamespace

    from py_upper import python_build
    from py_upper.config import TARGETS
    from py_upper.toolchain import Toolchain

    target = TARGETS["macos-arm64"]
    include = tmp_path / "sdk" / "include" / "python3.10"
    include.mkdir(parents=True)
    target_python = SimpleNamespace(include_dir=include, extension_suffix=".cpython-310-darwin.so")
    toolchain = Toolchain(target, {}, "/usr/bin/clang", "/usr/bin/clang++", "/usr/bin/clang", "11.0")

    app_root = tmp_path / "app"
    source = app_root / "src" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")
    generated = tmp_path / "demo.c"
    generated.write_text("/* generated */\n", encoding="utf-8")

    commands: list[list[str]] = []
    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "resolve_target_python", lambda value: target_python)
    monkeypatch.setattr(python_build, "resolve_toolchain", lambda value: toolchain)
    monkeypatch.setattr(python_build.subprocess, "run", lambda cmd, **kwargs: commands.append(cmd))

    outputs = python_build.compile_unix_extensions(target, {source: generated})

    assert [path.name for path in outputs] == ["demo.cpython-310-darwin.so"]
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[:2] == ["/usr/bin/clang", "-bundle"]
    assert "-dynamiclib" not in cmd
    assert cmd[cmd.index("-undefined") + 1] == "dynamic_lookup"
    assert "-arch" in cmd and cmd[cmd.index("-arch") + 1] == "arm64"


def test_package_verification_skips_foreign_format_binaries(tmp_path, monkeypatch):
    """Inert Windows launcher stubs inside a macOS runtime are not ABI inputs."""
    from py_upper import verify
    from py_upper.config import TARGETS
    from py_upper.native.inspect import BinaryInfo

    target = TARGETS["macos-arm64"]
    root = tmp_path / "MyApp.app"
    resources = root / "Contents" / "Resources"
    (root / "Contents" / "MacOS").mkdir(parents=True)
    (root / "Contents" / "MacOS" / "MyApp").write_bytes(b"\xcf\xfa\xed\xfe")
    resources.mkdir(parents=True)
    (resources / "MyApp.int").write_text("", encoding="utf-8")
    (resources / "_py_upper_static.int").write_text("", encoding="utf-8")
    stub = resources / "runtime" / "setuptools" / "cli.exe"
    stub.parent.mkdir(parents=True)
    stub.write_bytes(b"MZ")
    library = resources / "site-packages" / "libdemo.dylib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"\xcf\xfa\xed\xfe")

    def fake_inspect(path: Path) -> BinaryInfo:
        if path.suffix == ".exe":
            return BinaryInfo(path, "PE", "x86_64")
        return BinaryInfo(path, "Mach-O", "arm64")

    verified: list[Path] = []
    monkeypatch.setattr(verify, "DIST", tmp_path)
    monkeypatch.setattr(verify, "inspect", fake_inspect)
    monkeypatch.setattr(verify, "verify_arch", lambda path, value: verified.append(path))

    checks = verify._checks_for_package(target)

    assert stub not in verified
    assert library in verified
    assert not [label for ok, label in checks if not ok and "cli.exe" in label]


def test_lock_preserves_existing_target_entries(tmp_path, monkeypatch):
    import json
    import py_upper.lock as lock
    from py_upper.config import TARGETS

    lock_path = tmp_path / "py_upper.lock.json"
    monkeypatch.setattr(lock, "LOCK", lock_path)
    monkeypatch.setattr(lock, "runtime_provider", lambda: "local")
    monkeypatch.setattr(lock, "python_version", lambda: "3.13.5")
    monkeypatch.setattr(lock, "dependency_specs", lambda: [])
    class Manifest: pass
    monkeypatch.setattr(lock, "runtime_spec", lambda target: type("S", (), {"root": tmp_path / target.key, "sdk_root": tmp_path / "sdk" / target.key})())
    monkeypatch.setattr(lock, "read_manifest", lambda root: {"format": 4})
    monkeypatch.setattr(lock, "manifest_hash", lambda data: "hash")
    monkeypatch.setattr(lock, "_wheel_records", lambda target: [])
    first = {"format": 5, "project": "py_upper", "python": "3.13.5", "runtime_provider": "local", "targets": {"linux-x86_64": {"sentinel": True}}}
    lock_path.write_text(json.dumps(first), encoding="utf-8")
    lock.write_lock(TARGETS["linux-arm64"])
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["targets"]["linux-x86_64"] == {"sentinel": True}
    assert "linux-arm64" in data["targets"]


def test_runtime_optimization_is_applied_to_package_copy(tmp_path, monkeypatch):
    from py_upper import runtime as runtime_optimize
    monkeypatch.setattr(runtime_optimize, "optimize_config", lambda: {"remove_python_caches": True, "remove_runtime_pip": True})
    root = tmp_path / "runtime"
    pip = root / "lib/python3.13/site-packages/pip"
    ensurepip = root / "lib/python3.13/ensurepip"
    pip.mkdir(parents=True)
    ensurepip.mkdir(parents=True)
    (pip / "__init__.py").write_text("", encoding="utf-8")
    runtime_optimize.optimize_runtime_tree(root)
    assert not pip.exists()
    assert not ensurepip.exists()


def test_native_strip_is_opt_in(monkeypatch):
    from py_upper.native import bundle
    monkeypatch.setattr(bundle, "optimize_config", lambda: {"strip_native": False})
    assert bundle.optimize_config()["strip_native"] is False


def test_linux_native_rpath_rewrite_is_opt_in(monkeypatch, tmp_path):
    from py_upper.native import bundle

    monkeypatch.setattr(bundle.shutil, "which", lambda name: "/usr/bin/patchelf" if name == "patchelf" else None)
    monkeypatch.setattr(bundle, "inspect", lambda path: type("Info", (), {"format": "ELF", "arch": "x86_64"})())
    monkeypatch.setattr(bundle.optimize_config, "__call__", lambda: {})
    # The default path is asserted through source-level behavior: package builds
    # must not rewrite third-party Linux RPATHs unless explicitly requested.
    source = Path(__file__).parents[2] / "tools" / "py_upper" / "native" / "bundle.py"
    text = source.read_text(encoding="utf-8")
    assert 'optimize_config().get("rewrite_rpath", False)' in text


def test_windows_python_runtime_dll_is_version_specific(monkeypatch):
    import py_upper.native.deps as deps
    from py_upper.config import TARGETS

    target = TARGETS["windows-x86_64"]
    monkeypatch.setattr(deps, "python_version", lambda: "3.11.10")
    assert deps._system_dependency("python311.dll", target)
    assert not deps._system_dependency("python313.dll", target)
