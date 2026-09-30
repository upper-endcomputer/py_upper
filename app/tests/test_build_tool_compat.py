import sys
import stat
from pathlib import Path


def test_supported_build_python_version():
    assert sys.version_info >= (3, 8)


def test_toml_compatibility_layer():
    from py_upper.compat import tomllib

    assert callable(tomllib.loads)
    data = tomllib.loads('name = "py_upper"\n[tool.py_upper]\nentry = "main.py"\n')
    assert data["name"] == "py_upper"
    assert data["tool"]["py_upper"]["entry"] == "main.py"


def test_runtime_imports_user_agent():
    from py_upper import runtime
    assert callable(runtime.user_agent)


def test_cython_bootstrap_uses_project_cache_when_missing(monkeypatch, tmp_path):
    from py_upper import python_build

    calls = []
    versions = iter([None, "3.1.3"])

    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "_cython_version", lambda host, env=None: next(versions))
    monkeypatch.setattr(python_build, "run", lambda cmd, cwd=None, env=None: calls.append((cmd, cwd, env)))

    env = python_build._cython_env(Path(sys.executable))

    assert calls
    assert calls[0][0][0] == str(Path(sys.executable))
    assert calls[0][0][3] == "install"
    assert "Cython>=3.1,<3.2" in calls[0][0]
    assert "--upgrade" not in calls[0][0]
    assert env["PYTHONPATH"].startswith(str(tmp_path / "build" / "host-tools"))


def test_cython_bootstrap_reuses_existing_project_cache(monkeypatch, tmp_path):
    from py_upper import python_build

    cache = tmp_path / "build" / "host-tools" / "cython-python3.10-3.10.10"
    cache.mkdir(parents=True)
    calls = []
    versions = iter([None, "3.1.3"])

    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "_cython_cache_dir", lambda host: cache)
    monkeypatch.setattr(python_build, "_cython_version", lambda host, env=None: next(versions))
    monkeypatch.setattr(python_build, "run", lambda cmd, cwd=None, env=None: calls.append(cmd))

    env = python_build._cython_env(Path(sys.executable))

    assert not calls
    assert env["PYTHONPATH"].startswith(str(cache))


def test_selected_sources_compile_all_application_modules_except_package_markers():
    from py_upper import python_build

    sources = {python_build.module_name(p) for p in python_build.selected_sources()}
    assert sources == {"main", "core.app", "models.state", "services.hello", "utils.paths"}


def test_cythonize_keeps_generated_c_out_of_src(monkeypatch, tmp_path):
    from py_upper import python_build

    source = tmp_path / "src" / "core" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run_application():\n    return 1\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, cwd=None, env=None):
        calls.append((cmd, cwd))
        assert cwd == source.parent
        assert "--force" in cmd
        assert cmd[-1] == "app.py"
        output = cwd / cmd[-2]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("/* generated */\n", encoding="utf-8")

    monkeypatch.setattr(python_build, "APP", tmp_path)
    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "_cython_env", lambda host: {"PYTHONPATH": "cached-cython"})
    monkeypatch.setattr(python_build, "run", fake_run)
    generated = python_build.cythonize_to_c([source], Path(sys.executable))

    assert calls
    assert source not in generated or generated[source].exists()
    assert generated[source].parent == tmp_path / "build" / "cython" / "core"
    assert generated[source].exists()
    assert not (source.parent / "app.c").exists()


def test_macho_arm64_header_is_detected_with_correct_byte_order(tmp_path):
    from py_upper.native.inspect import inspect

    # Mach-O arm64 little-endian header: magic bytes CF FA ED FE,
    # cputype 0x0100000c encoded little-endian.
    binary = tmp_path / "libarm64.dylib"
    binary.write_bytes(bytes.fromhex("cffaedfe0c0000010000000000000000"))

    info = inspect(binary)
    assert info.format == "Mach-O"
    assert info.arch == "arm64"


def test_macho_universal_header_must_contain_target_arch(tmp_path):
    from py_upper.config import TARGETS
    from py_upper.native.inspect import verify_arch

    binary = tmp_path / "universal.dylib"
    # FAT_MAGIC, two slices: x86_64 then arm64.
    header = bytearray()
    header += bytes.fromhex("cafebabe")
    header += (2).to_bytes(4, "big")
    for cputype in (0x01000007, 0x0100000C):
        header += cputype.to_bytes(4, "big")
        header += (3).to_bytes(4, "big")  # CPU subtype
        header += (0).to_bytes(4, "big")  # offset
        header += (0).to_bytes(4, "big")  # size
        header += (0).to_bytes(4, "big")  # alignment
    binary.write_bytes(header)

    assert verify_arch(binary, TARGETS["macos-arm64"]).arch == "universal"

    x86_only = tmp_path / "x86-universal.dylib"
    x86_only.write_bytes(header[:4] + (1).to_bytes(4, "big") + header[8:28])
    try:
        verify_arch(x86_only, TARGETS["macos-arm64"])
    except RuntimeError as exc:
        assert "expected arm64" in str(exc)
    else:
        raise AssertionError("x86_64-only universal binary was accepted for arm64")


def test_remove_cython_sources_leaves_only_package_markers(tmp_path, monkeypatch):
    from py_upper import python_build

    src = tmp_path / "src"
    site = tmp_path / "site"
    (src / "core").mkdir(parents=True)
    (src / "utils").mkdir(parents=True)
    (src / "core" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (src / "core" / "__init__.py").write_text("", encoding="utf-8")
    (src / "utils" / "paths.py").write_text("x = 1\n", encoding="utf-8")
    (src / "utils" / "__init__.py").write_text("", encoding="utf-8")
    (site / "core").mkdir(parents=True)
    (site / "utils").mkdir(parents=True)
    (site / "core" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (site / "core" / "__init__.py").write_text("", encoding="utf-8")
    (site / "utils" / "paths.py").write_text("x = 1\n", encoding="utf-8")
    (site / "utils" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(python_build, "APP", tmp_path)
    monkeypatch.setattr(python_build, "selected_sources", lambda: [src / "core" / "app.py", src / "utils" / "paths.py"])

    python_build.remove_cython_source_py(site)

    assert not (site / "core" / "app.py").exists()
    assert not (site / "utils" / "paths.py").exists()
    assert (site / "core" / "__init__.py").exists()
    assert (site / "utils" / "__init__.py").exists()


def test_package_tree_copy_does_not_replay_source_modes_or_metadata(monkeypatch, tmp_path):
    from py_upper import package

    source = tmp_path / "runtime"
    destination = tmp_path / "bundle-runtime"
    (source / "lib" / "pkg").mkdir(parents=True)
    readonly = source / "lib" / "pkg" / "README.rst"
    readonly.write_text("read me\n", encoding="utf-8")
    readonly.chmod(0o444)

    calls = []
    real_chmod = package.os.chmod

    def record_chmod(*args, **kwargs):
        calls.append(args[0])
        return real_chmod(*args, **kwargs)

    monkeypatch.setattr(package.os, "chmod", record_chmod)
    package._copy_tree_contents(source, destination)

    copied = destination / "lib" / "pkg" / "README.rst"
    assert copied.read_text(encoding="utf-8") == "read me\n"
    assert copied not in calls
    assert stat.S_IMODE(copied.stat().st_mode) & 0o111 == 0


def test_package_tree_copy_preserves_symlinks_and_launcher_executable_mode(tmp_path):
    from py_upper import package

    source = tmp_path / "runtime"
    destination = tmp_path / "bundle-runtime"
    source.mkdir()
    executable = source / "python"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    link = source / "python3"
    link.symlink_to("python")

    package._copy_tree_contents(source, destination)

    assert (destination / "python").read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert (destination / "python3").is_symlink()
    assert (destination / "python3").readlink() == Path("python")


def test_make_executable_adds_only_execute_bits(tmp_path):
    from py_upper import package

    path = tmp_path / "launcher"
    path.write_bytes(b"x")
    path.chmod(0o644)

    package._make_executable(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_native_dependency_resolver_finds_nested_libraries(tmp_path):
    from py_upper.native.deps import resolve_dependency

    runtime = tmp_path / "runtime"
    nested = runtime / "lib" / "tcl9.0"
    nested.mkdir(parents=True)
    names = [
        "libtcl9thread3.0.6.dylib",
        "libthread3.0.6.dylib",
        "libitcl4.3.8.dylib",
        "libtcl9itcl4.3.8.dylib",
    ]
    for name in names:
        (nested / name).write_bytes(b"dylib")

    owner = tmp_path / "site-packages" / "_tkinter.cpython.so"
    owner.parent.mkdir(parents=True)
    owner.write_bytes(b"dylib")

    for name in names:
        assert resolve_dependency(name, [tmp_path / "site-packages", runtime], owner) == (nested / name).resolve()


def test_native_dependency_resolver_uses_macos_rpath(monkeypatch, tmp_path):
    from py_upper.native.deps import resolve_dependency

    runtime_lib = tmp_path / "runtime" / "lib"
    runtime_lib.mkdir(parents=True)
    lib = runtime_lib / "libitcl4.3.8.dylib"
    lib.write_bytes(b"dylib")
    owner = tmp_path / "runtime" / "lib" / "itcl4.3.8" / "libtcl9itcl4.3.8.dylib"
    owner.parent.mkdir(parents=True)
    owner.write_bytes(b"dylib")

    monkeypatch.setattr("py_upper.native.deps._mac_rpaths", lambda path: ["@loader_path/../"])
    assert resolve_dependency(
        "@rpath/libitcl4.3.8.dylib",
        [tmp_path / "runtime"],
        owner,
    ) == lib.resolve()
