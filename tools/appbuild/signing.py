# 中文维护说明：按镜像类型签署 Mach-O 与嵌套 bundle；扩展入 egg 前签名，应用封签在最后。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Sign loadable Mach-O images, including extension images before egg packing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
from .config import ROOT, DIST, app_name
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


def release(target, identity=None, notary_profile=None, *, output=None) -> int:
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
