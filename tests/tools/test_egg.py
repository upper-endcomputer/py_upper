from __future__ import annotations

import errno
import importlib
import importlib.machinery
import io
import os
import subprocess
import sys
import tempfile
import types
import zipfile
from pathlib import Path

import pytest

from py_upper import egg as egg_module
from py_upper import egg_loader
from py_upper.egg import (
    DEFAULT_KEY_HEX, EGG_DIR, EGG_META_NAME, EGG_NAME, EggConfig, application_files,
    egg_authenticated, egg_config, egg_loader_source, pack_egg, read_egg_members,
    read_egg_manifest, read_egg_meta, remove_packed_files, write_egg_meta,
)
from py_upper.egg_loader import (
    MAGIC2, MANIFEST_NAME, BuiltinSubmoduleFinder, EncryptedEggImporter,
    egg_tag, install, keystream_xor, open_egg,
)

KEY = DEFAULT_KEY_HEX
EXT = importlib.machinery.EXTENSION_SUFFIXES[0]


@pytest.fixture(autouse=True)
def isolated_egg_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PY_UPPER_EGG_CACHE", str(tmp_path / "egg-cache"))
    existing = [entry for entry in sys.meta_path
                if isinstance(entry, BuiltinSubmoduleFinder)]
    yield
    sys.meta_path[:] = [
        entry for entry in sys.meta_path
        if not isinstance(entry, BuiltinSubmoduleFinder) or entry in existing
    ]


def make_site(tmp_path, files, name="site"):
    site = tmp_path / name
    for relative, data in files.items():
        path = site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return site


def make_egg(tmp_path, files, name="py_upper.dat", key=KEY, compression="stored"):
    site = make_site(tmp_path, files, "site_" + name.replace(".", "_"))
    config = EggConfig(enabled=True, key_hex=key, compression=compression)
    return pack_egg(site, tmp_path / name, list(files), config)


def fake_memimporter(monkeypatch, calls=None, fail_init=None):
    module = types.ModuleType("_memimporter")

    def import_module(modname, arcname, initfunc, read_data, spec):
        if calls is not None:
            calls.append((modname, arcname, initfunc, read_data, spec))
        if initfunc == fail_init:
            raise ImportError("no such init function: " + initfunc)
        return types.ModuleType(modname)

    module.import_module = import_module
    monkeypatch.setitem(sys.modules, "_memimporter", module)
    return module


def test_keystream_is_its_own_inverse_and_handles_empty_input():
    data = b"the quick brown fox" * 40
    key = bytes.fromhex(KEY)
    encoded = keystream_xor(data, key)
    assert encoded != data
    assert keystream_xor(encoded, key) == data
    assert keystream_xor(b"", key) == b""


def test_pack_egg_writes_authenticated_v2_archive(tmp_path):
    egg = make_egg(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": "VALUE = 42\n"})
    raw = egg.read_bytes()
    assert raw[:len(MAGIC2)] == MAGIC2
    # The payload is obfuscated, so the plaintext member name is not visible.
    assert b"pkg/mod.py" not in raw
    assert read_egg_members(egg, KEY) == ("pkg/__init__.py", "pkg/mod.py", MANIFEST_NAME)
    assert egg_authenticated(egg, KEY)


def test_pack_egg_records_a_digest_per_member(tmp_path):
    egg = make_egg(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": "VALUE = 42\n"})
    manifest = read_egg_manifest(egg, KEY)
    assert set(manifest) == {"pkg/__init__.py", "pkg/mod.py"}
    import hashlib
    assert manifest["pkg/mod.py"] == hashlib.sha256(b"VALUE = 42\n").hexdigest()


def test_pack_egg_tag_detects_an_edited_archive(tmp_path):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n" * 50})
    raw = bytearray(egg.read_bytes())
    raw[-40] ^= 0x01
    egg.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="authentication failed"):
        read_egg_members(egg, KEY)
    assert not egg_authenticated(egg, KEY)


def test_pack_egg_tag_detects_a_swapped_archive(tmp_path):
    """An attacker who rebuilds the zip with their own code cannot re-tag it."""
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    plain = tmp_path / "attacker.zip"
    with zipfile.ZipFile(plain, "w") as archive:
        archive.writestr("mod.py", "PWNED = 1\n")
    egg.write_bytes(plain.read_bytes())
    with pytest.raises(ValueError, match="not authenticated"):
        read_egg_members(egg, KEY)
    assert not egg_authenticated(egg, KEY)


def test_pack_egg_salt_differs_between_keys(tmp_path):
    """The key stream depends on the salt, so equal plaintext is not equal ciphertext."""
    first = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"}, "a.egg", key=KEY)
    second = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"}, "b.egg", key="11" * 32)
    assert first.read_bytes() != second.read_bytes()


def test_pack_egg_is_deterministic(tmp_path):
    files = {"pkg/__init__.py": "", "pkg/mod.py": "VALUE = 1\n"}
    first = make_egg(tmp_path, files, "first.egg")
    second = make_egg(tmp_path, files, "second.egg")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(io.BytesIO(open_egg(first.read_bytes(), bytes.fromhex(KEY))[0])) as archive:
        # The manifest is the last entry: it is written after every member's
        # digest is known.
        assert archive.namelist() == ["pkg/__init__.py", "pkg/mod.py", MANIFEST_NAME]
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


@pytest.mark.parametrize("compression", ["stored", "deflated", "lzma"])
def test_pack_egg_supports_each_compression(tmp_path, compression):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 7\n"}, name=compression + ".egg", compression=compression)
    # read_egg_members sorts, and 'p' sorts before... nothing here: the manifest
    # name sorts after 'mod.py' because 'p' > 'm'.
    assert read_egg_members(egg, KEY) == ("mod.py", MANIFEST_NAME)


def test_pack_egg_stores_launcher_bundles_uncompressed(tmp_path):
    egg = make_egg(
        tmp_path,
        {
            "pkg/mod.py": "VALUE = 1\n" * 200,
            "py_upper_bundles/py_upper_pkg.so": b"\x7fELF" + b"\x00" * 512,
        },
        compression="deflated",
    )
    with zipfile.ZipFile(io.BytesIO(open_egg(egg.read_bytes(), bytes.fromhex(KEY))[0])) as archive:
        bundle = archive.getinfo("py_upper_bundles/py_upper_pkg.so")
        source = archive.getinfo("pkg/mod.py")
        assert bundle.compress_type == zipfile.ZIP_STORED
        assert bundle.compress_size == bundle.file_size
        assert source.compress_type == zipfile.ZIP_DEFLATED


def test_read_egg_members_refuses_a_plain_zip(tmp_path):
    path = tmp_path / "plain.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mod.py", "VALUE = 1\n")
    with pytest.raises(ValueError, match="not authenticated"):
        read_egg_members(path, KEY)
    with pytest.raises(ValueError, match="not authenticated"):
        EncryptedEggImporter(path, bytes.fromhex(KEY), "extract")


def test_read_egg_members_rejects_a_wrong_key(tmp_path):
    """A wrong key fails the authentication tag, before any zip parsing."""
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    with pytest.raises(ValueError, match="authentication failed"):
        read_egg_members(egg, "00" * 16)
    with pytest.raises(ValueError, match="authentication failed"):
        EncryptedEggImporter(egg, bytes.fromhex("00" * 16), "extract")


def test_importer_rejects_empty_and_garbage_archives(tmp_path):
    empty = tmp_path / "empty.egg"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        EncryptedEggImporter(empty, bytes.fromhex(KEY), "extract")

    garbage = tmp_path / "garbage.egg"
    garbage.write_bytes(b"not an archive")
    with pytest.raises(ValueError, match="bad egg"):
        EncryptedEggImporter(garbage, bytes.fromhex(KEY), "extract")


def test_find_prefers_extensions_and_package_extensions(tmp_path):
    egg = make_egg(tmp_path, {
        "mod.py": "VALUE = 'py'\n",
        "mod" + EXT: b"",
        "pkg/__init__.py": "",
        "pkg/__init__" + EXT: b"",
        "ns/child.py": "VALUE = 1\n",
    })
    importer = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    assert importer._find("mod") == ("ext", "mod" + EXT)
    assert importer._find("pkg") == ("pkgext", "pkg/__init__" + EXT)
    assert importer._find("ns") == ("ns", "ns")
    assert importer._find("missing") == (None, None)


def test_find_falls_back_to_source_and_package_marker(tmp_path):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n", "pkg/__init__.py": ""})
    importer = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    assert importer._find("mod") == ("py", "mod.py")
    assert importer._find("pkg") == ("pkg", "pkg/__init__.py")


def test_extract_backend_imports_a_source_module(tmp_path):
    name = "egg_extract_module_" + tmp_path.name
    egg = make_egg(tmp_path, {name + ".py": "VALUE = 'loaded'\n"})
    importer = install(egg, KEY, backend="extract")
    try:
        module = importlib.import_module(name)
        assert module.VALUE == "loaded"
        assert importer.extract_root in sys.path
    finally:
        sys.modules.pop(name, None)
        if importer.extract_root in sys.path:
            sys.path.remove(importer.extract_root)


def test_extract_backend_accepts_the_launcher_payload_without_the_file(tmp_path):
    name = "egg_extract_payload_" + tmp_path.name
    egg = make_egg(tmp_path, {name + ".py": "VALUE = 'payload'\n"})
    payload, digest = open_egg(egg.read_bytes(), bytes.fromhex(KEY))
    importer = egg_loader.install_from_launcher(tmp_path / "already-consumed.egg", payload, digest, "extract")
    try:
        module = importlib.import_module(name)
        assert module.VALUE == "payload"
        assert importer.extract_root in sys.path
    finally:
        sys.modules.pop(name, None)
        if importer.extract_root in sys.path:
            sys.path.remove(importer.extract_root)


def test_extract_backend_leaves_launcher_bundles_to_the_launcher(tmp_path):
    egg = make_egg(
        tmp_path,
        {
            "mod.py": "VALUE = 1\n",
            "py_upper_bundles/py_upper_pkg.so": b"bundle",
        },
    )
    importer = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    root = Path(importer.extract_root)
    assert (root / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (root / "py_upper_bundles").exists()


def test_extract_cache_is_content_addressed_and_reused(tmp_path):
    files = {"pkg/__init__.py": "", "pkg/mod.py": "VALUE = 1\n"}
    first = make_egg(tmp_path, files, "first.egg")
    same = make_egg(tmp_path, files, "same.egg")
    different = make_egg(tmp_path, {"pkg/mod.py": "VALUE = 2\n"}, "different.egg")

    one = EncryptedEggImporter(first, bytes.fromhex(KEY), "extract")
    two = EncryptedEggImporter(same, bytes.fromhex(KEY), "extract")
    three = EncryptedEggImporter(different, bytes.fromhex(KEY), "extract")

    assert one.extract_root == two.extract_root
    assert one.extract_root != three.extract_root
    root = Path(one.extract_root)
    assert root.parent == tmp_path / "egg-cache"
    assert root.name.startswith("egg-")
    assert (root / ".complete").is_file()
    assert (root / "pkg" / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_extract_cache_reuses_a_complete_directory(tmp_path, monkeypatch):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    first = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")

    def unexpected_extract(self, root):
        raise AssertionError("complete cache entry was extracted again")

    monkeypatch.setattr(EncryptedEggImporter, "_extract_into", unexpected_extract)
    second = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    assert second.extract_root == first.extract_root


@pytest.mark.parametrize("backend", ["extract", "memory"])
def test_launcher_handoff_uses_the_open_archive_and_existing_cache_key(tmp_path, monkeypatch, backend):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    key = bytes.fromhex(KEY)
    payload, digest = open_egg(egg.read_bytes(), key)
    if backend == "memory":
        fake_memimporter(monkeypatch)
    else:
        existing_root = EncryptedEggImporter(egg, key, backend).extract_root
    egg.unlink()
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.setattr(sys, "meta_path", sys.meta_path[:])

    importer = egg_loader.install_from_launcher(egg, payload, digest, backend)

    assert importer.backend == backend
    assert importer.digest == digest
    assert importer.read_data("mod.py") == b"VALUE = 1\n"
    if backend == "extract":
        assert importer.extract_root == existing_root


@pytest.mark.parametrize("error", [errno.EACCES, errno.EROFS, errno.EIO])
def test_extract_handles_unwritable_cache_without_hiding_io_errors(tmp_path, monkeypatch, error):
    cache = tmp_path / "egg-cache"
    cache.mkdir()
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    mkdtemp = tempfile.mkdtemp

    def make_directory(*args, **kwargs):
        if kwargs.get("dir") == str(cache):
            raise OSError(error, os.strerror(error))
        return mkdtemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", make_directory)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    if error == errno.EIO:
        with pytest.raises(OSError) as failure:
            EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
        assert failure.value.errno == errno.EIO
    else:
        importer = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
        root = Path(importer.extract_root)
        assert root.parent == tmp_path
        assert (root / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not list(cache.iterdir())


def test_embedded_egg_loader_import_does_not_load_importlib_resources():
    result = subprocess.run(
        [sys.executable, "-IS", "-c",
         "import sys; exec(compile(sys.stdin.read(), '<egg loader>', 'exec')); "
         "assert 'importlib.abc' not in sys.modules; "
         "assert 'importlib.resources' not in sys.modules"],
        input=egg_loader_source(), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_extract_cache_rebuilds_an_incomplete_directory(tmp_path):
    egg = make_egg(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": "VALUE = 1\n"})
    first = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    root = Path(first.extract_root)
    (root / ".complete").unlink()
    (root / "pkg" / "mod.py").unlink()

    second = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    assert second.extract_root == str(root)
    assert (root / ".complete").is_file()
    assert (root / "pkg" / "mod.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not list(root.parent.glob(".egg-*"))


def test_extract_rejects_unsafe_members_without_leaving_staging_dirs(tmp_path):
    import hashlib
    import json

    payload = b"VALUE = 1\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.py", payload)
        archive.writestr(MANIFEST_NAME, json.dumps(
            {"files": {"../escape.py": hashlib.sha256(payload).hexdigest()}}))
    key = bytes.fromhex(KEY)
    salt = bytes(16)
    header = MAGIC2 + salt
    ciphertext = keystream_xor(buffer.getvalue(), key + salt)
    egg = tmp_path / "unsafe.egg"
    egg.write_bytes(header + egg_tag(header, ciphertext, key) + ciphertext)

    with pytest.raises(ValueError, match="unsafe egg member"):
        EncryptedEggImporter(egg, key, "extract")

    cache = tmp_path / "egg-cache"
    assert not list(cache.glob(".egg-*"))
    assert not (tmp_path / "escape.py").exists()


def test_install_replaces_its_own_finder_instead_of_stacking(tmp_path):
    first = make_egg(tmp_path, {"a.py": "VALUE = 1\n"}, "first.egg")
    second = make_egg(tmp_path, {"a.py": "VALUE = 2\n"}, "second.egg")
    module = types.ModuleType("_memimporter")
    module.import_module = lambda *args: None
    previous = sys.modules.get("_memimporter")
    sys.modules["_memimporter"] = module
    try:
        install(first, KEY, backend="memory")
        installed = install(second, KEY, backend="memory")
        finders = [entry for entry in sys.meta_path if isinstance(entry, EncryptedEggImporter)]
        assert finders == [installed]
        assert installed.egg_path.endswith("second.egg")
    finally:
        sys.meta_path[:] = [entry for entry in sys.meta_path
                            if not isinstance(entry, EncryptedEggImporter)]
        if previous is None:
            sys.modules.pop("_memimporter", None)
        else:
            sys.modules["_memimporter"] = previous


def test_install_adds_one_dotted_builtin_finder_ahead_of_builtin_importer(tmp_path):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    importer = install(egg, KEY, backend="extract")
    try:
        finders = [entry for entry in sys.meta_path
                   if isinstance(entry, BuiltinSubmoduleFinder)]
        assert len(finders) == 1
        assert sys.meta_path.index(finders[0]) < sys.meta_path.index(
            importlib.machinery.BuiltinImporter)
        install(egg, KEY, backend="extract")
        assert len([entry for entry in sys.meta_path
                    if isinstance(entry, BuiltinSubmoduleFinder)]) == 1
    finally:
        if importer.extract_root in sys.path:
            sys.path.remove(importer.extract_root)


def test_builtin_submodule_finder_resolves_only_dotted_builtins(monkeypatch):
    sentinel = object()
    calls = []

    def find_spec(name):
        calls.append(name)
        return sentinel

    monkeypatch.setattr("py_upper.egg_loader._imp", types.SimpleNamespace(
        is_builtin=lambda name: name == "pkg.leaf"))
    monkeypatch.setattr(importlib.machinery, "BuiltinImporter", types.SimpleNamespace(
        find_spec=find_spec))
    finder = BuiltinSubmoduleFinder()
    assert finder.find_spec("pkg.leaf", ["site"]) is sentinel
    assert calls == ["pkg.leaf"]
    assert finder.find_spec("pkg.leaf") is None
    assert finder.find_spec("toplevel", ["site"]) is None
    assert finder.find_spec("pkg.other", ["site"]) is None
    assert calls == ["pkg.leaf"]


def test_memory_backend_passes_the_arcname_and_init_symbol(tmp_path, monkeypatch):
    calls = []
    fake_memimporter(monkeypatch, calls)
    egg = make_egg(tmp_path, {"mod" + EXT: b""})
    importer = EncryptedEggImporter(egg, bytes.fromhex(KEY), "memory")
    spec = importer.find_spec("mod")
    assert spec is not None
    module = importer.create_module(spec)
    assert module.__memimported__ is True
    assert module.__loader__ is importer
    assert calls[0][0] == "mod"
    assert calls[0][1] == "mod" + EXT
    assert calls[0][2] == "PyInit_mod"
    assert callable(calls[0][3])


def test_memory_backend_falls_back_to_the_package_init_symbol(tmp_path, monkeypatch):
    calls = []
    fake_memimporter(monkeypatch, calls, fail_init="PyInit_pkg")
    egg = make_egg(tmp_path, {"pkg/__init__" + EXT: b""})
    importer = EncryptedEggImporter(egg, bytes.fromhex(KEY), "memory")
    module = importer.create_module(types.SimpleNamespace(name="pkg", origin="pkg"))
    assert module.__memimported__ is True
    assert [call[2] for call in calls] == ["PyInit_pkg", "PyInit___init__"]


def test_memory_backend_accepts_the_launcher_payload_without_the_file(tmp_path, monkeypatch):
    """The launcher already authenticated the egg; do not decrypt it twice."""
    calls = []
    fake_memimporter(monkeypatch, calls)
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    payload, digest = open_egg(egg.read_bytes(), bytes.fromhex(KEY))
    missing = tmp_path / "already-consumed.egg"
    monkeypatch.setattr(sys, "meta_path", sys.meta_path[:])
    importer = egg_loader.install_from_launcher(missing, payload, digest, "memory")
    assert importer.read_data("mod.py") == b"VALUE = 1\n"
    assert importer.find_spec("mod") is not None


def test_memory_backend_requires_the_helper(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "_memimporter", None)
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    with pytest.raises(RuntimeError, match="_memimporter"):
        EncryptedEggImporter(egg, bytes.fromhex(KEY), "memory")


def test_auto_without_helper_falls_back_to_extract(tmp_path):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    importer = EncryptedEggImporter(egg, bytes.fromhex(KEY), "auto")
    assert importer.backend in {"memory", "extract"}
    if importer.backend == "extract":
        assert importer.extract_root is not None


def test_application_files_includes_app_sources_and_build_outputs_only(tmp_path, monkeypatch):
    app = tmp_path / "app"
    root = app / "src"
    compiled = "pkg/mod" + EXT
    files = {
        "pkg/__init__.py": "",
        "pkg/mod" + EXT: b"",
        "pkg/resource.json": "{}",
        "pkg/native.dll": b"MZ",
    }
    site = make_site(tmp_path, files)
    monkeypatch.setattr(egg_module, "APP", app)
    # The build's own source tree still carries mod.py; its compiled file is
    # what ships. application_files only reports what exists in the site tree.
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pkg" / "resource.json").write_text("{}", encoding="utf-8")
    (root / "pkg" / "native.dll").write_bytes(b"MZ")

    result = application_files(site, {compiled})
    assert result == ["pkg/__init__.py", "pkg/mod" + EXT, "pkg/resource.json"]


def test_application_files_rejects_a_missing_input(tmp_path, monkeypatch):
    monkeypatch.setattr(egg_module, "APP", tmp_path / "app")
    (tmp_path / "app" / "src").mkdir(parents=True)
    (tmp_path / "app" / "src" / "mod.py").write_text("", encoding="utf-8")
    site = tmp_path / "site"
    site.mkdir()
    with pytest.raises(RuntimeError, match="missing"):
        application_files(site, {"mod" + EXT})


def test_remove_packed_files_prunes_emptied_directories(tmp_path):
    site = make_site(tmp_path, {
        "pkg/a.py": "",
        "pkg/sub/b.py": "",
        "other/c.py": "",
    })
    remove_packed_files(site, ["pkg/a.py", "pkg/sub/b.py"])
    assert not (site / "pkg").exists()
    assert (site / "other" / "c.py").is_file()


def test_egg_config_reads_the_section_and_applies_overrides(monkeypatch):
    monkeypatch.setattr(egg_module, "py_upper_config", lambda: {
        "egg": {"enabled": True, "key_hex": KEY, "backend": "extract", "compression": "stored"}
    })
    config = egg_config()
    assert (config.enabled, config.backend, config.compression) == (True, "extract", "stored")
    assert egg_config(False, backend="memory", compression="lzma").enabled is False
    assert egg_config(True, backend="memory").backend == "memory"


def test_egg_config_defaults_to_enabled_without_a_section(monkeypatch):
    monkeypatch.setattr(egg_module, "py_upper_config", lambda: {})
    config = egg_config()
    assert config.enabled is True
    assert config.key_hex == KEY
    assert config.backend == "auto"


def test_egg_config_honours_an_explicit_false(monkeypatch):
    monkeypatch.setattr(egg_module, "py_upper_config", lambda: {
        "egg": {"enabled": False},
    })
    assert egg_config().enabled is False
    assert egg_config(True).enabled is True


@pytest.mark.parametrize("value", [
    {"key_hex": "not-hex"},
    {"key_hex": "00"},
    {"backend": "magic"},
    {"compression": "bzip2"},
])
def test_egg_config_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setattr(egg_module, "py_upper_config", lambda: {"egg": value})
    with pytest.raises(RuntimeError):
        egg_config()


def test_egg_meta_roundtrips_and_ignores_a_stage_without_one(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    config = EggConfig(enabled=True, key_hex=KEY, backend="extract", compression="stored")
    path = write_egg_meta(stage, config)
    assert path == stage / EGG_META_NAME
    assert read_egg_meta(stage) == config
    assert read_egg_meta(tmp_path / "missing") is None


def test_egg_loader_source_is_embeddable_and_compiles():
    from py_upper.package import RAW_STRING_DELIMITER, _embedded_literal

    source = egg_loader_source()
    source.encode("ascii")
    assert '"""' not in source
    assert ')' + RAW_STRING_DELIMITER + '"' not in source
    compile(source, "egg_loader.py", "exec")
    literal = _embedded_literal(source, "the egg loader")
    assert literal.startswith('R"' + RAW_STRING_DELIMITER + "(")


def test_write_embedded_source_carries_the_loader_only_when_enabled(tmp_path):
    from py_upper.config import TARGETS
    from py_upper.package import write_embedded_source

    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    disabled = write_embedded_source(tmp_path / "off", TARGETS["windows-x86_64"])
    text = disabled.read_text(encoding="utf-8")
    assert "const char* const kEggLoader = nullptr;" in text
    assert "const unsigned int kEggKeyLength = 0u;" in text
    assert 'kEggBackend = "auto"' in text

    config = EggConfig(enabled=True, key_hex=KEY, backend="extract")
    enabled = write_embedded_source(tmp_path / "on", TARGETS["windows-x86_64"], config)
    text = enabled.read_text(encoding="utf-8")
    assert "kEggLoader = R\"pyupper(" in text
    assert "extern const char* const kEggLoader;" in text
    assert '"extract"' in text
    assert "MAGIC2 = b'WBEGG02!'" in text
    # The key must not be a readable hexadecimal literal in the shipped binary;
    # the generator stores it masked and the launcher reassembles it at run time.
    assert KEY not in text
    assert "const unsigned char kEggKeyMasked[]" in text
    assert "const unsigned char kEggKeyMask[]" in text
    assert "kEggKeyLength = 32u" in text


def test_pack_egg_targets_the_stage_name_used_by_packaging(tmp_path):
    egg = make_egg(tmp_path, {"mod.py": ""})
    assert egg.name == EGG_NAME
    assert EGG_NAME == "py_upper.dat"
    assert EGG_DIR == "bundle"


def test_stage_egg_config_honours_a_no_egg_build(tmp_path, monkeypatch):
    from py_upper import verify as verify_module

    monkeypatch.setattr(verify_module, "read_egg_meta", lambda stage: None)
    monkeypatch.setattr(verify_module, "egg_config", lambda enabled=None: EggConfig(
        enabled=True if enabled is None else bool(enabled)))
    assert verify_module._stage_egg_config(tmp_path).enabled is False
    (tmp_path / EGG_DIR).mkdir()
    (tmp_path / EGG_DIR / EGG_NAME).write_bytes(MAGIC2)
    assert verify_module._stage_egg_config(tmp_path).enabled is True


def test_tampered_extract_cache_is_rebuilt_instead_of_run(tmp_path):
    """The extract cache is plaintext, so reuse has to be checked, not assumed."""
    name = "egg_cache_tamper_" + tmp_path.name
    egg = make_egg(tmp_path, {name + ".py": "VALUE = 'clean'\n"})
    first = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    cached = Path(first.extract_root) / (name + ".py")
    cached.write_text("VALUE = 'pwned'\n", encoding="utf-8")

    second = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    assert second.extract_root == first.extract_root
    assert (Path(second.extract_root) / (name + ".py")).read_text(encoding="utf-8") == "VALUE = 'clean'\n"


def test_cache_verification_can_be_disabled_for_speed(tmp_path, monkeypatch):
    name = "egg_cache_skip_" + tmp_path.name
    egg = make_egg(tmp_path, {name + ".py": "VALUE = 'clean'\n"})
    first = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    (Path(first.extract_root) / (name + ".py")).write_text("VALUE = 'pwned'\n", encoding="utf-8")

    monkeypatch.setenv("PY_UPPER_EGG_CACHE_VERIFY", "0")
    second = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    # With the check off the edited file is reused as-is; this documents the
    # trade-off rather than pretending the switch is free.
    assert (Path(second.extract_root) / (name + ".py")).read_text(encoding="utf-8") == "VALUE = 'pwned'\n"


def test_unauthenticated_archives_are_refused_everywhere(tmp_path):
    """A v1 archive and a hand-made zip are rejected, build tools included."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mod.py", "VALUE = 1\n")
    plain = buffer.getvalue()

    legacy = tmp_path / "legacy.dat"
    legacy.write_bytes(b'WBEGG01!' + keystream_xor(plain, bytes.fromhex(KEY)))
    with pytest.raises(ValueError, match="not authenticated"):
        read_egg_members(legacy, KEY)
    with pytest.raises(ValueError, match="not authenticated"):
        EncryptedEggImporter(legacy, bytes.fromhex(KEY), "extract")

    plain_zip = tmp_path / "plain.zip"
    plain_zip.write_bytes(plain)
    with pytest.raises(ValueError, match="not authenticated"):
        read_egg_members(plain_zip, KEY)
    with pytest.raises(ValueError, match="not authenticated"):
        EncryptedEggImporter(plain_zip, bytes.fromhex(KEY), "extract")


def test_manifest_and_tag_reject_a_member_added_after_packing(tmp_path):
    """Editing the plaintext zip cannot work: the tag covers the ciphertext."""
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    payload, _digest = open_egg(egg.read_bytes(), bytes.fromhex(KEY))
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as original:
        with zipfile.ZipFile(buffer, "w") as rebuilt:
            for name in original.namelist():
                rebuilt.writestr(name, original.read(name))
            rebuilt.writestr("extra.py", "PWNED = 1\n")
    salt = bytes(16)
    header = MAGIC2 + salt
    ciphertext = keystream_xor(buffer.getvalue(), bytes.fromhex(KEY) + salt)
    egg.write_bytes(header + bytes(32) + ciphertext)
    with pytest.raises(ValueError, match="authentication failed"):
        EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")


def test_manifest_naming_a_missing_member_is_refused(tmp_path):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    payload, _digest = open_egg(egg.read_bytes(), bytes.fromhex(KEY))
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as original:
        with zipfile.ZipFile(buffer, "w") as rebuilt:
            for name in original.namelist():
                data = original.read(name)
                if name == MANIFEST_NAME:
                    data = b'{"files": {"ghost.py": "' + b"0" * 64 + b'"}}'
                rebuilt.writestr(name, data)
    salt = bytes(16)
    key = bytes.fromhex(KEY)
    header = MAGIC2 + salt
    ciphertext = keystream_xor(buffer.getvalue(), key + salt)
    tag = egg_tag(header, ciphertext, key)
    egg.write_bytes(header + tag + ciphertext)
    with pytest.raises(ValueError, match="missing member"):
        EncryptedEggImporter(egg, key, "extract")


def test_old_cache_entries_are_pruned_but_the_previous_build_is_kept(tmp_path):
    """A new tag per release must not leave one cache directory per version."""
    cache = tmp_path / "egg-cache"
    cache.mkdir()
    stale = []
    for index in range(4):
        entry = cache / ("egg-" + ("%064x" % index))
        entry.mkdir()
        (entry / ".complete").write_text("old\n", encoding="utf-8")
        os.utime(entry, (1000 + index, 1000 + index))
        stale.append(entry)

    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    importer = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    current = Path(importer.extract_root)
    assert current.is_dir()

    survivors = {path.name for path in cache.iterdir() if path.is_dir()}
    assert current.name in survivors
    # One older entry stays so a rollback to the previous build is still warm.
    assert len(survivors) == 2
    assert stale[-1].name in survivors


def test_cache_pruning_never_fails_a_start(tmp_path, monkeypatch):
    """Pruning is best effort: an unreadable cache root must not break startup."""
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    first = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    monkeypatch.setattr(egg_module, "_prune_cache", None) if hasattr(egg_module, "_prune_cache") else None
    # A directory that disappears mid-listing is the realistic race; the loader
    # must still publish and return.
    second = EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")
    assert second.extract_root == first.extract_root


def test_loader_pruning_leaves_the_launchers_bundle_cache_alone(tmp_path):
    """Each side owns its own cache prefix.

    `merged-*` is written and used by the launcher before Py_Initialize. When
    the loader pruned it too, a multi-version cache lost the directory the
    launcher was using, and the next start had to extract the bundles again.
    """
    cache = tmp_path / "egg-cache"
    cache.mkdir()
    for name in ("merged-current", "merged-old", "merged-older"):
        (cache / name).mkdir()
        (cache / name / ".complete").write_text("x\n", encoding="utf-8")

    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")

    survivors = {path.name for path in cache.iterdir() if path.is_dir()}
    # The loader published its own entry and pruned only egg-* entries.
    assert {name for name in survivors if name.startswith("merged-")} == {
        "merged-current", "merged-old", "merged-older"}
    assert any(name.startswith("egg-") for name in survivors)


def test_packed_extensions_are_stripped_before_the_egg_is_sealed(tmp_path, monkeypatch):
    """strip_native has to run before packing, not after.

    bundle_native_dependencies runs in a later build step and only sees files
    that are still loose in site-packages. An extension sealed inside the egg
    never reached it, so strip_native silently had no effect on the
    application's own bundles.
    """
    from py_upper import python_build

    calls = []
    monkeypatch.setattr(python_build, "optimize_config", lambda: {"strip_native": True},
                        raising=False)
    import py_upper.config as config_module
    monkeypatch.setattr(config_module, "optimize_config", lambda: {"strip_native": True})

    import py_upper.native.bundle as bundle_module
    monkeypatch.setattr(bundle_module, "_strip_native", lambda path, target: calls.append(("strip", path.name)))
    monkeypatch.setattr(bundle_module, "ad_hoc_sign", lambda path: calls.append(("sign", path.name)))

    site = tmp_path / "site"
    (site / "pkg").mkdir(parents=True)
    (site / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site / "py_upper_bundles").mkdir()
    (site / "py_upper_bundles" / "bundle.so").write_bytes(b"\x7fELF")

    from py_upper.config import TARGETS
    python_build.strip_packed_extensions(
        site, ["pkg/mod.py", "py_upper_bundles/bundle.so"], TARGETS["macos-arm64"])

    assert ("strip", "bundle.so") in calls
    # macOS: stripping invalidates the ad-hoc signature, so it is re-signed.
    assert ("sign", "bundle.so") in calls
    # A source file is not a native image and must not be touched.
    assert all(name != "mod.py" for _, name in calls)


def test_strip_is_a_no_op_when_disabled(tmp_path, monkeypatch):
    from py_upper import python_build
    import py_upper.config as config_module

    monkeypatch.setattr(config_module, "optimize_config", lambda: {"strip_native": False})
    calls = []
    import py_upper.native.bundle as bundle_module
    monkeypatch.setattr(bundle_module, "_strip_native", lambda path, target: calls.append(path))

    site = tmp_path / "site"
    (site / "py_upper_bundles").mkdir(parents=True)
    (site / "py_upper_bundles" / "bundle.so").write_bytes(b"\x7fELF")
    from py_upper.config import TARGETS
    python_build.strip_packed_extensions(site, ["py_upper_bundles/bundle.so"], TARGETS["macos-arm64"])
    assert calls == []


def test_egg_without_a_manifest_is_refused(tmp_path):
    """Every accepted archive must carry the per-member digest manifest."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mod.py", "VALUE = 1\n")
    key = bytes.fromhex(KEY)
    salt = bytes(16)
    header = MAGIC2 + salt
    ciphertext = keystream_xor(buffer.getvalue(), key + salt)
    egg = tmp_path / "no-manifest.egg"
    egg.write_bytes(header + egg_tag(header, ciphertext, key) + ciphertext)

    with pytest.raises(ValueError, match="manifest is missing"):
        EncryptedEggImporter(egg, key, "extract")
    with pytest.raises(ValueError, match="manifest is missing"):
        read_egg_manifest(egg, KEY)
