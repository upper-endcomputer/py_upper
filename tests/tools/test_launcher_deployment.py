"""Compile the real launcher helpers and exercise deployment path semantics."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["embedded", "crash", "sidecar"])
def test_bootstrap_only_imports_failure_and_sidecar_helpers_when_needed(tmp_path, mode):
    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    body = launcher.read_text(encoding="utf-8").split('R"PYUPPER(', 1)[1].split(')PYUPPER"', 1)[0]
    report = tmp_path / "crash.log"
    script = tmp_path / "entry.py"
    script.write_text("print('sidecar reached')", encoding="utf-8")
    code = {
        "embedded": "import sys\nassert not {'traceback', 'linecache', 'runpy'} & sys.modules.keys()\n",
        "crash": "raise RuntimeError('entry failed')\n",
        "sidecar": None,
    }[mode]
    variables = {
        "ENTRY_FILE": str(tmp_path / "compiled-launcher"),
        "ENTRY_SCRIPT": str(script),
        "ENTRY_SOURCE": code,
        "CRASH_REPORT": str(report),
        "EGG_LOADER_SOURCE": None,
        "LAUNCHER_ENV": {},
    }
    prelude = "".join(f"{name} = {value!r}\n" for name, value in variables.items())
    result = subprocess.run(
        [sys.executable, "-IS", "-"], input=prelude + body, text=True,
        capture_output=True, timeout=10,
    )
    if mode == "crash":
        assert result.returncode != 0
        text = report.read_text(encoding="utf-8")
        assert "raise RuntimeError('entry failed')" in text
        assert "RuntimeError: entry failed" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert not report.exists()
        if mode == "sidecar":
            assert "sidecar reached" in result.stdout


@pytest.fixture(scope="module")
def launcher_probe(tmp_path_factory):
    root = tmp_path_factory.mktemp("launcher-paths") / "安装 目录"
    root.mkdir()
    launcher = Path(__file__).parents[2] / "launcher" / "src"
    source = root / "probe.cpp"
    source.write_text(
        '#include "PyUpper.cpp"\n'
        'int main() {\n'
        '  std::cout << path_to_utf8(executable_path()) << "\\n";\n'
        '  std::cout << path_to_utf8(bundle_cache_root()) << "\\n";\n'
        '  return 0;\n'
        '}\n',
        encoding="utf-8",
    )
    probe = root / ("probe.exe" if os.name == "nt" else "probe")
    include = sysconfig.get_path("include")
    env = None
    if os.name == "nt":
        from test_third_party import _windows_extension_compiler

        try:
            compiler, _library, env = _windows_extension_compiler()
        except RuntimeError as exc:
            pytest.skip(f"MSVC is required to compile the launcher probe: {exc}")
        command = [compiler, "/nologo", "/std:c++17", "/EHsc", "/utf-8", "/I" + include,
                   "/I" + str(launcher), str(source), "/Fe:" + str(probe),
                   "/Fo:" + str(root / "probe.obj"), "/link", "user32.lib"]
    else:
        compiler = shutil.which("c++")
        if not compiler:
            pytest.skip("C++ compiler required for launcher deployment checks")
        command = [compiler, "-std=c++17", "-I" + include, "-I" + str(launcher),
                   str(source), "-o", str(probe)]
        command += ["-framework", "CoreFoundation"] if sys.platform == "darwin" else ["-ldl"]
    subprocess.run(command, env=env, cwd=root, check=True, capture_output=True, timeout=30)
    return probe


def test_launcher_uses_loaded_image_even_with_an_unrelated_argv0(launcher_probe, tmp_path):
    result = subprocess.run(
        ["unrelated-name"], executable=str(launcher_probe), cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=10,
    )
    assert Path(result.stdout.splitlines()[0]) == launcher_probe.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX PATH lookup and symlink entry")
def test_launcher_resolves_path_and_symlink_launches(launcher_probe, tmp_path):
    link = tmp_path / "renamed"
    link.symlink_to(launcher_probe)
    env = dict(os.environ, PATH=str(tmp_path))
    elsewhere = tmp_path / "working-directory"
    elsewhere.mkdir()
    result = subprocess.run(
        [link.name], cwd=elsewhere, env=env, capture_output=True,
        text=True, encoding="utf-8", check=True, timeout=10,
    )
    assert Path(result.stdout.splitlines()[0]) == launcher_probe.resolve()


@pytest.mark.parametrize("override", [False, True])
def test_launcher_cache_paths_preserve_unicode(launcher_probe, tmp_path, override):
    home = tmp_path / "用户 目录"
    env = dict(os.environ)
    env.pop("PY_UPPER_EGG_CACHE", None)
    env.update(HOME=str(home), LOCALAPPDATA=str(home), XDG_CACHE_HOME=str(home))
    if override:
        expected = home / "指定 缓存"
        env["PY_UPPER_EGG_CACHE"] = str(expected)
    elif os.name == "nt":
        expected = home / "py_upper" / "cache" / "eggs"
    elif sys.platform == "darwin":
        expected = home / "Library" / "Caches" / "py_upper" / "eggs"
    else:
        expected = home / "py_upper" / "eggs"
    result = subprocess.run(
        [str(launcher_probe)], env=env, cwd=tmp_path, capture_output=True,
        text=True, encoding="utf-8", check=True, timeout=10,
    )
    assert Path(result.stdout.splitlines()[1]) == expected


def test_launcher_falls_back_when_the_cache_root_is_not_writable(tmp_path):
    """An unwritable cache must not stop the application from starting.

    The per-user cache directory can exist and still be unwritable: a read-only
    home, a roaming profile, a container without a writable $HOME. The launcher
    used to report "Could not create the bundle cache staging directory" and
    exit before Py_Initialize, which turns a cache problem into an application
    that will not run at all.
    """
    source = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    text = source.read_text(encoding="utf-8")
    assert "cache_root_usable" in text
    assert "usable_cache_root" in text
    # The probe writes and removes a file, so a directory that merely exists is
    # not mistaken for a usable one.
    assert ".probe-" in text
    # Both the bundle cache publish path and the plaintext cache go through it.
    assert "const fs::path base = usable_cache_root();" in text
