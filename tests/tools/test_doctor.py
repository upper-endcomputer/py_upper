# 中文维护说明：验证构建环境诊断；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_build_cli():
    """Load tools/build.py without running its ``main()``."""
    spec = importlib.util.spec_from_file_location("py_upper_build_cli", ROOT / "tools" / "build.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_doctor_fails_when_a_build_tool_is_missing(monkeypatch, capsys):
    """The diagnostic doubles as the CI toolchain gate, so a missing tool must exit non-zero."""
    from py_upper.config import TARGETS

    build = _load_build_cli()
    monkeypatch.setattr(build, "shutil", type("S", (), {"which": staticmethod(lambda name: None)}))
    monkeypatch.setattr(build, "describe_toolchain", lambda target: {"target": target.key})

    assert build._doctor(TARGETS["linux-x86_64"]) == 1
    assert "doctor: FAILED, missing: cmake, ninja" in capsys.readouterr().out


def test_doctor_passes_when_the_toolchain_resolves(monkeypatch, capsys):
    from py_upper.config import TARGETS

    build = _load_build_cli()
    monkeypatch.setattr(
        build, "shutil", type("S", (), {"which": staticmethod(lambda name: "/usr/bin/" + name)})
    )
    monkeypatch.setattr(build, "describe_toolchain", lambda target: {"target": target.key, "compiler": "/usr/bin/cc"})

    assert build._doctor(TARGETS["linux-x86_64"]) == 0
    assert "doctor: FAILED" not in capsys.readouterr().out


def test_doctor_reports_an_unresolvable_toolchain_as_missing(monkeypatch, capsys):
    from py_upper.config import TARGETS

    build = _load_build_cli()
    monkeypatch.setattr(
        build, "shutil", type("S", (), {"which": staticmethod(lambda name: "/usr/bin/" + name)})
    )

    def unavailable(target):
        raise RuntimeError("no compiler for this target")

    monkeypatch.setattr(build, "describe_toolchain", unavailable)

    assert build._doctor(TARGETS["linux-x86_64"]) == 1
    out = capsys.readouterr().out
    assert "toolchain: UNAVAILABLE: no compiler for this target" in out
    assert "doctor: FAILED, missing: toolchain" in out
