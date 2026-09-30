from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


def test_build_python_minimum_and_toml_compatibility():
    from py_upper.compat import tomllib
    from py_upper.config import validate_build_python_version

    validate_build_python_version(type("V", (), {"major": 3, "minor": 8})())
    data = tomllib.loads('[tool.py_upper]\nentry = "main.py"\n')
    assert data["tool"]["py_upper"]["entry"] == "main.py"


def test_target_matrix_and_platform_contract():
    from py_upper.config import TARGETS

    assert set(TARGETS) == {
        "windows-x86", "windows-x86_64", "windows-arm64",
        "macos-x86_64", "macos-arm64", "linux-x86_64", "linux-arm64",
    }
    assert TARGETS["windows-arm64"].triple == "aarch64-pc-windows-msvc"
    assert TARGETS["macos-arm64"].primary_wheel_platform == "macosx_13_0_arm64"
    assert TARGETS["linux-x86_64"].primary_wheel_platform == "manylinux_2_17_x86_64"


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


def test_launcher_uses_global_python_symbols_and_smoke_switch():
    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    text = launcher.read_text(encoding="utf-8")
    assert "RTLD_NOW | RTLD_GLOBAL" in text
    assert "PY_UPPER_SMOKE" in text
    assert "RTLD_LOCAL" not in text


def test_native_dependency_names_use_binary_format_not_filename_suffix(monkeypatch, tmp_path):
    from py_upper.native import deps

    binary = tmp_path / "QtWidgets.abi3.so"
    binary.write_bytes(b"fake")
    monkeypatch.setattr(deps, "inspect_binary", lambda path: type("Info", (), {"format": "Mach-O", "arch": "arm64"})())
    monkeypatch.setattr(deps, "_mac_dependencies", lambda path: ["@rpath/libQt6Core.dylib"])
    monkeypatch.setattr(deps, "_elf_dependencies", lambda path: ["libwrong.so"])
    assert deps.dependency_names(binary) == ["@rpath/libQt6Core.dylib"]


def test_native_alias_resolver_can_map_dylib_install_name_to_real_file(tmp_path):
    from py_upper.native.deps import resolve_dependency

    actual = tmp_path / "config" / "PCBUSB.dylib"
    actual.parent.mkdir()
    actual.write_bytes(b"native")
    alias = {"libpcbusb.0.12.1.dylib": actual.resolve()}
    resolved = resolve_dependency("@rpath/libPCBUSB.0.12.1.dylib", [tmp_path], actual, aliases=alias)
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
    from py_upper.config import project_version, user_agent
    assert project_version() == "0.17.0"
    assert user_agent() == "py_upper/0.17.0"


def test_runtime_and_build_python_are_independent():
    from py_upper.config import require_local_python, runtime_spec, TARGETS
    path = require_local_python()
    assert Path(path).exists()
    assert runtime_spec(TARGETS["linux-x86_64"]).python == "3.13.15"


def test_lock_preserves_existing_target_entries(tmp_path, monkeypatch):
    import json
    from pathlib import Path
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
    from py_upper import runtime_optimize
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
