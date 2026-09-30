## 0.17.2

### Problem
- A packaged PySide6 application shipped the entire Qt payload: 1.2 GB, of which `QtWebEngineCore` alone is about 450 MB of Chromium. Applications packaged by other tools are far smaller because they only bundle the Qt modules the application imports.
- The application had no window, so a broken Qt payload could only be detected by importing modules, not by using Qt.

### Root cause
- `[project].dependencies` installed whole wheels and the optimizer had no notion of which parts of a dependency an application actually uses.

### Changes
- Add a Qt main window to the application entry point. `PY_UPPER_HEADLESS=1` builds the same window on the offscreen platform, so CI and the build smoke can prove Qt works without a window server.
- Add import-driven Qt pruning, enabled with `[tool.py_upper.optimize].qt = "imports"`: read the `PySide6.Qt*` modules referenced by `app/src`, resolve the transitive closure with the target runtime, then drop unused wrapper modules, Qt libraries, plugin categories, the QML tree, shiboken's QML helper, Qt tooling binaries and tooling translations.
- Drop plugins whose Qt dependencies are not part of the kept module set. This removes the virtual-keyboard input context (which pulls QtQml/QtQuick) and the PDF image format (which pulls QtPdf), with an explicit exception so SVG icon support survives.
- Accept an explicit module list instead of `"imports"`; the list is expanded through the same runtime closure so it cannot drop a wrapper that the imported modules need.
- Keep `"all"` as the default, so projects that rely on Qt's dynamic loading keep the full payload.

### Verification
- macOS arm64 with `PySide6==6.11.0`: the packaged application shrank from 1.2 GB to 153 MB (PySide6 1.1 GB to 104 MB) with the window still opening, `--verify` passing and `codesign --verify --deep --strict` accepting the bundle. `strip_native = true` reaches 141 MB.
- Full suite on macOS arm64: 59 passed, 2 skipped, plus the hermetic local end-to-end build with `PY_UPPER_E2E=1`.

### Limitations
- Qt pruning cannot see modules loaded dynamically (for example through `importlib` or a QML file). Use an explicit module list for those applications.
- Pruning runs only when the target runtime can be executed on the build host; a cross target keeps the full Qt payload.

## 0.17.1

### Problem
- macOS builds failed at several stages that only a real host and a real dependency set expose: Cython extensions did not link, the package scan rejected the bundled runtime, the runtime copy lost its execute bits, ad-hoc signing left a stale bundle seal, and `--verify` was not idempotent after a build.
- The unit suite wrote its PBS metadata fixture into the project's real `.cache/pbs/uv-download-metadata.json`, so any later `macos-arm64` build failed with "No PBS runtime metadata" until the cache was deleted.
- With a real `PySide6`/`pyserial`/`bleak`/`qasync` dependency set the third-party path broke: environment markers were evaluated against the host interpreter, multi-hundred-megabyte wheels aborted on a single socket timeout, the Qt dependency closure degenerated to O(files x dependencies), optional Qt plugins with unsatisfiable dependencies failed the whole build, pyobjc `*.dSYM` debug bundles were treated as loadable images, and import roots were derived by scanning all of site-packages.
- Packaged Windows applications could not import dynamic stdlib extensions such as `_ssl`, `_socket` and `_ctypes`, because `DLLs` was missing from the module search path.
- Nested macOS code such as `QtWebEngineCore.framework/Helpers/QtWebEngineProcess.app` could not be ad-hoc signed inside-out, and a relative launcher invocation resolved the bundle root incorrectly.

### Root cause
- Platform- and interpreter-specific assumptions were applied where the target contract required the target's own values.
- Verification and smoke steps mutated the artifacts they were verifying, and test fixtures wrote into shared project state.
- Native resolution treated every lookup as a fresh tree walk and every Mach-O file as a loadable image.

### Changes
- Link macOS Cython extensions as bundles with `-undefined dynamic_lookup`, matching CPython's own `LDSHARED` and the launcher's `RTLD_GLOBAL` symbol resolution.
- Scope the whole-tree architecture check to the container format the target OS can load, so inert foreign-format payloads such as the runtime's Windows setuptools stubs no longer fail a correct package.
- Preserve execute bits when copying files while still never replaying restrictive source modes.
- Keep `--verify` idempotent by disabling bytecode writes for the target-Python smoke, and disable bytecode caching in the launcher through `PyConfig.write_bytecode` so a packaged app never mutates its own signed bundle.
- Sign macOS native payloads inside-out, deepest path first, with the launcher last.
- Isolate the PBS metadata fixture in tests so the real cache can no longer be poisoned.
- Resolve target wheels with the target runtime's own interpreter when it is natively executable, so environment markers, tags and `Requires-Python` all match the target instead of the host.
- Raise the pip socket timeout and retry budget, and download through a py_upper-owned pip cache so repeated builds and CI runs reuse wheels instead of refetching them.
- Index the bundle search roots once and cache `LC_RPATH` lookups, turning the Qt-sized closure from a tree walk per dependency into a single pass.
- Add `[tool.py_upper.native].exclude` for optional native payloads whose dependencies cannot exist in the bundle, and make the unresolved-dependency error point at it.
- Drop `*.dSYM` debug bundles during third-party optimization and skip them in the native scanner.
- Derive a distribution's import roots from its own `RECORD` instead of scanning the surrounding site-packages.
- Add the platform extension directory (`DLLs` on Windows, `lib-dynload` on Unix) to the launcher's module search path.
- Normalise the launcher's executable path so relative invocation resolves the bundle root correctly.
- Accept a list of manylinux baselines, because PySide6 publishes `manylinux_2_34_x86_64` and `manylinux_2_39_aarch64` wheels.
- Make the local end-to-end test host-portable and run it on every CI platform instead of Linux only.

### Verification
- macOS arm64 host: `python tools/build.py --run` with `PySide6==6.11.0`, `pyserial==3.5`, `bleak==3.0.2`, `qasync==0.28.0` - build, package, static verification (415 native binaries), target-runtime import smoke, launcher smoke and application run all pass; `codesign --verify --deep --strict` accepts the packaged bundle.
- Full suite with `PY_UPPER_E2E=1` on macOS arm64: 54 passed in 74s, including the hermetic local end-to-end build, third-party wheel install, application-owned native library closure and the lock/`--locked` round trip.
- The end-to-end gate caught three defects that unit tests could not: the missing platform extension directory, the site-packages-wide import scan and the lock round trip.

### Limitations
- Windows and Linux execution still relies on the CI matrix; this release was validated on macOS arm64 only.
- Cross-target dependency resolution keeps the host interpreter, so environment markers follow the host for targets that cannot be executed locally.
- `PySide6` payloads are large; the safe optimization profile does not prune Qt plugins that are present and resolvable.

## 0.17.0

### Problem
- The 0.16.x build chain accumulated fixes across PBS resolution, Cython, target extension naming, native dependency scanning, packaging permissions, verification, launcher symbol visibility, and third-party dependency handling without one end-to-end contract.
- Real builds could therefore pass unit tests and fail later during application launch.

### Root cause
- Target Python/ABI/platform information was duplicated across runtime, wheel, compiler, native, and verification code.
- Third-party wheel collection, staging, native dependency closure, and runtime smoke were only partially connected.
- Lock files were coupled to generated manifests instead of being an independent source of reproducible wheel inputs.

### Changes
- Introduce a unified target contract for runtime ABI, wheel platforms, extension suffixes, and target triples.
- Add a single PBS asset resolver used by both runtime and full SDK selection; automatic mode uses exact-version metadata and a single tagged release lookup.
- Add resilient HTTP retries and atomic `.part` downloads.
- Make third-party dependencies first-class inputs from `[project].dependencies`, resolving target-compatible wheels into staging.
- Add safe third-party/runtime optimization without modifying the source runtime cache; optional aggressive cleanup and native stripping are configurable.
- Separate Cython generation from Unix target extension compilation and ensure target headers are used without host Python include leakage.
- Treat application native libraries under `app/src` as package inputs and resolve their recursive native dependency closure.
- Make macOS package copying content-only and handle Mach-O identity vs dependency semantics correctly.
- Replace hardcoded verification of a `core` directory with project-layout-independent checks.
- Add target-runtime import smoke and launcher smoke before `--run`; smoke imports the entry module plus direct declared third-party imports so optional/lazy application modules are not treated as unconditional dependencies.
- Detect native binary format before choosing the parser, so Mach-O Python extensions ending in `.so` are handled as Mach-O rather than ELF.
- Make Linux RPATH rewriting opt-in instead of replacing third-party wheel RPATHs unconditionally.
- Make third-party import discovery work when `top_level.txt` is absent, and make Windows Python DLL system-dependency handling follow the configured target Python version.
- Make lock files preserve multiple target entries and make `--locked` independent of a pre-existing wheel manifest.
- Ignore `runtimes/` as a local runtime cache while keeping application native binaries trackable.

### Verification
- Local compatibility/unit suite: 43 passed, 1 skipped.
- Full local Linux end-to-end build/run: passed with a local target runtime, a transitive third-party wheel dependency, application-owned native libraries, launcher smoke, and application execution.
- Lock/locked round-trip: passed separately on the same end-to-end fixture.
- `compileall`, `py_upper doctor`, and `git diff --check` passed.
- Git history and release artifact integrity verified.

### Limitations
- The current development environment cannot perform real GitHub/PyPI network downloads reliably, so PBS network downloads and macOS/Windows CI execution are not claimed as locally executed. CI remains the authoritative cross-platform integration environment.

## 0.16.19

- Fix PBS automatic release resolution through a lightweight exact-version metadata index.
- Avoid the very large paginated GitHub Releases response when an exact PBS build mapping is available.
- Add HTTP retries for transient 408/425/429/5xx errors, including HTTP 504 gateway timeouts.
- Cache release API responses within a build so SDK/runtime resolution does not request the same release twice.

## 0.16.18

- Unify PBS asset matching across runtime selection, SDK selection, release detection, and diagnostics.
- Accept both `+` and `-` separators after the exact CPython patch version in PBS asset names.
- Normalize requested Python versions before asset matching.
- Add regression coverage preventing diagnostics from listing an available version that the selector cannot actually choose.

## 0.16.17

- Allow projects to configure only the exact target Python version when using PBS.
- Automatically select the newest PBS release containing both the matching `install_only_stripped` runtime and full SDK for the target triple.
- Fix the macOS runtime asset matcher so exact PBS filenames using `cpython-X.Y.Z+release-...` are recognized.
- Keep explicit `[tool.py_upper.pbs].release` support for pinned/reproducible builds.
- Record the resolved PBS release in the lock file and avoid requiring a network release lookup for `--locked` verification.

# py_upper Changelog

## 0.16.16 — Improve PBS Python-version diagnostics and ignore runtime caches

### Problem
- Selecting a target Python version that is not present in the configured PBS release produced a terse `No PBS ... asset` error with no indication of what versions were actually available.
- The downloaded `runtimes/` tree was a local build artifact but was not explicitly ignored by Git.

### Root cause
- PBS asset selectors only reported the requested version and target triple, without enumerating matching assets in the selected release.
- `.gitignore` did not contain the `runtimes/` directory, even though PBS runtime artifacts are machine-local and should not be committed.
- Global `*.dylib`, `*.so`, and `*.pyd` ignores also conflicted with the supported workflow where prebuilt native libraries under `app/src/` are application inputs.

### Changes
- Report the requested Python version, PBS release, target triple, and available exact Python versions when a PBS runtime or full SDK asset is missing.
- Best-effort search recent PBS releases and show a suggested exact-match release tag when one is discoverable; network lookup failure never hides the primary asset error.
- Keep the build exact: py_upper never silently substitutes another Python version.
- Add a dedicated `runtimes/` Git ignore entry.
- Stop globally ignoring native binary extensions so legitimate `app/src/` `.dylib`, `.so`, and `.pyd` inputs can be version-controlled; generated copies remain covered by `build/` and `dist/`.
- Add regression coverage for PBS runtime/SDK diagnostics and repository ignore rules.
- Bump the project version to 0.16.16.

### Verification
- Full local regression suite passes.
- `git diff --check` passes.

## 0.16.15 — Fix embedded CPython native-extension symbol resolution

### Problem
- A packaged macOS application could load the bundled CPython runtime but fail to import a Cython extension with `symbol not found in flat namespace '_PyArg_ValidateKeywordArguments'`.

### Root cause
- The launcher loaded bundled `libpython` with `RTLD_LOCAL`. Unix CPython extensions normally do not link directly to `libpython`, so an embedded interpreter loaded with local visibility cannot satisfy their Python C-API symbol references.

### Changes
- Load bundled `libpython` with `RTLD_GLOBAL` on Unix.
- Add a regression test preventing the launcher from reverting to `RTLD_LOCAL`.
- Bump the project version to 0.16.15.

### Verification
- Full local regression suite passes.
- Linux launcher compiles successfully.
- `git diff --check` passes.

## 0.16.14 — Use CPython-recognized extension suffixes

### Problem
- A packaged application could fail at startup with `ModuleNotFoundError: No module named 'main'` even though `main` had been Cythonized and copied into `site-packages`.

### Root cause
- Unix extensions were renamed to `.cp313-darwin.so` or `.cp313-x86_64-linux-gnu.so`. Those names do not match the target CPython importer's standard extension suffixes; the standard importer exposes `EXTENSION_SUFFIXES` as the authoritative list of recognized extension-module suffixes.

### Changes
- Derive target Unix extension filenames using CPython's `cpython-<major><minor>-...` ABI naming convention.
- Keep Windows `.cp<version>-<platform>.pyd` naming because that is the Windows CPython convention.
- Reuse the same target extension suffix rule in verification.
- Add regression coverage for target-specific extension suffixes.
- Bump the project version to 0.16.14.

### Verification
- Full local regression suite passed.
- `git diff --check` passed.

## 0.16.13 — Make verification project-layout agnostic

### Problem
- `tools/build.py --run` could finish packaging successfully but return `SystemExit: 1` because verification required an application package named `core`.
- Projects using `src/config`, `src/plugins`, or another valid source layout were therefore reported as `FAIL core`.

### Root cause
- `tools/py_upper/verify.py` hard-coded `stage/site-packages/core` as a mandatory package directory.
- The build system already derives actual application modules from `selected_sources()`, so the verification rule was inconsistent with the configurable source layout.

### Changes
- Replace the hard-coded `core` directory check with a generic `site-packages` staging check.
- Continue validating every actual Cython source through `selected_sources()`.
- Add a regression test for an application containing `src/config` without any `core` package.
- Bump the project version to 0.16.13.

### Verification
- Full local regression suite passed.
- `git diff --check` passed.

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

### Problem
- macOS packaging could fail while copying the PBS runtime into `Contents/Resources/runtime` with `shutil.Error` / `Errno 1: Operation not permitted`.
- The failure affected ordinary Python runtime files and vendored packages, including `pip/_vendor/urllib3`, even though the source files were readable.

### Root cause
- `shutil.copytree()` uses metadata-preserving file copies by default and also applies directory metadata. On macOS, reproducing source filesystem flags/metadata can be rejected with `EPERM` inside the destination app bundle.

### Changes
- Replace package-time `shutil.copytree()` calls with a controlled recursive copier.
- Copy file contents and POSIX mode only; do not clone ACLs, extended attributes, timestamps, or filesystem flags.
- Preserve symlink targets so runtime and package links continue to work.
- Use the safe copier for the bundled runtime, compiled site-packages, app resources, and launcher.
- Add a regression test that simulates `copystat()` returning `EPERM`.
- Bump the project version to 0.16.9.

### Verification
- 21 local tests passed.
- `git diff --check` passed.

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
- 20 local tests passed.
- `py_upper doctor` passed for linux-x86_64.
- `git diff --check` passed.

# v0.16.7

### Problem
- The packaged macOS application still copied application `.py` sources into `Contents/Resources/site-packages` for modules outside the narrow Cython include list, while `main.py` remained a Python entry module. Generated `.c` files could also pollute `app/src`.

### Root cause
- The source selector explicitly skipped `main.py` and only selected `core.*`, `services.*`, and `models.*`; `utils` was therefore copied as source. The staging step copied the entire source tree before removing only selected modules.

### Changes
- Compile every application `.py` module by default, including `main.py` and `utils.paths`; only package `__init__.py` markers remain as Python files.
- Remove the default Cython exclusion list.
- Generate C files under `build/cython` instead of writing them into `app/src`.
- Exclude `__pycache__`, `.pyc`, `.pyo`, and generated C/C++ files from staging.
- Add regression tests for module selection, clean C generation, and source removal.

### Verification
- Local regression suite passes.
- `git diff --check` passes.

# v0.16.6

- Fixed Mach-O byte-order detection for macOS native dependency inspection. The previous parser reversed the arm64 CPU type and reported `0x0c000001`, rejecting valid Apple Silicon dylibs such as `libtcl9.0.dylib`. Universal Mach-O binaries are now checked for the requested architecture instead of being accepted unconditionally.

## 0.16.5

### Fixed

- Fix fresh development environments failing at the Cython step with `No module named cython`.
- Automatically bootstrap the supported Cython 3.1.x build dependency into a py_upper-owned cache instead of requiring a manual install into the developer Python.
- Keep the Cython cache isolated from the user's selected development environment and reuse it on subsequent builds.

### Verification

- 15 local tests passed.
- Cython bootstrap regression test covers the missing-dependency path.
- The v0.16.4 macOS relative-path/`--force` Cython fix remains covered by regression tests.

## 0.16.4

### Fixed

- Fix Cython source translation failing on macOS when an absolute source/output path did not materialize the requested `.c` file.
- Run Cython from the source directory with relative paths and `--force`, then validate the generated artifact with actionable diagnostics.

### Verification

- Local regression tests pass.
- `git diff --check` passes.

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

# py_upper Changelog

## 0.16.2 — Build Tool Python 3.8+ compatibility

### Problem
- Python 3.10 could not start `tools/build.py` because the build tool imported `tomllib`, which was added to the Python standard library in 3.11.
- Build-tool type annotations needed to remain importable on Python 3.8.
- The boundary between the Build Tool Python and the bundled Target Python needed to be explicit for legacy-runtime work.

### Changes
- Add `tools/py_upper/compat.py` with a vendored Tomli fallback for Python 3.8–3.10.
- Keep Python 3.11+ on standard-library `tomllib`.
- Normalize build-tool modules around postponed annotations.
- Declare Python 3.8 as the minimum Build Tool Python and provide a clear diagnostic below it.
- Add compatibility tests and documentation.
- Keep Target Runtime selection independent from Build Tool Python.

### Verification
- Python 3.13: 12 tests passed.
- TOML fallback path exercised by simulating an unavailable `tomllib`.
- `tools/build.py --doctor --target linux-x86_64` passed locally.
- Python 3.10 is included in the CI compatibility matrix for direct validation.

## 0.16.1 — Project identity and source layout refactor
- Rename project identity to `py_upper`.
- Flatten `app/src/myapp/` to `app/src/`.
- Make `app/src/main.py` the direct development entrypoint.
- Rename internal tooling namespace to `tools/py_upper` and native launcher implementation to PyUpper.
- Add `[tool.py_upper.app]` for independently configurable packaged App name and bundle identifier.
- Make packaging, verification, native dependency scanning and release manifests consume the configured App name.

## 0.16.0
- Add release manifest with target, version, file size and SHA-256.
- Centralize project version usage.
- Add Windows ARM64 CI coverage and artifact aggregation.

## 0.15.0
- Add native multi-platform integration CI and validate the complete build/package/run chain.

## 0.14.0
- Consolidate runtime, SDK, wheel and native dependency packaging.

## 0.13.0
- Stabilize the seven-target matrix and improve target ABI/native dependency handling.

## 0.12.0
- Introduce runtime manifests and locked-build validation.

## 0.11.0
- Introduce PBS/local runtime providers and separate development Python from target Python.

## 0.10.0
- Strengthen reproducible locks, target wheels and ABI-aware native extensions.

## 0.9.6
- Stabilize the Cython/SDK/launcher/runtime/package chain and signing foundations.

## 0.9.5
- Add reproducible runtime/wheel inputs and release workflow foundations.

## 0.9.4
- Separate runtime, SDK, application build and packaging responsibilities.

## 0.9.3
- Establish the first cross-platform launcher/runtime packaging pipeline.

## 0.9.2
- Introduce the unified build entrypoint and early target configuration.

## 0.9.1
- Establish the initial application, launcher, runtime and tooling architecture.
