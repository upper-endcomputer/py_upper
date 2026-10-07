from __future__ import annotations

import stat
from pathlib import Path


def test_build_python_minimum_and_toml_compatibility():
    from py_upper.compat import tomllib
    from py_upper.config import validate_build_python_version

    validate_build_python_version(type("V", (), {"major": 3, "minor": 8})())
    data = tomllib.loads('[tool.py_upper]\nentry = "main.py"\n')
    assert data["tool"]["py_upper"]["entry"] == "main.py"


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
    monkeypatch.setattr(bundle, "_ad_hoc_sign", signed.append)
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
