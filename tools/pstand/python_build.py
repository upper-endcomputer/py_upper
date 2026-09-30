from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import APP, BUILD, Target, cython_config, require_local_python, staging_dir
from .pbs_sdk import sdk_info
from .target_python import resolve_target_python
from .toolchain import resolve_toolchain
from .wheel import install_wheels


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(map(str, cmd)))
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def module_name(path: Path) -> str:
    src = APP / "src"
    rel = path.relative_to(src).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def selected_sources() -> list[Path]:
    cfg = cython_config()
    include = cfg.get("include", [])
    exclude = cfg.get("exclude", [])
    out = []
    for p in (APP / "src" / "myapp").rglob("*.py"):
        mod = module_name(p)
        if mod in {"myapp", "myapp.__main__", "myapp.main"}:
            continue
        if any(fnmatch.fnmatch(mod, pat) for pat in include) and not any(fnmatch.fnmatch(mod, pat) for pat in exclude):
            out.append(p)
    return sorted(out)


def _target_lib_dir(root: Path, target: Target) -> Path | None:
    candidates = [
        root / "install" / "libs",
        root / "install" / "lib",
        root / "libs",
        root / "lib",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _python_library(root: Path, target: Target) -> tuple[Path | None, str | None]:
    libdir = _target_lib_dir(root, target)
    if not libdir:
        return None, None
    if target.os == "windows":
        matches = sorted(libdir.glob("python*.lib"))
        if matches:
            name = matches[0].stem
            return libdir, name
    else:
        # Extension modules on macOS normally resolve Python symbols from the
        # embedding executable/runtime. Do not force a PBS libpython dylib.
        return libdir, None
    return libdir, None


def cythonize_to_c(sources: list[Path], host_python: Path) -> None:
    for source in sources:
        c = source.with_suffix(".c")
        run([str(host_python), "-m", "cython", "-3", "-o", str(c), str(source)], cwd=APP)
        if not c.exists():
            raise RuntimeError(f"Cython did not generate {c}")


def build_target_extensions(target: Target, sources: list[Path]) -> list[Path]:
    if not sources:
        return []
    host_python = require_local_python()
    target_python = resolve_target_python(target)
    tc = resolve_toolchain(target)
    sdk = target_python.root
    libdir, python_lib = _python_library(sdk, target)

    # Generate C with the host build tools, then compile/link that generated C
    # using the target toolchain and target CPython headers/libs. This is the
    # important v0.7 split: Cython itself need not execute target machine code.
    c_sources: list[Path] = []
    cythonize_to_c(sources, host_python)
    for src in sources:
        c = src.with_suffix(".c")
        if not c.exists():
            raise RuntimeError(f"Cython did not generate {c}")
        c_sources.append(c)

    ext_names = [module_name(s) for s in sources]
    setup = APP / "_pystand2_target_setup.py"
    include = target_python.include_dir
    libdir_arg = repr(str(libdir)) if libdir else "None"
    libraries = repr([python_lib] if python_lib else [])
    source_repr = ",\n    ".join(repr(str(p)) for p in c_sources)
    name_repr = ",\n    ".join(repr(n) for n in ext_names)
    setup.write_text(
        "from setuptools import setup, Extension\n"
        "from pathlib import Path\n"
        f"sources=[{source_repr}]\n"
        f"names=[{name_repr}]\n"
        f"include={str(include)!r}\n"
        f"libdir={libdir_arg}\n"
        f"libraries={libraries}\n"
        "ext=[]\n"
        "for src,name in zip(sources,names):\n"
        "    ext.append(Extension(name, [src], include_dirs=[include], library_dirs=[libdir] if libdir else [], libraries=libraries))\n"
        "setup(name='pystand2-target-native', ext_modules=ext)\n",
        encoding="utf-8",
    )
    build_root = BUILD / "native" / target.key
    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True)

    env = dict(os.environ)
    env.update(tc.env)
    env["PYSTAND_TARGET"] = target.key
    env["PYSTAND_TARGET_PYTHON"] = str(target_python.executable)
    env["PYSTAND_TARGET_INCLUDE"] = str(include)
    if libdir:
        env["PYSTAND_TARGET_LIB"] = str(libdir)
    # Tell distutils not to replace our MSVC environment with host defaults.
    if target.os == "windows":
        env["DISTUTILS_USE_SDK"] = "1"
        env["MSSdk"] = "1"
    run([str(host_python), str(setup), "build_ext", "--build-lib", str(build_root)], cwd=APP, env=env)
    setup.unlink(missing_ok=True)

    outputs = []
    for p in build_root.rglob("*.pyd" if target.os == "windows" else "*.so"):
        outputs.append(p)
    if target.os == "windows":
        target_tag = {'x86': 'win32', 'x86_64': 'win_amd64', 'arm64': 'win_arm64'}[target.arch]
        target_suffix = f".cp{target_python.python_major_minor.replace('.', '')}-{target_tag}.pyd"
        renamed = []
        for out in outputs:
            if out.name.endswith(target_suffix):
                renamed.append(out)
                continue
            stem = out.name.split(".", 1)[0]
            dst = out.with_name(stem + target_suffix)
            out.rename(dst)
            renamed.append(dst)
        outputs = renamed
    if len(outputs) != len(sources):
        raise RuntimeError(f"Expected {len(sources)} native extensions, got {len(outputs)} in {build_root}")
    return outputs


def copy_python_tree(site: Path) -> None:
    shutil.copytree(APP / "src" / "myapp", site / "myapp", dirs_exist_ok=True)


def remove_cython_source_py(site: Path) -> None:
    for src in selected_sources():
        rel = src.relative_to(APP / "src").with_suffix("")
        py = site / rel.with_suffix(".py")
        if py.name != "__init__.py":
            py.unlink(missing_ok=True)


def copy_native_outputs(outputs: list[Path], site: Path, target: Target) -> None:
    build_root = BUILD / "native" / target.key
    for out in outputs:
        rel = out.relative_to(build_root)
        dest = site / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, dest)


def build_python_package(target: Target) -> Path:
    host_python = require_local_python()
    stage = staging_dir(target)
    if stage.exists():
        shutil.rmtree(stage)
    site = stage / "site-packages"
    site.mkdir(parents=True)

    install_wheels(target, site)
    copy_python_tree(site)
    sources = selected_sources()
    outputs = build_target_extensions(target, sources)
    copy_native_outputs(outputs, site, target)
    remove_cython_source_py(site)

    print(f"Python stage ready: {site}")
    return stage
