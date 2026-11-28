# 中文维护说明：生成认证归档及加载器配置；打包内嵌密钥仅增加读取成本，不能承担授权安全。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Encrypted application egg: packing, key handling and the loader source.

The application's own Python modules and compiled extensions are packed into a
single ``bundle/appbuild.dat`` instead of shipping as loose files in
``site-packages``:

    MAGIC2 (8) + SALT (16) + TAG (32) + XOR(zip bytes, SHAKE-256(key + SALT))

``TAG`` is ``SHAKE-256(key + MAGIC2 + SALT + ciphertext)[:32]``, so the archive
is authenticated: a rebuilt or edited egg no longer loads. Authenticating the
ciphertext costs one extra hash pass at ~1.4 ms/MB next to the XOR pass the
format already needs at ~6.7 ms/MB, which keeps startup inside measurement
noise. The zip also carries ``appbuild_manifest.json`` with a SHA-256 per
member, which is what makes a reused plaintext extract cache verifiable.

The launcher carries ``egg_loader.py`` and installs it before importing the
application entry. On Windows, extension modules are mapped straight from
memory with ``_memimporter`` (the py2exe MemoryModule helper vendored under
``vendor/memimporter``). Linux and macOS have no portable equivalent, so the
loader decrypts into a private content-addressed cache and puts that on
``sys.path``. Both paths run the same source modules from the same archive, so
the pack format and the import order do not depend on the platform.

This is obfuscation, not modern cryptography: it raises the cost of reading the
shipped application code, and it is not a secret storage format. The key is
assembled at run time from a masked pair in the launcher so it does not sit in
the binary as a readable hexadecimal string, which stops a casual ``strings``
grep and nothing more. It also barely changes the bundle size -- the bulk is
the runtime and third-party wheels; use ``[tool.appbuild.trim]`` and Qt pruning
for size.
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

from .config import (
    APP, BUILD, BUNDLE_DIR, Target, appbuild_config, resolve_target_python,
)
from .egg_loader import (
    BACKENDS, MAGIC2, MANIFEST_NAME, SALT_SIZE, TAG_SIZE, egg_tag,
    keystream_xor,
)

# The default SHAKE-256 key stream key. It lives here, in the build tool, and
# never in egg_loader.py: the loader is embedded verbatim into the launcher, so a
# constant there would put the key into the shipped binary as a readable string.
# The generator masks it before writing it out (see masked_key_arrays).
DEFAULT_KEY_HEX = "047636cf3ddbaae2346f597256416e858ee0b7c8b7448221e5babfbb4f74c20d"
from .native.inspect import inspect as inspect_native

EGG_DIR = "bundle"
EGG_NAME = "appbuild.dat"
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
    """``[tool.appbuild.egg]`` plus per-build CLI overrides."""

    enabled: bool = True
    key_hex: str = DEFAULT_KEY_HEX
    backend: str = "auto"
    compression: str = "deflated"

    def __post_init__(self) -> None:
        """规范化密钥、后端与压缩方式；无效取值阻止归档生成。"""
        text = str(self.key_hex or "").strip()
        if not _KEY_PATTERN.match(text):
            raise RuntimeError("[tool.appbuild.egg].key_hex must be a hexadecimal string")
        if len(bytes.fromhex(text)) < 16:
            raise RuntimeError("[tool.appbuild.egg].key_hex must contain at least 16 bytes")
        backend = str(self.backend or "auto").strip().lower()
        if backend not in BACKENDS:
            raise RuntimeError("[tool.appbuild.egg].backend must be one of: " + ", ".join(BACKENDS))
        compression = str(self.compression or "deflated").strip().lower()
        if compression not in COMPRESSIONS:
            raise RuntimeError("[tool.appbuild.egg].compression must be one of: " + ", ".join(COMPRESSIONS))
        object.__setattr__(self, "key_hex", text.lower())
        object.__setattr__(self, "backend", backend)
        object.__setattr__(self, "compression", compression)

    @property
    def key(self) -> bytes:
        """把已验证十六进制密钥转换为字节。"""
        return bytes.fromhex(self.key_hex)


def egg_config(enabled=None, key_hex=None, backend=None, compression=None) -> EggConfig:
    """读取项目配置并按非 None 的 CLI 参数覆盖；缺省启用 egg。

    Read the project configuration, applying per-build overrides.

    ``enabled`` is tri-state: True/False come from ``--egg`` / ``--no-egg`` and
    None keeps whatever the project declares. Encryption is the default: a
    project without the section still packs the egg, and ``--no-egg`` opts out
    for one build.
    """
    value = appbuild_config().get("egg", {})
    if not isinstance(value, dict):
        raise RuntimeError("[tool.appbuild.egg] must be a table")
    resolved = EggConfig(
        enabled=bool(value.get("enabled", True)),
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
    """在 staging 旁记录实际构建密钥、后端及压缩方式，供后续校验读取。

    Record the resolved egg configuration next to the stage."""
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
    """读取该 staging 的实际 egg 配置；缺失或不可识别时返回 None。

    The egg configuration a previous stage was built with, if any.

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
    """把压缩名称映射为 ZIP 方法和压缩级别。"""
    return {
        "deflated": (zipfile.ZIP_DEFLATED, 9),
        "stored": (zipfile.ZIP_STORED, None),
        "lzma": (zipfile.ZIP_LZMA, None),
    }[name]


def _manifest_bytes(files: dict) -> bytes:
    """稳定编码每个归档成员的 SHA-256，确保相同输入生成相同清单。

    The per-member digest record stored in the archive.

    Sorted and indented so two builds over the same inputs produce identical
    bytes; the archive as a whole stays reproducible.
    """
    return (json.dumps({"format": 2, "files": files}, indent=2, sort_keys=True) + "\n").encode("utf-8")


def pack_egg(site: Path, destination: Path, files, config: EggConfig) -> Path:
    """按固定顺序和时间戳生成认证归档；launcher 预加载的合并扩展必须不压缩。

    Write the authenticated encrypted zip containing exactly ``files``.

    Entries are sorted and carry a fixed timestamp, so two builds over the same
    inputs produce byte-identical eggs and a release manifest stays stable.

    Merged extension bundles are stored uncompressed: the launcher reads them
    from the decrypted archive before ``Py_Initialize`` without linking zlib or
    lzma, so their entry method is part of the format contract.

    Each member's SHA-256 is computed from the bytes that were just read for the
    archive, so the manifest costs no second pass over the inputs.
    """
    names = sorted({str(name).replace("\\", "/") for name in files})
    method, level = _compression(config.compression)
    digests = {}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method, compresslevel=level) as archive:
        for name in names:
            source = site / Path(name)
            if not source.is_file():
                raise RuntimeError("egg input is missing: " + str(source))
            data = source.read_bytes()
            digests[name] = hashlib.sha256(data).hexdigest()
            stored = name.startswith(BUNDLE_DIR + "/")
            entry_method = zipfile.ZIP_STORED if stored else method
            entry_level = None if stored else level
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = entry_method
            info.external_attr = 0o644 << 16
            archive.writestr(
                info, data,
                compress_type=entry_method, compresslevel=entry_level,
            )
        manifest = zipfile.ZipInfo(MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0))
        manifest.compress_type = zipfile.ZIP_DEFLATED
        manifest.external_attr = 0o644 << 16
        archive.writestr(manifest, _manifest_bytes(digests), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    zip_bytes = buffer.getvalue()
    # The salt makes the key stream differ per build, so two eggs of the same
    # tree are not byte-identical ciphertexts and a known-plaintext XOR of them
    # yields nothing. It is derived from the payload instead of os.urandom so a
    # rebuild of the same inputs still reproduces the same archive.
    salt = hashlib.shake_256(zip_bytes + config.key).digest(SALT_SIZE)
    header = MAGIC2 + salt
    ciphertext = keystream_xor(zip_bytes, config.key + salt)
    tag = egg_tag(header, ciphertext, config.key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(header + tag + ciphertext)
    return destination


def open_egg_archive(egg_path: Path, key_hex: str) -> zipfile.ZipFile:
    """验证并解密一次 egg，返回 ZIP 句柄；调用者负责关闭。

    Open an egg once for all build-side ZIP reads of this file."""
    from .egg_loader import open_egg

    key = bytes.fromhex(str(key_hex).strip())
    payload, _digest = open_egg(Path(egg_path).read_bytes(), key)
    return zipfile.ZipFile(io.BytesIO(payload))


def read_egg_members(egg_path: Path, key_hex: str) -> tuple:
    """返回已认证归档的有序成员名称，不复用跨调用校验结果。

    Decrypt an egg in the build process and return its member names."""
    with open_egg_archive(egg_path, key_hex) as archive:
        return tuple(sorted(archive.namelist()))


def read_egg_manifest(egg_path: Path, key_hex: str) -> dict:
    """读取已认证归档中的成员摘要表；缺少清单时拒绝。

    The member digests recorded in an authenticated egg."""
    with open_egg_archive(egg_path, key_hex) as archive:
        if MANIFEST_NAME not in archive.namelist():
            raise ValueError("egg manifest is missing")
        data = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
    files = data.get("files") if isinstance(data, dict) else None
    return dict(files) if isinstance(files, dict) else {}


def egg_authenticated(egg_path: Path, key_hex: str) -> bool:
    """尝试验证并打开 ZIP；预期格式和读取错误返回 False。

    Whether the file opens as an authenticated ZIP egg."""
    try:
        read_egg_members(egg_path, key_hex)
    except (OSError, ValueError, zipfile.BadZipFile):
        return False
    return True


def _native_input(path: Path) -> bool:
    """按已知后缀或二进制头识别应用原生输入，避免将闭包输入藏入 egg。"""
    if path.suffix.lower() in NATIVE_SUFFIXES:
        return True
    try:
        inspect_native(path)
    except ValueError:
        return False
    return True


def application_files(site: Path, compiled) -> list:
    """选择应用源码及本次编译产物；第三方原生输入保留在外供依赖闭包处理。

    The files that belong in the egg, relative to ``site``.

    ``compiled`` is the set of extension paths this build produced. Everything
    else under ``app/src`` ships as-is, except native inputs outside that set:
    those are inputs to the dependency closure and must stay visible in
    ``site-packages``. File headers also identify versioned ELF libraries and
    native helpers whose names have no conventional extension.
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
        if _native_input(path):
            continue
        if (site / relative).is_file():
            result.add(relative)
    missing = [name for name in result if not (site / name).is_file()]
    if missing:
        raise RuntimeError("egg input missing from site-packages: " + ", ".join(sorted(missing)))
    return sorted(result)


def remove_packed_files(site: Path, files) -> None:
    """删除已封入 egg 的 staging 文件，再由深到浅清理空目录。

    Remove packed files from the stage, then prune directories they emptied."""
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


def _ascii_loader_comments(text: str) -> str:
    """将注释转为 ASCII 转义文本，保留行数、缩进和全部运行 token。

    中文仍保存在源码；只在嵌入阶段转换 COMMENT token，字符串和标识符
    不作替换，继续交给后续 ASCII 与分隔符检查拒绝不受支持的输入。
    """
    import io
    import tokenize

    lines = text.splitlines(keepends=True)
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and not token.string.isascii():
            row, start = token.start
            end = token.end[1]
            line = lines[row - 1]
            lines[row - 1] = (line[:start] + token.string.encode("ascii", "backslashreplace").decode("ascii")
                              + line[end:])
    return "".join(lines)


def egg_loader_source() -> str:
    """读取 loader 并仅转义注释；校验 ASCII、嵌入分隔符及 Python 语法。

    The loader text the launcher embeds.

    It has to survive a C++ raw string literal and a Python triple-quoted raw
    literal, so the generator refuses anything it could not carry instead of
    silently producing a broken launcher.
    """
    text = (Path(__file__).resolve().parent / "egg_loader.py").read_text(encoding="utf-8")
    text = _ascii_loader_comments(text)
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


def _memimporter_cache(target: Target) -> Path:
    """返回按目标架构隔离的内存加载器编译缓存路径。"""
    return BUILD / "cache" / "memimporter" / target.key


def build_memimporter(target: Target, site: Path) -> Path:
    """以目标 SDK 编译并缓存 Windows 内存加载器，再复制到 staging；仅支持已接入的 Zig 路径。

    Compile the vendored ``_memimporter`` for the target runtime.

    Only Windows has an in-memory loader. The helper links against the target
    Python import library exactly like an application extension does, so the
    resulting ``_memimporter.pyd`` is ABI-specific but not named after the
    Python version (``.pyd`` is on ``EXTENSION_SUFFIXES`` for every version).
    """
    if target.os != "windows":
        raise RuntimeError("_memimporter is only available for Windows targets")
    from .python_build import _is_current, _mark_current
    from .fs import sha256
    from .toolchain import resolve_toolchain, windows_import_library

    target_python = resolve_target_python(target)
    tc = resolve_toolchain(target)
    if tc.compiler_args is None:
        raise RuntimeError(
            "_memimporter must be compiled with the Zig toolchain; MSVC is not wired up. "
            "Use backend = 'extract' or install Zig."
        )
    include, libdir, python_lib = windows_import_library(target_python)
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
        digest.update(sha256(path).encode())
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
