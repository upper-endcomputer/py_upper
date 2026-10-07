"""Application build: Cython generation, target extension compilation and staging."""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from pathlib import Path

from .config import (
    APP, BUILD, Target, cython_config, pip_transfer_args, require_local_python,
    resolve_target_python, staging_dir,
)
from .fs import copy_file_contents, copy_tree_contents
from .toolchain import resolve_toolchain


CYTHON_REQUIREMENT = "Cython>=3.1,<3.3"


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(str(x) for x in cmd))
    try:
        subprocess.run(cmd, cwd=cwd, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Command failed ({exc.returncode}): {' '.join(map(str, cmd))}") from exc


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(APP / "src").with_suffix("").parts)


def selected_sources() -> list[Path]:
    cfg = cython_config()
    include = [str(x) for x in cfg.get("include", ["*"])]
    exclude = [str(x) for x in cfg.get("exclude", [])]
    root = APP / "src"
    result: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        name = module_name(path)
        if any(fnmatch.fnmatch(name, pattern) for pattern in include) and not any(fnmatch.fnmatch(name, pattern) for pattern in exclude):
            result.append(path)
    return result


def _cython_cache_dir(host_python: Path) -> Path:
    probe = subprocess.run([str(host_python), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"], capture_output=True, text=True, check=True)
    version = probe.stdout.strip().replace('.', '_')
    return BUILD / "host-tools" / f"cython-py{version}"


def _cython_version(host_python: Path, env: dict[str, str]) -> str | None:
    probe = subprocess.run(
        [str(host_python), "-c", "import importlib.metadata as m; d=next(m.distributions(name='Cython'), None); print(d.version if d else '')"],
        env=env, capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def _supported_cython(version: str | None) -> bool:
    if not version:
        return False
    try:
        major, minor = (int(x) for x in version.split(".")[:2])
        return (major, minor) in {(3, 1), (3, 2)}
    except (ValueError, TypeError):
        return False


def _cython_env(host_python: Path) -> dict[str, str]:
    base = dict(os.environ)
    installed = _cython_version(host_python, base)
    cache = _cython_cache_dir(host_python)
    env = dict(base)
    if cache.exists():
        env["PYTHONPATH"] = str(cache) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cached = _cython_version(host_python, env)
    if not _supported_cython(installed) and not _supported_cython(cached):
        cache.mkdir(parents=True, exist_ok=True)
        _run([
            str(host_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
            "--target", str(cache),
        ] + pip_transfer_args() + [CYTHON_REQUIREMENT])
        env = dict(base)
        env["PYTHONPATH"] = str(cache) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        cached = _cython_version(host_python, env)
        if not _supported_cython(cached):
            raise RuntimeError(f"Unable to bootstrap {CYTHON_REQUIREMENT} for {host_python}; cache={cache}")
    elif not _supported_cython(installed):
        env["PYTHONPATH"] = str(cache) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def cythonize_to_c(sources: list[Path], host_python: Path) -> dict[Path, Path]:
    generated_root = BUILD / "cython"
    if generated_root.exists():
        # Rebuild the exact source set; stale C output must never be packaged.
        shutil.rmtree(generated_root)
    generated_root.mkdir(parents=True)
    env = _cython_env(host_python)
    outputs: dict[Path, Path] = {}
    for source in sources:
        rel = source.relative_to(APP / "src")
        destination = generated_root / rel.with_suffix(".c")
        destination.parent.mkdir(parents=True, exist_ok=True)
        output_rel = os.path.relpath(destination, source.parent)
        _run(
            [str(host_python), "-m", "cython", "--force", "-3", "-o", output_rel, source.name],
            cwd=source.parent,
            env=env,
        )
        if not destination.exists():
            raise RuntimeError(f"Cython completed but did not generate {destination}")
        outputs[source] = destination
    return outputs


def build_target_extensions(target: Target, sources: list[Path]) -> list[Path]:
    if not sources:
        return []
    host_python = require_local_python()
    generated = cythonize_to_c(sources, host_python)
    if target.os != "windows":
        outputs = compile_unix_extensions(target, generated)
    else:
        # MSVC cross-compilation still uses setuptools because Visual Studio's
        # import-library and compiler environment are target-specific.
        target_python = resolve_target_python(target)
        tc = resolve_toolchain(target)
        build_root = BUILD / "native" / target.key
        if build_root.exists():
            shutil.rmtree(build_root)
        build_root.mkdir(parents=True)
        include = target_python.include_dir
        candidates = [target_python.root / "libs", target_python.root / "install" / "libs", target_python.root / "lib", target_python.root / "install" / "lib"]
        libdir = next((path for path in candidates if path.exists() and any(path.glob("python*.lib"))), None)
        if libdir is None:
            raise RuntimeError(f"Target Python import library not found under {target_python.root}")
        # A CPython tree carries two import libraries: the versioned one
        # (python313.lib, the ABI of the interpreter being targeted) and the
        # stable-ABI shim (python3.lib, a subset of the same exports). Link
        # against the versioned one, so the extension imports the DLL the
        # runtime actually ships and the closure resolves it as a system
        # dependency. Sort order used to decide this, and it picked the shim.
        python_lib = f"python{target_python.python_major_minor.replace('.', '')}"
        if not (libdir / f"{python_lib}.lib").is_file():
            raise RuntimeError(f"{python_lib}.lib not found in {libdir}")
        source_repr = ", ".join(repr(str(generated[p])) for p in sources)
        name_repr = ", ".join(repr(module_name(p)) for p in sources)
        setup = BUILD / "target-setup" / target.key / "setup.py"
        setup.parent.mkdir(parents=True, exist_ok=True)
        setup.write_text(
            "from setuptools import setup, Extension\n"
            "from setuptools.command.build_ext import build_ext as _build_ext\n"
            f"TARGET_INCLUDE={str(include)!r}\n"
            f"TARGET_LIBDIR={str(libdir)!r}\n"
            f"TARGET_LIBRARIES={[python_lib]!r}\n"
            f"SOURCES=[{source_repr}]\n"
            f"NAMES=[{name_repr}]\n"
            "class TargetBuildExt(_build_ext):\n"
            "    def finalize_options(self):\n"
            "        super().finalize_options()\n"
            "        self.include_dirs=[TARGET_INCLUDE]\n"
            "        self.library_dirs=[TARGET_LIBDIR]\n"

            "extensions=[Extension(n,[s],include_dirs=[TARGET_INCLUDE],library_dirs=[TARGET_LIBDIR],libraries=TARGET_LIBRARIES) for n,s in zip(NAMES,SOURCES)]\n"
            "setup(name='py-upper-target-native',ext_modules=extensions,cmdclass={'build_ext':TargetBuildExt})\n",
            encoding="utf-8",
        )
        env=dict(os.environ); env.update(tc.env); env["PY_UPPER_TARGET"]=target.key
        env["DISTUTILS_USE_SDK"]="1"; env["MSSdk"]="1"
        try:
            _run([str(host_python), str(setup), "build_ext", "--build-lib", str(build_root)], cwd=APP, env=env)
        finally:
            setup.unlink(missing_ok=True)
        outputs=[]
        suffix=target_python.extension_suffix
        for source in sources:
            expected=build_root.joinpath(*source.relative_to(APP / "src").with_suffix("").parts[:-1], source.stem+suffix)
            if not expected.exists():
                raise RuntimeError(f"Target extension missing: {expected}")
            outputs.append(expected)
    return outputs


def copy_python_tree(site: Path) -> None:
    copy_tree_contents(APP / "src", site, replace=False)
    for cache in list(site.rglob("__pycache__")):
        if cache.is_dir():
            shutil.rmtree(cache)
    for path in list(site.rglob("*.pyc")) + list(site.rglob("*.pyo")) + list(site.rglob("*.c")) + list(site.rglob("*.cpp")):
        path.unlink(missing_ok=True)


def remove_compiled_source_py(site: Path, sources: list[Path]) -> None:
    for source in sources:
        relative = source.relative_to(APP / "src").with_suffix(".py")
        (site / relative).unlink(missing_ok=True)


def copy_native_outputs(outputs: list[Path], site: Path, target: Target) -> None:
    build_root = BUILD / "native" / target.key
    for source in outputs:
        copy_file_contents(source, site / source.relative_to(build_root))


def build_python_package(target: Target) -> Path:
    stage = staging_dir(target)
    if stage.exists():
        shutil.rmtree(stage)
    site = stage / "site-packages"
    site.mkdir(parents=True)

    # Third-party dependencies are resolved/installed exactly once here. They are
    # not Cythonized; only application source modules are compiled below.
    from .third_party import install_wheels
    install_wheels(target, stage)
    copy_python_tree(site)
    sources = selected_sources()
    outputs = build_target_extensions(target, sources)
    copy_native_outputs(outputs, site, target)
    remove_compiled_source_py(site, sources)
    print(f"Python stage ready: {site}")
    return stage


def compile_unix_extensions(target: Target, generated: dict[Path, Path]) -> list[Path]:
    target_python = resolve_target_python(target)
    tc = resolve_toolchain(target)
    out_root = BUILD / "native" / target.key
    if out_root.exists():
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
            # CPython extensions on macOS are Mach-O bundles that leave the
            # Python C-API symbols unresolved and resolve them from the
            # embedding launcher, which loads the bundled libpython with
            # RTLD_GLOBAL. Linking with `-dynamiclib` would instead require
            # every symbol to be defined at link time and fail with
            # "symbol(s) not found for architecture <arch>".
            cmd = [
                tc.compiler, "-bundle", "-undefined", "dynamic_lookup",
                "-fPIC", "-O2", "-DNDEBUG",
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
