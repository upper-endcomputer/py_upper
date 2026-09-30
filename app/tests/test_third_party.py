from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path


def make_wheel(root: Path, name="demo_pkg", version="1.0.0", payload="ok", requires_dist: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    filename = f"{name}-{version}-py3-none-any.whl"
    wheel = root / filename
    dist = f"{name}-{version}.dist-info"
    entries = {
        f"{name}/__init__.py": f"VALUE={payload!r}\n".encode(),
        f"{dist}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name.replace('_','-')}\nVersion: {version}\n"
            + (f"Requires-Dist: {requires_dist}\n" if requires_dist else "")
        ).encode(),
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist}/top_level.txt": f"{name}\n".encode(),
    }
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        records=[]
        for path, data in entries.items():
            archive.writestr(path, data)
            digest=__import__('base64').urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            records.append(f"{path},sha256={digest},{len(data)}")
        records.append(f"{dist}/RECORD,,")
        archive.writestr(f"{dist}/RECORD", "\n".join(records)+"\n")
    return wheel


def test_wheel_extraction_and_direct_import_discovery(tmp_path):
    from py_upper.third_party import _extract_wheel, _write_manifest, _wheel_files
    from py_upper.config import TARGETS

    wheel = make_wheel(tmp_path)
    site = tmp_path / "site-packages"
    site.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    _extract_wheel(wheel, site, staging)
    assert (site / "demo_pkg" / "__init__.py").exists()
    from py_upper.third_party import _top_level_imports
    dist = site / "demo_pkg-1.0.0.dist-info"
    assert _top_level_imports(dist) == ["demo_pkg"]


def test_wheel_collision_with_different_content_fails(tmp_path):
    from py_upper.third_party import _extract_wheel

    a = make_wheel(tmp_path / "a")
    b = make_wheel(tmp_path / "b", payload="different")
    site = tmp_path / "site"
    site.mkdir()
    staging = site.parent / "staging"
    staging.mkdir()
    _extract_wheel(a, site, staging)
    try:
        _extract_wheel(b, site, staging)
    except RuntimeError as exc:
        assert "collision" in str(exc).lower()
    else:
        raise AssertionError("expected wheel collision")


def test_pip_target_arguments_use_target_compatibility_set():
    from py_upper.third_party import _pip_args
    from py_upper.config import TARGETS

    args = _pip_args(TARGETS["macos-arm64"])
    platforms = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--platform"]
    assert platforms == list(TARGETS["macos-arm64"].wheel_platforms)
    assert args.count("--abi") == 3


def test_pip_target_arguments_honor_offline_find_links(monkeypatch):
    from py_upper.third_party import _pip_args
    from py_upper.config import TARGETS
    import py_upper.third_party as third_party

    monkeypatch.setattr(third_party, "dependency_config", lambda: {"no_index": True, "find_links": ["wheelhouse"]})
    args = _pip_args(TARGETS["linux-x86_64"])
    assert "--no-index" in args
    assert "--find-links" in args
    assert str((third_party.ROOT / "wheelhouse").resolve()) in args


def make_native_wheel(root: Path, name="demo_native", version="1.0.0") -> Path:
    import base64
    import hashlib
    import subprocess
    import sysconfig
    import shutil

    root.mkdir(parents=True, exist_ok=True)
    import_name = name.replace('-', '_')
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    source = root / f"{import_name}.c"
    binary = root / f"{import_name}{suffix}"
    source.write_text(
        '''#include <Python.h>\n\nstatic PyObject *value(PyObject *self, PyObject *args) {\n    (void)self; (void)args;\n    return PyLong_FromLong(42);\n}\n\nstatic PyMethodDef methods[] = {\n    {"value", value, METH_NOARGS, "return 42"},\n    {NULL, NULL, 0, NULL}\n};\n\nstatic struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "demo_native", NULL, -1, methods};\nPyMODINIT_FUNC PyInit_demo_native(void) { return PyModule_Create(&module); }\n''',
        encoding="utf-8",
    )
    cc = shutil.which("gcc") or shutil.which("cc")
    if not cc:
        raise RuntimeError("gcc/cc required for native wheel fixture")
    include = sysconfig.get_path("include")
    subprocess.run([cc, "-shared", "-fPIC", "-O2", "-DNDEBUG", f"-I{include}", str(source), "-o", str(binary)], check=True)
    tag = f"cp{sysconfig.get_config_var('py_version_nodot')}-cp{sysconfig.get_config_var('py_version_nodot')}-manylinux_2_17_x86_64"
    filename = f"{name}-{version}-{tag}.whl"
    wheel = root / filename
    dist = f"{name}-{version}.dist-info"
    entries = {
        binary.name: binary.read_bytes(),
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode(),
        f"{dist}/WHEEL": f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: false\nTag: {tag}\n".encode(),
        f"{dist}/top_level.txt": f"{import_name}\n".encode(),
    }
    import zipfile
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        records=[]
        for path, data in entries.items():
            archive.writestr(path, data)
            digest=base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            records.append(f"{path},sha256={digest},{len(data)}")
        records.append(f"{dist}/RECORD,,")
        archive.writestr(f"{dist}/RECORD", "\n".join(records)+"\n")
    source.unlink()
    binary.unlink()
    return wheel


def test_native_wheel_fixture_has_target_extension_and_import_metadata(tmp_path):
    wheel = make_native_wheel(tmp_path)
    assert "cp313-cp313-manylinux_2_17_x86_64" in wheel.name


def test_direct_import_fallback_does_not_require_top_level_txt(tmp_path):
    from py_upper.third_party import _top_level_imports

    dist = tmp_path / "some-distribution-1.0.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: some-distribution\nVersion: 1.0.0\n", encoding="utf-8")
    (tmp_path / "actual_pkg").mkdir()
    (tmp_path / "other_module.py").write_text("x=1\n", encoding="utf-8")
    assert _top_level_imports(dist) == ["actual_pkg", "other_module"]
