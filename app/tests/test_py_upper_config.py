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
    assert project_version() == "0.16.5"
    assert user_agent() == "py_upper/0.16.5"


def test_app_name_is_configurable(monkeypatch):
    import py_upper.config as config
    monkeypatch.setattr(config, "load_app_config", lambda: {
        "project": {"version": "0.16.3"},
        "tool": {"py_upper": {"app": {"name": "Demo Tool", "identifier": "com.example.demo"}}},
    })
    assert config.app_name() == "Demo Tool"
    assert config.app_identifier() == "com.example.demo"
