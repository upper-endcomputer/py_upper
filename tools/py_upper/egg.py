"""Encrypted application egg: packing, key handling and the loader source.

The application's own Python modules and compiled extensions are packed into a
single ``app.egg`` instead of shipping as loose files in ``site-packages``:

    MAGIC (8 bytes) + XOR(zip bytes, SHAKE-256 key stream)

The launcher carries ``egg_loader.py`` and installs it before importing the
application entry. On Windows, extension modules are mapped straight from
memory with ``_memimporter`` (the py2exe MemoryModule helper vendored under
``vendor/memimporter``). Linux and macOS have no portable equivalent, so the
loader decrypts into a private temporary directory and puts that on
``sys.path``. Both paths run the same source modules from the same archive, so
the pack format and the import order do not depend on the platform.

This is obfuscation, not modern cryptography: it raises the cost of reading the
shipped application code, and it is not a secret storage format. It also barely
changes the bundle size -- the bulk is the runtime and third-party wheels; use
``[tool.py_upper.trim]`` and Qt pruning for size.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import APP, BUILD, Target, py_upper_config, resolve_target_python
from .egg_loader import BACKENDS, DEFAULT_KEY_HEX, MAGIC, keystream_xor

EGG_NAME = "app.egg"
# The resolved configuration of the build that produced a stage. It records the
# per-build overrides (--egg-key and friends) so --verify and the smoke step
# read the key an egg was actually written with, not the project default.
EGG_META_NAME = "egg.json"
COMPRESSIONS = ("deflated", "stored", "lzma")
VENDOR = Path(__file__).resolve().parent / "vendor" / "memimporter"
MEMIMPORTER_SOURCES = (
    "_memimporter.c",
    "MemoryModule.c",
    "MyLoadLibrary.c",
    "actctx.c",
)
# Application inputs that are part of the native dependency closure. They stay
# in site-packages so bundle_native_dependencies can see and rewrite them; only
# the extensions this build produced are packed into the egg.
NATIVE_SUFFIXES = (".pyd", ".so", ".dll", ".dylib", ".exe")

_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]+$")


@dataclass(frozen=True)
class EggConfig:
    """``[tool.py_upper.egg]`` plus per-build CLI overrides."""

    enabled: bool = False
    key_hex: str = DEFAULT_KEY_HEX
    backend: str = "auto"
    compression: str = "deflated"

    def __post_init__(self) -> None:
        text = str(self.key_hex or "").strip()
        if not _KEY_PATTERN.match(text):
            raise RuntimeError("[tool.py_upper.egg].key_hex must be a hexadecimal string")
        if len(bytes.fromhex(text)) < 16:
            raise RuntimeError("[tool.py_upper.egg].key_hex must contain at least 16 bytes")
        backend = str(self.backend or "auto").strip().lower()
        if backend not in BACKENDS:
            raise RuntimeError("[tool.py_upper.egg].backend must be one of: " + ", ".join(BACKENDS))
        compression = str(self.compression or "deflated").strip().lower()
        if compression not in COMPRESSIONS:
            raise RuntimeError("[tool.py_upper.egg].compression must be one of: " + ", ".join(COMPRESSIONS))
        object.__setattr__(self, "key_hex", text.lower())
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "compression", compression)

    @property
    def key(self) -> bytes:
        return bytes.fromhex(self.key_hex)


def egg_config(enabled=None, key_hex=None, backend=None, compression=None) -> EggConfig:
    """Read the project configuration, applying per-build overrides.

    ``enabled`` is tri-state: True/False come from ``--egg`` / ``--no-egg`` and
    None keeps whatever the project declares. A project without the section
    keeps the historical loose-file layout.
    """
    value = py_upper_config().get("egg", {})
    if not isinstance(value, dict):
        raise RuntimeError("[tool.py_upper.egg] must be a table")
    resolved = EggConfig(
        enabled=bool(value.get("enabled", False)),
        key_hex=str(value.get("key_hex") or DEFAULT_KEY_HEX),
        backend=str(value.get("backend") or "auto"),
        compression=str(value.get("compression") or "deflated"),
    )
    if enabled is not None:
        resolved = EggConfig(enabled=bool(enabled), key_hex=resolved.key_hex,
                             backend=resolved.backend, compression=resolved.compression)
    if key_hex is not None:
        resolved = EggConfig(enabled=resolved.enabled, key_hex=key_hex,
                             backend=resolved.backend, compression=resolved.compression)
    if backend is not None:
        resolved = EggConfig(enabled=resolved.enabled, key_hex=resolved.key_hex,
                             backend=backend, compression=resolved.compression)
    if compression is not None:
        resolved = EggConfig(enabled=resolved.enabled, key_hex=resolved.key_hex,
                             backend=resolved.backend, compression=compression)
    return resolved


def write_egg_meta(stage: Path, config: EggConfig) -> Path:
    """Record the resolved egg configuration next to the stage."""
    path = Path(stage) / EGG_META_NAME
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "key_hex": config.key_hex,
                "backend": config.backend,
                "compression": config.compression,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    return path


def read_egg_meta(stage: Path):
    """The egg configuration a previous stage was built with, if any.

    A build may override the key or backend on the command line, so the files
    on disk are the authority for verification. A stage without the record was
    built without an egg and returns None.
    """
    path = Path(stage) / EGG_META_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("enabled"):
        return None
    try:
        return EggConfig(
            enabled=True,
            key_hex=str(data.get("key_hex") or DEFAULT_KEY_HEX),
            backend=str(data.get("backend") or "auto"),
            compression=str(data.get("compression") or "deflated"),
        )
    except RuntimeError:
        return None


def _compression(name: str):
    return {
        "deflated": (zipfile.ZIP_DEFLATED, 9),
        "stored": (zipfile.ZIP_STORED, None),
        "lzma": (zipfile.ZIP_LZMA, None),
    }[name]


def pack_egg(site: Path, destination: Path, files, config: EggConfig) -> Path:
    """Write the encrypted zip containing exactly ``files``.

    Entries are sorted and carry a fixed timestamp, so two builds over the same
    inputs produce byte-identical eggs and a release manifest stays stable.
    """
    names = sorted({str(name).replace("\\", "/") for name in files})
    method, level = _compression(config.compression)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method, compresslevel=level) as archive:
        for name in names:
            source = site / Path(name)
            if not source.is_file():
                raise RuntimeError("egg input is missing: " + str(source))
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = method
            info.external_attr = 0o644 << 16
            with source.open("rb") as handle:
                archive.writestr(info, handle.read(), compress_type=method, compresslevel=level)
    payload = MAGIC + keystream_xor(buffer.getvalue(), config.key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


def read_egg_members(egg_path: Path, key_hex: str) -> tuple:
    """Decrypt an egg in the build process and return its member names."""
    key = bytes.fromhex(str(key_hex).strip())
    raw = Path(egg_path).read_bytes()
    payload = raw[len(MAGIC):] if raw[:len(MAGIC)] == MAGIC else raw
    if raw[:len(MAGIC)] == MAGIC:
        payload = keystream_xor(payload, key)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return tuple(sorted(archive.namelist()))


def application_files(site: Path, compiled) -> list:
    """The files that belong in the egg, relative to ``site``.

    ``compiled`` is the set of extension paths this build produced. Everything
    else under ``app/src`` ships as-is, except native inputs (``.dll``/``.so``/
    ``.pyd`` outside that set): those are inputs to the dependency closure and
    must stay visible in ``site-packages``.
    """
    compiled_paths = {str(name).replace("\\", "/") for name in compiled}
    result = set(compiled_paths)
    root = APP / "src"
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in compiled_paths:
            continue
        if path.suffix.lower() in NATIVE_SUFFIXES:
            continue
        if (site / relative).is_file():
            result.add(relative)
    missing = [name for name in result if not (site / name).is_file()]
    if missing:
        raise RuntimeError("egg input missing from site-packages: " + ", ".join(sorted(missing)))
    return sorted(result)


def remove_packed_files(site: Path, files) -> None:
    """Remove packed files from the stage, then prune directories they emptied."""
    parents = set()
    for name in files:
        target = site / Path(str(name))
        target.unlink(missing_ok=True)
        parent = target.parent
        while parent != site and site in parent.parents:
            parents.add(parent)
            parent = parent.parent
    for directory in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def egg_loader_source() -> str:
    """The loader text the launcher embeds.

    It has to survive a C++ raw string literal and a Python triple-quoted raw
    literal, so the generator refuses anything it could not carry instead of
    silently producing a broken launcher.
    """
    text = (Path(__file__).resolve().parent / "egg_loader.py").read_text(encoding="utf-8")
    try:
        text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError("egg_loader.py must be pure ASCII: " + str(exc))
    if '"""' in text:
        raise RuntimeError('egg_loader.py contains a triple quote and cannot be embedded')
    if ')pyupper"' in text:
        raise RuntimeError('egg_loader.py contains the C++ raw string delimiter )pyupper"')
    compile(text, "egg_loader.py", "exec")
    return text


def _windows_import_library(target_python):
    """The SDK include/lib directories and import library for the target ABI."""
    candidates = [
        target_python.sdk_root / "libs",
        target_python.sdk_root / "install" / "libs",
        target_python.sdk_root / "lib",
        target_python.sdk_root / "install" / "lib",
    ]
    libdir = next((path for path in candidates if path.exists() and any(path.glob("python*.lib"))), None)
    if libdir is None:
        raise RuntimeError(
            "Import library of target Python " + target_python.python_major_minor
            + " not found under the SDK tree " + str(target_python.sdk_root)
        )
    python_lib = "python" + target_python.python_major_minor.replace(".", "")
    if not (libdir / (python_lib + ".lib")).is_file():
        raise RuntimeError(python_lib + ".lib not found in " + str(libdir))
    return target_python.include_dir, libdir, python_lib


def _memimporter_cache(target: Target) -> Path:
    return BUILD / "cache" / "memimporter" / target.key


def build_memimporter(target: Target, site: Path) -> Path:
    """Compile the vendored ``_memimporter`` for the target runtime.

    Only Windows has an in-memory loader. The helper links against the target
    Python import library exactly like an application extension does, so the
    resulting ``_memimporter.pyd`` is ABI-specific but not named after the
    Python version (``.pyd`` is on ``EXTENSION_SUFFIXES`` for every version).
    """
    if target.os != "windows":
        raise RuntimeError("_memimporter is only available for Windows targets")
    from .python_build import _is_current, _mark_current, _sha256_file
    from .toolchain import resolve_toolchain

    target_python = resolve_target_python(target)
    tc = resolve_toolchain(target)
    if tc.compiler_args is None:
        raise RuntimeError(
            "_memimporter must be compiled with the Zig toolchain; MSVC is not wired up. "
            "Use backend = 'extract' or install Zig."
        )
    include, libdir, python_lib = _windows_import_library(target_python)
    cache = _memimporter_cache(target)
    cache.mkdir(parents=True, exist_ok=True)
    artifact = cache / "_memimporter.pyd"
    sources = [VENDOR / name for name in MEMIMPORTER_SOURCES]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise RuntimeError("vendored _memimporter sources are missing: " + ", ".join(missing))
    compiler_args = list(tc.compiler_args or ())
    cmd = [
        tc.compiler, *compiler_args,
        "-shared", "-O2", "-DNDEBUG", "-DSTANDALONE", "-DPy_BUILD_CORE_MODULE",
        "-I" + str(include),
        *[str(path) for path in sources],
        "-L" + str(libdir),
        "-l" + python_lib,
        "-luser32", "-lshell32",
        "-o", str(artifact),
    ]
    digest = hashlib.sha256()
    for token in cmd:
        digest.update(str(token).encode())
        digest.update(b"\0")
    for path in sources:
        digest.update(_sha256_file(path).encode())
        digest.update(b"\0")
    key = digest.hexdigest()
    if not _is_current(artifact, key):
        print("+", " ".join(str(token) for token in cmd))
        import subprocess
        subprocess.run(cmd, check=True, env=tc.env)
        if not artifact.is_file():
            raise RuntimeError("Zig completed but produced no _memimporter.pyd")
        _mark_current(artifact, key)
    destination = site / "_memimporter.pyd"
    shutil.copyfile(artifact, destination)
    return destination