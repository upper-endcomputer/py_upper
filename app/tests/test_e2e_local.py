from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def _make_local_runtime(repo: Path):
    py = Path(sys.executable).resolve()
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    import sysconfig
    runtime = repo / "runtimes" / "linux-x86_64" / version
    sdk = repo / "build" / "e2e-sdk" / "linux-x86_64" / version
    if runtime.exists(): shutil.rmtree(runtime)
    if sdk.exists(): shutil.rmtree(sdk)
    (runtime / "bin").mkdir(parents=True)
    (runtime / "lib").mkdir(parents=True)
    shutil.copyfile(py, runtime / "bin" / "python3.13")
    (runtime / "bin" / "python3.13").chmod(0o755)
    libpython = Path(sysconfig.get_config_var("LIBDIR")) / sysconfig.get_config_var("LDLIBRARY")
    shutil.copyfile(libpython, runtime / "lib" / libpython.name)
    stdlib = Path(sysconfig.get_path("stdlib"))
    shutil.copytree(stdlib, runtime / "lib" / "python3.13", symlinks=True)
    (sdk / "bin").mkdir(parents=True)
    (sdk / "include").mkdir(parents=True)
    (sdk / "bin" / "python3.13").symlink_to(runtime / "bin" / "python3.13")
    (sdk / "include" / "python3.13").symlink_to(sysconfig.get_path("include"))
    manifest = {
        "format": 4, "provider": "local", "target": "linux-x86_64", "target_triple": "x86_64-unknown-linux-gnu",
        "python": version, "python_major_minor": "3.13", "python_abi": "cp313",
        "python_platform_tag": "manylinux_2_17_x86_64", "python_implementation": "cpython",
        "runtime": {"root": "."}, "sdk": {"root": "configured"},
        "python_executable": "bin/python3.13", "include_dir": "include/python3.13",
    }
    (runtime / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return version


def _make_app_native_tree(repo: Path):
    import shutil
    native = repo / "app" / "src" / "config"
    native.mkdir(parents=True, exist_ok=True)
    cc = shutil.which("gcc") or shutil.which("cc")
    if not cc:
        raise RuntimeError("gcc/cc required for app-native E2E fixture")
    child_c = native / "child.c"
    parent_c = native / "parent.c"
    child = native / "libdemo_child.so"
    parent = native / "libdemo_parent.so"
    child_c.write_text('int demo_child(void) { return 7; }\n', encoding="utf-8")
    parent_c.write_text('extern int demo_child(void); int demo_parent(void) { return demo_child(); }\n', encoding="utf-8")
    subprocess.run([cc, "-shared", "-fPIC", "-O2", str(child_c), "-o", str(child)], check=True, cwd=repo)
    subprocess.run([cc, "-shared", "-fPIC", "-O2", str(parent_c), "-L", str(native), "-ldemo_child", "-Wl,-rpath,$ORIGIN", "-o", str(parent)], check=True, cwd=repo)
    child_c.unlink(); parent_c.unlink()


def _make_test_wheels(repo: Path):
    from test_third_party import make_wheel, make_native_wheel
    wheel_dir = repo / "build" / "e2e-wheelhouse"
    if wheel_dir.exists(): shutil.rmtree(wheel_dir)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    make_native_wheel(wheel_dir)
    make_wheel(wheel_dir, requires_dist="demo-native==1.0.0")
    return wheel_dir


@pytest.mark.skipif(os.environ.get("PY_UPPER_E2E") != "1", reason="set PY_UPPER_E2E=1 to run the full local build E2E")
def test_local_build_end_to_end_with_third_party_dependency():
    source_repo = Path(__file__).parents[2]
    temp_root = Path(tempfile.mkdtemp(prefix="py-upper-e2e-", dir="/tmp"))
    repo = temp_root / "repo"
    shutil.copytree(
        source_repo, repo,
        ignore=shutil.ignore_patterns(".git", ".cache", "build", "dist", "runtimes", "__pycache__", ".pytest_cache"),
        symlinks=True,
    )
    pyproject = repo / "app" / "pyproject.toml"
    main_py = repo / "app" / "src" / "main.py"
    version = _make_local_runtime(repo)
    _make_app_native_tree(repo)
    _make_test_wheels(repo)
    project = pyproject.read_text(encoding="utf-8")
    project = project.replace(
        'provider = "pbs"\npython = "3.13.15"',
        f'provider = "local"\npython = "{version}"\nruntime = "runtimes/{{target}}/{{python}}"\nsdk = "build/e2e-sdk/{{target}}/{{python}}"',
    )
    project = project.replace("dependencies = []", 'dependencies = ["demo-pkg==1.0.0"]')
    marker = "[tool.py_upper.cython]"
    dependency_table = '[tool.py_upper.dependencies]\nfind_links = ["build/e2e-wheelhouse"]\nno_index = true\n\n'
    project = project.replace(marker, dependency_table + marker, 1)
    pyproject.write_text(project, encoding="utf-8")
    original_main = main_py.read_text(encoding="utf-8")
    main_py.write_text(
        original_main.replace(
            'from core.app import run_application',
            'from core.app import run_application\nimport demo_pkg\nimport demo_native',
        ).replace(
            'def main() -> None:\n    run_application()',
            'def main() -> None:\n    assert demo_pkg.VALUE == "ok"\n    assert demo_native.value() == 42\n    run_application()',
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PY_UPPER_PYTHON"] = sys.executable
    env["PY_UPPER_E2E"] = "1"
    cmd = [sys.executable, "tools/build.py", "--run", "--target", "linux-x86_64"]
    try:
        result = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert "SMOKE PASS launcher" in result.stdout
        assert "Hello from py_upper" in result.stdout
        packaged = repo / "dist" / "MyApp" / "site-packages"
        assert (packaged / "demo_pkg" / "__init__.py").exists()
        assert any(path.name.startswith("demo_native") and path.suffix == ".so" for path in packaged.iterdir())
        assert not (packaged / "main.py").exists()
        wheel_manifest = json.loads((repo / "build" / "wheels" / "linux-x86_64" / "manifest.json").read_text(encoding="utf-8"))
        assert sorted(wheel_manifest["requirements"]) == ["demo-pkg==1.0.0"]
        wheel_names = {item["file"] for item in wheel_manifest["wheels"]}
        assert any(name.startswith("demo_pkg-") for name in wheel_names)
        assert any(name.startswith("demo_native-") for name in wheel_names)

        # Lock semantics are covered separately in the build-tool lock tests;
        # this test focuses on the complete target build/package/run path.
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
