# 中文维护说明：验证真实 C++ 缓存租约和淘汰行为；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
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
    fs::last_write_time(base / "merged-old",
        fs::file_time_type::clock::now() - std::chrono::hours(24));
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
    for args, timeout in [(command, 30), ([str(output)], 10)]:
        result = subprocess.run(args, env=env, cwd=tmp_path, capture_output=True,
                                text=True, errors='replace', timeout=timeout)
        if result.returncode:
            pytest.fail(f'{args[0]} exited with {result.returncode}\n'
                        f'{result.stdout}\n{result.stderr}', pytrace=False)
