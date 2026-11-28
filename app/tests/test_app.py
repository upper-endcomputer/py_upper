# 中文维护说明：验证示例应用入口；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

from pathlib import Path

import pytest

from application import run_application


def test_application(capsys):
    """验证默认命令行入口输出项目问候语。"""
    run_application()
    assert "py_upper" in capsys.readouterr().out


def test_resource_root_prefers_the_launcher_exported_home(monkeypatch, tmp_path):
    """验证资源目录优先采用启动器导出的应用目录，开发态回到源码资源目录。

    The launcher knows the bundle root; counting parent directories guesses."""
    from utils import paths

    monkeypatch.delenv(paths.BUNDLE_HOME_ENV, raising=False)
    assert paths.bundle_home() is None
    assert paths.resource_root() == Path(paths.__file__).resolve().parents[2] / "resources"

    monkeypatch.setenv(paths.BUNDLE_HOME_ENV, str(tmp_path))
    assert paths.bundle_home() == tmp_path.resolve()
    assert paths.resource_root() == tmp_path.resolve() / "resources"


def test_app_name_agrees_with_the_packaging_configuration(monkeypatch):
    """验证应用显示名与打包配置一致，并支持项目自定义名称。

    The name is written once, in app/pyproject.toml, and read from there."""
    from appbuild.config import app_name as packaged_app_name
    from utils import paths

    monkeypatch.delenv(paths.APP_NAME_ENV, raising=False)
    assert paths.app_name() == packaged_app_name()

    # The launcher wins: a renamed executable still reports the configured name.
    monkeypatch.setenv(paths.APP_NAME_ENV, "Renamed Tool")
    assert paths.app_name() == "Renamed Tool"


def test_paths_survive_the_builtin_file_a_merged_bundle_reports(monkeypatch, tmp_path):
    """验证合并扩展的内建模块路径不会被误当成源码路径。

    A merged bundle is a built-in module: __file__ is "built-in", not a path."""
    from utils import paths

    monkeypatch.delenv(paths.BUNDLE_HOME_ENV, raising=False)
    monkeypatch.delenv(paths.APP_NAME_ENV, raising=False)
    monkeypatch.setattr(paths, "__file__", "built-in")
    monkeypatch.setattr(paths.sys, "executable", str(tmp_path / "Upper.exe"))

    assert paths._module_file() is None
    assert paths._project_root() is None
    assert paths._declared_app_name() is None
    assert paths.app_name() == paths._DEFAULT_APP_NAME
    assert paths.resource_root() == tmp_path.resolve() / "resources"


def test_declared_app_name_ignores_the_project_section(tmp_path, monkeypatch):
    """验证产品名称读取专用配置，而不误用发行包名称。"""
    from utils import paths

    project = tmp_path / "app" / "pyproject.toml"
    project.parent.mkdir(parents=True)
    project.write_text(
        '[project]\nname = "not-the-app-name"\n\n[tool.appbuild.app]\nname = "Real App"  # inline comment\n',
        encoding="utf-8",
    )
    module = project.parent / "src" / "utils" / "paths.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "__file__", str(module))

    assert paths._declared_app_name() == "Real App"


def test_gui_opens_a_window_headlessly(monkeypatch, capsys):
    """运行无显示 GUI 入口，验证窗口初始化成功并正常退出。

    The Qt window is the real check that a bundled PySide6 payload works.

    The development interpreter may not have PySide6 installed, so this is
    skipped there; the packaged application exercises the same code path in the
    build's launcher smoke and in the CI integration run.
    """
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is only required for the packaged application")

    from application import run_gui

    monkeypatch.setenv("APPBUILD_HEADLESS", "1")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)

    assert run_gui() == 0
    assert "GUI OK offscreen" in capsys.readouterr().out
