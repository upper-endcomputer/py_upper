from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import APP, BUILD, Target, cython_config, require_local_python, staging_dir, runtime_spec
from .pbs_sdk import sdk_info
from .config import resolve_target_python
from .toolchain import resolve_toolchain
from .wheel import install_wheels


CYTHON_REQUIREMENT = "Cython>=3.1,<3.2"


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
    for p in (APP / "src").rglob("*.py"):
        # Package __init__.py files remain Python package markers. Every
        # actual application module, including the entry module, is compiled.
        if p.name == "__init__.py":
            continue
        mod = module_name(p)
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


def _cython_version(host_python: Path, env: dict[str, str] | None = None) -> str | None:
    """Read Cython's installed distribution version without importing Cython.

    The build tool may be launched under a debugger. Importing a missing
    Cython package in a child ``python -c`` process causes debuggers to stop
    on the expected ModuleNotFoundError before the bootstrap code can recover.
    ``importlib.metadata`` lets us probe the installed distribution without
    raising that exception.
    """
    probe = subprocess.run(
        [
            str(host_python),
            "-c",
            "import importlib.metadata as m; "
            "d=next(m.distributions(name=\"Cython\"), None); "
            "print(d.version if d else \"\")",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def _supported_cython(version: str | None) -> bool:
    if not version:
        return False
    parts = version.split(".")
    try:
        return (int(parts[0]), int(parts[1])) == (3, 1)
    except (ValueError, IndexError):
        return False


def _cython_cache_dir(host_python: Path) -> Path:
    return BUILD / "host-tools" / f"cython-{host_python.stem}-{host_python.parent.name}"


def _cython_env(host_python: Path) -> dict[str, str]:
    """Return an environment where a supported Cython is importable.

    The build tool deliberately does not require the developer to pre-install
    Cython into their selected Python. If it is missing (or outside the
    supported 3.1.x range), bootstrap it once into a py_upper-owned cache
    instead of modifying the user's Python environment.

    The cache is checked before running pip, so repeated builds do not
    reinstall Cython. The version probe never imports Cython, which also keeps
    VS Code/debugpy from stopping on an expected missing-module exception.
    """
    env = dict(os.environ)
    host_version = _cython_version(host_python, env)
    if _supported_cython(host_version):
        return env

    cache = _cython_cache_dir(host_python)
    cache_env = dict(env)
    cache_env["PYTHONPATH"] = str(cache) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cache_version = _cython_version(host_python, cache_env) if cache.exists() else None
    if not _supported_cython(cache_version):
        cache.mkdir(parents=True, exist_ok=True)
        run(
            [
                str(host_python), "-m", "pip", "install",
                "--disable-pip-version-check", "--no-input",
                "--target", str(cache), CYTHON_REQUIREMENT,
            ],
        )
        cache_env = dict(env)
        cache_env["PYTHONPATH"] = str(cache) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        cache_version = _cython_version(host_python, cache_env)
        if not _supported_cython(cache_version):
            raise RuntimeError(
                f"Cython bootstrap completed but a supported Cython is still unavailable under {host_python}. "
                f"Expected {CYTHON_REQUIREMENT}; cache={cache}"
            )

    env["PYTHONPATH"] = str(cache) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def cythonize_to_c(sources: list[Path], host_python: Path) -> dict[Path, Path]:
    """Translate application modules to C without writing generated files into src/.

    Cython is invoked from each source directory with a relative source/output
    path for macOS portability, but generated C files live under build/. This
    keeps the source tree clean and guarantees that packaged output is driven
    by compiled extensions rather than copied .py sources.
    """
    cython_env = _cython_env(host_python)
    generated_root = BUILD / "cython"
    generated_root.mkdir(parents=True, exist_ok=True)
    outputs: dict[Path, Path] = {}
    for source in sources:
        rel = source.relative_to(APP / "src")
        c = generated_root / rel.with_suffix(".c")
        c.parent.mkdir(parents=True, exist_ok=True)
        c.unlink(missing_ok=True)
        # Keep Cython's cwd/source arguments relative (important on macOS),
        # while placing the generated artifact outside app/src.
        output_rel = os.path.relpath(c, source.parent)
        run(
            [str(host_python), "-m", "cython", "--force", "-3", "-o", output_rel, source.name],
            cwd=source.parent,
            env=cython_env,
        )
        if not c.exists():
            candidates = sorted(c.parent.glob(f"{source.stem}.*"))
            generated = ", ".join(p.name for p in candidates if p.suffix in {".c", ".cpp"}) or "none"
            raise RuntimeError(
                f"Cython completed successfully but did not generate {c}. "
                f"source={source}, cwd={source.parent}, generated={generated}, "
                f"host_python={host_python}"
            )
        outputs[source] = c
    return outputs


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
    generated = cythonize_to_c(sources, host_python)
    c_sources: list[Path] = []
    for src in sources:
        c = generated[src]
        if not c.exists():
            raise RuntimeError(f"Cython did not generate {c}")
        c_sources.append(c)

    ext_names = [module_name(s) for s in sources]
    setup = APP / "_py_upper_target_setup.py"
    include = target_python.include_dir
    libdir_arg = repr(str(libdir)) if libdir else "None"
    libraries = repr([python_lib] if python_lib else [])
    source_repr = ",\n    ".join(repr(str(p)) for p in c_sources)
    name_repr = ",\n    ".join(repr(n) for n in ext_names)
    setup.write_text(
        "from setuptools import setup, Extension\n"
        "from setuptools.command.build_ext import build_ext as _build_ext\n"
        "import sysconfig\n"
        f"TARGET_INCLUDE={str(include)!r}\n"
        f"TARGET_LIBDIR={str(libdir) if libdir else None!r}\n"
        f"TARGET_LIBRARIES={libraries}\n"
        f"sources=[{source_repr}]\n"
        f"names=[{name_repr}]\n"
        "class TargetBuildExt(_build_ext):\n"
        "    def finalize_options(self):\n"
        "        super().finalize_options()\n"
        "        host_inc = sysconfig.get_path('include')\n"
        "        self.include_dirs = [d for d in (self.include_dirs or []) if d != host_inc]\n"
        "        if TARGET_INCLUDE not in self.include_dirs: self.include_dirs.insert(0, TARGET_INCLUDE)\n"
        "        if TARGET_LIBDIR and TARGET_LIBDIR not in (self.library_dirs or []): self.library_dirs.insert(0, TARGET_LIBDIR)\n"
        "ext=[Extension(name, [src], include_dirs=[TARGET_INCLUDE], library_dirs=[TARGET_LIBDIR] if TARGET_LIBDIR else [], libraries=TARGET_LIBRARIES) for src,name in zip(sources,names)]\n"
        "setup(name='py-upper-target-native', ext_modules=ext, cmdclass={'build_ext': TargetBuildExt})\n",
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
    # setuptools runs under the development interpreter, so its extension
    # suffix describes the host Python. Normalize it to the *target* ABI.
    # This is essential when, for example, a Python 3.13 build host produces
    # a Python 3.8 runtime package.
    if target.os == "windows":
        target_suffix = f".{target_python.abi_tag}-{target_python.platform_tag}.pyd"
    elif target.os == "linux":
        arch = {"x86_64": "x86_64", "arm64": "aarch64"}[target.arch]
        target_suffix = f".{target_python.abi_tag}-{arch}-linux-gnu.so"
    else:
        target_suffix = f".{target_python.abi_tag}-darwin.so"
    renamed = []
    for out in outputs:
        stem = out.name.split(".", 1)[0]
        dst = out if out.name.endswith(target_suffix) else out.with_name(stem + target_suffix)
        if out != dst:
            out.rename(dst)
        renamed.append(dst)
    outputs = renamed
    if len(outputs) != len(sources):
        raise RuntimeError(f"Expected {len(sources)} native extensions, got {len(outputs)} in {build_root}")
    return outputs


def copy_python_tree(site: Path) -> None:
    # Never copy development caches or generated bytecode into the package.
    # Actual application modules are removed after their native extensions are
    # installed; package __init__.py files remain as Python package markers.
    shutil.copytree(
        APP / "src",
        site,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "*.c", "*.cpp"),
    )


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
