"""Directory-level native merging: grouping, symbol rewriting and layout checks.

macOS charges a code-signing registration for every Mach-O image a process
maps. A thousand one-module extensions therefore cost minutes on a cold start;
one bundle per top-level package costs three image loads. These tests pin the
parts that make that legal: package markers stay source, every Cython entry
point gets a unique name, and the launcher registers the table before the
interpreter exists.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from py_upper import python_build, verify
from py_upper.config import TARGETS
from py_upper.egg import EggConfig, pack_egg, read_egg_members
from py_upper.toolchain import Toolchain


def _source(app_root: Path, relative: str) -> Path:
    path = app_root / "src" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")
    return path


def _cython_c(directory: Path, name: str, basename: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"/* generated */\n"
        f"PyObject* PyInit_{basename}(void) {{ return 0; }}\n",
        encoding="utf-8",
    )
    return path


def test_merged_groups_leave_package_markers_out(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    monkeypatch.setattr(python_build, "APP", app_root)
    sources = [
        _source(app_root, "main.py"),
        _source(app_root, "pkg/__init__.py"),
        _source(app_root, "pkg/a.py"),
        _source(app_root, "pkg/sub/__init__.py"),
        _source(app_root, "pkg/sub/b.py"),
        _source(app_root, "utils/__init__.py"),
        _source(app_root, "utils/paths.py"),
    ]

    groups = python_build.merged_groups(sources)

    assert sorted(groups) == ["_root", "pkg", "utils"]
    assert [python_build.module_name(path) for path in groups["_root"]] == ["main"]
    assert [python_build.module_name(path) for path in groups["pkg"]] == ["pkg.a", "pkg.sub.b"]
    assert [python_build.module_name(path) for path in groups["utils"]] == ["utils.paths"]


def test_entry_point_rewrite_is_unique_and_rejects_ambiguous_units(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    source = _source(app_root, "demo.py")
    monkeypatch.setattr(python_build, "APP", app_root)
    text = "PyObject* PyInit_demo(void) { return 0; }\n"

    renamed = python_build._renamed_translation_unit(text, "PyInit_py_upper_demo_abc", source)

    assert "PyInit_demo" not in renamed
    assert "PyInit_py_upper_demo_abc" in renamed
    with pytest.raises(RuntimeError, match="no PyInit"):
        python_build._renamed_translation_unit("/* empty */\n", "PyInit_x", source)
    with pytest.raises(RuntimeError, match="more than one"):
        python_build._renamed_translation_unit(
            "PyObject* PyInit_a(void);\nPyObject* PyInit_b(void);\n", "PyInit_x", source
        )


def test_registration_table_names_every_module(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    first = _source(app_root, "pkg/a.py")
    second = _source(app_root, "pkg/b.py")
    generated = {
        first: _cython_c(tmp_path / "gen", "a.c", "a"),
        second: _cython_c(tmp_path / "gen", "b.c", "b"),
    }

    inputs, register = python_build.prepare_merged_group(TARGETS["linux-x86_64"], "pkg", [first, second], generated)

    assert len(inputs) == 2
    text = register.read_text(encoding="utf-8")
    assert "(char*)\"pkg.a\"" in text
    assert "(char*)\"pkg.b\"" in text
    assert "PyImport_ExtendInittab" in text
    assert "py_upper_register_modules" in text
    for path in inputs:
        assert "PyInit_a(" not in path.read_text(encoding="utf-8")
        assert "PyInit_b(" not in path.read_text(encoding="utf-8")


def test_merged_compile_links_every_translation_unit_once(tmp_path, monkeypatch):
    target = TARGETS["linux-x86_64"]
    include = tmp_path / "sdk" / "include"
    include.mkdir(parents=True)
    target_python = SimpleNamespace(include_dir=include, extension_suffix=".cpython-313-x86_64-linux-gnu.so")
    toolchain = Toolchain(target, {}, "gcc", "g++", "gcc", None)
    app_root = tmp_path / "app"
    first = _source(app_root, "pkg/a.py")
    second = _source(app_root, "pkg/b.py")
    generated = {
        first: _cython_c(tmp_path / "gen", "a.c", "a"),
        second: _cython_c(tmp_path / "gen", "b.c", "b"),
    }
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append([str(token) for token in cmd])
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "resolve_target_python", lambda value: target_python)
    monkeypatch.setattr(python_build, "resolve_toolchain", lambda value: toolchain)
    monkeypatch.setattr(python_build.subprocess, "run", fake_run)

    outputs = python_build.compile_merged_unix_extensions(target, {"pkg": [first, second]}, generated)
    second_run = python_build.compile_merged_unix_extensions(target, {"pkg": [first, second]}, generated)

    assert outputs == second_run
    assert [path.name for path in outputs] == ["py_upper_pkg.cpython-313-x86_64-linux-gnu.so"]
    assert len(commands) == 1
    cmd = commands[0]
    linked = [Path(token).name for token in cmd if token.endswith(".c")]
    assert linked == ["0000_pkg_a.c", "0001_pkg_b.c", "register.c"]
    assert outputs[0].parent.name == python_build.BUNDLE_DIR


def test_merge_mode_cythonizes_only_leaves(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    marker = _source(app_root, "pkg/__init__.py")
    leaf = _source(app_root, "pkg/a.py")
    generated = {leaf: _cython_c(tmp_path / "gen", "a.c", "a")}
    seen: dict[str, object] = {}

    def fake_cython(sources, host):
        seen["sources"] = list(sources)
        return generated

    def fake_merged(target, groups, generated_arg, host):
        seen["groups"] = groups
        return []

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "cython_merge_mode", lambda: "directory")
    monkeypatch.setattr(python_build, "require_local_python", lambda: Path("/fake/python3"))
    monkeypatch.setattr(python_build, "cythonize_to_c", fake_cython)
    monkeypatch.setattr(python_build, "build_merged_extensions", fake_merged)

    assert python_build.build_target_extensions(TARGETS["linux-x86_64"], [marker, leaf]) == []
    assert seen["sources"] == [leaf]
    assert seen["groups"] == {leaf.parent.name: [leaf]}


@pytest.mark.skipif(shutil.which("c++") is None, reason="no C++ compiler")
def test_native_egg_reader_matches_the_python_egg_format(tmp_path):
    from py_upper.egg_loader import decrypt_if_needed

    config = EggConfig()
    relative = "py_upper_bundles/py_upper_pkg.so"
    payload = b"\x7fELF" + bytes(range(256)) * 4
    site = tmp_path / "site"
    (site / "pkg").mkdir(parents=True)
    (site / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site / relative).parent.mkdir(parents=True)
    (site / relative).write_bytes(payload)
    egg = pack_egg(site, tmp_path / "py_upper.dat", ["pkg/mod.py", relative], config)

    source = tmp_path / "probe.cpp"
    source.write_text(
        '#include "egg_bundles.h"\n'
        '#include <iostream>\n'
        'int main(int argc, char** argv) {\n'
        '    std::vector<pyupper::EggBundleFile> files;\n'
        '    std::string key, error;\n'
        '    if (!pyupper::read_egg_bundles(argv[1], argv[2], "py_upper_bundles/", files, key, error)) {\n'
        '        std::cout << "ERROR " << error << "\\n";\n'
        '        return 1;\n'
        '    }\n'
        '    std::cout << "KEY " << key << "\\n";\n'
        '    for (const auto& file : files) {\n'
        '        std::cout << "FILE " << file.name << " " << file.data.size() << "\\n";\n'
        '    }\n'
        '    return 0;\n'
        '}\n',
        encoding="utf-8",
    )
    include = Path(__file__).parents[2] / "launcher" / "src"
    binary = tmp_path / "probe"
    subprocess.run(
        [shutil.which("c++"), "-std=c++17", "-I" + str(include), str(source), "-o", str(binary)],
        check=True,
        capture_output=True,
    )

    result = subprocess.run([str(binary), str(egg), config.key_hex], check=True, capture_output=True, text=True)
    plaintext = decrypt_if_needed(egg.read_bytes(), bytes.fromhex(config.key_hex))
    expected_key = hashlib.shake_256(plaintext).hexdigest(16)
    assert f"KEY {expected_key}" in result.stdout
    assert f"FILE {relative} {len(payload)}" in result.stdout
    assert "pkg/mod.py" not in result.stdout

    bad = subprocess.run([str(binary), str(egg), "00" * 16], capture_output=True, text=True)
    assert bad.returncode == 1
    assert "not a zip archive" in bad.stdout


def test_merged_layout_keeps_markers_and_reads_bundles_from_egg(tmp_path, monkeypatch):
    app_root = tmp_path / "app"
    marker = _source(app_root, "pkg/__init__.py")
    leaf = _source(app_root, "pkg/a.py")
    site = tmp_path / "site-packages"
    (site / "pkg").mkdir(parents=True)
    (site / "pkg" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    relative = "py_upper_bundles/py_upper_pkg.cp313-darwin.so"
    bundle = site / relative
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"bundle")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(verify, "APP", app_root)
    monkeypatch.setattr(verify, "cython_merge_mode", lambda: "directory")
    monkeypatch.setattr(verify, "runtime_spec", lambda target: SimpleNamespace(major_minor="3.13", abi_tag="cp313"))
    monkeypatch.setattr(verify, "target_extension_suffix", lambda target, major, abi: ".cp313-darwin.so")
    monkeypatch.setattr(verify, "verify_arch", lambda path, target: None)

    # A build without an egg still has a loose bundle, and verification accepts it.
    checks = verify._application_source_checks(site, [marker, leaf], TARGETS["macos-arm64"], None, "")
    assert all(ok for ok, _ in checks), checks

    config = EggConfig()
    egg = pack_egg(site, tmp_path / "bundle" / "py_upper.dat", ["pkg/__init__.py", relative], config)
    egg_members = set(read_egg_members(egg, config.key_hex))
    assert relative in egg_members

    (site / "pkg" / "__init__.py").unlink()
    bundle.unlink()
    bundle.parent.rmdir()
    checks = verify._application_source_checks(
        site, [marker, leaf], TARGETS["macos-arm64"], egg_members, "",
        egg_path=egg, egg_key=config.key_hex,
    )
    assert all(ok for ok, _ in checks), checks
    assert any("merged bundle stored" in label for ok, label in checks if ok)


def test_launcher_registers_bundles_before_interpreter_initialization():
    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    text = launcher.read_text(encoding="utf-8")
    assert "py_upper_bundles" in text
    assert "py_upper_register_modules" in text
    assert text.index("load_module_bundles(site, egg, runtime)") < text.index("PyStatus status = init(&config)")
