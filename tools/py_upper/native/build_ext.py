from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..config import BUILD, APP, Target, resolve_target_python
from ..toolchain import resolve_toolchain


def _run(cmd: list[str]) -> None:
    print("+", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True)


def compile_unix_extensions(target: Target, generated: dict[Path, Path]) -> list[Path]:
    target_python = resolve_target_python(target)
    tc = resolve_toolchain(target)
    out_root = BUILD / "native" / target.key
    if out_root.exists():
        import shutil
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)
    for source, c_source in generated.items():
        rel = source.relative_to(APP / "src").with_suffix("")
        output = out_root.joinpath(*rel.parts[:-1], rel.name + target_python.extension_suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        if target.os == "linux":
            cmd = [
                tc.compiler, "-shared", "-fPIC", "-O2", "-DNDEBUG",
                f"-I{target_python.include_dir}", str(c_source), "-lm", "-o", str(output),
            ]
        elif target.os == "macos":
            cmd = [
                tc.compiler, "-dynamiclib", "-fPIC", "-O2", "-DNDEBUG",
                "-arch", target.arch,
                f"-mmacosx-version-min={tc.deployment_target or '11.0'}",
                f"-I{target_python.include_dir}", str(c_source), "-o", str(output),
            ]
        else:
            raise RuntimeError("compile_unix_extensions only supports Unix targets")
        _run(cmd)
    return [
        out_root.joinpath(*source.relative_to(APP / "src").with_suffix("").parts[:-1], source.stem + target_python.extension_suffix)
        for source in generated
    ]
