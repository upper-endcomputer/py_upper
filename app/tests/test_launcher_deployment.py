"""Compile the real launcher helpers and exercise deployment path semantics."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest


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

        compiler, _library, env = _windows_extension_compiler()
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
