import sys
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
    assert env["PYTHONPATH"].startswith(str(tmp_path / "build" / "host-tools"))


def test_cythonize_uses_relative_source_paths_and_force(monkeypatch, tmp_path):
    from py_upper import python_build

    source = tmp_path / "src" / "core" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run_application():\n    return 1\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, cwd=None, env=None):
        calls.append((cmd, cwd))
        assert cwd == source.parent
        assert "--force" in cmd
        assert cmd[-2:] == ["app.c", "app.py"]
        (cwd / "app.c").write_text("/* generated */\n", encoding="utf-8")

    monkeypatch.setattr(python_build, "APP", tmp_path)
    monkeypatch.setattr(python_build, "_cython_env", lambda host: {"PYTHONPATH": "cached-cython"})
    monkeypatch.setattr(python_build, "run", fake_run)
    python_build.cythonize_to_c([source], Path(sys.executable))

    assert calls
    assert (source.parent / "app.c").exists()


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
