# 中文维护说明：验证外部声明变更与增量输出一致性；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Real Cython regression for declarations outside the application tree."""
import os
from pathlib import Path
import sys

import pytest

from py_upper import python_build as pb


def test_external_include_changes_generated_code(tmp_path, monkeypatch):
    cython = pytest.importorskip('Cython')
    if not pb._supported_cython(cython.__version__):
        pytest.skip('requires a supported Cython version')
    app = tmp_path / 'app'
    src = app / 'src/demo.py'
    src.parent.mkdir(parents=True)
    src.write_text('def f(x):\n    return x + 1\n')
    src.with_suffix('.pxd').write_text('include "../../external declaration.pxi"\n')
    declaration = tmp_path / 'external declaration.pxi'
    declaration.write_text('cpdef int f(int x)\n')
    monkeypatch.setattr(pb, 'APP', app)
    monkeypatch.setattr(pb, 'BUILD', tmp_path / 'build')
    monkeypatch.setattr(pb, '_cython_env', lambda _: os.environ.copy())
    monkeypatch.setattr(pb, 'incremental_build_enabled', lambda: True)
    output = pb.cythonize_to_c([src], Path(sys.executable))[src]
    before = output.read_bytes()
    stamp = output.stat().st_mtime_ns
    pb.cythonize_to_c([src], Path(sys.executable))
    assert output.stat().st_mtime_ns == stamp
    unrelated = src.parent / 'unrelated.pxd'
    unrelated.write_text('ctypedef int unused\n')
    pb.cythonize_to_c([src], Path(sys.executable))
    stamp = output.stat().st_mtime_ns
    unrelated.write_text('ctypedef double unused\n')
    pb.cythonize_to_c([src], Path(sys.executable))
    assert output.stat().st_mtime_ns == stamp
    declaration.write_text('cpdef double f(double x)\n')
    pb.cythonize_to_c([src], Path(sys.executable))
    incremental = output.read_bytes()
    assert incremental != before
    monkeypatch.setattr(pb, 'incremental_build_enabled', lambda: False)
    pb.cythonize_to_c([src], Path(sys.executable))
    assert output.read_bytes() == incremental
