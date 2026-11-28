import importlib.util
import marshal
from pathlib import Path
import subprocess
import sys

from py_upper import package
from py_upper.config import TARGETS


def test_linux_precompile_is_relocatable_and_rejects_stale_source(tmp_path, monkeypatch):
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
    monkeypatch.setattr(package, 'host_target', lambda: TARGETS['linux-x86_64'])
    def unexpected(_):
        raise AssertionError('cross-target interpreter must not execute')
    monkeypatch.setattr(package, 'runtime_executable', unexpected)
    package._precompile_linux_stdlib(tmp_path, TARGETS['linux-arm64'])
