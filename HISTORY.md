# v0.16.13

**Problem solved:** Verification could fail with `FAIL core` and make a successful package return `SystemExit: 1` when the user's source tree did not contain a `core` package.

**Root cause:** `tools/py_upper/verify.py` assumed the sample project's `core/` directory was mandatory instead of validating the actual configured application layout.

**Implementation:** Replaced the hard-coded `core` staging check with a generic `site-packages` check. Real application modules continue to be validated from `selected_sources()`, so layouts such as `src/config` and `src/plugins` remain supported. Added a regression test covering a project without `core/`.

**Verification:** Full local test suite passed; `git diff --check` passed.

# v0.16.12

### Problem
- Applications with prebuilt native libraries under `app/src/` needed an explicit native-library packaging path, and macOS dylib identity paths were not normalized.
- A dylib's own `LC_ID_DYLIB` could also be mistaken for a dependency when parsing `otool -L` output.

### Root cause
- Source-tree copying was not explicitly native-aware and used a generic Python tree copier without regression coverage for `.dylib` payloads and symlinked native libraries.
- The Mach-O dependency parser treated the dylib's own install name as if it were another load dependency.
- macOS packaging rewrote dependency load commands but did not normalize bundled dylib `LC_ID_DYLIB` values.

### Changes
- Preserve native libraries and symlinks from `app/src/` into staged `site-packages` using the metadata-safe content copier.
- Keep native files in the same recursive native dependency graph as Cython extensions and bundled runtime libraries.
- Exclude Mach-O `LC_ID_DYLIB` from dependency edges returned by `otool -L`.
- Normalize bundled macOS dylib IDs to `@loader_path/<filename>`.
- Continue rewriting actual resolved dylib dependencies to `@loader_path`-relative paths.
- Document the supported `src` native-library workflow and add regression coverage.
- Bump the project version to 0.16.12.

### Verification
- Full local regression suite passed.
- `py_upper doctor --target linux-x86_64` passed.
- `git diff --check` passed.

# v0.16.11

### Problem
- macOS native dependency bundling could report valid PBS Tcl/Thread/Itcl libraries as unresolved.

### Root cause
- Native dependency resolution only checked the top level of each search root and did not emulate macOS `@rpath` entries. PBS places Tcl/Thread/Itcl dylibs below `runtime/lib`, so names such as `libtcl9thread3.0.6.dylib` were not found. The previous rewrite step also ignored dependencies that already used `@rpath`.

### Changes
- Recursively resolve native libraries by basename while preserving search-root priority.
- Parse Mach-O `LC_RPATH` entries and use them for `@rpath` resolution.
- Rewrite resolved macOS native dependencies to `@loader_path`-relative paths, including existing `@rpath` references.
- Copy externally resolved native dependencies by content only.
- Add regressions for nested Tcl-style dylibs and `@rpath` resolution.
- Bump the project version to 0.16.11.

### Verification
- Full local regression suite passed.
- `py_upper doctor --target linux-x86_64` passed.
- `git diff --check` passed.

# v0.16.10

### Problem
- macOS packaging could still fail while copying an ordinary read-only PBS runtime file, with `PermissionError: [Errno 1] Operation not permitted` from `os.chmod()`.

### Root cause
- The v0.16.9 custom copier stopped `copytree()`/`copy2()` metadata replay but still applied the full source POSIX mode to every copied file. A source mode such as `0444` caused the build to attempt a restrictive `chmod()` on the new file, which can be rejected by macOS in the app-bundle destination.

### Changes
- Make package-tree copies content-only; do not replay source POSIX modes or other filesystem metadata.
- Preserve executable status only for the launcher through an explicit platform packaging step.
- Keep runtime/native library copying independent of source executable/read-only modes.
- Add regressions for read-only runtime files, symlinks, and launcher executable mode.
- Bump the project version to 0.16.10.

### Verification
- Full local regression suite passed.
- `git diff --check` passed.

# v0.16.9

**Problem solved:** macOS app packaging failed during runtime tree copying with `Errno 1: Operation not permitted`.

**Root cause:** `shutil.copytree()` copied filesystem metadata/flags via its metadata-preserving copy path; macOS can reject reproducing those attributes in the destination app bundle.

**Implementation:** Added a packaging-specific recursive copier that transfers file contents, executable/readable mode, and symlink targets without cloning ACLs, xattrs, timestamps, or filesystem flags. Applied it to the runtime, site-packages, resources, and launcher.

**Verification:** 21 tests passed locally, including a regression test that makes `copystat()` fail with `EPERM`.

# v0.16.8

### Problem
- Every build from a development Python without Cython could trigger a `ModuleNotFoundError: No module named 'Cython'` under VS Code/debugpy before the bootstrap logic recovered.
- The v0.16.7 bootstrap path also used an unconditional `pip --upgrade`, which could reinstall Cython on every build instead of reusing the project cache.

### Root cause
- The Cython availability probe executed `import Cython` in a child Python process. A debugger can pause on that expected exception even though the parent process captures the non-zero exit status.
- Cache probing did not distinguish an already-populated supported Cython cache from a missing dependency before invoking pip.

### Changes
- Probe the installed Cython distribution through `importlib.metadata` without importing the `Cython` package.
- Reuse a py_upper-owned Cython cache when a supported 3.1.x distribution is already present.
- Remove the unconditional pip `--upgrade` flag from the bootstrap path.
- Keep the developer Python environment untouched.
- Add regression coverage for cache reuse and non-reinstall behavior.

### Verification
- Full local test suite passes.
- `git diff --check` passes.

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

This repository preserves the complete release evolution from v0.9.1 through v0.16.11.

Every release has a dedicated commit and tag. The commit body records the problem, root cause, implementation changes, verification, and impact for that release. Earlier tags are immutable historical snapshots; v0.16.12 is the current native library packaging and relocation release.


## v0.16.2

**Problem solved:** The Build Tool failed on Python 3.10 because `tomllib` only exists in Python 3.11+. The build tooling also needed a clean Python 3.8 floor for future legacy-runtime work.

**Implementation:** Added a TOML compatibility layer with vendored Tomli for Python 3.8–3.10, kept standard-library `tomllib` for 3.11+, normalized build-tool annotation evaluation, added an explicit Build Tool Python minimum, and added compatibility tests/documentation.

**Impact:** Developers can use Python 3.8–3.13+ to run the Build Tool while independently selecting the bundled Target Python, including custom Python 3.8.10-compatible runtimes for legacy targets.
