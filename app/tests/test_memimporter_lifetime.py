"""编译实际内存模块链表函数，验证释放顺序与引用计数；系统加载器使用可观测桩。"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


def _function(source, name):
    """提取实际 C 函数定义供可移植探针编译，避免在测试中复制实现。"""
    pattern = r'(?m)^[^\n]*\b' + re.escape(name) + r'\([^;{}]*\)\s*\{'
    match = re.search(pattern, source)
    assert match, name
    depth = 1
    end = match.end()
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]


def test_memory_module_release_unlinks_before_unload(tmp_path):
    """释放中间和头节点后不应返回悬空句柄；依赖卸载回调必须先看见节点已移除。"""
    source = (Path(__file__).parents[2] / 'tools/appbuild/vendor/memimporter/MyLoadLibrary.c').read_text(encoding='utf-8')
    struct = re.search(r'typedef struct tagLIST.*?} LIST;', source, re.S).group()
    probe = tmp_path / 'lifetime.cpp'
    probe.write_text('''#include <cstdlib>
#include <cstring>
#include <cstdio>
#include <cstdint>
using LPCSTR = const char*;
using HMODULE = void*;
using HCUSTOMMODULE = void*;
using BOOL = int;
#define TRUE 1
#define ERROR_OUTOFMEMORY 14
#define dprintf(...) 0
#ifndef _WIN32
#include <strings.h>
#define _stricmp strcasecmp
#define _strdup strdup
#endif
static int releases = 0;
static int stale = 0;
static void SetLastError(int) {}
static BOOL FreeLibrary(HMODULE) { return 1; }
static void MemoryFreeLibrary(HMODULE);
''' + struct + '\nstatic LIST *libraries;\n' + '\n'.join(
        _function(source, name) for name in ('_FindMemoryModule', '_AddMemoryModule', 'MyGetModuleHandle', 'MyFreeLibrary')
    ) + '''
static void MemoryFreeLibrary(HMODULE module) {
    if (_FindMemoryModule(nullptr, module)) ++stale;
    ++releases;
}
int main() {
    auto a = reinterpret_cast<void*>(uintptr_t(1));
    auto b = reinterpret_cast<void*>(uintptr_t(2));
    auto c = reinterpret_cast<void*>(uintptr_t(3));
    auto first = _AddMemoryModule("a", a);
    _AddMemoryModule("b", b);
    auto last = _AddMemoryModule("c", c);
    if (!first || !last) return 1;
    MyFreeLibrary(b);
    if (MyGetModuleHandle("b") != nullptr || stale || releases != 1) return 2;
    ++last->refcount;
    MyFreeLibrary(c);
    if (MyGetModuleHandle("c") != c || releases != 1) return 3;
    MyFreeLibrary(c);
    if (MyGetModuleHandle("c") || MyGetModuleHandle("a") != a || stale) return 4;
    MyFreeLibrary(a);
    return libraries || stale || releases != 3 ? 5 : 0;
}
''', encoding='utf-8')
    _compile_and_run(probe)


def _compile_and_run(probe):
    """使用本机 C++ 编译器构建实际 C 函数探针，并保留完整失败输出。"""
    tmp_path = probe.parent
    binary = tmp_path / (probe.stem + ('.exe' if os.name == 'nt' else ''))
    env = None
    if os.name == 'nt':
        from test_third_party import _windows_extension_compiler
        try:
            compiler, _, env = _windows_extension_compiler()
        except RuntimeError as exc:
            pytest.skip(str(exc))
        command = [compiler, '/nologo', '/EHsc', '/std:c++17', '/utf-8', str(probe), '/Fe:' + str(binary), '/Fo:' + str(probe.with_suffix('.obj'))]
    else:
        compiler = shutil.which('c++')
        if not compiler:
            pytest.skip('C++ compiler unavailable')
        command = [compiler, '-std=c++17', str(probe), '-o', str(binary)]
    for command in (command, [str(binary)]):
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, errors='replace', timeout=30)
        if result.returncode:
            pytest.fail(f'{command[0]} exited {result.returncode}\n{result.stdout}\n{result.stderr}', pytrace=False)


def test_export_ordinal_bounds_and_unnamed_exports(tmp_path):
    """编译真实导出解析函数，验证序号上界拒绝越界且无名称表的序号导出仍可访问。"""
    source = (Path(__file__).parents[2] / 'tools/appbuild/vendor/memimporter/MemoryModule.c').read_text(encoding='utf-8')
    probe = tmp_path / 'exports.cpp'
    probe.write_text('''#include <cstdlib>
#include <cstring>
#include <cstdint>
using DWORD = uint32_t;
using WORD = uint16_t;
using LPCSTR = const char*;
using LPVOID = void*;
using FARPROC = void*;
using HMEMORYMODULE = void*;
struct ExportNameEntry { const char *name; WORD idx; };
struct ExportDirectory {
    DWORD Base, NumberOfFunctions, NumberOfNames;
    DWORD AddressOfFunctions, AddressOfNames, AddressOfNameOrdinals;
};
using PIMAGE_EXPORT_DIRECTORY = ExportDirectory*;
struct DataDirectory { DWORD VirtualAddress, Size; };
using PIMAGE_DATA_DIRECTORY = DataDirectory*;
struct Module { unsigned char *codeBase; ExportNameEntry *nameExportsTable; DataDirectory directory; };
using PMEMORYMODULE = Module*;
#define GET_HEADER_DICTIONARY(module, entry) (&module->directory)
#define HIWORD(value) ((uintptr_t(value) >> 16) & 0xffff)
#define LOWORD(value) (uintptr_t(value) & 0xffff)
#define ERROR_PROC_NOT_FOUND 127
#define ERROR_OUTOFMEMORY 14
static DWORD error_code;
static void SetLastError(DWORD error) { error_code = error; }
''' + '\n'.join(_function(source, name) for name in ('_compare', '_find', 'MemoryGetProcAddress')) + '''
int main() {
    alignas(16) unsigned char image[256] = {};
    Module module = {image, nullptr, {0, sizeof(ExportDirectory)}};
    auto exports = reinterpret_cast<ExportDirectory*>(image);
    exports->Base = 10;
    exports->NumberOfFunctions = 1;
    exports->NumberOfNames = 1;
    exports->AddressOfFunctions = 64;
    auto addresses = reinterpret_cast<DWORD*>(image + 64);
    addresses[0] = 128;
    addresses[1] = 200; // 越界项故意非零，避免错误实现偶然返回空值。
    if (MemoryGetProcAddress(&module, reinterpret_cast<LPCSTR>(uintptr_t(11))) != nullptr || error_code != ERROR_PROC_NOT_FOUND) return 1;
    exports->NumberOfNames = 0;
    if (MemoryGetProcAddress(&module, reinterpret_cast<LPCSTR>(uintptr_t(10))) != image + 128) return 2;
    if (MemoryGetProcAddress(&module, "missing") != nullptr) return 3;
    return 0;
}
''', encoding='utf-8')
    _compile_and_run(probe)
