# 中文维护说明：将已验证候选目录交给 CPack 生成 Linux 安装包；全部请求格式成功后才发布归档。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Linux installer generation from the verified bundle, using CPack."""
from __future__ import annotations
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from .config import BUILD, DIST, app_name, project_version
from .package import _publish, write_release_manifest


def _literal(value):
    value = str(value)
    if ']=]' in value:
        raise ValueError('Invalid CPack value')
    return '[=[' + value + ']=]'


def linux_release(target, formats=('TGZ', 'DEB'), deb_depends=None, *, source=None):
    if target.os != 'linux':
        raise ValueError('Linux packages require a Linux target')
    formats = tuple(dict.fromkeys(formats))
    if not formats or any(x not in ('TGZ', 'DEB', 'RPM') for x in formats):
        raise ValueError('Unsupported Linux package format')
    for tool in ('cpack',) + (('rpmbuild',) if 'RPM' in formats else ()) + (('dpkg-shlibdeps', 'file') if 'DEB' in formats and deb_depends is None else ()):
        if not shutil.which(tool):
            raise RuntimeError('Linux release requires ' + tool)
    name, version = app_name(), project_version()
    package_name = name.lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', package_name):
        raise ValueError('Linux package name must contain only letters, digits, +, . or -')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', version):
        raise ValueError('Linux release version must be numeric dotted version')
    source = source or (DIST / name)
    if not (source / name).is_file():
        raise RuntimeError('Verified bundle missing: ' + str(source))
    work_root = BUILD / 'publish'
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='linux-release-', dir=str(work_root)) as temporary:
        work = Path(temporary)
        payload = work / 'payload'
        shutil.copytree(source, payload / 'opt' / package_name, symlinks=True)
        binary = payload / 'usr/bin' / package_name
        binary.parent.mkdir(parents=True)
        binary.symlink_to('../../opt/' + package_name + '/' + name)
        desktop = payload / 'usr/share/applications' / (package_name + '.desktop')
        desktop.parent.mkdir(parents=True)
        desktop.write_text('[Desktop Entry]\nType=Application\nName=' + name + '\nExec=/opt/' + package_name + '/' + name + '\nTerminal=false\nCategories=Utility;\n', encoding='utf-8')
        output = work / 'artifacts'
        output.mkdir()
        values = {
            'CPACK_GENERATOR': ';'.join(formats), 'CPACK_PACKAGE_NAME': package_name,
            'CPACK_PACKAGE_VERSION': version, 'CPACK_PACKAGE_CONTACT': package_name + ' maintainers',
            'CPACK_PACKAGE_DESCRIPTION_SUMMARY': name + ' standalone application',
            'CPACK_PACKAGE_DESCRIPTION': name + ' standalone application',
            'CPACK_PACKAGE_FILE_NAME': package_name + '-' + version + '-' + target.key,
            'CPACK_PACKAGE_DIRECTORY': output,
            'CPACK_INSTALLED_DIRECTORIES': str(payload) + ';/','CPACK_PACKAGING_INSTALL_PREFIX': '/',
            'CPACK_DEBIAN_PACKAGE_ARCHITECTURE': {'arm64':'arm64','x86_64':'amd64'}[target.arch],
            'CPACK_DEBIAN_PACKAGE_SHLIBDEPS': 'ON',
            'CPACK_RPM_PACKAGE_ARCHITECTURE': {'arm64':'aarch64','x86_64':'x86_64'}[target.arch],
            'CPACK_RPM_PACKAGE_LICENSE': 'Unspecified',
            'CPACK_RPM_SPEC_MORE_DEFINE': '%define __os_install_post %{nil}',
        }
        if deb_depends is not None:
            if not deb_depends.strip() or '\n' in deb_depends:
                raise ValueError('Explicit Debian dependencies must be nonempty and single-line')
            values['CPACK_DEBIAN_PACKAGE_SHLIBDEPS'] = 'OFF'
            values['CPACK_DEBIAN_PACKAGE_DEPENDS'] = deb_depends
        config = work / 'CPackConfig.cmake'
        config.write_text('\n'.join('set(' + k + ' ' + _literal(v) + ')' for k,v in values.items()) + '\n', encoding='utf-8')
        subprocess.run(['cpack', '--config', str(config)], cwd=work, check=True)
        expected = {'.tar.gz' if x == 'TGZ' else '.' + x.lower() for x in formats}
        artifacts = [p for p in output.iterdir() if p.is_file() and any(p.name.endswith(s) for s in expected)]
        if len(artifacts) != len(formats):
            raise RuntimeError('CPack did not generate every requested format')
        # Publish only this release, never hash unrelated historical dist files.
        release = work / 'release'
        release.mkdir()
        for artifact in artifacts:
            shutil.move(str(artifact), release / artifact.name)
        write_release_manifest(target, release)
        return _publish(work, release, DIST / 'releases' / target.key)
