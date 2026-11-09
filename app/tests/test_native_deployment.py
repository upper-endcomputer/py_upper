from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from py_upper.config import TARGETS
from py_upper import egg
from py_upper.native import bundle, deps


def _elf(path: Path, machine: int = 62, file_type: int = 3) -> Path:
    data = bytearray(64)
    data[:6] = b"\x7fELF\x02\x01"
    data[16:18] = file_type.to_bytes(2, "little")
    data[18:20] = machine.to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _macho(path: Path, file_type: int = 6) -> Path:
    data = bytearray(32)
    data[:4] = bytes.fromhex("cffaedfe")
    data[4:8] = (0x0100000C).to_bytes(4, "little")
    data[12:16] = file_type.to_bytes(4, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _fat_macho(path: Path, slices: list[bytes], endian: str = ">", fat64: bool = False) -> Path:
    magic = 0xCAFEBABF if fat64 else 0xCAFEBABE
    table = bytearray(struct.pack(endian + "II", magic, len(slices)))
    # Headers beyond the initial read exercise slice seeking, not accidental
    # parsing of bytes already read as part of the fat header.
    offset = 8192
    payload = bytearray(offset)
    for cpu, content in zip((0x01000007, 0x0100000C), slices):
        fields = (cpu, 0, offset, len(content), 0)
        table += struct.pack(endian + ("IIQQII" if fat64 else "IIIII"), *(fields + (0,) if fat64 else fields))
        payload.extend(content)
        offset += len(content)
    payload[:len(table)] = table
    path.write_bytes(payload)
    return path


def _pe(path: Path, imports=(), machine: int = 0x8664) -> Path:
    """A PE32+ image with real import descriptors, readable without host tools."""
    pe = 0x80
    optional = pe + 24
    table = optional + 0xF0
    data = bytearray(4096)
    data[:2] = b"MZ"
    data[0x3C:0x40] = pe.to_bytes(4, "little")
    data[pe:pe + 4] = b"PE\0\0"
    data[pe + 4:pe + 6] = machine.to_bytes(2, "little")
    data[pe + 6:pe + 8] = (1).to_bytes(2, "little")
    data[pe + 20:pe + 22] = (0xF0).to_bytes(2, "little")
    data[optional:optional + 2] = (0x20B).to_bytes(2, "little")
    data[optional + 120:optional + 124] = (0x1000).to_bytes(4, "little")
    data[table:table + 8] = b".idata\0\0"
    data[table + 12:table + 16] = (0x1000).to_bytes(4, "little")
    data[table + 16:table + 20] = (0xE00).to_bytes(4, "little")
    data[table + 20:table + 24] = (0x200).to_bytes(4, "little")
    text_offset = 0x400
    for index, name in enumerate(imports):
        descriptor = 0x200 + index * 20
        rva = 0x1000 + text_offset - 0x200
        data[descriptor + 12:descriptor + 16] = rva.to_bytes(4, "little")
        encoded = name.encode("ascii") + b"\0"
        data[text_offset:text_offset + len(encoded)] = encoded
        text_offset += len(encoded)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def closure_workspace(tmp_path, monkeypatch):
    root = tmp_path / "Upper"
    (root / "site-packages").mkdir(parents=True)
    monkeypatch.setattr(bundle, "BUILD", tmp_path / "build")
    monkeypatch.setattr(bundle, "app_name", lambda: "Upper")
    monkeypatch.setattr(bundle, "optimize_config", lambda: {})
    # These graph tests use header-only files, not executable host libraries.
    monkeypatch.setattr(bundle, "linux_host_libraries", lambda target: {})
    monkeypatch.setattr(bundle, "_relocate_linux_dependencies", lambda dependencies, copied: None)
    # Exercise the portable PE parser even on Windows hosts with dumpbin.
    monkeypatch.setattr(deps, "_dumpbin_dependencies", lambda path, env=None: [])
    return root


@pytest.mark.parametrize("tool,output", [
    ("readelf", " 0x1 (NEEDED) Shared library: [libdemo.so.1]\n 0xf (RPATH) Library rpath: [$ORIGIN/old]\n"),
    ("objdump", " NEEDED libdemo.so.1\n RPATH $ORIGIN/old\n"),
])
def test_elf_dynamic_entries_preserve_dependency_and_path_tags(tmp_path, monkeypatch, tool, output):
    from types import SimpleNamespace

    monkeypatch.setattr(deps.shutil, "which", lambda name: name if name == tool else None)
    monkeypatch.setattr(deps.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=output))
    assert deps.elf_dynamic_entries(tmp_path / "library") == [
        ("NEEDED", "libdemo.so.1"), ("RPATH", "$ORIGIN/old"),
    ]


def test_linux_host_cache_filters_foreign_architectures_and_system_libraries(tmp_path, monkeypatch):
    from types import SimpleNamespace

    target = TARGETS["linux-x86_64"]
    native = _elf(tmp_path / "native" / "libdemo.so.1")
    foreign = _elf(tmp_path / "foreign" / "libdemo.so.1", machine=183)
    optimized = _elf(tmp_path / "x86-64-v3" / "libdemo.so.1")
    libc = _elf(tmp_path / "libc.so.6")
    monkeypatch.setattr(deps, "host_target", lambda: target)
    monkeypatch.setattr(deps.shutil, "which", lambda *args, **kwargs: "ldconfig")
    output = (f'libdemo.so.1 (libc6, hwcap: "x86-64-v3") => {optimized}\n'
              f"libdemo.so.1 (libc6) => {foreign}\nlibdemo.so.1 (libc6) => {native}\nlibc.so.6 (libc6) => {libc}\n")
    monkeypatch.setattr(deps.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=output))
    assert deps.linux_host_libraries(target) == {"libdemo.so.1": native}


def test_linux_cross_build_never_reads_host_loader_cache(monkeypatch):
    monkeypatch.setattr(deps, "host_target", lambda: TARGETS["linux-arm64"])
    monkeypatch.setattr(deps.subprocess, "run", lambda *args, **kwargs: pytest.fail("queried foreign host"))
    assert deps.linux_host_libraries(TARGETS["linux-x86_64"]) == {}


def test_linux_closure_bundles_host_dependencies_transitively(closure_workspace, monkeypatch):
    root = closure_workspace
    owner = _elf(root / "site-packages" / "module.so")
    host = root.parent / "host"
    middle = _elf(host / "libmiddle.so.1")
    leaf = _elf(host / "libleaf.so.2")
    monkeypatch.setattr(bundle, "linux_host_libraries", lambda target: {middle.name: middle, leaf.name: leaf})
    graph = {owner.name: [middle.name], middle.name: [leaf.name], leaf.name: ["libc.so.6"]}
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env: graph.get(path.name, []))
    result = bundle.bundle_native_dependencies(root, TARGETS["linux-x86_64"])
    assert [(item.owner.name, item.resolved.name) for item in result] == [
        (owner.name, middle.name), (middle.name, leaf.name),
    ]
    assert (owner.parent / middle.name).read_bytes() == middle.read_bytes()
    assert (owner.parent / leaf.name).read_bytes() == leaf.read_bytes()
    assert not (owner.parent / "libc.so.6").exists()


@pytest.mark.parametrize("tag,force", [("RPATH", ["--force-rpath"]), ("RUNPATH", [])])
def test_linux_relocation_appends_paths_without_changing_existing_tag(tmp_path, monkeypatch, tag, force):
    owner = tmp_path / "libowner.so.1"
    copied = tmp_path / "deps" / "libchild.so.2"
    calls = []
    monkeypatch.setattr(bundle.shutil, "which", lambda name: "patchelf")
    monkeypatch.setattr(bundle, "elf_dynamic_entries", lambda path: [(tag, "$ORIGIN/old")] if path == owner else [])
    monkeypatch.setattr(bundle.subprocess, "run", lambda command, **kwargs: calls.append(command))
    bundle._relocate_linux_dependencies([deps.Dependency(owner, copied.name, copied, False)], {copied})
    assert calls == [["patchelf", *force, "--set-rpath", "$ORIGIN/old:$ORIGIN/deps", str(owner)]]


def test_linux_relocation_leaves_self_contained_wheel_paths_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle.subprocess, "run", lambda *args, **kwargs: pytest.fail("modified a self-contained wheel"))
    bundle._relocate_linux_dependencies([], set())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires native ELF tools")
def test_linux_bundled_external_chain_loads_after_removing_source(tmp_path, monkeypatch):
    from py_upper.config import host_target

    required = ("cc", "patchelf", "readelf")
    if not all(shutil.which(tool) for tool in required):
        pytest.skip("requires cc, patchelf and readelf")
    root = tmp_path / "package"
    site = root / "site-packages"
    host = tmp_path / "host"
    site.mkdir(parents=True)
    host.mkdir()
    leaf = host / "libfixture_leaf.so.1"
    middle = host / "libfixture_middle.so.1"
    owner = site / "module.so"
    for output, code, links in [
        (leaf, "int fixture_leaf(void) { return 7; }", []),
        (middle, "int fixture_leaf(void); int fixture_middle(void) { return fixture_leaf(); }", ["-l:" + leaf.name]),
        (owner, "int fixture_middle(void); int application(void) { return fixture_middle(); }", ["-l:" + middle.name, "-Wl,-rpath,$ORIGIN/old"]),
    ]:
        source = output.with_suffix(".c")
        source.write_text(code, encoding="utf-8")
        subprocess.run(["cc", "-shared", "-fPIC", str(source), "-L" + str(host), *links,
                        "-Wl,-soname," + output.name, "-o", str(output)], check=True, timeout=10)
    monkeypatch.setattr(bundle, "BUILD", tmp_path / "build")
    monkeypatch.setattr(bundle, "app_name", lambda: "Upper")
    monkeypatch.setattr(bundle, "optimize_config", lambda: {})
    monkeypatch.setattr(bundle, "linux_host_libraries", lambda target: {leaf.name: leaf, middle.name: middle})
    bundle.bundle_native_dependencies(root, host_target())
    assert ("RUNPATH", "$ORIGIN/old:$ORIGIN") in deps.elf_dynamic_entries(owner)
    host.rename(tmp_path / "hidden-host")
    script = "import ctypes,sys; assert ctypes.CDLL(sys.argv[1]).application() == 7"
    subprocess.run([sys.executable, "-I", "-c", script, str(owner)], check=True, cwd=tmp_path, timeout=10)


def test_native_discovery_uses_target_format_instead_of_filename_suffix(tmp_path):
    expected = [
        _elf(tmp_path / "extension.so"),
        _elf(tmp_path / "libdemo.so.1"),
        _elf(tmp_path / "libdemo.so.1.2"),
        _elf(tmp_path / "helper"),
    ]
    _pe(tmp_path / "foreign.so")
    (tmp_path / "data.so").write_text("ordinary data", encoding="utf-8")
    _elf(tmp_path / "symbols.dSYM" / "Contents" / "Resources" / "DWARF" / "helper")

    assert bundle._native_files(tmp_path, TARGETS["linux-x86_64"]) == sorted(expected)


def test_native_discovery_skips_relocatable_objects(tmp_path):
    elf_object = _elf(tmp_path / "config" / "python.o", file_type=1)
    macho_object = _macho(tmp_path / "config" / "python-mac.o", file_type=1)
    macho_library = _macho(tmp_path / "libpython.dylib")

    from py_upper.native.inspect import inspect

    assert inspect(elf_object).loadable is False
    assert inspect(macho_object).loadable is False
    assert bundle._native_files(tmp_path, TARGETS["linux-x86_64"]) == []
    assert bundle._native_files(tmp_path, TARGETS["macos-arm64"]) == [macho_library]


@pytest.mark.parametrize("endian,fat64", [(">", False), ("<", False), (">", True), ("<", True)])
def test_native_discovery_checks_universal_slices(tmp_path, endian, fat64):
    from py_upper.native.inspect import inspect, verify_arch

    obj = _macho(tmp_path / "object", file_type=1).read_bytes()
    dylib = _macho(tmp_path / "library").read_bytes()
    bitcode = b"\xde\xc0\x17\x0b" + bytes(28)
    objects = _fat_macho(tmp_path / "python.o", [obj, obj], endian, fat64)
    bitcodes = _fat_macho(tmp_path / "lto.o", [bitcode, bitcode], endian, fat64)
    library = _fat_macho(tmp_path / "universal.dylib", [dylib, dylib], endian, fat64)

    assert not inspect(objects).loadable
    assert not inspect(bitcodes).loadable
    assert inspect(library).loadable
    assert verify_arch(library, TARGETS["macos-arm64"]).arch == "universal"
    found = bundle._native_files(tmp_path, TARGETS["macos-arm64"])
    assert objects not in found and bitcodes not in found and library in found


@pytest.mark.parametrize("data", [bytes.fromhex("cffaedfe"), bytes.fromhex("cafebabe00000002")])
def test_native_discovery_ignores_truncated_headers(tmp_path, data):
    (tmp_path / "truncated").write_bytes(data)
    assert bundle._native_files(tmp_path, TARGETS["macos-arm64"]) == []


@pytest.mark.parametrize("name", ["libssl.so.3", "libcrypto.so.3", "libsqlite3.so.0", "libbz2.so.1.0"])
def test_linux_stdlib_dependencies_still_require_a_complete_runtime(closure_workspace, monkeypatch, name):
    library = _elf(closure_workspace / "runtime" / "lib" / "python3.13" / "lib-dynload" / "module.so")
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env: [name] if path == library else [])
    with pytest.raises(RuntimeError, match=name):
        bundle.bundle_native_dependencies(closure_workspace, TARGETS["linux-x86_64"])


def test_linux_e2e_fixture_copies_transitive_libraries_with_relative_rpaths(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import test_e2e_local as e2e

    runtime = tmp_path / "runtime"
    extension = _elf(runtime / "lib" / "python3.13" / "lib-dynload" / "_ssl.so")
    host = tmp_path / "host libs"
    ssl = _elf(host / "libssl.so.3")
    crypto = _elf(host / "libcrypto.so.3")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=f"libssl.so.3 => {ssl} (0x1)\n"
                                     f"libcrypto.so.3 => {crypto} (0x2)\n"
                                     "libc.so.6 => /lib/libc.so.6 (0x3)\n")

    monkeypatch.setattr(e2e.subprocess, "run", run)
    e2e._complete_linux_runtime(runtime, TARGETS["linux-x86_64"])

    assert (runtime / "lib" / ssl.name).read_bytes() == ssl.read_bytes()
    assert (runtime / "lib" / crypto.name).read_bytes() == crypto.read_bytes()
    assert not (runtime / "lib" / "libc.so.6").exists()
    assert ["patchelf", "--set-rpath", "$ORIGIN/../..", str(extension)] in calls
    assert ["patchelf", "--set-rpath", "$ORIGIN", str(runtime / "lib" / ssl.name)] in calls


def test_linux_e2e_fixture_reports_missing_host_dependencies(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import test_e2e_local as e2e

    _elf(tmp_path / "lib" / "python3.13" / "lib-dynload" / "_ssl.so")
    monkeypatch.setattr(e2e.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="libssl.so.3 => not found\n"))
    with pytest.raises(RuntimeError, match="E2E host runtime dependency missing.*libssl"):
        e2e._complete_linux_runtime(tmp_path, TARGETS["linux-x86_64"])


def test_scan_tree_checks_versioned_elf_dependencies(tmp_path, monkeypatch):
    library = _elf(tmp_path / "libdemo.so.2")
    monkeypatch.setattr(deps, "dependency_names", lambda path, env: ["libmissing.so.1"])

    result = deps.scan_tree(tmp_path, TARGETS["linux-x86_64"])

    assert [(item.owner, item.name, item.external) for item in result] == [
        (library, "libmissing.so.1", True),
    ]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs Windows developer mode")
def test_resolver_preserves_soname_symlink_alias(tmp_path):
    actual = _elf(tmp_path / "libdemo.so.1.2")
    (tmp_path / "libdemo.so.1").symlink_to(actual.name)

    resolver = deps.DependencyResolver([tmp_path])

    assert resolver.resolve("libdemo.so.1") == actual.resolve()
    assert resolver.resolve("libdemo.so.1.2") == actual.resolve()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs Windows developer mode")
def test_closure_accepts_a_symlinked_package_root(closure_workspace, monkeypatch):
    root = closure_workspace
    _elf(root / "site-packages" / "module.so")
    alias = root.with_name("linked-package")
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env: ["libmissing.so.1"])

    with pytest.raises(RuntimeError, match=r"site-packages[/\\]module.so: libmissing.so.1"):
        bundle.bundle_native_dependencies(alias, TARGETS["linux-x86_64"])


def test_closure_follows_dependencies_already_inside_package(closure_workspace, monkeypatch):
    root = closure_workspace
    parent = _elf(root / "site-packages" / "module.so")
    middle = _elf(root / "runtime" / "lib" / "libmiddle.so.1")
    child = _elf(root / "resources" / "libchild.so.2")
    graph = {
        parent: ["libmiddle.so.1"],
        middle: ["libchild.so.2"],
        child: ["libmissing.so.3"],
    }
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env: graph.get(path, []))

    with pytest.raises(RuntimeError, match=r"resources[/\\]libchild.so.2: libmissing.so.3"):
        bundle.bundle_native_dependencies(root, TARGETS["linux-x86_64"])


def test_closure_reuses_a_shared_external_library(closure_workspace, monkeypatch):
    root = closure_workspace
    first = _elf(root / "site-packages" / "first.so")
    second = _elf(root / "site-packages" / "second.so")
    external = _elf(root.parent / "external" / "libshared.so")
    graph = {first: [str(external)], second: [str(external)]}
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env: graph.get(path, []))

    result = bundle.bundle_native_dependencies(root, TARGETS["linux-x86_64"])

    copied = root / "site-packages" / external.name
    assert copied.read_bytes() == external.read_bytes()
    assert [(item.owner, item.resolved) for item in result] == [
        (first, copied), (second, copied),
    ]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs Windows developer mode")
def test_linux_closure_copies_external_library_under_its_load_name(closure_workspace, monkeypatch):
    root = closure_workspace
    module = _elf(root / "site-packages" / "module.so")
    external = _elf(root.parent / "external" / "libdevice.so.1.2")
    load_name = external.with_name("libdevice.so.1")
    load_name.symlink_to(external.name)
    monkeypatch.setattr(
        bundle, "dependency_names",
        lambda path, env: [str(load_name)] if path == module else [],
    )

    result = bundle.bundle_native_dependencies(root, TARGETS["linux-x86_64"])

    copied = module.parent / load_name.name
    assert copied.is_file()
    assert copied.read_bytes() == external.read_bytes()
    assert [(item.owner, item.name, item.resolved) for item in result] == [
        (module, str(load_name), copied),
    ]
    assert not (module.parent / external.name).exists()


def test_closure_rejects_different_libraries_with_the_same_basename(closure_workspace, monkeypatch):
    root = closure_workspace
    first = _elf(root / "site-packages" / "first.so")
    second = _elf(root / "site-packages" / "second.so")
    external = _elf(root.parent / "external" / "libshared.so")
    conflicting = _elf(root.parent / "other" / "libshared.so")
    conflicting.write_bytes(conflicting.read_bytes() + b"different build")
    graph = {first: [str(external)], second: [str(conflicting)]}
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env: graph.get(path, []))

    with pytest.raises(RuntimeError, match="Native dependency basename collision"):
        bundle.bundle_native_dependencies(root, TARGETS["linux-x86_64"])


@pytest.mark.parametrize("name", ["vcruntime140.dll", "VCRUNTIME140_1.dll"])
def test_vc_runtime_is_a_required_bundle_dependency(closure_workspace, name):
    root = closure_workspace
    _pe(root / "site-packages" / "extension.pyd", [name])

    assert not deps._system_dependency(name, TARGETS["windows-x86_64"])
    with pytest.raises(RuntimeError, match=name):
        bundle.bundle_native_dependencies(root, TARGETS["windows-x86_64"])


def test_windows_installer_apis_are_system_provided(closure_workspace):
    """CPython 3.8-3.12 ship ``_msi.pyd`` in the embeddable runtime.

    It imports ``msi.dll`` and ``Cabinet.dll`` from System32. Treating those as
    wheel payload makes every Windows package with those Python lines fail the
    native closure even though the OS provides both libraries.
    """
    root = closure_workspace
    _pe(root / "runtime" / "_msi.pyd", ["msi.dll", "Cabinet.dll"])

    assert bundle.bundle_native_dependencies(root, TARGETS["windows-x86_64"]) == []


def test_closure_resolves_vc_runtime_from_bundled_python(closure_workspace):
    root = closure_workspace
    extension = _pe(root / "site-packages" / "extension.pyd", ["VCRUNTIME140.dll"])
    runtime = _pe(root / "runtime" / "vcruntime140.dll", ["KERNEL32.dll"])

    result = bundle.bundle_native_dependencies(root, TARGETS["windows-x86_64"])

    assert [(item.owner, item.name, item.resolved) for item in result] == [
        (extension, "VCRUNTIME140.dll", runtime),
    ]


def test_runtime_libraries_are_closure_roots(closure_workspace):
    root = closure_workspace
    _pe(root / "runtime" / "python313.dll", ["VCRUNTIME140.dll"])

    with pytest.raises(RuntimeError, match=r"runtime[/\\]python313.dll: VCRUNTIME140.dll"):
        bundle.bundle_native_dependencies(root, TARGETS["windows-x86_64"])


def test_foreign_arch_runtime_is_rejected_without_validating_tool_stubs(closure_workspace):
    root = closure_workspace
    _pe(root / "runtime" / "Lib" / "venv" / "scripts" / "python.exe", machine=0x14C)
    _pe(root / "runtime" / "vcruntime140.dll", machine=0x14C)

    with pytest.raises(RuntimeError, match=r"vcruntime140.dll: x86, expected x86_64"):
        bundle.bundle_native_dependencies(root, TARGETS["windows-x86_64"])

    (root / "runtime" / "vcruntime140.dll").unlink()
    assert bundle.bundle_native_dependencies(root, TARGETS["windows-x86_64"]) == []


@pytest.mark.parametrize("native_name", ["libdevice.so.1", "device-helper"])
def test_egg_leaves_native_inputs_visible_to_the_closure(closure_workspace, monkeypatch, native_name):
    root = closure_workspace
    site = root / "site-packages"
    app = root.parent / "app"
    source = app / "src"
    native = _elf(source / "drivers" / native_name)
    (source / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    # A similar filename with ordinary data must still be packed as data.
    (source / "settings.so.1").write_text("plain text settings\n", encoding="utf-8")
    shutil.copytree(source, site, dirs_exist_ok=True)
    (site / "main.py").unlink()
    compiled = _elf(site / "py_upper_bundles" / "py_upper_main.so")
    monkeypatch.setattr(egg, "APP", app)

    files = egg.application_files(site, {compiled.relative_to(site).as_posix()})

    assert files == ["py_upper_bundles/py_upper_main.so", "settings.so.1"]
    config = egg.EggConfig()
    archive = egg.pack_egg(site, root / "bundle" / egg.EGG_NAME, files, config)
    egg.remove_packed_files(site, files)
    assert egg.read_egg_members(archive, config.key_hex) == tuple(files)
    assert (site / native.relative_to(source)).is_file()
    assert not compiled.exists()
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env: ["libmissing.so.1"])
    with pytest.raises(RuntimeError, match="libmissing.so.1"):
        bundle.bundle_native_dependencies(root, TARGETS["linux-x86_64"])
