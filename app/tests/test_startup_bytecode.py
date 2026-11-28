# 中文维护说明：验证目标标准库字节码预编译；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import importlib.util
import marshal
from pathlib import Path
import subprocess
import sys

from appbuild import package
from appbuild.config import TARGETS


def test_linux_precompile_is_relocatable_and_rejects_stale_source(tmp_path, monkeypatch):
    """验证预编译字节码使用相对源码路径和校验哈希，源码改变后重新执行新内容。"""
    target = TARGETS['linux-arm64']
    monkeypatch.setattr(package, 'host_target', lambda: target)
    monkeypatch.setattr(package, 'runtime_executable', lambda _: sys.executable)
    bundle = tmp_path / 'assembly'
    stdlib = bundle / 'runtime/lib/python3.13'
    stdlib.mkdir(parents=True)
    source = stdlib / 'startup_fixture.py'
    source.write_text('value = 1\n')
    package._precompile_linux_stdlib(bundle, target)
    pyc = importlib.util.cache_from_source(str(source))
    with open(pyc, 'rb') as stream:
        header = stream.read(16)
        code = marshal.load(stream)
    assert int.from_bytes(header[4:8], 'little') == 3  # checked hash
    assert Path(code.co_filename).as_posix() == 'runtime/lib/python3.13/startup_fixture.py'
    moved = tmp_path / 'installed'
    bundle.rename(moved)
    installed = moved / 'runtime/lib/python3.13'
    (installed / source.name).write_text('value = 2\n')  # same length, immediate edit
    result = subprocess.run([sys.executable, '-B', '-c',
        'import sys; sys.path.insert(0, sys.argv[1]); import startup_fixture; print(startup_fixture.value)',
        str(installed)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == '2'


def test_cross_target_does_not_execute_target_interpreter(tmp_path, monkeypatch):
    """验证交叉构建不会执行不可在主机运行的目标解释器。"""
    monkeypatch.setattr(package, 'host_target', lambda: TARGETS['linux-x86_64'])
    def unexpected(_):
        """若交叉构建调用目标进程则立即令测试失败。"""
        raise AssertionError('cross-target interpreter must not execute')
    monkeypatch.setattr(package, 'runtime_executable', unexpected)
    package._precompile_linux_stdlib(tmp_path, TARGETS['linux-arm64'])
