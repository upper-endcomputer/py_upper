from __future__ import annotations

import importlib
import importlib.machinery
import io
import sys
import types
import zipfile
from pathlib import Path

import pytest

from py_upper import egg as egg_module
from py_upper.egg import (
    EGG_DIR, EGG_META_NAME, EGG_NAME, EggConfig, application_files, egg_config,
    egg_loader_source, pack_egg, read_egg_members, read_egg_meta,
    remove_packed_files, write_egg_meta,
)
from py_upper.egg_loader import (
    DEFAULT_KEY_HEX, MAGIC, EncryptedEggImporter, decrypt_if_needed, install,
    keystream_xor,
)

KEY = DEFAULT_KEY_HEX
EXT = importlib.machinery.EXTENSION_SUFFIXES[0]


@pytest.fixture(autouse=True)
def isolated_egg_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PY_UPPER_EGG_CACHE", str(tmp_path / "egg-cache"))


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


def test_pack_egg_writes_magic_and_encrypted_zip(tmp_path):
    egg = make_egg(tmp_path, {"pkg/__init__.py": "", "pkg/mod.py": "VALUE = 42\n"})
    raw = egg.read_bytes()
    assert raw[:len(MAGIC)] == MAGIC
    # The payload is obfuscated, so the plaintext member name is not visible.
    assert b"pkg/mod.py" not in raw
    assert read_egg_members(egg, KEY) == ("pkg/__init__.py", "pkg/mod.py")


def test_pack_egg_is_deterministic(tmp_path):
    files = {"pkg/__init__.py": "", "pkg/mod.py": "VALUE = 1\n"}
    first = make_egg(tmp_path, files, "first.egg")
    second = make_egg(tmp_path, files, "second.egg")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(io.BytesIO(decrypt_if_needed(first.read_bytes(), bytes.fromhex(KEY)))) as archive:
        assert archive.namelist() == ["pkg/__init__.py", "pkg/mod.py"]
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


@pytest.mark.parametrize("compression", ["stored", "deflated", "lzma"])
def test_pack_egg_supports_each_compression(tmp_path, compression):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 7\n"}, name=compression + ".egg", compression=compression)
    assert read_egg_members(egg, KEY) == ("mod.py",)


def test_read_egg_members_accepts_a_plain_zip(tmp_path):
    path = tmp_path / "plain.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mod.py", "VALUE = 1\n")
    assert read_egg_members(path, KEY) == ("mod.py",)


def test_read_egg_members_rejects_a_wrong_key(tmp_path):
    egg = make_egg(tmp_path, {"mod.py": "VALUE = 1\n"})
    with pytest.raises(zipfile.BadZipFile):
        read_egg_members(egg, "00" * 16)


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
    egg = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(egg, "w") as archive:
        archive.writestr("../escape.py", "VALUE = 1\n")

    with pytest.raises(ValueError, match="unsafe egg member"):
        EncryptedEggImporter(egg, bytes.fromhex(KEY), "extract")

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
    assert 'kEggKey = ""' in text
    assert 'kEggBackend = "auto"' in text

    config = EggConfig(enabled=True, key_hex=KEY, backend="extract")
    enabled = write_embedded_source(tmp_path / "on", TARGETS["windows-x86_64"], config)
    text = enabled.read_text(encoding="utf-8")
    assert "kEggLoader = R\"pyupper(" in text
    assert "extern const char* const kEggLoader;" in text
    assert '"' + KEY + '"' in text
    assert '"extract"' in text
    assert "MAGIC = b'WBEGG01!'" in text


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
    (tmp_path / EGG_DIR / EGG_NAME).write_bytes(MAGIC)
    assert verify_module._stage_egg_config(tmp_path).enabled is True
