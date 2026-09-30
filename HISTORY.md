# v0.16.7

**Problem solved:** The package staging logic left Python source files in the final application because only a subset of modules was Cythonized and `main.py` was explicitly excluded.

**Root cause:** `selected_sources()` limited compilation to `core.*`, `services.*`, and `models.*`, while `copy_python_tree()` copied all source files before deleting only the selected modules.

**Implementation:** Compile all non-`__init__.py` application modules by default, including the entry module and utility modules; keep only package marker `__init__.py` files as Python; move generated C output into `build/cython`; and filter development caches/bytecode from staging.

**Impact:** The packaged `Contents/Resources/site-packages` no longer contains application implementation `.py` files for the default configuration.

# v0.16.6

- Fixed Mach-O byte-order detection for macOS native dependency inspection. The previous parser reversed the arm64 CPU type and reported `0x0c000001`, rejecting valid Apple Silicon dylibs such as `libtcl9.0.dylib`. Universal Mach-O binaries are now checked for the requested architecture instead of being accepted unconditionally.

## 0.16.5

Fixes fresh-build failures caused by the selected development Python not having Cython installed. The build now bootstraps supported Cython 3.1.x into a py_upper-owned cache and uses it without modifying the developer's environment.

## 0.16.4

Fixes a macOS build failure where Cython exited successfully but the expected generated `app.c` file was not found. Cython is now invoked from each source directory using relative paths and `--force`, with clearer diagnostics when generation still does not produce the requested file.

## v0.16.3

### Problem
- The v0.16.2 PBS runtime resolver called `user_agent()` without importing it, so a normal PBS-backed build failed with `NameError` before downloading the runtime.

### Changes
- Import `user_agent` explicitly in `tools/py_upper/runtime.py`.
- Add a regression test covering the runtime module's User-Agent dependency.
- Keep the v0.16.2 Python 3.8+ compatibility changes unchanged.

### Verification
- 12 tests passed locally.
- `python tools/build.py --doctor --target linux-x86_64` passed.

# py_upper Git history

This repository preserves the complete release evolution from v0.9.1 through v0.16.2.

Every release has a dedicated commit and tag. The commit body records the principal architectural and build-system changes introduced by that release. Earlier tags are immutable historical snapshots; v0.16.2 is the current Build Tool compatibility release.


## v0.16.2

**Problem solved:** The Build Tool failed on Python 3.10 because `tomllib` only exists in Python 3.11+. The build tooling also needed a clean Python 3.8 floor for future legacy-runtime work.

**Implementation:** Added a TOML compatibility layer with vendored Tomli for Python 3.8–3.10, kept standard-library `tomllib` for 3.11+, normalized build-tool annotation evaluation, added an explicit Build Tool Python minimum, and added compatibility tests/documentation.

**Impact:** Developers can use Python 3.8–3.13+ to run the Build Tool while independently selecting the bundled Target Python, including custom Python 3.8.10-compatible runtimes for legacy targets.
