# 中文维护说明：验证既有审查问题的回归边界；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Behavioral regressions found during the engineering audit."""
import gc
import io
import math
import os
import tarfile
from types import SimpleNamespace
import weakref
from urllib.error import HTTPError

import pytest

from mvvm.core import AsyncRelayCommand, BaseViewModel, ObservableProperty, RelayCommand
from appbuild import net
import appbuild.package as package
from appbuild.runtime import safe_extract
from mvvm.views import ViewBase


@pytest.mark.parametrize('kind', ['property', 'viewmodel', 'command', 'async_command'])
def test_self_unsubscribe_does_not_skip_next_listener(kind):
    """验证监听器在通知中取消自己时，后续监听器仍收到事件。"""
    calls = []
    if kind == 'property':
        obj = ObservableProperty(0)
        subscribe = obj.subscribe
        notify = lambda: obj.set_value(1)
    elif kind == 'viewmodel':
        obj = BaseViewModel()
        obj._register_property('value', ObservableProperty(0))
        subscribe = obj.subscribe_property_changed
        notify = lambda: obj.set_property_value('value', 1)
    else:
        obj = (RelayCommand if kind == 'command' else AsyncRelayCommand)(lambda _: None)
        subscribe = obj.subscribe_can_execute_changed
        notify = obj.notify_can_execute_changed

    def first(*args):
        """记录首个通知并立即取消自身订阅。"""
        calls.append('first')
        unsubscribe()

    unsubscribe = subscribe(first)
    subscribe(lambda *args: calls.append('second'))
    notify()
    assert calls == ['first', 'second']


def test_disposed_viewmodel_is_not_retained_by_external_property():
    """验证释放后的 ViewModel 不再被外部属性订阅持有。"""
    prop = ObservableProperty(0)
    vm = BaseViewModel()
    vm._register_property('value', prop)
    ref = weakref.ref(vm)
    vm.dispose()
    vm.dispose()
    del vm
    gc.collect()
    assert ref() is None
    prop.set_value(1)


def test_replaced_property_stops_notifying_viewmodel():
    """验证替换属性会解绑旧属性，只传播新属性变化。"""
    vm = BaseViewModel()
    old = ObservableProperty(0)
    new = ObservableProperty(0)
    changes = []
    vm.subscribe_property_changed(lambda event: changes.append(event.new_value))
    vm._register_property('value', old)
    vm._register_property('value', new)
    old.set_value(1)
    new.set_value(2)
    assert changes == [2]


@pytest.mark.parametrize('header', ['-1', 'NaN', 'Infinity'])
def test_retry_after_always_produces_a_finite_nonnegative_delay(header):
    """验证非法 Retry-After 不会产生负数、无穷大或 NaN 等待时间。"""
    error = HTTPError('https://example.invalid', 429, 'busy', {'Retry-After': header}, None)
    delay = net._retry_delay(error, 1)
    assert math.isfinite(delay) and 0 <= delay <= net.MAX_BACKOFF
    assert delay == 2.0


@pytest.mark.parametrize('header,expected', [('0', 0.0), ('3', 3.0), ('100', net.MAX_BACKOFF)])
def test_valid_retry_after_is_respected_and_bounded(header, expected):
    """验证有效重试等待时间被采用并受到最大退避限制。"""
    error = HTTPError('https://example.invalid', 429, 'busy', {'Retry-After': header}, None)
    assert net._retry_delay(error, 1) == expected


@pytest.mark.parametrize('fail_promotion', [False, True])
def test_publish_preserves_previous_bundle_until_promotion(tmp_path, monkeypatch, fail_promotion):
    """验证候选产物提升成功前保留上一份可用产物。"""
    assembly = tmp_path / 'build' / 'publish'
    source = assembly / 'App'
    destination = tmp_path / 'dist' / 'App'
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / 'version').write_text('new', encoding='utf-8')
    (destination / 'version').write_text('old', encoding='utf-8')
    monkeypatch.setattr(package, 'BUILD', tmp_path / 'build')
    replace = package.os.replace

    def promote(src, dst):
        """在提升动作中检查旧产物仍存在，再执行实际替换。"""
        if src == source and fail_promotion:
            raise OSError('injected promotion failure')
        replace(src, dst)

    monkeypatch.setattr(package.os, 'replace', promote)
    if fail_promotion:
        with pytest.raises(OSError, match='injected promotion failure'):
            package._publish(assembly, source, destination)
        assert (destination / 'version').read_text(encoding='utf-8') == 'old'
        assert (source / 'version').read_text(encoding='utf-8') == 'new'
    else:
        assert package._publish(assembly, source, destination) == destination
        assert (destination / 'version').read_text(encoding='utf-8') == 'new'
        assert not assembly.exists()


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'parent'])
def test_runtime_archive_cannot_write_outside_destination(tmp_path, monkeypatch, kind):
    # Reproduce the pre-3.14 default even when tests run on newer Python.
    """验证恶意归档路径或链接无法写入解压目录外。"""
    if hasattr(tarfile, 'fully_trusted_filter'):
        monkeypatch.setattr(tarfile.TarFile, 'extraction_filter', staticmethod(tarfile.fully_trusted_filter))
    neighbor = tmp_path / 'neighbor'
    neighbor.mkdir()
    (neighbor / 'existing').write_bytes(b'old')
    archive = tmp_path / 'archive.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        if kind != 'parent':
            member = tarfile.TarInfo('link')
            member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
            member.linkname = '../neighbor' if kind == 'symlink' else '../neighbor/existing'
            tf.addfile(member)
        member = tarfile.TarInfo('../neighbor/payload' if kind == 'parent' else 'link/payload' if kind == 'symlink' else 'link')
        member.size = 3
        tf.addfile(member, io.BytesIO(b'new'))
    with pytest.raises((tarfile.TarError, RuntimeError)):
        safe_extract(archive, tmp_path / 'extracted')
    assert not (neighbor / 'payload').exists()
    assert (neighbor / 'existing').read_bytes() == b'old'


@pytest.mark.skipif(not hasattr(tarfile, 'data_filter'), reason='Python lacks safe tar extraction')
def test_runtime_archive_preserves_safe_links_and_executable(tmp_path):
    """验证合法归档链接和可执行文件仍能正常解压。"""
    archive = tmp_path / 'archive.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        binary = tarfile.TarInfo('python/bin/python3')
        binary.size = 3
        binary.mode = 0o755
        tf.addfile(binary, io.BytesIO(b'bin'))
        link = tarfile.TarInfo('python/bin/python')
        link.type = tarfile.SYMTYPE
        link.linkname = 'python3'
        tf.addfile(link)
    dest = tmp_path / 'extracted'
    safe_extract(archive, dest)
    assert (dest / 'python/bin/python').read_bytes() == b'bin'
    if os.name == 'posix':
        assert (dest / 'python/bin/python3').stat().st_mode & 0o100


def test_runtime_archive_requires_safe_filter_support(tmp_path, monkeypatch):
    """验证缺少安全解压过滤能力时拒绝继续处理归档。"""
    monkeypatch.delattr(tarfile, 'data_filter', raising=False)
    with pytest.raises(RuntimeError, match='tarfile.data_filter'):
        safe_extract(tmp_path / 'unused.tar.gz', tmp_path / 'extracted')


def test_disposed_view_releases_property_and_command_bindings():
    """验证视图释放后属性、命令和信号绑定全部解除且对象可回收。"""
    class Signal:
        def __init__(self):
            """创建可观测的测试信号处理器列表。"""
            self.callbacks = []

        def connect(self, callback):
            """登记回调，用于检查视图建立的信号绑定。"""
            self.callbacks.append(callback)

        def disconnect(self, callback):
            """移除回调，用于检查视图释放是否正确解绑。"""
            self.callbacks.remove(callback)

        def emit(self):
            """遍历当前回调快照，模拟允许回调中解绑的信号。"""
            for callback in tuple(self.callbacks):
                callback()

    vm = BaseViewModel()
    prop = ObservableProperty(0)
    vm._register_property('value', prop)
    calls = []
    vm.action_command = RelayCommand(lambda _: calls.append('called'))
    widget = SimpleNamespace(value=0, clicked=Signal())
    view = ViewBase(vm)
    binding = view.bind_property('value', widget)
    command_binding = view.bind_command('action', widget)
    references = [weakref.ref(binding), weakref.ref(command_binding)]
    widget.clicked.emit()
    prop.set_value(1)
    assert calls == ['called'] and widget.value == 1
    view.dispose()
    view.dispose()
    del binding, command_binding
    gc.collect()
    prop.set_value(2)
    widget.clicked.emit()
    assert widget.value == 1
    assert calls == ['called']
    assert all(ref() is None for ref in references)


def test_application_stop_disposes_registered_views():
    """验证应用停止会先关闭已注册视图，再释放其绑定。"""
    from mvvm.mvvm_application import MVVMApplication

    class View(ViewBase):
        def __init__(self):
            """初始化视图生命周期事件记录。"""
            super().__init__()
            self.events = []

        def close(self):
            """记录窗口关闭动作以检查清理顺序。"""
            self.events.append('close')

        def dispose(self):
            """记录绑定释放动作以检查清理顺序。"""
            self.events.append('dispose')
            super().dispose()

    app = MVVMApplication()
    view = View()
    app.register_view('main', view)
    app.stop()
    app.stop()
    assert view.events == ['close', 'dispose']
