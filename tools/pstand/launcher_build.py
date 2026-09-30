from __future__ import annotations

import os
import shutil
import subprocess

from .config import BUILD, LAUNCHER, Target
from .target_python import resolve_target_python
from .toolchain import resolve_toolchain


def build_launcher(target: Target):
    sdk = resolve_target_python(target)
    tc = resolve_toolchain(target)
    b = BUILD / "launcher" / target.key
    if b.exists():
        shutil.rmtree(b)
    b.mkdir(parents=True)
    env = dict(os.environ)
    env.update(tc.env)
    cmd = [
        "cmake", "-S", str(LAUNCHER), "-B", str(b), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DPYSTAND_PYTHON_INCLUDE={sdk.include_dir}",
    ]
    if target.os == "macos":
        cmd += [f"-DCMAKE_OSX_ARCHITECTURES={target.arch}"]
        if tc.deployment_target:
            cmd += [f"-DCMAKE_OSX_DEPLOYMENT_TARGET={tc.deployment_target}"]
    subprocess.run(cmd, check=True, env=env)
    subprocess.run(["cmake", "--build", str(b), "--config", "Release"], check=True, env=env)
    exe = b / ("PyStand.exe" if target.os == "windows" else "PyStand")
    if not exe.exists() and (b / "Release" / exe.name).exists():
        exe = b / "Release" / exe.name
    if not exe.exists():
        raise RuntimeError(f"Launcher build did not produce {exe}")
    return exe
