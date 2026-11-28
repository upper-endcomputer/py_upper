# 中文维护说明：验证Linux 安装包生成及发布边界；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import shutil
import subprocess
import pytest
from appbuild import linux_release as release
from appbuild import package
from appbuild.config import TARGETS


def test_release_missing_rpm_tool_fails_before_publishing(monkeypatch):
    """验证缺少 RPM 工具时在发布前失败，保留已有产物。"""
    monkeypatch.setattr(shutil, 'which', lambda name: None if name == 'rpmbuild' else '/bin/cpack')
    with pytest.raises(RuntimeError, match='rpmbuild'):
        release.linux_release(TARGETS['linux-arm64'], ['RPM'])


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
    result = release.linux_release(TARGETS['linux-arm64'])
    assert not (result / 'old.deb').exists()
    assert unrelated.read_text() == 'preserve'
    assert (result / 'release-manifest.json').is_file()
