# py_upper encrypted egg loader.
#
# The loader is deliberately plain ASCII and uses only single quoted strings:
# tools/py_upper/package.py embeds this file into the launcher as a C++ raw
# string literal, and the bootstrap wraps that text in a Python triple quoted
# literal. A triple quote or a non-ASCII character would break that handoff.
#
# Egg layout:
#   MAGIC (8 bytes) + XOR(zip bytes, SHAKE-256 key stream)
# An egg without MAGIC is treated as a plain zip, which keeps the loader usable
# with hand-made archives.
#
# Backends:
#   memory  - extension modules are mapped from memory through _memimporter.
#             This is the Windows path and needs the py2exe MemoryModule
#             helper that tools/py_upper builds into the bundle.
#   extract - the egg is decrypted into a content-addressed cache directory
#             and added to sys.path. The directory name is the SHA-256 of the
#             decrypted archive, so a rebuilt egg gets a fresh root and an
#             existing one is reused without extracting again. This is the
#             portable fallback for Linux and macOS, where no stable in-memory
#             dlopen API exists. PY_UPPER_EGG_CACHE overrides the cache root.
#   auto    - memory on Windows when _memimporter imports, extract otherwise.
#
# The key stream is obfuscation, not modern cryptography: it raises the cost of
# reading the shipped application code, and it is not a secret storage format.

import _imp
import atexit
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import zipfile

MAGIC = b'WBEGG01!'
DEFAULT_KEY_HEX = '047636cf3ddbaae2346f597256416e858ee0b7c8b7448221e5babfbb4f74c20d'
BACKENDS = ('auto', 'memory', 'extract')
CACHE_PREFIX = 'egg-'
# Merged bundles are consumed by the launcher before this loader runs.
BUNDLE_PREFIX = 'py_upper_bundles/'
CACHE_MARKER = '.complete'
CACHE_ATTEMPTS = 3


def keystream_xor(data, key):
    size = len(data)
    if size == 0:
        return data
    stream = hashlib.shake_256(key).digest(size)
    return (int.from_bytes(data, 'little')
            ^ int.from_bytes(stream, 'little')).to_bytes(size, 'little')


def decrypt_if_needed(raw, key):
    if raw[:len(MAGIC)] == MAGIC:
        return keystream_xor(raw[len(MAGIC):], key)
    return raw


def _key_bytes(key_hex):
    text = str(key_hex or DEFAULT_KEY_HEX).strip()
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
class BuiltinSubmoduleFinder(importlib.abc.MetaPathFinder):

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


class EncryptedEggImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, egg_path, key, backend='auto'):
        backend = str(backend or 'auto').strip().lower()
        if backend not in BACKENDS:
            raise ValueError('egg backend must be one of: ' + ', '.join(BACKENDS))
        self.egg_path = os.path.abspath(egg_path)
        self.key = key
        self.requested_backend = backend
        self.lock = threading.Lock()
        with open(self.egg_path, 'rb') as handle:
            raw = handle.read()
        if not raw:
            raise ValueError('egg is empty: ' + self.egg_path)
        try:
            payload = decrypt_if_needed(raw, key)
            self.archive = zipfile.ZipFile(io.BytesIO(payload))
            self.names = frozenset(self.archive.namelist())
            self.payload = payload
        except Exception as exc:
            raise ValueError('bad egg, not a zip or wrong key: ' + self.egg_path + ': ' + str(exc))
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

    def _extract_into(self, root):
        for name in self.names:
            if name.endswith('/') or name.startswith(BUNDLE_PREFIX):
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

    def _extract(self):
        digest = hashlib.sha256(self.payload).hexdigest()
        cache_root = _cache_root()
        if cache_root is None:
            root = tempfile.mkdtemp(prefix='py_upper_egg_')
            try:
                self._extract_into(root)
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                raise
            self.extract_root = root
            atexit.register(shutil.rmtree, root, True)
            return
        final = os.path.join(cache_root, CACHE_PREFIX + digest)
        marker = os.path.join(final, CACHE_MARKER)
        if os.path.isfile(marker):
            self.extract_root = final
            return
        staging = tempfile.mkdtemp(prefix='.egg-' + digest + '-', dir=cache_root)
        try:
            self._extract_into(staging)
            _write_marker(os.path.join(staging, CACHE_MARKER), digest)
            self._publish(staging, final, marker)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.extract_root = final

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


def install(egg_path, key_hex=None, backend='auto'):
    key = _key_bytes(key_hex)
    importer = EncryptedEggImporter(egg_path, key, backend)
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