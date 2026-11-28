import asyncio
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from py_upper import package, python_build
from py_upper.config import TARGETS
from mvvm.async_lifecycle import AsyncLifecycle
from mvvm.mvvm_application import MVVMApplication


def test_declaration_change_invalidates_cython(tmp_path, monkeypatch):
    monkeypatch.setattr(python_build, 'APP', tmp_path)
    src = tmp_path / 'src/demo.py'
    src.parent.mkdir()
    src.write_text('def f(x): return x + 1\n')
    declaration = src.with_suffix('.pxd')
    declaration.write_text('cpdef int f(int x)\n')
    first = python_build._cython_key(src, '3.2.9')
    declaration.write_text('cpdef double f(double x)\n')
    assert first != python_build._cython_key(src, '3.2.9')


def test_plist_roundtrips_product_name():
    assert plistlib.loads(package._mac_info_plist('研发 R&D <工具>').encode())['CFBundleName'] == '研发 R&D <工具>'


def test_default_manifest_excludes_old_products(tmp_path, monkeypatch):
    monkeypatch.setattr(package, 'DIST', tmp_path)
    monkeypatch.setattr(package, 'app_name', lambda: 'Current')
    for name in ('Current', 'Old'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'payload').write_text(name)
    result = package.write_release_manifest(TARGETS['linux-x86_64'])
    assert result.parent == tmp_path
    assert 'Old' not in result.read_text()


def test_concurrent_close_and_cancelled_waiter():
    async def scenario():
        owner = AsyncLifecycle()
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def closer():
            calls.append(1)
            entered.set()
            await finish.wait()
        owner.register_close(closer)
        first = asyncio.create_task(owner.aclose())
        await entered.wait()
        second = asyncio.create_task(owner.aclose())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        finish.set()
        await second
        await owner.aclose()
        assert calls == [1]
    asyncio.run(scenario())


def test_cleanup_continues_after_view_failure():
    app = MVVMApplication()
    calls = []
    def fail():
        raise ValueError('close failed')
    app.views['bad'] = SimpleNamespace(close=fail, dispose=lambda: calls.append('view'))
    app.viewmodels['vm'] = SimpleNamespace(dispose=lambda: calls.append('vm'))
    with pytest.raises(RuntimeError, match='cleanup'):
        app.stop()
    assert calls == ['view', 'vm']
    assert not app.views and not app.viewmodels
    app.stop()


def test_workspace_lock_rejects_second_writer(tmp_path):
    from py_upper.workspace_lock import workspace_lock
    with workspace_lock(tmp_path):
        with pytest.raises(RuntimeError, match='Another build'):
            with workspace_lock(tmp_path):
                pass
    with workspace_lock(tmp_path):
        pass


@pytest.mark.parametrize('failure', ['verify', 'runtime', 'launcher', 'sign'])
def test_failed_candidate_never_publishes(tmp_path, monkeypatch, failure):
    import build
    target = TARGETS['macos-arm64']
    candidate = tmp_path / 'candidate/Upper.app'
    candidate.mkdir(parents=True)
    published = []
    monkeypatch.setattr(build, 'validate_target', lambda _: target)
    for name in ('ensure_sdk', 'ensure_runtime', 'build_python_package'):
        monkeypatch.setattr(build, name, lambda *a, **k: None)
    monkeypatch.setattr(build, 'build_launcher', lambda *a: tmp_path / 'launcher')
    monkeypatch.setattr(build, 'package', lambda *a, **k: candidate)
    monkeypatch.setattr(build, '_publish', lambda *a: published.append(a))
    def fail(*a, **k):
        raise RuntimeError('injected failure')
    monkeypatch.setattr(build, 'verify', lambda *a: int(failure == 'verify'))
    monkeypatch.setattr(build, 'run_target_python_smoke', fail if failure == 'runtime' else lambda *a: None)
    monkeypatch.setattr(build, 'run_launcher_smoke', fail if failure == 'launcher' else lambda *a: None)
    monkeypatch.setattr(build, 'release', fail if failure == 'sign' else lambda *a, **k: None)
    assert build._main(['--release', '--identity', 'Test']) == 1
    assert published == []


@pytest.mark.parametrize('status', ['Accepted', 'Invalid'])
def test_notarization_submits_zip_and_checks_status(tmp_path, monkeypatch, status):
    import build
    from py_upper import signing
    app = tmp_path / 'Product.app'
    app.mkdir()
    calls = []
    monkeypatch.setattr(build.shutil, 'which', lambda name: name)
    monkeypatch.setattr(signing, 'sign_macos_files', lambda *a: None)
    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout='{"status": "' + status + '"}')
    monkeypatch.setattr(build.subprocess, 'run', run)
    if status == 'Accepted':
        build.release(TARGETS['macos-arm64'], 'Test', 'Profile', output=app)
        assert any('staple' in c for c in calls)
    else:
        with pytest.raises(RuntimeError, match='not accepted'):
            build.release(TARGETS['macos-arm64'], 'Test', 'Profile', output=app)
        assert not any('staple' in c for c in calls)
    submit = next(c for c in calls if 'submit' in c)
    assert submit[3].endswith('.zip')
    assert next(c for c in calls if c[0] == 'ditto')[1:4] == ['-c', '-k', '--keepParent']


def test_embedded_source_does_not_rewrite_unchanged_file(tmp_path):
    path = tmp_path / 'generated.cpp'
    package._write_changed(path, 'same')
    import os
    os.utime(path, ns=(1000000000, 1000000000))
    package._write_changed(path, 'same')
    assert path.stat().st_mtime_ns == 1000000000
    package._write_changed(path, 'different')
    assert path.read_text() == 'different'


def test_native_header_content_invalidates_cache(tmp_path):
    include = tmp_path / 'include'
    include.mkdir()
    header = include / 'example.h'
    header.write_text('#define VALUE 1\n')
    source, output = tmp_path / 'module.c', tmp_path / 'module.so'
    source.write_text('int value;')
    cmd = ['missing-compiler', '-I' + str(include), str(source), '-o', str(output)]
    first = python_build._native_key(cmd, source, output)
    output.write_bytes(b'compiled output')
    assert python_build._native_key(cmd, source, output) == first
    header.write_text('#define VALUE 2\n')
    assert python_build._native_key(cmd, source, output) != first


def test_signing_detects_extensionless_macho(tmp_path, monkeypatch):
    from py_upper import signing
    import struct
    native = tmp_path / 'Python'
    native.write_bytes(struct.pack('<IIII', 0xfeedfacf, 0x0100000c, 0, 2) + bytes(64))
    text = tmp_path / 'readme'
    text.write_text('plain text')
    calls = []
    monkeypatch.setattr(signing.shutil, 'which', lambda _: 'codesign')
    monkeypatch.setattr(signing.subprocess, 'run', lambda cmd, **kw: calls.append(cmd))
    signing.sign_macos_files([text, native], 'Identity')
    assert len(calls) == 1 and calls[0][-1] == str(native)


def test_cache_pruning_preserves_live_entry(tmp_path):
    from py_upper import egg_loader as loader
    cache = tmp_path / 'cache'
    cache.mkdir()
    for name in ('egg-old', 'egg-previous', 'egg-current'):
        (cache / name).mkdir()
    import os
    os.utime(cache / 'egg-old', (1, 1))
    release = loader._cache_lease(loader._cache_lease_path(str(cache), 'egg-old'))
    assert release is not None
    try:
        loader._prune_cache(str(cache), 'egg-current')
        assert (cache / 'egg-old').exists()
    finally:
        release()
    loader._prune_cache(str(cache), 'egg-current')
    assert not (cache / 'egg-old').exists()


def test_cache_namespace_isolates_applications(tmp_path, monkeypatch):
    from py_upper import egg_loader as loader
    monkeypatch.setenv('PY_UPPER_EGG_CACHE', str(tmp_path))
    monkeypatch.setenv('PY_UPPER_CACHE_NAMESPACE', 'a' * 32)
    first = loader._cache_root()
    monkeypatch.setenv('PY_UPPER_CACHE_NAMESPACE', 'b' * 32)
    assert loader._cache_root() != first


def test_macos_build_info_stays_inside_contents(tmp_path):
    from py_upper.build_info import write_build_info
    write_build_info(tmp_path, TARGETS['macos-arm64'], {})
    assert (tmp_path / 'Contents/Resources/build-info.json').is_file()
    assert not (tmp_path / 'build-info.json').exists()
