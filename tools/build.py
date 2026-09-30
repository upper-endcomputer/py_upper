from __future__ import annotations
import argparse, os, shutil, subprocess, platform

from py_upper.config import BUILD, DIST, TARGETS, app_name, pbs_release, pbs_sdk_dir, python_version, target_runtime_dir, validate_target, host_target, require_local_python, runtime_provider, runtime_spec, host_description
from py_upper.toolchain import describe_toolchain
from py_upper.runtime_provider import ensure_sdk, sdk_info
from py_upper.python_build import build_python_package
from py_upper.launcher_build import build_launcher
from py_upper.package import package
from py_upper.runtime_provider import ensure_runtime
from py_upper.wheel import resolve_wheels
from py_upper.lock import write_lock, verify_lock
from py_upper.verify import verify
from py_upper.release_artifacts import write_release_manifest

def main(argv=None):
    p = argparse.ArgumentParser(
        description="py_upper - build, verify and release a standalone Python application",
        epilog=(
            "Default: build.  Examples: python tools/build.py --target linux-x86_64, "
            "python tools/build.py --run, python tools/build.py --release"
        ),
    )
    p.add_argument("command", nargs="?", choices=["doctor", "build", "run", "verify", "lock", "release", "clean"],
                   help="legacy command form; flags below are preferred")
    action = p.add_mutually_exclusive_group()
    action.add_argument("--doctor", action="store_true", help="check the host/target toolchain")
    action.add_argument("--run", dest="run_app", action="store_true", help="build, verify and run (native target only)")
    action.add_argument("--verify", action="store_true", help="verify an existing build")
    action.add_argument("--lock", action="store_true", help="resolve and write the py_upper build lock")
    action.add_argument("--release", action="store_true", help="build, verify and sign the release")
    action.add_argument("--clean", action="store_true", help="remove build/dist outputs")
    p.add_argument("--target", choices=list(TARGETS), help="target platform; defaults to the current host")
    p.add_argument("--locked", action="store_true", help="build only from the existing lock/cache")
    p.add_argument("--identity", help="macOS Developer ID signing identity")
    p.add_argument("--notary-profile", help="macOS notarytool keychain profile")
    a = p.parse_args(argv)

    # Keep the old positional interface working, but make the normal interface
    # one command with a small set of action flags.
    action_name = a.command or "build"
    if a.doctor: action_name = "doctor"
    elif a.run_app: action_name = "run"
    elif a.verify: action_name = "verify"
    elif a.lock: action_name = "lock"
    elif a.release: action_name = "release"
    elif a.clean: action_name = "clean"

    t = validate_target(a.target)

    if action_name == "doctor":
        print("host:", host_description())
        print("target:", t.key, t.triple)
        spec = runtime_spec(t)
        print("target-python:", spec.python)
        print("target-abi:", spec.abi_tag)
        print("runtime-provider:", spec.provider)
        if runtime_provider() == "pbs":
            print("PBS release:", pbs_release())
        try:
            dev = require_local_python()
            probe = subprocess.run([str(dev), "-c", "import sys; print(sys.version)"] , capture_output=True, text=True, check=True).stdout.strip()
            print("dev-python:", dev)
            print("dev-python-version:", probe)
        except Exception as e:
            print("dev-python: UNAVAILABLE:", e)
        print("SDK:", pbs_sdk_dir(t))
        print("runtime:", target_runtime_dir(t))
        print("cmake:", shutil.which("cmake") or "MISSING")
        print("ninja:", shutil.which("ninja") or "MISSING")
        try:
            print("toolchain:", describe_toolchain(t))
        except Exception as e:
            print("toolchain: UNAVAILABLE:", e)
        return 0

    if action_name == "clean":
        for x in (BUILD, DIST):
            if x.exists(): shutil.rmtree(x)
        print("Cleaned build/ and dist/")
        return 0

    if action_name == "lock":
        ensure_sdk(t); ensure_runtime(t); resolve_wheels(t)
        print("Lock written:", write_lock(t))
        return 0

    if action_name == "verify":
        return verify(t)

    if action_name in {"build", "run", "release"}:
        if action_name == "run" and t != host_target():
            raise SystemExit(f"run requires a native target; host={host_target().key}, target={t.key}")
        if a.locked:
            # Validate the existing lock and cache before any operation that could
            # attempt a network download. A locked build is deliberately offline.
            verify_lock(t)
            os.environ["PYSTAND_LOCKED"] = "1"
        ensure_sdk(t); ensure_runtime(t)
        resolve_wheels(t)
        build_python_package(t)
        out = package(t, build_launcher(t))
        rc = verify(t)
        if rc: return rc
        if action_name == "run":
            name = app_name()
            exe = out / (f"{name}.exe" if t.os == "windows" else name) if t.os != "macos" else out / f"Contents/MacOS/{name}"
            return subprocess.call([str(exe)])
        if action_name == "release":
            rc = release(t, a.identity, a.notary_profile)
            if rc:
                return rc
            print("Release manifest:", write_release_manifest(t))
            return 0
        print("READY:", out)
        return 0

    return 1

def release(t, identity=None, notary_profile=None):
    if t.os=="linux":
        print("Linux release: use your distro/package signing system.")
        return 0
    if t.os=="windows":
        tool=shutil.which("signtool")
        cert=os.environ.get("PYSTAND_SIGN_CERT")
        if not tool or not cert:
            raise RuntimeError("Windows release requires signtool and PYSTAND_SIGN_CERT")
        for p in (DIST/app_name()).rglob("*"):
            if p.is_file() and p.suffix.lower() in {".exe",".dll",".pyd"}:
                subprocess.run([tool,"sign","/fd","SHA256","/a","/f",cert,str(p)],check=True)
        return 0
    app=DIST/f"{app_name()}.app"; cs=shutil.which("codesign"); xr=shutil.which("xcrun")
    ident=identity or os.environ.get("PYSTAND_CODESIGN_IDENTITY")
    if not cs or not xr or not ident:
        raise RuntimeError("macOS release requires codesign/xcrun and --identity")
    nested=[p for p in app.rglob("*") if p.is_file() and p.suffix in {".dylib",".so"}]
    nested.append(app/f"Contents/MacOS/{app_name()}")
    for p in nested:
        subprocess.run([cs,"--force","--timestamp","--options","runtime","--sign",ident,str(p)],check=True)
    subprocess.run([cs,"--force","--timestamp","--options","runtime","--sign",ident,str(app)],check=True)
    subprocess.run([cs,"--verify","--deep","--strict",str(app)],check=True)
    if notary_profile:
        subprocess.run([xr,"notarytool","submit",str(app),"--keychain-profile",notary_profile,"--wait"],check=True)
        subprocess.run([xr,"stapler","staple",str(app)],check=True)
        subprocess.run([xr,"stapler","validate",str(app)],check=True)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
