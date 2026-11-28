# 中文维护说明：验证并加载应用归档；缓存需校验内容并持有租约，防止运行实例被淘汰。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
# appbuild encrypted egg loader.
#
# The loader is deliberately plain ASCII and uses only single quoted strings:
# tools/appbuild/package.py embeds this file into the launcher as a C++ raw
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
#             helper that tools/appbuild builds into the bundle.
#   extract - the egg is decrypted into a content-addressed cache directory
#             and added to sys.path. The directory name is derived from the
#             authentication tag, so a rebuilt egg gets a fresh root and an
#             existing one is reused without extracting again. This is the
#             portable fallback for Linux and macOS, where no stable in-memory
#             dlopen API exists. APPBUILD_EGG_CACHE overrides the cache root.
#   auto    - memory on Windows when _memimporter imports, extract otherwise.
#
# The extracted cache is plaintext on disk, so every reuse is checked against
# the per-member digests recorded in the archive manifest. The check reads the
# cached files once (a typical application is ~1 MB, so ~1 ms); set
# APPBUILD_EGG_CACHE_VERIFY=0 to skip it, at the cost of trusting whatever is
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
MANIFEST_NAME = 'appbuild_manifest.json'
BACKENDS = ('auto', 'memory', 'extract')
CACHE_PREFIX = 'egg-'
# Merged bundles are consumed by the launcher before this loader runs.
BUNDLE_PREFIX = 'appbuild_bundles/'
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


# 用 SHAKE-256 字节流执行可逆 XOR；它是代码混淆，不是密钥保管方案。
def keystream_xor(data, key):
    size = len(data)
    if size == 0:
        return data
    stream = hashlib.shake_256(key).digest(size)
    return (int.from_bytes(data, 'little')
            ^ int.from_bytes(stream, 'little')).to_bytes(size, 'little')


# 对密钥、头部和密文计算固定长度认证标签。
def egg_tag(header, ciphertext, key):
    shake = hashlib.shake_256()
    shake.update(key)
    shake.update(header)
    shake.update(ciphertext)
    return shake.digest(TAG_SIZE)


# 按当前格式切分头部、标签和密文；长度不足时抛出 ValueError。
def split_v2(raw):
    header_size = len(MAGIC2) + SALT_SIZE
    if len(raw) < header_size + TAG_SIZE:
        raise ValueError('egg is truncated')
    header = raw[:header_size]
    tag = raw[header_size:header_size + TAG_SIZE]
    ciphertext = raw[header_size + TAG_SIZE:]
    return header, tag, ciphertext


# 先校验格式和标签再解密，返回 ZIP 字节与缓存摘要；旧格式直接拒绝。
def open_egg(raw, key):
    if not raw:
        raise ValueError('egg is empty')
    if raw[:len(MAGIC2)] != MAGIC2:
        raise ValueError('egg is not authenticated: rebuild it with this version of appbuild')
    header, tag, ciphertext = split_v2(raw)
    expected = egg_tag(header, ciphertext, key)
    if not hmac.compare_digest(tag, expected):
        raise ValueError('egg authentication failed: archive was rebuilt or edited')
    return keystream_xor(ciphertext, key + header[len(MAGIC2):]), tag.hex()


# 解析调用者提供的十六进制密钥，要求至少 16 字节；不内置默认密钥。
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


# 规范化归档成员分隔符并拒绝空段、点段及绝对路径。
def _safe_member(name):
    text = name.replace('\\', '/')
    parts = text.split('/')
    if not text or text.startswith('/') or any(part in ('', '.', '..') for part in parts):
        raise ValueError('unsafe egg member: ' + name)
    return '/'.join(parts)


# 解析平台缓存根及应用命名空间；目录无法创建时返回 None 以启用临时解包。
def _cache_root():
    override = os.environ.get('APPBUILD_EGG_CACHE')
    if override:
        root = os.path.abspath(os.path.expanduser(override))
    elif os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.join(
            os.path.expanduser('~'), 'AppData', 'Local')
        root = os.path.join(base, 'appbuild', 'cache', 'eggs')
    elif sys.platform == 'darwin':
        root = os.path.join(os.path.expanduser('~'), 'Library', 'Caches',
                            'appbuild', 'eggs')
    else:
        base = os.environ.get('XDG_CACHE_HOME') or os.path.join(
            os.path.expanduser('~'), '.cache')
        root = os.path.join(base, 'appbuild', 'eggs')
    namespace = os.environ.get('APPBUILD_CACHE_NAMESPACE', '')
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


# 写入 ASCII 完成标记；只有全部内容解包成功后才能调用。
def _write_marker(path, digest):
    with open(path, 'w', encoding='ascii') as handle:
        handle.write(digest + '\n')


# 分块计算缓存文件摘要；独立实现以免嵌入 loader 依赖构建器包。
def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# 默认启用缓存摘要核验，仅显式环境变量值 0 关闭。
def _cache_verify_enabled():
    return os.environ.get('APPBUILD_EGG_CACHE_VERIFY', '1') != '0'


# Drops cache entries from earlier builds.
#
# Entries are named after the authentication tag, so a rebuilt application never
# reuses one: they would accumulate one per release. Deletion is best effort and
# never fails a start; another process may be using an entry, and a leftover
# directory only costs disk space.
# 在缓存树旁创建租约目录并返回稳定锁文件路径。
def _cache_lease_path(root, name):
    directory = root + '.leases'
    os.makedirs(directory, mode=0o700, exist_ok=True)
    return os.path.join(directory, name)


# 租约必须覆盖实际使用期；清理方只有拿到独占锁才能删除对应版本。
# 尝试非阻塞共享或独占租约；成功返回释放回调，冲突返回 None。
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
# 保留当前及最近版本；旧项仅在取得独占租约后删除，清理失败不阻断启动。
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
    # 读取缓存修改时间供新旧排序；目录消失时视为最旧。
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


# 尝试加载 Windows 内存导入扩展；缺失或无法装载时返回 None。
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

    # 为真实包下已注册的内建子模块生成 spec；其他模块交给后续 finder。
    def find_spec(self, fullname, path=None, target=None):
        if path is None or '.' not in fullname:
            return None
        if not _imp.is_builtin(fullname):
            return None
        return importlib.machinery.BuiltinImporter.find_spec(fullname)


# 只插入一个内建子模块 finder，位置必须在 BuiltinImporter 之前。
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
    # 验证归档清单并选择加载后端；可接收 launcher 已认证的 payload 以避免重复解密。
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
    # 核对清单与归档成员集合及摘要格式；缺失或多余成员均拒绝。
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

    # 把应用成员写入指定私有目录；跳过清单及由 launcher 管理的合并扩展。
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
    # 核对完成标记及成员摘要；缓存内容缺失或变化即判失效。
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

    # 以有界 rename 重试发布完整解包目录，允许另一进程先完成同一缓存。
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

    # 解包到临时目录并注册退出清理；异常时立即移除半成品。
    def _extract_temporary(self):
        import tempfile

        root = tempfile.mkdtemp(prefix='appbuild_egg_')
        try:
            self._extract_into(root)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        self.extract_root = root
        atexit.register(shutil.rmtree, root, True)

    # 持有使用期租约，校验或重建缓存；只对只读及权限问题回退临时解包。
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

    # 在导入器锁内读取归档成员，串行访问共享 ZIP 句柄。
    def read_data(self, name):
        with self.lock:
            return self.archive.read(name)

    # 判断成员集合中是否存在指定目录前缀，用于命名空间包识别。
    def _has_dir(self, prefix):
        for name in self.names:
            if name.startswith(prefix):
                return True
        return False

    # 按扩展包、源码包、扩展模块、源码模块及命名空间包顺序查找成员。
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

    # 仅 memory 后端提供归档模块 spec；extract 后端使用普通文件导入。
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

    # 通过内存加载器调用扩展初始化符号；包扩展可尝试 __init__ 符号。
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

    # 执行归档源码模块；已由内存加载器初始化的扩展和命名空间包跳过。
    def exec_module(self, module):
        if getattr(module, '__memimported__', False):
            return
        kind, arcname = self._find(module.__name__)
        if kind is None or kind == 'ns':
            return
        filename = getattr(module, '__file__', None) or (self.egg_path + '/' + arcname)
        code = compile(self.read_data(arcname), filename, 'exec')
        exec(code, module.__dict__)


# 安装解包路径或替换同类 finder，避免重复堆叠导入钩子。
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


# 解析外部密钥并安装独立验证的归档导入器。
def install(egg_path, key_hex=None, backend='auto'):
    key = _key_bytes(key_hex)
    return _install(EncryptedEggImporter(egg_path, key, backend))


# 接收 launcher 已认证的 payload 和摘要；保留同一内容缓存键。
def install_from_launcher(egg_path, payload, digest, backend):
    # Not a loader mode: the launcher has already verified the archive and
    # hands over the original tag, preserving the extract cache key.
    return _install(EncryptedEggImporter(
        egg_path, None, backend, _opened_egg=(payload, digest)))
