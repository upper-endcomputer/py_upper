# 中文维护说明：解析构建命令并协调候选包验证、签名与发布；构建写入使用工作区互斥。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import argparse
import json
import time
import os
import shutil
import subprocess
import sys
from pathlib import Path

from appbuild.appdir import APP_DIR_ENV, resolve_app_dir, select_from_argv

# --app has to reach config.py before the module-level APP/APP_CONFIG paths are
# resolved. Programmatic callers can set APPBUILD_APP_DIR instead.
select_from_argv(sys.argv[1:])

from appbuild.config import (
    APP, ROOT, BUILD, DIST, TARGETS, app_name, host_description, host_target, pbs_release,
    pbs_sdk_dir, python_version, runtime_provider, target_runtime_dir,
    trim_policy, validate_build_python_version, validate_target, require_local_python,
)
from appbuild.egg import COMPRESSIONS, egg_config
from appbuild.input_lock import verify_lock, write_lock
from appbuild.package import _publish, build_launcher, package, write_release_manifest
from appbuild.application_build import build_python_package
from appbuild.runtime import ensure_runtime, ensure_sdk
from appbuild.release import sign_release
from appbuild.fs import retire_tree
from appbuild.verify import run_launcher_smoke, run_packaged_launcher, run_target_python_smoke
from appbuild.toolchain import describe_toolchain
from appbuild.wheels import resolve_wheels
from appbuild.trim import PRESETS
from appbuild.egg_loader import BACKENDS
from appbuild.verify import verify


def _run_app(out: Path, target) -> int:
    """启动已打包目标应用并返回退出码；缺少可执行文件时报错。"""
    name = app_name()
    executable = out / (f"{name}.exe" if target.os == "windows" else name)
    if target.os == "macos":
        executable = out / "Contents" / "MacOS" / name
    if not executable.exists():
        raise RuntimeError(f"Packaged executable missing: {executable}")
    result = run_packaged_launcher(executable, cwd=out)
    print("Application exit code:", result.returncode)
    return result.returncode


def _clean() -> int:
    # dist/ carries a working interpreter that environment discovery tools
    # execute, so deleting it in place races with the bytecode they write.
    """先退役 dist 再删除 build，降低运行中解释器与目录删除的竞争。"""
    retire_tree(DIST, trash=BUILD / "retired")
    if BUILD.exists():
        shutil.rmtree(BUILD)
    print("Cleaned build/ and dist/")
    return 0


def _doctor(target) -> int:
    """报告宿主、应用、runtime 和工具链状态；关键工具不可用返回非零。"""
    print("host:", host_description())
    print("app:", APP)
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
    missing = []
    for tool in ("cmake", "ninja"):
        path = shutil.which(tool)
        print(f"{tool}:", path or "MISSING")
        if not path:
            missing.append(tool)
    try:
        print("toolchain:", describe_toolchain(target))
    except Exception as exc:
        print("toolchain: UNAVAILABLE:", exc)
        missing.append("toolchain")
    # A build cannot start without these, so the diagnostic exits non-zero and
    # doubles as the toolchain gate in CI instead of a second, hand-rolled probe.
    if missing:
        print("doctor: FAILED, missing:", ", ".join(missing))
        return 1
    return 0


def main(argv=None) -> int:
    """在配置导入前选择应用并持有工作区锁；帮助请求直接交给参数解析器。"""
    if argv is not None:
        requested = select_from_argv(argv)
        if requested is not None and resolve_app_dir(requested) != APP:
            # config.APP is fixed when appbuild.config is imported. The normal
            # CLI path selects it before that import; a programmatic caller
            # that already imported this module is re-run in a child process
            # so the same --app contract still applies.
            env = os.environ.copy()
            env[APP_DIR_ENV] = str(resolve_app_dir(requested))
            return subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), *argv],
                env=env,
            ).returncode
    # 帮助由 argparse 输出并退出，不会进入写入阶段；无需等待构建互斥。
    arguments = sys.argv[1:] if argv is None else argv
    if "--help" in arguments or "-h" in arguments:
        return _main(argv)
    from appbuild.fs import workspace_lock
    try:
        with workspace_lock(ROOT):
            return _main(argv)
    except RuntimeError as exc:
        print("ERROR:", exc)
        return 1


def _main(argv=None) -> int:
    """解析 CLI，顺序执行构建、验证、签名和发布；失败候选包不替换已发布版本。"""
    validate_build_python_version()
    parser = argparse.ArgumentParser(description="appbuild - standalone Python application builder")
    parser.add_argument("command", nargs="?", choices=["doctor", "build", "run", "verify", "lock", "release", "clean"])
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--doctor", action="store_true")
    actions.add_argument("--run", dest="run_app", action="store_true")
    actions.add_argument("--verify", action="store_true")
    actions.add_argument("--lock", action="store_true")
    actions.add_argument("--release", action="store_true")
    actions.add_argument("--clean", action="store_true")
    parser.add_argument("--app", metavar="DIR",
                        help="application directory containing pyproject.toml and src/ "
                             "(default: <repo>/app; env APPBUILD_APP_DIR)")
    parser.add_argument("--target", choices=list(TARGETS))
    parser.add_argument("--locked", action="store_true")
    parser.add_argument("--linux-format", action="append", choices=["TGZ", "DEB", "RPM"],
                        help="Linux release format; repeatable; default TGZ and DEB")
    parser.add_argument("--linux-deb-depends", help="explicit Debian Depends for cross/sysroot packaging; otherwise use dpkg-shlibdeps")
    parser.add_argument("--identity")
    parser.add_argument("--notary-profile")
    parser.add_argument("--trim-preset", choices=tuple(PRESETS), metavar="NAME",
                        help="add a named trim rule set for this build: " + ", ".join(PRESETS))
    parser.add_argument("--trim-site", action="append", default=[], metavar="GLOB",
                        help="remove paths from site-packages; repeatable (relative glob)")
    parser.add_argument("--trim-site-keep", action="append", default=[], metavar="GLOB",
                        help="never remove matching site-packages paths; repeatable")
    parser.add_argument("--trim-runtime", action="append", default=[], metavar="GLOB",
                        help="remove paths from the bundled runtime; repeatable")
    parser.add_argument("--trim-runtime-keep", action="append", default=[], metavar="GLOB",
                        help="never remove matching runtime paths; repeatable")
    egg_actions = parser.add_mutually_exclusive_group()
    egg_actions.add_argument("--egg", dest="egg", action="store_true", default=None,
                             help="pack application code into an encrypted bundle archive for this build")
    egg_actions.add_argument("--no-egg", dest="egg", action="store_false", default=None,
                             help="ship loose application files even when the project enables the egg")
    parser.add_argument("--egg-key", metavar="HEX",
                        help="override [tool.appbuild.egg].key_hex (at least 16 hex bytes)")
    parser.add_argument("--egg-backend", choices=BACKENDS, metavar="NAME",
                        help="egg extension loader: auto, memory or extract")
    parser.add_argument("--egg-compression", choices=COMPRESSIONS, metavar="NAME",
                        help="egg archive compression: " + ", ".join(COMPRESSIONS))
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
            os.environ["APPBUILD_LOCKED"] = "1"

        trim = trim_policy(args.trim_preset).merge(
            site_packages=args.trim_site,
            site_packages_keep=args.trim_site_keep,
            runtime=args.trim_runtime,
            runtime_keep=args.trim_runtime_keep,
        )

        egg = egg_config(args.egg, args.egg_key, args.egg_backend, args.egg_compression)

        started = time.perf_counter()
        timings = {}
        def phase(name):
            """记录上一阶段耗时并推进计时起点，供本次构建诊断使用。"""
            nonlocal started
            now = time.perf_counter()
            timings[name] = round(now - started, 6)
            started = now
        print("[1/6] Runtime + SDK")
        ensure_sdk(target)
        ensure_runtime(target)
        phase("runtime-sdk")
        print("[2/6] Third-party dependencies")
        signing_identity = None
        if action == "release" and target.os == "macos":
            signing_identity = args.identity or os.environ.get("APPBUILD_CODESIGN_IDENTITY")
            if not signing_identity:
                raise RuntimeError("macOS release requires --identity")
        build_python_package(target, trim, egg, signing_identity=signing_identity)
        phase("dependencies-cython")
        print("[3/6] Target launcher")
        launcher = build_launcher(target, egg)
        phase("launcher-build")
        print("[4/6] Package + native dependency closure")
        out = package(target, launcher, trim, publish=False)
        phase("assembly")
        print("[5/6] Static verification")
        if verify(target, out):
            return 1
        phase("static-verify")
        print("[6/6] Target runtime smoke")
        run_target_python_smoke(target)
        run_launcher_smoke(target, out / ("Contents/MacOS/" + app_name() if target.os == "macos" else (app_name() + ".exe" if target.os == "windows" else app_name())))

        phase("smoke")
        from appbuild.manifest import write_build_info
        write_build_info(out, target, {"egg": egg.enabled, "backend": egg.backend,
                                      "compression": egg.compression, "trim": vars(trim)})
        timing_path = BUILD / "timings" / (target.key + ".json")
        timing_path.parent.mkdir(parents=True, exist_ok=True)
        timing_path.write_text(json.dumps(timings, indent=2) + "\n", encoding="utf-8")
        if action == "release" and target.os != "linux":
            sign_release(target, args.identity, args.notary_profile, output=out)
            run_launcher_smoke(target, out / ("Contents/MacOS/" + app_name() if target.os == "macos" else app_name() + ".exe"))

        if action == "release" and target.os == "linux":
            from appbuild.release import build_linux_installers
            print("Linux release:", build_linux_installers(target, args.linux_format or ("TGZ", "DEB"), args.linux_deb_depends, source=out))
        out = _publish(out.parent, out, DIST / out.name)
        if action == "run":
            return _run_app(out, target)
        if action == "release":
            if target.os != "linux":
                print("Release manifest:", write_release_manifest(target))
        else:
            print("READY:", out)
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed with exit code {exc.returncode}: {' '.join(map(str, exc.cmd)) if isinstance(exc.cmd, (list, tuple)) else exc.cmd}")
        return exc.returncode or 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        if os.environ.get("APPBUILD_TRACEBACK") == "1":
            raise
        return 1



if __name__ == "__main__":
    raise SystemExit(main())
