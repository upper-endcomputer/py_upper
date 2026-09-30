# PyStand2

Standalone Python application runtime/packaging for Windows, macOS and Linux.

## One command

The public build interface is intentionally small. **You normally only need `tools/build.py`.**

```bash
# Build the current host target
python tools/build.py

# Build a specific target
python tools/build.py --target linux-x86_64

# Build + run on the current host
python tools/build.py --run

# Verify an existing package
python tools/build.py --verify

# Inspect the host/toolchain
python tools/build.py --doctor

# Re-resolve and record the build lock
python tools/build.py --lock --target linux-x86_64

# Reproduce strictly from the lock/cache
python tools/build.py --locked --target linux-x86_64

# Build, verify and sign a release
python tools/build.py --release --target macos-arm64 --identity "Developer ID Application: ..." --notary-profile pystand-notary

# Remove generated build outputs
python tools/build.py --clean
```

The old positional forms (`build`, `run`, `verify`, etc.) remain accepted for compatibility, but the flag form is the preferred interface.

The PBS release is pinned in `app/pyproject.toml` under `[tool.pstand.pbs]`. Each lock records the selected PBS release, exact asset and SHA256, so a locked build cannot silently move to a newer PBS release.

## What `build` does

`build` is the single normal workflow:

```text
pyproject.toml
    ↓
PBS target SDK + runtime
    ↓
target wheels
    ↓
Cython → C
    ↓
target compiler → .pyd/.so
    ↓
native dependency closure
    ↓
C++ launcher
    ↓
package
    ↓
verify
```

Internal Python modules under `tools/pstand/` are implementation details, not separate commands or user-facing scripts.

## Targets

```text
windows-x86_64
windows-arm64
macos-x86_64
macos-arm64
linux-x86_64
linux-arm64
```

Linux uses PBS GNU/glibc targets `x86_64-unknown-linux-gnu` and `aarch64-unknown-linux-gnu`. PBS documents these target triples and notes a minimum glibc version of 2.17 for most GNU distributions. 

PBS's full archives contain `PYTHON.json` plus build/install artifacts; the build uses that metadata rather than guessing SDK paths. Final application bundles use install-only runtime artifacts. 

## Locking

`pystand.lock.json` is intentionally a **PyStand build lock**, not a replacement for Python's package lock format. It records the PBS SDK/runtime artifacts and resolved wheel hashes for each target.

Python dependency lock files follow the standard `pylock.toml` format where appropriate; PyPA defines `pylock.toml` as the reproducible-environment format. The PyStand lock additionally covers non-Python build inputs such as PBS.


## Locked/offline builds

`--locked` validates the existing `pystand.lock.json` and the locally cached PBS SDK/runtime before the build starts. If the cache or lock is incomplete, the build fails instead of silently downloading another PBS artifact.

## Linux native dependencies

Linux `.so` dependencies are read from ELF `DT_NEEDED` entries with `readelf` (or `objdump` as fallback). The packager closes dependencies inside the final bundle and uses `$ORIGIN` RPATH when `patchelf` is available. System glibc/libstdc++/loader libraries are treated as host-provided and are not copied.

## Development

The build system does **not** require `venv`. It accepts any Python 3.13
interpreter that can provide the build dependencies.

The recommended explicit override is `PYSTAND_PYTHON`. For example:

```bash
export PYSTAND_PYTHON=/opt/python/3.13/bin/python
python tools/build.py
```

PowerShell:

```powershell
$env:PYSTAND_PYTHON = "C:\Python313\python.exe"
python tools/build.py
```

If `PYSTAND_PYTHON` is not set, PyStand2 looks for `app/.venv` first and
then falls back to the Python interpreter running `tools/build.py`. The
selected interpreter must be Python 3.13 and needs the build requirements
from `app/pyproject.toml`.

For VS Code, install the Python, Pylance and CMake Tools extensions. The
workspace deliberately does not hard-code an interpreter path, so you can
select a venv, conda environment, pyenv environment, Poetry environment,
or another discovered Python installation. VS Code's current Python
Environments workflow discovers common environment managers automatically.

After selecting the interpreter with **Python: Select Interpreter**, these
commands are sufficient:

```text
Run Task -> pystand: doctor
Run Task -> pystand: build
Run Task -> pystand: run
```

The Python application source is exposed to IntelliSense through
`app/src`; installing the application into the selected environment is
optional for editor navigation.

## Platform requirements

- Linux x86_64: GCC/G++, CMake and Ninja.
- Linux arm64 cross-build: `aarch64-linux-gnu-gcc/g++`.
- Windows: Visual Studio C++ build tools; ARM64 targets require the ARM64 cross compiler.
- macOS: Xcode Command Line Tools; target builds use the selected architecture and deployment target.

Cross-target builds are build-only on the host. `--run` requires target == host.

## Current validation status

Linux x86_64 has been exercised in this environment at the launcher/CMake level. A full PBS-backed package build could not be completed here because this execution environment currently cannot resolve external network hosts, so PBS download was not falsely reported as tested.

The repository now includes `.github/workflows/validate.yml`, which runs the
full native build/run workflow on Linux x86_64, Linux arm64 cross-build, Windows x86_64, macOS x86_64,
and macOS arm64. These CI jobs are the authoritative cross-platform
validation path; this development environment cannot execute Windows or
macOS binaries locally. GitHub currently provides Windows 2025 x64 and macOS 14 arm64 hosted runners,
so the workflow uses those supported runner labels. The Linux arm64 job is a
cross-build validation and does not attempt to execute the ARM64 artifact on the x86_64 runner.

## Python environment

The Python used to run `tools/build.py` is a **development/build Python**, not necessarily the Python shipped with the application. Configure it with `PYSTAND_PYTHON`, or select any suitable interpreter in VS Code. A project-local `.venv` is optional.

The bundled target Python is controlled independently by `[tool.pstand].python` in `app/pyproject.toml`. Cython runs on the development Python and generates C; the generated C is compiled against the target Python SDK. Therefore the two Python versions do not have to match.

For Python 3.8 development environments, the build requirements intentionally allow an older setuptools line because current setuptools releases have raised their minimum Python version. Cython 3.1 supports CPython 3.8+.

### Windows XP / legacy Python

Do not assume that selecting `python = "3.8.10"` makes a Windows XP package. Official CPython 3.8.10 Windows binaries do not support Windows XP, and current Python-build-standalone distributions for CPython 3.13 and earlier require Windows 8.1 or newer. A Windows XP product therefore needs a separately built/custom Python 3.8.x runtime, matching headers/import library, and an XP-compatible compiler/toolchain. The build system keeps development Python, target Python, and runtime provider as separate concepts so such a legacy provider can be added without constraining modern targets.
