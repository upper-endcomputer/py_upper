import shutil
import subprocess
import pytest
from py_upper import linux_release as release
from py_upper import package
from py_upper.config import TARGETS


def test_release_missing_rpm_tool_fails_before_publishing(monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda name: None if name == 'rpmbuild' else '/bin/cpack')
    with pytest.raises(RuntimeError, match='rpmbuild'):
        release.linux_release(TARGETS['linux-arm64'], ['RPM'])


def test_release_replaces_only_target_artifacts(tmp_path, monkeypatch):
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
        root = cwd / 'payload'
        assert (root / 'usr/bin/upper').resolve() == root / 'opt/upper/Upper'
        for name in ['Upper.deb','Upper.tar.gz']:
            (cwd / 'artifacts' / name).write_text('new')
    monkeypatch.setattr(subprocess, 'run', cpack)
    result = release.linux_release(TARGETS['linux-arm64'])
    assert not (result / 'old.deb').exists()
    assert unrelated.read_text() == 'preserve'
    assert (result / 'release-manifest.json').is_file()
