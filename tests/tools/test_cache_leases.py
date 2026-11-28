"""Exercise the actual C++ cache lease and eviction helpers."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import pytest


def test_native_pruner_preserves_active_cache(tmp_path):
    launcher = Path(__file__).parents[2] / 'launcher/src'
    source = tmp_path / 'probe.cpp'
    source.write_text('''#include "PyUpper.cpp"
int main() {
    fs::path base = "cache";
    fs::create_directories(base / "merged-old");
    fs::create_directories(base / "merged-previous");
    fs::create_directories(base / "merged-current");
    fs::last_write_time(base / "merged-old", fs::file_time_type::min());
    {
        CacheLease active(base / ".lease-merged-old", false);
        if (!active.valid()) return 1;
        prune_bundle_cache(base, "merged-current");
        if (!fs::exists(base / "merged-old")) return 2;
    }
    prune_bundle_cache(base, "merged-current");
    return fs::exists(base / "merged-old") ? 3 : 0;
}
''')
    output = tmp_path / ('probe.exe' if os.name == 'nt' else 'probe')
    include = sysconfig.get_path('include')
    env = None
    if os.name == 'nt':
        from test_third_party import _windows_extension_compiler
        try:
            compiler, _, env = _windows_extension_compiler()
        except RuntimeError as exc:
            pytest.skip(str(exc))
        command = [compiler, '/nologo', '/std:c++17', '/EHsc', '/utf-8',
                   '/I' + include, '/I' + str(launcher), str(source),
                   '/Fe:' + str(output), '/Fo:' + str(tmp_path / 'probe.obj'), '/link', 'user32.lib']
    else:
        compiler = shutil.which('c++')
        if not compiler:
            pytest.skip('C++ compiler unavailable')
        command = [compiler, '-std=c++17', '-I' + include, '-I' + str(launcher), str(source), '-o', str(output)]
        command += ['-framework', 'CoreFoundation'] if sys.platform == 'darwin' else ['-ldl']
    subprocess.run(command, env=env, cwd=tmp_path, check=True, capture_output=True, timeout=30)
    subprocess.run([str(output)], cwd=tmp_path, check=True, capture_output=True, timeout=10)
