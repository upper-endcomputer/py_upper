import os
import sys
from pathlib import Path

from py_upper.config import TARGETS, runtime_spec

def test_targets_and_abi():
    assert TARGETS["windows-x86"].triple == "i686-pc-windows-msvc"
    assert runtime_spec(TARGETS["linux-x86_64"]).abi_tag.startswith("cp")

def test_development_python_is_independent(monkeypatch):
    monkeypatch.setenv("PYSTAND_PYTHON", sys.executable)
    from py_upper.config import require_local_python
    assert require_local_python().resolve() == Path(sys.executable).resolve()

def test_all_release_targets_are_declared():
    assert set(TARGETS) == {
        "windows-x86", "windows-x86_64", "windows-arm64",
        "macos-x86_64", "macos-arm64",
        "linux-x86_64", "linux-arm64",
    }


def test_project_version_and_user_agent():
    from py_upper.config import project_version, user_agent
    assert project_version() == "0.16.16"
    assert user_agent() == "py_upper/0.16.16"


def test_app_name_is_configurable(monkeypatch):
    import py_upper.config as config
    monkeypatch.setattr(config, "load_app_config", lambda: {
        "project": {"version": "0.16.3"},
        "tool": {"py_upper": {"app": {"name": "Demo Tool", "identifier": "com.example.demo"}}},
    })
    assert config.app_name() == "Demo Tool"
    assert config.app_identifier() == "com.example.demo"


def test_target_extension_suffix_matches_cpython_importer_conventions():
    from py_upper.config import TARGETS, target_extension_suffix

    assert target_extension_suffix(TARGETS["windows-x86_64"], "3.13", "cp313") == ".cp313-win_amd64.pyd"
    assert target_extension_suffix(TARGETS["linux-x86_64"], "3.13", "cp313") == ".cpython-313-x86_64-linux-gnu.so"
    assert target_extension_suffix(TARGETS["linux-arm64"], "3.13", "cp313") == ".cpython-313-aarch64-linux-gnu.so"
    assert target_extension_suffix(TARGETS["macos-arm64"], "3.13", "cp313") == ".cpython-313-darwin.so"
    assert target_extension_suffix(TARGETS["macos-x86_64"], "3.13", "cp313") == ".cpython-313-darwin.so"
