"""Incremental application compilation: only changed modules are rebuilt.

The build tool used to wipe its intermediate tree and regenerate every
translation unit and every extension on every build, which made a rebuild cost
the same as the first build. These tests pin the cache contract: an unchanged
module reuses its artifact, a changed one does not, and the two layers chain
(a changed source invalidates the extension built from its C output).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from py_upper import python_build
from py_upper.config import TARGETS
from py_upper.toolchain import Toolchain


def _app_source(root: Path, relative: str = "pkg/demo.py") -> Path:
    source = root / "src" / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("x = 1\n", encoding="utf-8")
    return source


def _stub_cython(monkeypatch, app_root: Path, build: Path) -> list[list[str]]:
    """Cython as a process that writes exactly the ``-o`` it was given."""
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        destination = (Path(kwargs.get("cwd") or ".") / cmd[cmd.index("-o") + 1]).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("/* generated */\n", encoding="utf-8")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", build)
    monkeypatch.setattr(python_build, "_cython_env", lambda host: {})
    monkeypatch.setattr(python_build, "_cython_version", lambda host, env: "3.1.2")
    monkeypatch.setattr(python_build.subprocess, "run", fake_run)
    return commands


def test_unchanged_source_reuses_the_cached_translation_unit(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    source = _app_source(app_root)
    commands = _stub_cython(monkeypatch, app_root, tmp_path / "build")

    first = python_build.cythonize_to_c([source], Path("/fake/python3"))
    second = python_build.cythonize_to_c([source], Path("/fake/python3"))

    assert len(commands) == 1
    assert first[source] == second[source]
    assert first[source].is_file()


def test_changed_source_regenerates_only_that_module(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    stable = _app_source(app_root, "pkg/stable.py")
    edited = _app_source(app_root, "pkg/edited.py")
    commands = _stub_cython(monkeypatch, app_root, tmp_path / "build")

    python_build.cythonize_to_c([stable, edited], Path("/fake/python3"))
    assert len(commands) == 2

    edited.write_text("x = 2\n", encoding="utf-8")
    outputs = python_build.cythonize_to_c([stable, edited], Path("/fake/python3"))

    assert len(commands) == 3
    assert [Path(cmd[-1]).name for cmd in commands[2:]] == ["edited.py"]
    assert outputs[stable].is_file() and outputs[edited].is_file()


def test_a_new_cython_version_invalidates_the_cache(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    source = _app_source(app_root)
    commands = _stub_cython(monkeypatch, app_root, tmp_path / "build")

    python_build.cythonize_to_c([source], Path("/fake/python3"))
    monkeypatch.setattr(python_build, "_cython_version", lambda host, env: "3.2.4")
    python_build.cythonize_to_c([source], Path("/fake/python3"))

    assert len(commands) == 2


def _unix_fixture(tmp_path, monkeypatch):
    target = TARGETS["linux-x86_64"]
    include = tmp_path / "sdk" / "include"
    include.mkdir(parents=True)
    target_python = SimpleNamespace(include_dir=include, extension_suffix=".cpython-310-x86_64-linux-gnu.so")
    toolchain = Toolchain(target, {}, "gcc", "g++", "gcc", None)

    app_root = tmp_path / "app"
    source = _app_source(app_root)
    generated = tmp_path / "demo.c"
    generated.write_text("/* generated */\n", encoding="utf-8")

    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "resolve_target_python", lambda value: target_python)
    monkeypatch.setattr(python_build, "resolve_toolchain", lambda value: toolchain)
    monkeypatch.setattr(python_build.subprocess, "run", fake_run)
    return target, source, generated, commands


def test_unchanged_extension_is_not_recompiled(tmp_path, monkeypatch):
    target, source, generated, commands = _unix_fixture(tmp_path, monkeypatch)

    first = python_build.compile_unix_extensions(target, {source: generated})
    second = python_build.compile_unix_extensions(target, {source: generated})

    assert len(commands) == 1
    assert [path.name for path in first] == ["demo.cpython-310-x86_64-linux-gnu.so"]
    assert first == second


def test_changed_c_output_invalidates_the_extension(tmp_path, monkeypatch):
    target, source, generated, commands = _unix_fixture(tmp_path, monkeypatch)

    python_build.compile_unix_extensions(target, {source: generated})
    generated.write_text("/* regenerated */\n", encoding="utf-8")
    python_build.compile_unix_extensions(target, {source: generated})

    assert len(commands) == 2


def test_incremental_build_can_be_switched_off(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    source = _app_source(app_root)
    commands = _stub_cython(monkeypatch, app_root, tmp_path / "build")
    monkeypatch.setattr(python_build, "incremental_build_enabled", lambda: False)

    python_build.cythonize_to_c([source], Path("/fake/python3"))
    python_build.cythonize_to_c([source], Path("/fake/python3"))

    assert len(commands) == 2


def test_windows_unchanged_extension_skips_the_setuptools_driver(tmp_path, monkeypatch):
    target = TARGETS["windows-x86_64"]
    runtime = tmp_path / "runtime"
    libs = runtime / "libs"
    libs.mkdir(parents=True)
    (libs / "python313.lib").write_bytes(b"")
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
    source = _app_source(app_root)
    generated = tmp_path / "demo.c"
    generated.write_text("/* generated */\n", encoding="utf-8")
    build = tmp_path / "build"

    invocations: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        invocations.append(cmd)
        output = build / "native" / target.key / "pkg" / "demo.cp313-win_amd64.pyd"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", build)
    monkeypatch.setattr(python_build, "require_local_python", lambda: Path("/fake/python3"))
    monkeypatch.setattr(python_build, "cythonize_to_c", lambda sources, host: {source: generated})
    monkeypatch.setattr(python_build, "resolve_target_python", lambda value: target_python)
    monkeypatch.setattr(python_build, "resolve_toolchain", lambda value: toolchain)
    monkeypatch.setattr(python_build.subprocess, "run", fake_run)

    first = python_build.build_target_extensions(target, [source])
    second = python_build.build_target_extensions(target, [source])

    assert len(invocations) == 1
    assert [path.name for path in first] == ["demo.cp313-win_amd64.pyd"]
    assert first == second


def test_build_jobs_and_incremental_are_configurable(monkeypatch):
    from py_upper import config

    monkeypatch.setattr(config, "py_upper_config", lambda: {})
    assert config.build_jobs() >= 1
    assert config.incremental_build_enabled() is True

    monkeypatch.setattr(config, "py_upper_config", lambda: {"build": {"jobs": 3, "incremental": False}})
    assert config.build_jobs() == 3
    assert config.incremental_build_enabled() is False

    monkeypatch.setattr(config, "py_upper_config", lambda: {"build": {"jobs": 0}})
    assert config.build_jobs() >= 1

    monkeypatch.setattr(config, "py_upper_config", lambda: {"build": {"jobs": -1}})
    with pytest.raises(RuntimeError, match="jobs"):
        config.build_jobs()

    monkeypatch.setattr(config, "py_upper_config", lambda: {"build": {"jobs": "many"}})
    with pytest.raises(RuntimeError, match="jobs"):
        config.build_jobs()
