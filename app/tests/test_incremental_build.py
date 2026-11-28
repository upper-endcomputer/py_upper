# 中文维护说明：验证Cython 与原生编译的增量命中及失效；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
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

from appbuild import python_build
from appbuild.config import TARGETS
from appbuild.toolchain import Toolchain


def _app_source(root: Path, relative: str = "pkg/demo.py") -> Path:
    """创建最小应用源文件并将模块路径解析定位到测试目录。"""
    source = root / "src" / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("x = 1\n", encoding="utf-8")
    return source


def _stub_cython(monkeypatch, app_root: Path, build: Path) -> list[list[str]]:
    """替换 Cython 子进程为可记录生成次数的桩。

    Cython as a process that writes exactly the ``-o`` it was given."""
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        """记录翻译命令并创建预期生成的 C 文件。"""
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
    """验证源码未变化时复用已生成的 C 文件。"""
    app_root = tmp_path / "app"
    source = _app_source(app_root)
    commands = _stub_cython(monkeypatch, app_root, tmp_path / "build")

    first = python_build.cythonize_to_c([source], Path("/fake/python3"))
    second = python_build.cythonize_to_c([source], Path("/fake/python3"))

    assert len(commands) == 1
    assert first[source] == second[source]
    assert first[source].is_file()


def test_changed_source_regenerates_only_that_module(tmp_path, monkeypatch):
    """验证单个源码变化只重译对应模块。"""
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


def test_cython_output_path_survives_a_foreign_windows_drive(tmp_path, monkeypatch):
    """验证源码和缓存跨盘时仍生成有效绝对输出路径。

    The app and build/ may live on different Windows drives."""
    app_root = tmp_path / "app"
    source = _app_source(app_root)
    commands = _stub_cython(monkeypatch, app_root, tmp_path / "build")

    def cross_drive(*_args, **_kwargs):
        """模拟 Windows 跨盘相对路径计算失败。"""
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(python_build.os.path, "relpath", cross_drive)
    python_build.cythonize_to_c([source], Path("/fake/python3"))

    output = Path(commands[0][commands[0].index("-o") + 1])
    assert output.is_absolute()


def test_a_new_cython_version_invalidates_the_cache(tmp_path, monkeypatch):
    """验证 Cython 版本升级会重新生成 C 文件。"""
    app_root = tmp_path / "app"
    source = _app_source(app_root)
    commands = _stub_cython(monkeypatch, app_root, tmp_path / "build")

    python_build.cythonize_to_c([source], Path("/fake/python3"))
    monkeypatch.setattr(python_build, "_cython_version", lambda host, env: "3.2.4")
    python_build.cythonize_to_c([source], Path("/fake/python3"))

    assert len(commands) == 2


def _unix_fixture(tmp_path, monkeypatch):
    """创建 Unix 编译测试输入及记录命令的桩环境。"""
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
        """记录原生编译并生成模拟扩展产物。"""
        commands.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "resolve_target_python", lambda value: target_python)
    monkeypatch.setattr(python_build, "resolve_toolchain", lambda value: toolchain)
    monkeypatch.setattr(python_build.subprocess, "run", fake_run)
    return target, source, generated, commands


def test_unchanged_extension_is_not_recompiled(tmp_path, monkeypatch):
    """验证编译输入不变时复用原生扩展。"""
    target, source, generated, commands = _unix_fixture(tmp_path, monkeypatch)

    first = python_build.compile_unix_extensions(target, {source: generated})
    second = python_build.compile_unix_extensions(target, {source: generated})

    assert len(commands) == 1
    assert [path.name for path in first] == ["demo.cpython-310-x86_64-linux-gnu.so"]
    assert first == second


def test_changed_c_output_invalidates_the_extension(tmp_path, monkeypatch):
    """验证生成 C 内容变化后重新链接扩展。"""
    target, source, generated, commands = _unix_fixture(tmp_path, monkeypatch)

    python_build.compile_unix_extensions(target, {source: generated})
    generated.write_text("/* regenerated */\n", encoding="utf-8")
    python_build.compile_unix_extensions(target, {source: generated})

    assert len(commands) == 2


def test_incremental_build_can_be_switched_off(tmp_path, monkeypatch):
    """验证关闭增量选项后每次都执行原生编译。"""
    app_root = tmp_path / "app"
    source = _app_source(app_root)
    commands = _stub_cython(monkeypatch, app_root, tmp_path / "build")
    monkeypatch.setattr(python_build, "incremental_build_enabled", lambda: False)

    python_build.cythonize_to_c([source], Path("/fake/python3"))
    python_build.cythonize_to_c([source], Path("/fake/python3"))

    assert len(commands) == 2


def test_windows_unchanged_extension_skips_the_setuptools_driver(tmp_path, monkeypatch):
    """验证 Windows 扩展缓存命中时不启动 setuptools 构建驱动。"""
    target = TARGETS["windows-x86_64"]
    sdk = tmp_path / "sdk"
    libs = sdk / "install" / "libs"
    libs.mkdir(parents=True)
    (libs / "python313.lib").write_bytes(b"")
    include = sdk / "install" / "include"
    include.mkdir(parents=True)
    target_python = SimpleNamespace(
        sdk_root=sdk,
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
        """记录 setuptools 调用并写出目标扩展。"""
        invocations.append(cmd)
        output = build / "native" / target.key / "pkg" / "demo.cp313-win_amd64.pyd"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", build)
    monkeypatch.setattr(python_build, "require_local_python", lambda: Path("/fake/python3"))
    monkeypatch.setattr(python_build, "cython_merge_mode", lambda: "none")
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
    """验证构建并行数和增量开关的默认值及覆盖配置。"""
    from appbuild import config

    monkeypatch.setattr(config, "appbuild_config", lambda: {})
    assert config.build_jobs() >= 1
    assert config.incremental_build_enabled() is True

    monkeypatch.setattr(config, "appbuild_config", lambda: {"build": {"jobs": 3, "incremental": False}})
    assert config.build_jobs() == 3
    assert config.incremental_build_enabled() is False

    monkeypatch.setattr(config, "appbuild_config", lambda: {"build": {"jobs": 0}})
    assert config.build_jobs() >= 1

    monkeypatch.setattr(config, "appbuild_config", lambda: {"build": {"jobs": -1}})
    with pytest.raises(RuntimeError, match="jobs"):
        config.build_jobs()

    monkeypatch.setattr(config, "appbuild_config", lambda: {"build": {"jobs": "many"}})
    with pytest.raises(RuntimeError, match="jobs"):
        config.build_jobs()


@pytest.mark.parametrize("value", ["sometimes", "", 1, 0, [], None])
def test_incremental_rejects_non_boolean_config(monkeypatch, value):
    """验证增量开关拒绝非布尔配置值。"""
    from appbuild import config

    monkeypatch.setattr(config, "appbuild_config", lambda: {"build": {"incremental": value}})
    with pytest.raises(RuntimeError, match="incremental.*boolean"):
        config.incremental_build_enabled()
