# 中文维护说明：验证并加载应用归档；缓存需校验内容并持有租约，防止运行实例被淘汰。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
# py_upper encrypted egg loader.
#
# The loader is deliberately plain ASCII and uses only single quoted strings:
# tools/py_upper/package.py embeds this file into the launcher as a C++ raw
# string literal, and the bootstrap wraps that text in a Python triple quoted
# literal. A triple quote or a non-ASCII character would break that handoff.
#
# Egg layout, current format (v2):
#   MAGIC2 (8 bytes) + SALT (16 bytes) + TAG (32 bytes) + ciphertext
#   ciphertext = XOR(zip bytes, SHAKE-256(key + SALT) key stream)
#   TAG        = SHAKE-256(key + MAGIC2 + SALT + ciphertext)[:32]
#
# A packaged startup authenticates and decrypts once in the launcher, then
# hands the opened archive and its tag to this loader in memory.
#
# Untagged and legacy archives are not accepted.
#
# Backends:
#   memory  - extension modules are mapped from memory through _memimporter.
#             This is the Windows path and needs the py2exe MemoryModule
#             helper that tools/py_upper builds into the bundle.
#   extract - the egg is decrypted into a content-addressed cache directory
#             and added to sys.path. The directory name is derived from the
#             authentication tag, so a rebuilt egg gets a fresh root and an
#             existing one is reused without extracting again. This is the
#             portable fallback for Linux and macOS, where no stable in-memory
#             dlopen API exists. PY_UPPER_EGG_CACHE overrides the cache root.
#   auto    - memory on Windows when _memimporter imports, extract otherwise.
#
# The extracted cache is plaintext on disk, so every reuse is checked against
# the per-member digests recorded in the archive manifest. The check reads the
# cached files once (a typical application is ~1 MB, so ~1 ms); set
# PY_UPPER_EGG_CACHE_VERIFY=0 to skip it, at the cost of trusting whatever is
# in that directory.
#
# The key stream is obfuscation, not modern cryptography: it raises the cost of
# reading the shipped application code, and it is not a secret storage format.

import _imp
import atexit
import errno
import hashlib
import hmac
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import threading
import zipfile

MAGIC2 = b'WBEGG02!'
SALT_SIZE = 16
TAG_SIZE = 32
MANIFEST_NAME = 'py_upper_manifest.json'
BACKENDS = ('auto', 'memory', 'extract')
CACHE_PREFIX = 'egg-'
# Merged bundles are consumed by the launcher before this loader runs.
BUNDLE_PREFIX = 'py_upper_bundles/'
CACHE_MARKER = '.complete'
CACHE_ATTEMPTS = 3
# Cache entries for other builds are removed once this one is published. A new
# release changes the tag, so without this every version ever installed left its
# own ~1 MB directory behind forever. The previous entry is kept so a rollback
# to the previous build still starts warm.
CACHE_KEEP = 2
# Chunk size for hashing cached files: large enough to stay in the fast path of
# hashlib, small enough that a bundle-sized file is never held twice in memory.
HASH_CHUNK = 1 << 20


def keystream_xor(data, key):
    size = len(data)
    if size == 0:
        return data
    stream = hashlib.shake_256(key).digest(size)
    return (int.from_bytes(data, 'little')
            ^ int.from_bytes(stream, 'little')).to_bytes(size, 'little')


def egg_tag(header, ciphertext, key):
    shake = hashlib.shake_256()
    shake.update(key)
    shake.update(header)
    shake.update(ciphertext)
    return shake.digest(TAG_SIZE)


def split_v2(raw):
    header_size = len(MAGIC2) + SALT_SIZE
    if len(raw) < header_size + TAG_SIZE:
        raise ValueError('egg is truncated')
    header = raw[:header_size]
    tag = raw[header_size:header_size + TAG_SIZE]
    ciphertext = raw[header_size + TAG_SIZE:]
    return header, tag, ciphertext


def open_egg(raw, key):
    if not raw:
        raise ValueError('egg is empty')
    if raw[:len(MAGIC2)] != MAGIC2:
        raise ValueError('egg is not authenticated: rebuild it with this version of py_upper')
    header, tag, ciphertext = split_v2(raw)
    expected = egg_tag(header, ciphertext, key)
    if not hmac.compare_digest(tag, expected):
        raise ValueError('egg authentication failed: archive was rebuilt or edited')
    return keystream_xor(ciphertext, key + header[len(MAGIC2):]), tag.hex()


def _key_bytes(key_hex):
    # No default key lives in this file: the launcher embeds this source
    # verbatim, so a constant here would put the key back into the shipped
    # binary as a readable string. The caller always supplies it.
    text = str(key_hex or '').strip()
    try:
        key = bytes.fromhex(text)
    except ValueError:
        raise ValueError('egg key must be a hexadecimal string')
    if len(key) < 16:
        raise ValueError('egg key must contain at least 16 bytes')
    return key


def _safe_member(name):
    text = name.replace('\\', '/')
    parts = text.split('/')
    if not text or text.startswith('/') or any(part in ('', '.', '..') for part in parts):
        raise ValueError('unsafe egg member: ' + name)
    return '/'.join(parts)


def _cache_root():
    override = os.environ.get('PY_UPPER_EGG_CACHE')
    if override:
        root = os.path.abspath(os.path.expanduser(override))
    elif os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.join(
            os.path.expanduser('~'), 'AppData', 'Local')
        root = os.path.join(base, 'py_upper', 'cache', 'eggs')
    elif sys.platform == 'darwin':
        root = os.path.join(os.path.expanduser('~'), 'Library', 'Caches',
                            'py_upper', 'eggs')
    else:
        base = os.environ.get('XDG_CACHE_HOME') or os.path.join(
            os.path.expanduser('~'), '.cache')
        root = os.path.join(base, 'py_upper', 'eggs')
    namespace = os.environ.get('PY_UPPER_CACHE_NAMESPACE', '')
    if namespace and len(namespace) == 32 and all(c in '0123456789abcdef' for c in namespace):
        root = os.path.join(root, namespace)
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
    except OSError:
        return None
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


def _write_marker(path, digest):
    with open(path, 'w', encoding='ascii') as handle:
        handle.write(digest + '\n')


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _cache_verify_enabled():
    return os.environ.get('PY_UPPER_EGG_CACHE_VERIFY', '1') != '0'


# Drops cache entries from earlier builds.
#
# Entries are named after the authentication tag, so a rebuilt application never
# reuses one: they would accumulate one per release. Deletion is best effort and
# never fails a start; another process may be using an entry, and a leftover
# directory only costs disk space.
def _cache_lease_path(root, name):
    directory = root + '.leases'
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return os.path.join(directory, name)


# 租约必须覆盖实际使用期；清理方只有拿到独占锁才能删除对应版本。
def _cache_lease(path, exclusive=False):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        handle = create(path, 0xc0000000, 0 if exclusive else 3, None, 4, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            return None
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        return lambda: close(handle)
    import fcntl
    handle = open(path, 'a+b')
    try:
        fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle.close


# 淘汰旧版本前检查活跃租约；锁文件本身保留，防止不同进程锁住不同 inode。
def _prune_cache(cache_root, keep_name):
    # Only entries this loader owns. `merged-*` belongs to the launcher, which
    # extracts the extension bundles before Py_Initialize and is using that
    # directory right now: pruning it here deleted a live cache and forced the
    # launcher to rebuild it on the next start. Each side prunes its own prefix.
    try:
        entries = [name for name in os.listdir(cache_root)
                   if name.startswith(CACHE_PREFIX) and name != keep_name]
    except OSError:
        return
    if len(entries) <= CACHE_KEEP - 1:
        return
    def age(name):
        try:
            return os.path.getmtime(os.path.join(cache_root, name))
        except OSError:
            return 0.0
    entries.sort(key=age, reverse=True)
    for name in entries[CACHE_KEEP - 1:]:
        try:
            close = _cache_lease(_cache_lease_path(cache_root, name), True)
        except OSError:
            continue
        if close is not None:
            try:
                shutil.rmtree(os.path.join(cache_root, name), ignore_errors=True)
            finally:
                close()


def _memimporter_module():
    try:
        import _memimporter
    except (ImportError, OSError):
        return None
    return _memimporter


# Resolve a dotted built-in module that lives under a real package.
#
# A merged bundle registers leaf modules such as mvvm.views.base_view through
# PyImport_ExtendInittab. Importing one hands the parent package's __path__ to
# BuiltinImporter, and every CPython before 3.13 refuses a built-in name as
# soon as path is not None, even when the name is in sys.builtin_module_names.
# Python 3.13 dropped that check. Restore the newer behaviour here instead of
# requiring 3.13: the egg keeps package markers as .py files, so the leaves a
# merged bundle registers are always reached through a parent package.
#
# This finder runs after the egg importer, so a loose module shipped in the egg
# still wins, and before BuiltinImporter, which cannot see the name.
# The import machinery calls find_spec/create_module/exec_module directly.
# Inheriting the ABCs would also import the unused importlib.resources stack.
class BuiltinSubmoduleFinder:

    def find_spec(self, fullname, path=None, target=None):
        if path is None or '.' not in fullname:
            return None
        if not _imp.is_builtin(fullname):
            return None
        return importlib.machinery.BuiltinImporter.find_spec(fullname)


def _install_builtin_submodule_finder():
    for finder in sys.meta_path:
        if isinstance(finder, BuiltinSubmoduleFinder):
            return
    index = len(sys.meta_path)
    for position, finder in enumerate(sys.meta_path):
        if finder is importlib.machinery.BuiltinImporter:
            index = position
            break
    sys.meta_path.insert(index, BuiltinSubmoduleFinder())


class EncryptedEggImporter:
    def __init__(self, egg_path, key, backend='auto', *, _opened_egg=None):
        backend = str(backend or 'auto').strip().lower()
        if backend not in BACKENDS:
            raise ValueError('egg backend must be one of: ' + ', '.join(BACKENDS))
        self.egg_path = os.path.abspath(egg_path)
        self.requested_backend = backend
        self.lock = threading.Lock()
        if _opened_egg is None:
            with open(self.egg_path, 'rb') as handle:
                raw = handle.read()
        try:
            if _opened_egg is None:
                _opened_egg = open_egg(raw, key)
            payload, self.digest = _opened_egg
            self.archive = zipfile.ZipFile(io.BytesIO(payload))
            self.names = frozenset(self.archive.namelist())
        except Exception as exc:
            raise ValueError('bad egg, not a zip or wrong key: ' + self.egg_path + ': ' + str(exc))
        self.manifest = self._read_manifest()
        self.extensions = tuple(importlib.machinery.EXTENSION_SUFFIXES)
        self.memimporter = None
        self.extract_root = None
        self.backend = backend
        if backend == 'memory':
            self.memimporter = _memimporter_module()
            if self.memimporter is None:
                raise RuntimeError('egg backend memory requires _memimporter in the bundle')
        elif backend == 'auto':
            self.memimporter = _memimporter_module() if os.name == 'nt' else None
        if self.memimporter is None:
            self.backend = 'extract'
            self._extract()
        else:
            self.backend = 'memory'

    # Per-member digests recorded at pack time.
    #
    # A manifest that disagrees with the archive is a build bug, not a
    # tampering signal (the tag already covers the bytes), so it is refused
    # here instead of being used to validate a cache.
    def _read_manifest(self):
        if MANIFEST_NAME not in self.names:
            raise ValueError('egg manifest is missing')
        try:
            data = json.loads(self.archive.read(MANIFEST_NAME).decode('utf-8'))
        except Exception as exc:
            raise ValueError('egg manifest is unreadable: ' + str(exc))
        files = data.get('files') if isinstance(data, dict) else None
        if not isinstance(files, dict) or not files:
            raise ValueError('egg manifest is empty')
        for name, digest in files.items():
            if name not in self.names:
                raise ValueError('egg manifest names a missing member: ' + str(name))
            if not isinstance(digest, str) or len(digest) != 64:
                raise ValueError('egg manifest digest is malformed: ' + str(name))
        missing = [name for name in self.names
                   if name != MANIFEST_NAME and name not in files]
        if missing:
            raise ValueError('egg member has no manifest digest: ' + sorted(missing)[0])
        return files

    def _extract_into(self, root):
        for name in self.names:
            if name.endswith('/') or name == MANIFEST_NAME or name.startswith(BUNDLE_PREFIX):
                continue
            safe = _safe_member(name)
            target = os.path.join(root, *safe.split('/'))
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with self.lock:
                data = self.archive.read(name)
            with open(target, 'wb') as handle:
                handle.write(data)

    # Whether a reused cache directory still holds the archived bytes.
    #
    # The cache is plaintext, so a file can be edited in place without the
    # archive changing. Checking the recorded digests is what makes a warm
    # start trustworthy; the cost is one read of the extracted sources.
    def _cache_valid(self, root):
        marker = os.path.join(root, CACHE_MARKER)
        try:
            with open(marker, 'r', encoding='ascii') as handle:
                recorded = handle.read().strip()
        except OSError:
            return False
        if recorded != self.digest:
            return False
        if not _cache_verify_enabled():
            return True
        for name, expected in self.manifest.items():
            if name.startswith(BUNDLE_PREFIX):
                continue
            try:
                actual = _hash_file(os.path.join(root, *name.split('/')))
            except OSError:
                return False
            if actual != expected:
                return False
        return True

    def _publish(self, staging, final, marker):
        for _ in range(CACHE_ATTEMPTS):
            if os.path.isfile(marker):
                shutil.rmtree(staging, ignore_errors=True)
                return
            try:
                os.replace(staging, final)
                return
            except OSError:
                if os.path.isfile(marker):
                    shutil.rmtree(staging, ignore_errors=True)
                    return
                shutil.rmtree(final, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError('cannot publish egg cache: ' + final)

    def _extract_temporary(self):
        import tempfile

        root = tempfile.mkdtemp(prefix='py_upper_egg_')
        try:
            self._extract_into(root)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        self.extract_root = root
        atexit.register(shutil.rmtree, root, True)

    def _extract(self):
        cache_root = _cache_root()
        if cache_root is None:
            return self._extract_temporary()
        try:
            close = _cache_lease(_cache_lease_path(cache_root, CACHE_PREFIX + self.digest))
        except OSError as exc:
            if not isinstance(exc, PermissionError) and exc.errno != errno.EROFS:
                raise
            return self._extract_temporary()
        if close is None:
            return self._extract_temporary()
        atexit.register(close)
        final = os.path.join(cache_root, CACHE_PREFIX + self.digest)
        marker = os.path.join(final, CACHE_MARKER)
        if os.path.isfile(marker):
            if self._cache_valid(final):
                self.extract_root = final
                return
            # A cached directory that no longer matches the archive is dropped
            # and rebuilt; leaving it in place would run edited code.
            shutil.rmtree(final, ignore_errors=True)
        # mkdir(exist_ok=True) only proves the root exists, not that we can
        # write inside it. Check the real staging operation, with no probe on
        # a warm start and no fallback for unexpected I/O or extraction errors.
        import tempfile

        try:
            staging = tempfile.mkdtemp(prefix='.egg-' + self.digest + '-', dir=cache_root)
        except OSError as exc:
            if not isinstance(exc, PermissionError) and exc.errno != errno.EROFS:
                raise
            return self._extract_temporary()
        try:
            self._extract_into(staging)
            _write_marker(os.path.join(staging, CACHE_MARKER), self.digest)
            self._publish(staging, final, marker)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.extract_root = final
        _prune_cache(cache_root, CACHE_PREFIX + self.digest)

    def read_data(self, name):
        with self.lock:
            return self.archive.read(name)

    def _has_dir(self, prefix):
        for name in self.names:
            if name.startswith(prefix):
                return True
        return False

    def _find(self, fullname):
        relative = fullname.replace('.', '/')
        # CPython loads an extension module before a source module with the same
        # name, and a package marker follows the same rule. build_pyd.py-style
        # bundles may carry an empty __init__.py for zipimport; if that file won
        # the lookup, the package would expose no children.
        for suffix in self.extensions:
            candidate = relative + '/__init__' + suffix
            if candidate in self.names:
                return ('pkgext', candidate)
        candidate = relative + '/__init__.py'
        if candidate in self.names:
            return ('pkg', candidate)
        for suffix in self.extensions:
            candidate = relative + suffix
            if candidate in self.names:
                return ('ext', candidate)
        candidate = relative + '.py'
        if candidate in self.names:
            return ('py', candidate)
        if self._has_dir(relative + '/'):
            return ('ns', relative)
        return (None, None)

    def find_spec(self, fullname, path=None, target=None):
        if self.backend != 'memory':
            return None
        kind, arcname = self._find(fullname)
        if kind is None:
            return None
        if kind == 'ns':
            origin = self.egg_path + '/' + fullname.replace('.', '/')
            spec = importlib.util.spec_from_loader(fullname, self, origin=origin)
            spec.has_location = False
            spec.submodule_search_locations = [origin]
            return spec
        origin = self.egg_path + '/' + arcname
        spec = importlib.util.spec_from_loader(fullname, self, origin=origin)
        spec.has_location = True
        if kind in ('pkg', 'pkgext'):
            spec.submodule_search_locations = [self.egg_path + '/' + fullname.replace('.', '/')]
        return spec

    def create_module(self, spec):
        kind, arcname = self._find(spec.name)
        if kind not in ('ext', 'pkgext'):
            return None
        names = ['PyInit_' + spec.name.split('.')[-1]]
        if kind == 'pkgext':
            names.append('PyInit___init__')
        error = None
        for initname in names:
            try:
                module = self.memimporter.import_module(
                    spec.name, arcname, initname, self.read_data, spec)
            except ImportError as exc:
                error = exc
                continue
            module.__file__ = spec.origin
            module.__loader__ = self
            module.__memimported__ = True
            return module
        if error is not None:
            raise error
        return None

    def exec_module(self, module):
        if getattr(module, '__memimported__', False):
            return
        kind, arcname = self._find(module.__name__)
        if kind is None or kind == 'ns':
            return
        filename = getattr(module, '__file__', None) or (self.egg_path + '/' + arcname)
        code = compile(self.read_data(arcname), filename, 'exec')
        exec(code, module.__dict__)


def _install(importer):
    _install_builtin_submodule_finder()
    if importer.backend == 'extract':
        root = importer.extract_root
        if root not in sys.path:
            sys.path.insert(0, root)
        return importer
    for index, existing in enumerate(sys.meta_path):
        if isinstance(existing, EncryptedEggImporter):
            sys.meta_path[index] = importer
            return importer
    sys.meta_path.insert(0, importer)
    return importer


def install(egg_path, key_hex=None, backend='auto'):
    key = _key_bytes(key_hex)
    return _install(EncryptedEggImporter(egg_path, key, backend))


def install_from_launcher(egg_path, payload, digest, backend):
    # Not a loader mode: the launcher has already verified the archive and
    # hands over the original tag, preserving the extract cache key.
    return _install(EncryptedEggImporter(
        egg_path, None, backend, _opened_egg=(payload, digest)))
