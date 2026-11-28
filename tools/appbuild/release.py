# 中文维护说明：生成 Linux 安装包并签署 Windows/macOS 发布产物；扩展签名须先于归档。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""发布产物：Linux 安装包生成，以及 Windows/macOS 签名、公证。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import tempfile
import shutil
import subprocess
from .config import ROOT, BUILD, DIST, app_name, project_version
from .native.inspect import inspect


def sign_macos_files(paths, identity):
    """对可加载 Mach-O 和嵌套 bundle 从内到外签名；调用者负责最后对外层应用封签。"""
    tool = shutil.which("codesign")
    if not tool:
        raise RuntimeError("macOS release requires codesign")
    for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not path.is_symlink() and path.suffix in {".framework", ".app", ".xpc"}:
            subprocess.run([tool, "--force", "--timestamp", "--options", "runtime",
                            "--sign", identity, str(path)], check=True)
            continue
        if not path.is_file() or path.is_symlink():
            continue
        try:
            info = inspect(path)
        except (OSError, ValueError):
            continue
        if info.loadable and info.format.startswith("Mach-O"):
            subprocess.run([tool, "--force", "--timestamp", "--options", "runtime",
                            "--sign", identity, str(path)], check=True)


def sign_release(target, identity=None, notary_profile=None, *, output=None) -> int:
    """签署候选 Windows/macOS 包；macOS 可提交 ZIP 公证并钉固，工具失败向上层传播。"""
    if target.os == "linux":
        print("Linux release: use your distribution signing/package system.")
        return 0
    if target.os == "windows":
        tool = shutil.which("signtool")
        cert = os.environ.get("APPBUILD_SIGN_CERT")
        if not tool or not cert:
            raise RuntimeError("Windows release requires signtool and APPBUILD_SIGN_CERT")
        for path in (output or (DIST / app_name())).rglob("*"):
            if path.is_file() and path.suffix.lower() in {".exe", ".dll", ".pyd"}:
                subprocess.run([tool, "sign", "/fd", "SHA256", "/a", "/f", cert, str(path)], check=True)
        return 0
    app = output or (DIST / f"{app_name()}.app")
    codesign = shutil.which("codesign")
    xcrun = shutil.which("xcrun")
    ident = identity or os.environ.get("APPBUILD_CODESIGN_IDENTITY")
    if not codesign or not xcrun or not ident:
        raise RuntimeError("macOS release requires codesign/xcrun and --identity")
    sign_macos_files(app.rglob("*"), ident)
    subprocess.run([codesign, "--force", "--timestamp", "--options", "runtime", "--sign", ident, str(app)], check=True)
    subprocess.run([codesign, "--verify", "--deep", "--strict", "--verbose=2", str(app)], check=True)
    if notary_profile:
        temporary_root = ROOT / ".tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="notary-", dir=str(temporary_root)) as work:
            archive = Path(work) / (app.name + ".zip")
            subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
            result = subprocess.run([xcrun, "notarytool", "submit", str(archive), "--keychain-profile", notary_profile, "--wait", "--output-format", "json"], check=True, capture_output=True, text=True)
            if json.loads(result.stdout).get("status") != "Accepted":
                raise RuntimeError("Notarization was not accepted: " + result.stdout)
        subprocess.run([xcrun, "stapler", "staple", str(app)], check=True)
        subprocess.run([xcrun, "stapler", "validate", str(app)], check=True)
    return 0


def _cmake_literal(value):
    """把值编码为 CMake bracket literal；拒绝能提前结束该字面量的内容。"""
    value = str(value)
    if ']=]' in value:
        raise ValueError('Invalid CPack value')
    return '[=[' + value + ']=]'


def build_linux_installers(target, formats=('TGZ', 'DEB'), deb_depends=None, *, source=None):
    """从已验证目录生成请求的 Linux 归档格式；全部成功后替换目标发布目录，工具失败向上传播。"""
    from .package import _publish, write_release_manifest

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
        config.write_text('\n'.join('set(' + k + ' ' + _cmake_literal(v) + ')' for k,v in values.items()) + '\n', encoding='utf-8')
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
