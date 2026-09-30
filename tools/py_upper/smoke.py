from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .config import BUILD, Target, app_name, entry_module, host_target, python_version, target_runtime_dir
from .python_build import module_name, selected_sources
from .third_party import WHEEL_MANIFEST, wheel_dir


def runtime_python(target: Target) -> Path:
    root = target_runtime_dir(target)
    names = ["python.exe", "python3.exe"] if target.os == "windows" else [
        f"python{python_version()}", f"python{'.'.join(python_version().split('.')[:2])}", "python3", "python"
    ]
    for name in names:
        for candidate in (root / "bin" / name, root / "install" / "bin" / name, root / name):
            if candidate.is_file():
                return candidate
    for name in names:
        for candidate in root.rglob(name):
            if candidate.is_file():
                return candidate
    raise RuntimeError(f"Target Python executable not found under {root}")


def direct_dependency_imports(target: Target) -> list[str]:
    path = wheel_dir(target) / WHEEL_MANIFEST
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    values = data.get("imports", [])
    return sorted({str(value).strip() for value in values if str(value).strip()}) if isinstance(values, list) else []


def smoke_modules(target: Target) -> list[str]:
    # Import the application entry point and every directly declared third-party
    # distribution. Imported application modules are exercised transitively by
    # the entry point; importing every Cython module independently would turn
    # optional/lazy application modules into false build failures.
    values = [entry_module()] + direct_dependency_imports(target)
    return sorted({value for value in values if value})


def write_smoke_script(target: Target, destination: Path) -> Path:
    modules = smoke_modules(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "import importlib\n"
        f"modules = {modules!r}\n"
        "for name in modules:\n"
        "    importlib.import_module(name)\n"
        "print('SMOKE PASS', len(modules))\n",
        encoding="utf-8",
    )
    return destination


def run_target_python_smoke(target: Target) -> None:
    if target != host_target():
        print("SKIP target runtime smoke: cross target")
        return
    stage_root = BUILD / "staging" / target.key
    site = stage_root / "site-packages"
    script = write_smoke_script(target, BUILD / "smoke" / target.key / "target.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site)
    env["PYTHONHOME"] = str(target_runtime_dir(target))
    env["PYTHONNOUSERSITE"] = "1"
    py = runtime_python(target)
    print("+", py, script)
    subprocess.run([str(py), str(script)], env=env, cwd=stage_root, check=True)


def run_launcher_smoke(target: Target, launcher: Path) -> None:
    name = app_name()
    script_name = f"{name}.smoke.int"
    if target.os == "macos":
        expected = launcher.parent.parent / "Resources" / script_name
    else:
        expected = launcher.parent / script_name
    if not expected.exists():
        raise RuntimeError(f"Packaged launcher smoke script missing: {expected}")
    env = dict(os.environ)
    env["PY_UPPER_SMOKE"] = "1"
    print("+", launcher, "[launcher smoke]")
    subprocess.run([str(launcher)], env=env, cwd=launcher.parent, check=True)
