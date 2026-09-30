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

Python dependency lock files follow the standard `pylock.toml` format where appropriate; PyPA defines `pylock.toml` as the reproducible-environment format. 


## Locked/offline builds

`--locked` validates the existing `pystand.lock.json` and the locally cached PBS SDK/runtime before the build starts. If the cache or lock is incomplete, the build fails instead of silently downloading another PBS artifact.

## Linux native dependencies

Linux `.so` dependencies are read from ELF `DT_NEEDED` entries with `readelf` (or `objdump` as fallback). The packager closes dependencies inside the final bundle and uses `$ORIGIN` RPATH when `patchelf` is available. System glibc/libstdc++/loader libraries are treated as host-provided and are not copied.

## Development

```bash
python -m venv app/.venv
app/.venv/bin/python -m pip install -U pip
app/.venv/bin/python -m pip install -e app
```

On Windows:

```powershell
py -3.13 -m venv app\.venv
app\.venv\Scripts\python.exe -m pip install -U pip
app\.venv\Scripts\python.exe -m pip install -e app
```

## Platform requirements

- Linux x86_64: GCC/G++, CMake and Ninja.
- Linux arm64 cross-build: `aarch64-linux-gnu-gcc/g++`.
- Windows: Visual Studio C++ build tools; ARM64 targets require the ARM64 cross compiler.
- macOS: Xcode Command Line Tools; target builds use the selected architecture and deployment target.

Cross-target builds are build-only on the host. `--run` requires target == host.

## Current validation status

Linux x86_64 has been exercised in this environment at the launcher/CMake level. A full PBS-backed package build could not be completed here because this execution environment currently cannot resolve external network hosts, so PBS download was not falsely reported as tested.
