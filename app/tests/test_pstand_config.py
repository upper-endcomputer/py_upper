import os
import sys
from pathlib import Path

from pstand.config import TARGETS, runtime_spec

def test_targets_and_abi():
    assert TARGETS["windows-x86"].triple == "i686-pc-windows-msvc"
    assert runtime_spec(TARGETS["linux-x86_64"]).abi_tag.startswith("cp")

def test_development_python_is_independent(monkeypatch):
    monkeypatch.setenv("PYSTAND_PYTHON", sys.executable)
    from pstand.config import require_local_python
    assert require_local_python().resolve() == Path(sys.executable).resolve()
