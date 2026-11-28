# 中文维护说明：验证缓存、签名、发布和关闭流程的整改边界；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import asyncio
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from appbuild import package, application_build
from appbuild.config import TARGETS
from mvvm.async_lifecycle import AsyncLifecycle
from mvvm.mvvm_application import MVVMApplication


def test_declaration_change_invalidates_cython(tmp_path, monkeypatch):
    """验证 Cython 声明文件内容改变会使生成缓存失效。"""
    monkeypatch.setattr(application_build, 'APP', tmp_path)
    src = tmp_path / 'src/demo.py'
    src.parent.mkdir()
    src.write_text('def f(x): return x + 1\n')
    declaration = src.with_suffix('.pxd')
    declaration.write_text('cpdef int f(int x)\n')
    first = application_build._cython_key(src, '3.2.9')
    declaration.write_text('cpdef double f(double x)\n')
    assert first != application_build._cython_key(src, '3.2.9')


def test_plist_roundtrips_product_name():
    """验证产品名含中文和 XML 保留字符时 plist 仍可往返解析。"""
    assert plistlib.loads(package._mac_info_plist('研发 R&D <工具>').encode())['CFBundleName'] == '研发 R&D <工具>'


def test_default_manifest_excludes_old_products(tmp_path, monkeypatch):
    """验证新 manifest 不会混入旧产品目录。"""
    monkeypatch.setattr(package, 'DIST', tmp_path)
    monkeypatch.setattr(package, 'app_name', lambda: 'Current')
    for name in ('Current', 'Old'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'payload').write_text(name)
    result = package.write_release_manifest(TARGETS['linux-x86_64'])
    assert result.parent == tmp_path
    assert 'Old' not in result.read_text()


def test_concurrent_close_and_cancelled_waiter():
    """验证并发关闭和等待方取消不会重复关闭资源。"""
    async def scenario():
        """协调两个关闭等待者并确认底层服务只清理一次。"""
        owner = AsyncLifecycle()
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def closer():
            """通过异步屏障控制关闭完成时机，暴露等待者取消窗口。"""
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
    """验证视图清理失败后仍释放 ViewModel 并清空注册表。"""
    app = MVVMApplication()
    calls = []
    def fail():
        """记录视图清理并注入异常，检查后续清理是否继续。"""
        raise ValueError('close failed')
    app.views['bad'] = SimpleNamespace(close=fail, dispose=lambda: calls.append('view'))
    app.viewmodels['vm'] = SimpleNamespace(dispose=lambda: calls.append('vm'))
    with pytest.raises(RuntimeError, match='cleanup'):
        app.stop()
    assert calls == ['view', 'vm']
    assert not app.views and not app.viewmodels
    app.stop()


def test_workspace_lock_rejects_second_writer(tmp_path):
    """验证同一工作区不能同时启动两个写入构建。"""
    from appbuild.fs import workspace_lock
    with workspace_lock(tmp_path):
        with pytest.raises(RuntimeError, match='Another build'):
            with workspace_lock(tmp_path):
                pass
    with workspace_lock(tmp_path):
        pass


@pytest.mark.parametrize('failure', ['verify', 'runtime', 'launcher', 'sign'])
def test_failed_candidate_never_publishes(tmp_path, monkeypatch, failure):
    """验证候选发布检查失败时不会调用正式产物发布。"""
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
        """模拟候选产物检查失败。"""
        raise RuntimeError('injected failure')
    monkeypatch.setattr(build, 'verify', lambda *a: int(failure == 'verify'))
    monkeypatch.setattr(build, 'run_target_python_smoke', fail if failure == 'runtime' else lambda *a: None)
    monkeypatch.setattr(build, 'run_launcher_smoke', fail if failure == 'launcher' else lambda *a: None)
    monkeypatch.setattr(build, 'sign_release', fail if failure == 'sign' else lambda *a, **k: None)
    assert build._main(['--release', '--identity', 'Test']) == 1
    assert published == []


def test_embedded_source_does_not_rewrite_unchanged_file(tmp_path):
    """验证相同嵌入源码不重写磁盘文件，变化后才更新。"""
    path = tmp_path / 'generated.cpp'
    package._write_changed(path, 'same')
    import os
    os.utime(path, ns=(1000000000, 1000000000))
    package._write_changed(path, 'same')
    assert path.stat().st_mtime_ns == 1000000000
    package._write_changed(path, 'different')
    assert path.read_text() == 'different'


def test_native_header_content_invalidates_cache(tmp_path):
    """验证原生头文件内容变化参与编译缓存键。"""
    include = tmp_path / 'include'
    include.mkdir()
    header = include / 'example.h'
    header.write_text('#define VALUE 1\n')
    source, output = tmp_path / 'module.c', tmp_path / 'module.so'
    source.write_text('int value;')
    cmd = ['missing-compiler', '-I' + str(include), str(source), '-o', str(output)]
    first = application_build._native_key(cmd, source, output)
    output.write_bytes(b'compiled output')
    assert application_build._native_key(cmd, source, output) == first
    header.write_text('#define VALUE 2\n')
    assert application_build._native_key(cmd, source, output) != first


def test_cache_pruning_preserves_live_entry(tmp_path):
    """验证缓存清理保留仍被进程租约使用的条目。"""
    from appbuild import egg_loader as loader
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
    """验证不同应用使用独立的运行时缓存命名空间。"""
    from appbuild import egg_loader as loader
    monkeypatch.setenv('APPBUILD_EGG_CACHE', str(tmp_path))
    monkeypatch.setenv('APPBUILD_CACHE_NAMESPACE', 'a' * 32)
    first = loader._cache_root()
    monkeypatch.setenv('APPBUILD_CACHE_NAMESPACE', 'b' * 32)
    assert loader._cache_root() != first


def test_macos_build_info_stays_inside_contents(tmp_path):
    """验证 macOS 构建信息保存在应用包 Contents 内。"""
    from appbuild.manifest import write_build_info
    write_build_info(tmp_path, TARGETS['macos-arm64'], {})
    assert (tmp_path / 'Contents/Resources/build-info.json').is_file()
    assert not (tmp_path / 'build-info.json').exists()
