# 中文维护说明：验证应用配置默认值与校验；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import sys
from pathlib import Path

import pytest

from appbuild.config import TARGETS, runtime_spec

def test_targets_and_abi():
    """验证目标平台三元组和运行时 ABI 标记。"""
    assert TARGETS["windows-x86"].triple == "i686-pc-windows-msvc"
    assert runtime_spec(TARGETS["linux-x86_64"]).abi_tag.startswith("cp")

def test_development_python_is_independent(monkeypatch):
    """验证开发解释器不依赖目标平台运行时路径。"""
    monkeypatch.setenv("APPBUILD_PYTHON", sys.executable)
    from appbuild.config import require_local_python
    assert require_local_python().resolve() == Path(sys.executable).resolve()

def test_all_release_targets_are_declared():
    """验证七个受支持发布目标均有配置且没有意外增减。"""
    assert set(TARGETS) == {
        "windows-x86", "windows-x86_64", "windows-arm64",
        "macos-x86_64", "macos-arm64",
        "linux-x86_64", "linux-arm64",
    }


def test_project_version_and_user_agent():
    """验证下载客户端标识使用项目声明的版本号。"""
    from appbuild.config import load_app_config, project_version, user_agent
    assert project_version() == load_app_config()["project"]["version"]
    assert user_agent() == f"appbuild/{project_version()}"


def test_app_name_is_configurable(monkeypatch):
    """验证产品名称与标识符均可通过配置覆盖。"""
    import appbuild.config as config
    monkeypatch.setattr(config, "load_app_config", lambda: {
        "project": {"version": "0.16.3"},
        "tool": {"appbuild": {"app": {"name": "Demo Tool", "identifier": "com.example.demo"}}},
    })
    assert config.app_name() == "Demo Tool"
    assert config.app_identifier() == "com.example.demo"


def test_target_extension_suffix_matches_cpython_importer_conventions():
    """验证各平台扩展后缀与 CPython 的导入命名一致。"""
    from appbuild.config import TARGETS, target_extension_suffix

    assert target_extension_suffix(TARGETS["windows-x86_64"], "3.13", "cp313") == ".cp313-win_amd64.pyd"
    assert target_extension_suffix(TARGETS["linux-x86_64"], "3.13", "cp313") == ".cpython-313-x86_64-linux-gnu.so"
    assert target_extension_suffix(TARGETS["linux-arm64"], "3.13", "cp313") == ".cpython-313-aarch64-linux-gnu.so"
    assert target_extension_suffix(TARGETS["macos-arm64"], "3.13", "cp313") == ".cpython-313-darwin.so"
    assert target_extension_suffix(TARGETS["macos-x86_64"], "3.13", "cp313") == ".cpython-313-darwin.so"


@pytest.mark.parametrize(("value", "mode"), [("all", "all"), ("directory", "directory"), (True, "directory"), (False, "none")])
def test_cython_merge_mode_preserves_existing_config_values(monkeypatch, value, mode):
    """验证 Cython 合并模式配置被原样解析。"""
    from appbuild import config

    monkeypatch.setattr(config, "cython_config", lambda: {"merge": value})
    assert config.cython_merge_mode() == mode


@pytest.mark.parametrize(('version', 'tag'), [('3.8.10', 'cp38'), ('3.9.13', 'cp39'), ('3.13.5', 'cp313')])
def test_python_tag_omits_the_patch_version(monkeypatch, version, tag):
    """wheel 的 Python 标签只包含主次版本，补丁号不能泄漏进 ABI 标签。"""
    from appbuild import config
    monkeypatch.setattr(config, 'python_version', lambda: version)
    assert TARGETS['windows-x86_64'].python_tag == tag


def test_import_library_search_skips_stable_abi_only_directory(tmp_path):
    """优先目录仅有稳定 ABI 库时，仍须找到后续目录中的目标版本库。"""
    from types import SimpleNamespace
    from appbuild.toolchain import windows_import_library
    (tmp_path / 'libs').mkdir()
    (tmp_path / 'libs/python3.lib').touch()
    (tmp_path / 'install/libs').mkdir(parents=True)
    (tmp_path / 'install/libs/python313.lib').touch()
    target = SimpleNamespace(sdk_root=tmp_path, include_dir=tmp_path / 'include', python_major_minor='3.13')
    assert windows_import_library(target) == (target.include_dir, tmp_path / 'install/libs', 'python313')
