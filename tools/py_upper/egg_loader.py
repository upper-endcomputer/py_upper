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
#   extract - the egg is decrypted into a private temporary directory and
#             added to sys.path. This is the portable fallback for Linux and
#             macOS, where no stable in-memory dlopen API exists.
#   auto    - memory on Windows when _memimporter imports, extract otherwise.
#
# The key stream is obfuscation, not modern cryptography: it raises the cost of
# reading the shipped application code, and it is not a secret storage format.

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


def _memimporter_module():
    try:
        import _memimporter
    except (ImportError, OSError):
        return None
    return _memimporter


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

    def _extract(self):
        root = tempfile.mkdtemp(prefix='py_upper_egg_')
        try:
            for name in self.names:
                if name.endswith('/'):
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
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        self.extract_root = root
        atexit.register(shutil.rmtree, root, True)

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