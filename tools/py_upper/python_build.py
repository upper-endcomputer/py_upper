"""Application build: Cython generation, target extension compilation and staging."""
from __future__ import annotations

import concurrent.futures
import fnmatch
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from .config import (
    APP, BUILD, Target, build_jobs, cython_config, incremental_build_enabled,
    pip_transfer_args, require_local_python, resolve_target_python, staging_dir,
)
from .fs import copy_file_contents, copy_tree_contents
from .toolchain import resolve_toolchain


CYTHON_REQUIREMENT = "Cython>=3.1,<3.3"

# Cache schemas. Each names the exact recipe that produced an artifact, so
# bumping one discards every output it describes. Without that, a cache written
# by an older tool would keep answering a question the tool no longer asks.
CYTHON_CACHE_SCHEMA = "py_upper-cython-1"
NATIVE_CACHE_SCHEMA = "py_upper-native-1"


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(str(x) for x in cmd))
    try:
        subprocess.run(cmd, cwd=cwd, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Command failed ({exc.returncode}): {' '.join(map(str, cmd))}") from exc


def _run_parallel(tasks: list[tuple[list[str], Path | None, dict[str, str] | None]]) -> None:
    """Run independent Cython/compiler invocations concurrently.

    Every module is translated and compiled by its own process and that process
    is CPU-bound, so a sequential build costs the sum of every file. The
    invocations share no state, so they are dispatched to a thread pool; the
    threads only wait on child processes, which is enough to overlap the work.
    """
    if not tasks:
        return
    workers = min(len(tasks), build_jobs())
    if workers <= 1:
        for cmd, cwd, env in tasks:
            _run(cmd, cwd=cwd, env=env)
        return
    failures: list[BaseException] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run, cmd, cwd=cwd, env=env) for cmd, cwd, env in tasks]
        # Drain every future before reporting: a failure must not leave orphan
        # compilers running behind a raised exception.
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                failures.append(exc)
    if failures:
        raise failures[0]


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cython_output_root() -> Path:
    """Where generated translation units live between builds."""
    return BUILD / "cache" / "cython"


def native_output_root(target: Target) -> Path:
    """Where compiled target extensions live between builds."""
    return BUILD / "cache" / "native" / target.key


def _stamp_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + ".stamp")


def _is_current(artifact: Path, key: str) -> bool:
    """True when ``artifact`` was produced by exactly the inputs in ``key``."""
    if not artifact.is_file():
        return False
    stamp = _stamp_path(artifact)
    if not stamp.is_file():
        return False
    try:
        return stamp.read_text(encoding="utf-8").strip() == key
    except OSError:
        return False


def _mark_current(artifact: Path, key: str) -> None:
    _stamp_path(artifact).write_text(key + "\n", encoding="utf-8")


def _cython_key(source: Path, cython_version: str) -> str:
    """Identity of one generated translation unit.

    Cython's output is a pure function of the source text and the Cython
    version that translated it, so those two are the whole key. The module name
    is part of the key because it is embedded in the generated unit.
    """
    digest = hashlib.sha256()
    digest.update(CYTHON_CACHE_SCHEMA.encode())
    digest.update(b"\0")
    digest.update(cython_version.encode())
    digest.update(b"\0")
    digest.update(source.name.encode())
    digest.update(b"\0")
    digest.update(source.read_bytes())
    return digest.hexdigest()


def _native_key(cmd: list[str], c_source: Path, artifact: Path) -> str:
    """Identity of one compiled extension.

    The command line already names the compiler, the target architecture, the
    ABI suffix, the SDK include directory and the optimization flags, so it is
    the complete description of the build. Only the C source is replaced by its
    content: the cache must answer for the bytes that are compiled, not for the
    path they happen to sit at.
    """
    digest = hashlib.sha256()
    digest.update(NATIVE_CACHE_SCHEMA.encode())
    digest.update(b"\0")
    digest.update(str(artifact).encode())
    digest.update(b"\0")
    digest.update(_sha256_file(c_source).encode())
    digest.update(b"\0")
    for token in cmd:
        text = str(token)
        if text == str(c_source):
            continue
        digest.update(text.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _windows_native_key(
    c_source: Path,
    artifact: Path,
    include: Path,
    libdir: Path,
    libraries: list[str],
    suffix: str,
    env: dict[str, str],
) -> str:
    """Identity of one MSVC-built extension.

    Windows compiles through setuptools, whose driver is not observable as a
    command line, so the key names the inputs that driver is given plus the
    identifiers of the MSVC toolset and Windows SDK that turn them into code.
    A new Visual Studio changes code generation, so its identifiers must
    invalidate the cache.
    """
    digest = hashlib.sha256()
    digest.update(NATIVE_CACHE_SCHEMA.encode())
    digest.update(b"\0windows\0")
    digest.update(str(artifact).encode())
    digest.update(b"\0")
    digest.update(_sha256_file(c_source).encode())
    digest.update(b"\0")
    digest.update(str(include).encode())
    digest.update(b"\0")
    digest.update(str(libdir).encode())
    digest.update(b"\0")
    digest.update(",".join(libraries).encode())
    digest.update(b"\0")
    digest.update(str(suffix).encode())
    digest.update(b"\0")
    for name in ("VCTOOLSINSTALLDIR", "WindowsSdkDir", "WindowsSDKVersion", "VSCMD_VER", "Platform"):
        digest.update(f"{name}={env.get(name, '')}".encode())
        digest.update(b"\0")
    return digest.hexdigest()


def cythonize_to_c(sources: list[Path], host_python: Path) -> dict[Path, Path]:
    """Generate the C translation unit for every selected source.

    A module whose source text and Cython version did not change reuses the
    unit a previous build already produced. Regenerating every module on every
    build was the dominant cost of a rebuild, and the artifact is a pure
    function of those two inputs, so it can be cached by content.
    """
    root = cython_output_root()
    root.mkdir(parents=True, exist_ok=True)
    env = _cython_env(host_python)
    version = _cython_version(host_python, env) or "unknown"
    incremental = incremental_build_enabled()

    outputs: dict[Path, Path] = {}
    pending: list[tuple[Path, Path, str]] = []
    for source in sources:
        rel = source.relative_to(APP / "src")
        destination = root / rel.with_suffix(".c")
        destination.parent.mkdir(parents=True, exist_ok=True)
        key = _cython_key(source, version)
        outputs[source] = destination
        if incremental and _is_current(destination, key):
            continue
        pending.append((source, destination, key))

    if not pending:
        print(f"Cython: {len(sources)} module(s) up to date")
        return outputs

    print(f"Cython: {len(pending)} of {len(sources)} module(s) changed")
    tasks: list[tuple[list[str], Path | None, dict[str, str] | None]] = []
    for source, destination, _ in pending:
        output_rel = os.path.relpath(destination, source.parent)
        tasks.append(([str(host_python), "-m", "cython", "--force", "-3", "-o", output_rel, source.name], source.parent, env))
    _run_parallel(tasks)
    for source, destination, key in pending:
        if not destination.exists():
            raise RuntimeError(f"Cython completed but did not generate {destination}")
        _mark_current(destination, key)
    return outputs


def windows_setup_script(
    include: Path,
    libdir: Path,
    libraries: list[str],
    generated: dict[Path, Path],
    sources: list[Path],
    suffix: str,
) -> str:
    """The setuptools driver that compiles the target extensions with MSVC.

    setuptools names an extension after the interpreter that runs the build,
    and on Windows that interpreter is the build host. A 3.13 host compiling
    for a 3.11 runtime would emit core/app.cp313-win_amd64.pyd: a name the
    target runtime cannot import, and one the layout check in
    build_target_extensions rejects. TargetBuildExt answers with the target's
    own suffix instead, the same value that names the Unix outputs.
    """
    source_repr = ", ".join(repr(str(generated[p])) for p in sources)
    name_repr = ", ".join(repr(module_name(p)) for p in sources)
    return (
        "import os\n"
        "from setuptools import setup, Extension\n"
        "from setuptools.command.build_ext import build_ext as _build_ext\n"
        f"TARGET_INCLUDE={str(include)!r}\n"
        f"TARGET_LIBDIR={str(libdir)!r}\n"
        f"TARGET_LIBRARIES={libraries!r}\n"
        f"TARGET_SUFFIX={suffix!r}\n"
        f"SOURCES=[{source_repr}]\n"
        f"NAMES=[{name_repr}]\n"
        "class TargetBuildExt(_build_ext):\n"
        "    def finalize_options(self):\n"
        "        super().finalize_options()\n"
        "        self.include_dirs=[TARGET_INCLUDE]\n"
        "        self.library_dirs=[TARGET_LIBDIR]\n"
        "    def get_ext_fullpath(self, ext_name):\n"
        "        fullname = self.get_ext_fullname(ext_name)\n"
        "        return os.path.join(self.build_lib, *fullname.split('.')) + TARGET_SUFFIX\n"
        "extensions=[Extension(n,[s],include_dirs=[TARGET_INCLUDE],library_dirs=[TARGET_LIBDIR],libraries=TARGET_LIBRARIES) for n,s in zip(NAMES,SOURCES)]\n"
        "setup(name='py-upper-target-native',ext_modules=extensions,cmdclass={'build_ext':TargetBuildExt})\n"
    )


def build_target_extensions(target: Target, sources: list[Path]) -> list[Path]:
    if not sources:
        return []
    host_python = require_local_python()
    generated = cythonize_to_c(sources, host_python)
    if target.os != "windows":
        return compile_unix_extensions(target, generated)
    return compile_windows_extensions(target, generated, sources, host_python)


def _windows_import_library(target_python) -> tuple[Path, Path, str]:
    """The target Python include directory, import library directory and name.

    A CPython tree carries two import libraries: the versioned one
    (python313.lib, the ABI of the interpreter being targeted) and the
    stable-ABI shim (python3.lib, a subset of the same exports). Link against
    the versioned one, so the extension imports the DLL the runtime actually
    ships and the closure resolves it as a system dependency. Sort order used
    to decide this, and it picked the shim.
    """
    candidates = [
        target_python.root / "libs",
        target_python.root / "install" / "libs",
        target_python.root / "lib",
        target_python.root / "install" / "lib",
    ]
    libdir = next((path for path in candidates if path.exists() and any(path.glob("python*.lib"))), None)
    if libdir is None:
        raise RuntimeError(f"Target Python import library not found under {target_python.root}")
    python_lib = f"python{target_python.python_major_minor.replace('.', '')}"
    if not (libdir / f"{python_lib}.lib").is_file():
        raise RuntimeError(f"{python_lib}.lib not found in {libdir}")
    return target_python.include_dir, libdir, python_lib


def compile_windows_extensions(
    target: Target,
    generated: dict[Path, Path],
    sources: list[Path],
    host_python: Path,
) -> list[Path]:
    """Compile the target extensions with MSVC, reusing unchanged outputs.

    MSVC cross-compilation uses setuptools because Visual Studio's import
    library and compiler environment are target-specific. Only the modules
    whose inputs changed are handed to the driver; the rest come from the
    cache, which is what keeps a rebuild proportional to the edit.
    """
    target_python = resolve_target_python(target)
    tc = resolve_toolchain(target)
    include, libdir, python_lib = _windows_import_library(target_python)
    suffix = target_python.extension_suffix

    root = native_output_root(target)
    root.mkdir(parents=True, exist_ok=True)
    build_root = BUILD / "native" / target.key
    env = dict(os.environ)
    env.update(tc.env)
    env["PY_UPPER_TARGET"] = target.key
    env["DISTUTILS_USE_SDK"] = "1"
    env["MSSdk"] = "1"
    incremental = incremental_build_enabled()

    outputs: dict[Path, Path] = {}
    stale: list[tuple[Path, Path, str]] = []
    for source in sources:
        rel = source.relative_to(APP / "src").with_suffix("")
        artifact = root.joinpath(*rel.parts[:-1], rel.name + suffix)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        key = _windows_native_key(generated[source], artifact, include, libdir, [python_lib], suffix, env)
        outputs[source] = artifact
        if incremental and _is_current(artifact, key):
            continue
        stale.append((source, artifact, key))

    if not stale:
        print(f"Compile: {len(sources)} extension(s) up to date")
        return list(outputs.values())

    print(f"Compile: {len(stale)} of {len(sources)} extension(s) changed")
    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True)
    stale_sources = [source for source, _, _ in stale]
    setup = BUILD / "target-setup" / target.key / "setup.py"
    setup.parent.mkdir(parents=True, exist_ok=True)
    setup.write_text(
        windows_setup_script(include, libdir, [python_lib], {source: generated[source] for source in stale_sources}, stale_sources, suffix),
        encoding="utf-8",
    )
    try:
        _run([str(host_python), str(setup), "build_ext", "--build-lib", str(build_root)], cwd=APP, env=env)
    finally:
        setup.unlink(missing_ok=True)
    for source, artifact, key in stale:
        expected = build_root.joinpath(*source.relative_to(APP / "src").with_suffix("").parts[:-1], source.stem + suffix)
        if not expected.exists():
            raise RuntimeError(f"Target extension missing: {expected}")
        copy_file_contents(expected, artifact)
        _mark_current(artifact, key)
    return list(outputs.values())


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
    root = native_output_root(target)
    for artifact in outputs:
        copy_file_contents(artifact, site / artifact.relative_to(root))


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


def _unix_compile_command(target: Target, target_python, tc, c_source: Path, output: Path) -> list[str]:
    if target.os == "linux":
        return [
            tc.compiler, "-shared", "-fPIC", "-O2", "-DNDEBUG",
            f"-I{target_python.include_dir}", str(c_source), "-lm", "-o", str(output),
        ]
    if target.os == "macos":
        # CPython extensions on macOS are Mach-O bundles that leave the
        # Python C-API symbols unresolved and resolve them from the
        # embedding launcher, which loads the bundled libpython with
        # RTLD_GLOBAL. Linking with `-dynamiclib` would instead require
        # every symbol to be defined at link time and fail with
        # "symbol(s) not found for architecture <arch>".
        return [
            tc.compiler, "-bundle", "-undefined", "dynamic_lookup",
            "-fPIC", "-O2", "-DNDEBUG",
            "-arch", target.arch,
            f"-mmacosx-version-min={tc.deployment_target or '11.0'}",
            f"-I{target_python.include_dir}", str(c_source), "-o", str(output),
        ]
    raise RuntimeError("compile_unix_extensions only supports Unix targets")


def compile_unix_extensions(target: Target, generated: dict[Path, Path]) -> list[Path]:
    """Compile the target extensions with the host compiler, reusing outputs.

    The command line is the complete description of the build, so a module
    whose generated C and flags are unchanged reuses the extension a previous
    build already produced instead of being recompiled.
    """
    target_python = resolve_target_python(target)
    tc = resolve_toolchain(target)
    root = native_output_root(target)
    root.mkdir(parents=True, exist_ok=True)
    incremental = incremental_build_enabled()

    outputs: dict[Path, Path] = {}
    pending: list[tuple[list[str], Path, str]] = []
    for source, c_source in generated.items():
        rel = source.relative_to(APP / "src").with_suffix("")
        artifact = root.joinpath(*rel.parts[:-1], rel.name + target_python.extension_suffix)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        cmd = _unix_compile_command(target, target_python, tc, c_source, artifact)
        key = _native_key(cmd, c_source, artifact)
        outputs[source] = artifact
        if incremental and _is_current(artifact, key):
            continue
        pending.append((cmd, artifact, key))

    if not pending:
        print(f"Compile: {len(outputs)} extension(s) up to date")
        return list(outputs.values())

    print(f"Compile: {len(pending)} of {len(outputs)} extension(s) changed")
    _run_parallel([(cmd, None, None) for cmd, _, _ in pending])
    for _, artifact, key in pending:
        # A stubbed runner produces no file; a real compiler always does. Only a
        # real output is worth remembering.
        if artifact.is_file():
            _mark_current(artifact, key)
    return list(outputs.values())
