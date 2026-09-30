from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from py_upper.config import (
    BUILD, DIST, TARGETS, app_name, host_description, host_target, pbs_release,
    pbs_sdk_dir, python_version, runtime_provider, staging_dir, target_runtime_dir,
    validate_build_python_version, validate_target, require_local_python,
)
from py_upper.launcher_build import build_launcher
from py_upper.lock import verify_lock, write_lock
from py_upper.package import package
from py_upper.python_build import build_python_package
from py_upper.release_artifacts import write_release_manifest
from py_upper.runtime_provider import ensure_runtime, ensure_sdk, sdk_info
from py_upper.smoke import run_launcher_smoke, run_target_python_smoke
from py_upper.toolchain import describe_toolchain
from py_upper.third_party import resolve_wheels
from py_upper.verify import verify


def _run_app(out: Path, target) -> int:
    name = app_name()
    executable = out / (f"{name}.exe" if target.os == "windows" else name)
    if target.os == "macos":
        executable = out / "Contents" / "MacOS" / name
    if not executable.exists():
        raise RuntimeError(f"Packaged executable missing: {executable}")
    result = subprocess.run([str(executable)], cwd=out, check=False)
    print("Application exit code:", result.returncode)
    return result.returncode


def _clean() -> int:
    for path in (BUILD, DIST):
        if path.exists():
            shutil.rmtree(path)
    print("Cleaned build/ and dist/")
    return 0


def _doctor(target) -> int:
    print("host:", host_description())
    print("target:", target.key, target.triple)
    print("target-python:", python_version())
    print("runtime-provider:", runtime_provider())
    print("PBS release:", pbs_release() or "auto")
    try:
        print("dev-python:", require_local_python())
    except Exception as exc:
        print("dev-python: UNAVAILABLE:", exc)
    print("SDK:", pbs_sdk_dir(target))
    print("runtime:", target_runtime_dir(target))
    print("cmake:", shutil.which("cmake") or "MISSING")
    print("ninja:", shutil.which("ninja") or "MISSING")
    try:
        print("toolchain:", describe_toolchain(target))
    except Exception as exc:
        print("toolchain: UNAVAILABLE:", exc)
    return 0


def main(argv=None) -> int:
    validate_build_python_version()
    parser = argparse.ArgumentParser(description="py_upper - standalone Python application builder")
    parser.add_argument("command", nargs="?", choices=["doctor", "build", "run", "verify", "lock", "release", "clean"])
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--doctor", action="store_true")
    actions.add_argument("--run", dest="run_app", action="store_true")
    actions.add_argument("--verify", action="store_true")
    actions.add_argument("--lock", action="store_true")
    actions.add_argument("--release", action="store_true")
    actions.add_argument("--clean", action="store_true")
    parser.add_argument("--target", choices=list(TARGETS))
    parser.add_argument("--locked", action="store_true")
    parser.add_argument("--identity")
    parser.add_argument("--notary-profile")
    args = parser.parse_args(argv)

    action = args.command or "build"
    if args.doctor:
        action = "doctor"
    elif args.run_app:
        action = "run"
    elif args.verify:
        action = "verify"
    elif args.lock:
        action = "lock"
    elif args.release:
        action = "release"
    elif args.clean:
        action = "clean"

    target = validate_target(args.target)
    try:
        if action == "doctor":
            return _doctor(target)
        if action == "clean":
            return _clean()
        if action == "verify":
            return verify(target)
        if action == "lock":
            ensure_sdk(target)
            ensure_runtime(target)
            resolve_wheels(target)
            print("Lock written:", write_lock(target))
            return 0
        if action not in {"build", "run", "release"}:
            return 1
        if action == "run" and target != host_target():
            raise RuntimeError(f"--run requires host target; host={host_target().key}, target={target.key}")
        if args.locked:
            verify_lock(target)
            os.environ["PY_UPPER_LOCKED"] = "1"
            os.environ["PYSTAND_LOCKED"] = "1"

        print("[1/6] Runtime + SDK")
        ensure_sdk(target)
        ensure_runtime(target)
        print("[2/6] Third-party dependencies")
        build_python_package(target)
        print("[3/6] Target launcher")
        launcher = build_launcher(target)
        print("[4/6] Package + native dependency closure")
        out = package(target, launcher)
        print("[5/6] Static verification")
        if verify(target):
            return 1
        print("[6/6] Target runtime smoke")
        run_target_python_smoke(target)
        run_launcher_smoke(target, out / ("Contents/MacOS/" + app_name() if target.os == "macos" else (app_name() + ".exe" if target.os == "windows" else app_name())))

        if action == "run":
            return _run_app(out, target)
        if action == "release":
            release(target, args.identity, args.notary_profile)
            print("Release manifest:", write_release_manifest(target))
        else:
            print("READY:", out)
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed with exit code {exc.returncode}: {' '.join(map(str, exc.cmd)) if isinstance(exc.cmd, (list, tuple)) else exc.cmd}")
        return exc.returncode or 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        if os.environ.get("PY_UPPER_TRACEBACK") == "1":
            raise
        return 1


def release(target, identity=None, notary_profile=None) -> int:
    if target.os == "linux":
        print("Linux release: use your distribution signing/package system.")
        return 0
    if target.os == "windows":
        tool = shutil.which("signtool")
        cert = os.environ.get("PY_UPPER_SIGN_CERT") or os.environ.get("PYSTAND_SIGN_CERT")
        if not tool or not cert:
            raise RuntimeError("Windows release requires signtool and PY_UPPER_SIGN_CERT")
        for path in (DIST / app_name()).rglob("*"):
            if path.is_file() and path.suffix.lower() in {".exe", ".dll", ".pyd"}:
                subprocess.run([tool, "sign", "/fd", "SHA256", "/a", "/f", cert, str(path)], check=True)
        return 0
    app = DIST / f"{app_name()}.app"
    codesign = shutil.which("codesign")
    xcrun = shutil.which("xcrun")
    ident = identity or os.environ.get("PY_UPPER_CODESIGN_IDENTITY") or os.environ.get("PYSTAND_CODESIGN_IDENTITY")
    if not codesign or not xcrun or not ident:
        raise RuntimeError("macOS release requires codesign/xcrun and --identity")
    nested = [p for p in app.rglob("*") if p.is_file() and p.suffix.lower() in {".dylib", ".so"}]
    nested.append(app / "Contents" / "MacOS" / app_name())
    for path in nested:
        subprocess.run([codesign, "--force", "--timestamp", "--options", "runtime", "--sign", ident, str(path)], check=True)
    subprocess.run([codesign, "--force", "--timestamp", "--options", "runtime", "--sign", ident, str(app)], check=True)
    subprocess.run([codesign, "--verify", "--deep", "--strict", "--verbose=2", str(app)], check=True)
    if notary_profile:
        subprocess.run([xcrun, "notarytool", "submit", str(app), "--keychain-profile", notary_profile, "--wait"], check=True)
        subprocess.run([xcrun, "stapler", "staple", str(app)], check=True)
        subprocess.run([xcrun, "stapler", "validate", str(app)], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
