# 中文维护说明：验证安装包生成、签名、公证及发布边界；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import shutil
from types import SimpleNamespace
import subprocess
import pytest
from appbuild import release
from appbuild import package
from appbuild.config import TARGETS


def test_release_missing_rpm_tool_fails_before_publishing(monkeypatch):
    """验证缺少 RPM 工具时在发布前失败，保留已有产物。"""
    monkeypatch.setattr(shutil, 'which', lambda name: None if name == 'rpmbuild' else '/bin/cpack')
    with pytest.raises(RuntimeError, match='rpmbuild'):
        release.build_linux_installers(TARGETS['linux-arm64'], ['RPM'])


def test_release_replaces_only_target_artifacts(tmp_path, monkeypatch):
    """验证 Linux 发布仅替换本目标产物，并生成发布清单。"""
    dist = tmp_path / 'dist'
    source = dist / 'Upper'
    source.mkdir(parents=True)
    (source / 'Upper').write_text('binary')
    previous = dist / 'releases/linux-arm64'
    previous.mkdir(parents=True)
    (previous / 'old.deb').write_text('old')
    unrelated = dist / 'other.tar.gz'
    unrelated.write_text('preserve')
    monkeypatch.setattr(release, 'BUILD', tmp_path / 'build')
    monkeypatch.setattr(package, 'BUILD', tmp_path / 'build')
    monkeypatch.setattr(release, 'DIST', dist)
    monkeypatch.setattr(release, 'app_name', lambda: 'Upper')
    monkeypatch.setattr(shutil, 'which', lambda name: '/bin/' + name)
    def cpack(cmd, cwd, check):
        """检查安装树中的入口链接并模拟生成安装包。"""
        root = cwd / 'payload'
        assert (root / 'usr/bin/upper').resolve() == root / 'opt/upper/Upper'
        for name in ['Upper.deb','Upper.tar.gz']:
            (cwd / 'artifacts' / name).write_text('new')
    monkeypatch.setattr(subprocess, 'run', cpack)
    result = release.build_linux_installers(TARGETS['linux-arm64'])
    assert not (result / 'old.deb').exists()
    assert unrelated.read_text() == 'preserve'
    assert (result / 'release-manifest.json').is_file()


@pytest.mark.parametrize('target', ['macos-x86_64', 'macos-arm64'])
@pytest.mark.parametrize('status', ['Accepted', 'Invalid'])
def test_notarization_submits_zip_and_checks_status(tmp_path, monkeypatch, status, target):
    """验证公证提交使用 ZIP 且根据返回状态决定成功或失败。"""
    app = tmp_path / 'Product.app'
    app.mkdir()
    calls = []
    monkeypatch.setattr(release.shutil, 'which', lambda name: name)
    monkeypatch.setattr(release, 'sign_macos_files', lambda *a: None)
    def run(cmd, **kwargs):
        """记录签名工具调用，并返回指定公证状态。"""
        calls.append(cmd)
        return SimpleNamespace(stdout='{"status": "' + status + '"}')
    monkeypatch.setattr(release.subprocess, 'run', run)
    if status == 'Accepted':
        release.sign_release(TARGETS[target], 'Test', 'Profile', output=app)
        assert any('staple' in c for c in calls)
    else:
        with pytest.raises(RuntimeError, match='not accepted'):
            release.sign_release(TARGETS[target], 'Test', 'Profile', output=app)
        assert not any('staple' in c for c in calls)
    submit = next(c for c in calls if 'submit' in c)
    assert submit[3].endswith('.zip')
    assert next(c for c in calls if c[0] == 'ditto')[1:4] == ['-c', '-k', '--keepParent']


def test_signing_detects_extensionless_macho(tmp_path, monkeypatch):
    """验证没有扩展名的 Mach-O 文件也会参与签名。"""
    import struct
    native = tmp_path / 'Python'
    native.write_bytes(struct.pack('<IIII', 0xfeedfacf, 0x0100000c, 0, 2) + bytes(64))
    text = tmp_path / 'readme'
    text.write_text('plain text')
    calls = []
    monkeypatch.setattr(release.shutil, 'which', lambda _: 'codesign')
    monkeypatch.setattr(release.subprocess, 'run', lambda cmd, **kw: calls.append(cmd))
    release.sign_macos_files([text, native], 'Identity')
    assert len(calls) == 1 and calls[0][-1] == str(native)


@pytest.mark.parametrize('target', ['windows-x86', 'windows-x86_64', 'windows-arm64'])
def test_windows_signing_uses_candidate_native_files(tmp_path, monkeypatch, target):
    """三种 Windows 架构都只签候选包内原生文件；mock 验证命令，不证明真实证书有效。"""
    candidate = tmp_path / '候选 包'
    candidate.mkdir()
    native_names = ['Upper.EXE', 'runtime/python.dll', 'site-packages/extension.pyd']
    for name in native_names + ['data.json']:
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture')
    outside = tmp_path / 'old.exe'
    outside.write_bytes(b'old')
    monkeypatch.setenv('APPBUILD_SIGN_CERT', 'test certificate.pfx')
    monkeypatch.setattr(release.shutil, 'which', lambda name: name)
    calls = []
    def run(command, *, check):
        """记录签名对象和失败传播参数，避免实际调用宿主签名程序。"""
        assert check is True
        calls.append(command)
    monkeypatch.setattr(release.subprocess, 'run', run)
    assert release.sign_release(TARGETS[target], output=candidate) == 0
    assert {command[-1] for command in calls} == {str(candidate / name) for name in native_names}
    assert all(command[:-1] == ['signtool', 'sign', '/fd', 'SHA256', '/a', '/f', 'test certificate.pfx'] for command in calls)


@pytest.mark.parametrize('missing', ['tool', 'certificate'])
def test_windows_signing_requires_tool_and_certificate(monkeypatch, tmp_path, missing):
    """缺少签名工具或证书时必须失败，不能把未签名候选包报告为成功。"""
    monkeypatch.setattr(release.shutil, 'which', lambda name: None if missing == 'tool' else name)
    monkeypatch.delenv('APPBUILD_SIGN_CERT', raising=False)
    if missing != 'certificate':
        monkeypatch.setenv('APPBUILD_SIGN_CERT', 'test.pfx')
    with pytest.raises(RuntimeError, match='signtool and APPBUILD_SIGN_CERT'):
        release.sign_release(TARGETS['windows-x86_64'], output=tmp_path)


def test_windows_signing_propagates_failure(tmp_path, monkeypatch):
    """signtool 失败向 CLI 传播，阻止后续发布步骤。"""
    (tmp_path / 'Upper.exe').write_bytes(b'fixture')
    monkeypatch.setenv('APPBUILD_SIGN_CERT', 'test.pfx')
    monkeypatch.setattr(release.shutil, 'which', lambda name: name)
    def fail(command, *, check):
        """模拟签名程序拒绝证书后的非零退出。"""
        assert check is True
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(release.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        release.sign_release(TARGETS['windows-x86_64'], output=tmp_path)
