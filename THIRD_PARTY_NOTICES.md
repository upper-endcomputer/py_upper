# Third-party notices

## Tomli

`tools/py_upper/_vendor/tomli/` contains the vendored Tomli TOML parser used by
the build tool on Python 3.8–3.10, where the standard-library `tomllib` module
is unavailable.

Tomli is distributed under the MIT License. The vendored source files retain
their SPDX license identifiers.

## _memimporter / MemoryModule

`tools/py_upper/vendor/memimporter/` contains the in-memory extension loader used
by the Windows `memory` egg backend:

- `_memimporter.c`, `MyLoadLibrary.c` and `actctx.c` come from py2exe and are
  distributed under the MIT License (see `MIT-License.txt`).
- `MemoryModule.c` is Joachim Bauch's MemoryModule and is distributed under the
  Mozilla Public License 2.0 (see `MPL2-License.txt`).

The vendored files retain their original copyright and license headers.
