# Third-party notices

## Tomli

`tools/appbuild/_vendor/tomli/` contains the vendored Tomli TOML parser used by
the build tool on Python 3.8–3.10, where the standard-library `tomllib` module
is unavailable.

Tomli is distributed under the MIT License. The vendored source files retain
their SPDX license identifiers.

## _memimporter / MemoryModule

`tools/appbuild/vendor/memimporter/` contains the in-memory extension loader used
by the Windows `memory` egg backend:

- `_memimporter.c`, `MyLoadLibrary.c` and `actctx.c` come from py2exe and are
  distributed under the MIT License (see `MIT-License.txt`).
- `MemoryModule.c` is Joachim Bauch's MemoryModule and is distributed under the
  Mozilla Public License 2.0 (see `MPL2-License.txt`).

The vendored files retain their original copyright and license headers.

本项目在上述 vendored 源码中补充中文接口说明，并修复内存模块链表释放、分配失败清理、Python 回调引用释放和导出序号边界；这些修改属于本项目，不代表上游原始实现。
