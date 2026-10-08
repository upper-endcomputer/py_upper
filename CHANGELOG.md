## 0.17.6

### Problem
- Every build regenerated the Cython translation unit of every application module and recompiled every extension, and ran those invocations one at a time. A rebuild cost the same as a first build, and the cost scaled with the size of the application instead of the size of the edit.

### Root cause
- `build/cython` and `build/native/<target>` were deleted at the start of every build and regenerated with `cython --force`, so no output ever survived a build. Each module was compiled by its own blocking `subprocess.run`, so the wall clock was the sum of every file.

### Changes
- Cython output and compiled extensions are cached under `build/cache/`, keyed by content: a translation unit by the source text and the Cython version, an extension by the exact compiler command line (compiler, architecture, ABI suffix, SDK include directory, flags) and the content of the C it compiles. A module whose inputs did not change is taken from the cache instead of rebuilt. The two layers chain, so editing a `.py` invalidates its `.c` and its own extension and nothing else.
- Only current sources are staged, so artifacts of removed or renamed modules can never be packaged, and the "wipe the intermediate tree first" step is gone.
- Cython and compiler invocations run on a thread pool, one job per CPU by default, because each invocation is an independent CPU-bound process.
- `[tool.py_upper.build]` adds `jobs` (0 = one per CPU) and `incremental` (default `true`; `false` forces a full recompile).
- `--clean` still removes `build/`, and therefore the cache.

### Verification
- 115 unit/compatibility tests pass (5 skipped), including 8 new tests that pin cache reuse, per-module invalidation, Cython-version invalidation, the `incremental = false` escape hatch, the Windows setuptools path and the new configuration.
- Two consecutive full macOS arm64 PySide6 builds: 33.2 s then 31.4 s, the second reporting `Cython: 5 module(s) up to date` and `Compile: 5 extension(s) up to date`, with `--verify` green (56 native binaries checked) and the launcher smoke passing.
- On a synthetic 60-module application (14 CPUs) the Cython + extension stage went from 48.0 s sequential to 4.5 s parallel cold, 0.14 s with nothing changed, and 0.74 s after editing a single module.

### Impact
- Rebuild time now scales with the size of the edit rather than the size of the application. `build/cache/` grows with the application and is disposable: `--clean` removes it.

## 0.17.5

### Problem
- The launcher inherited four gaps from PyStand that only show up in a finished application: no entry fallback when the executable is renamed, no way for the application to learn its own bundle root, a fatal error that vanished silently when there was no console, and a Windows build that either opened a console window on every launch or lost its output entirely.
- A packaged Windows executable had no version resource, so it looked like an anonymous binary in the file properties.

### Root cause
- Everything was derived from `argv[0]`; diagnostics went to a stream that may not exist; the Windows build used the default (dynamic) CRT and the default subsystem; and the application guessed its resource directory by counting parent directories.

### Changes
- Entry resolution now tries `<stem>.int`, `<stem>.py`, `<stem>.pyw` and finally the rename-safe `_py_upper_static.int`; the packager writes both `<App>.int` and `_py_upper_static.int` with identical content, and `--verify` checks for the static entry.
- The launcher exports `PY_UPPER_HOME`, `PY_UPPER_RUNTIME`, `PY_UPPER_SITE_PACKAGES`, `PY_UPPER_SCRIPT` and `PY_UPPER_EXECUTABLE`. `utils.paths.resource_root()` prefers `PY_UPPER_HOME/resources` and only falls back to counting parent directories when the variable is absent.
- Fatal errors are reported through a native dialog (Windows `MessageBoxW`, macOS `CFUserNotification` with a 120 s timeout, Linux `zenity`/`xmessage`) unless stderr is already visible or `PY_UPPER_NO_DIALOG` is set.
- Unhandled application exceptions are captured by the bootstrap into `TMPDIR/py_upper-<pid>.log` and reported after `Py_FinalizeEx`, so a double-clicked bundle shows a traceback instead of exiting silently; the report file is deleted on the way out.
- Windows: wide-character entry point (`wWinMain`, `wmain` in the console build), UTF-8 paths through `std::filesystem::u8path`, `LoadLibraryExW(LOAD_WITH_ALTERED_SEARCH_PATH)`, `AttachConsole(ATTACH_PARENT_PROCESS)` that never steals an existing redirection, static CRT (`/MT`), GUI subsystem by default with `PY_UPPER_LAUNCHER_CONSOLE=1` as the debug build, and a generated `VERSIONINFO` resource.
- The launcher smoke test and the CI integration run now set `PY_UPPER_NO_DIALOG=1`, so a failure can never block a job on a dialog.
- Removed the leftover `app/resources/config/app.toml` and the packaging-time writer that regenerated it: nothing ever read the file, and the application name already has one source of truth in `app/pyproject.toml`. `app/resources` is now an optional payload directory — a project without it packages normally, and the tree is copied only when it exists.
- The application name is usable from the application itself: the packager passes it to CMake (`-DPY_UPPER_APP_NAME`), the launcher exports `PY_UPPER_APP_NAME` (falling back to its own filename stem only when built by hand), and `utils.paths.app_name()` reads it. Running from a source tree there is no launcher, so the helper reads the same `[tool.py_upper.app].name` instead of a second copy. The demo window title now comes from that helper rather than a hardcoded `"py_upper"`.
- The application configuration is a Git-ignored working copy now: `app/pyproject.toml.example` is the tracked template and `app/pyproject.toml` — application name, dependencies, runtime version — stays on the machine. A missing working copy is a hard error naming the exact `cp` command, not a silent fallback to the template, and CI creates the file before building.
- The tracked template pins no dependencies: `app/pyproject.toml.example` declares `dependencies = []` and describes a real entry in a comment, so a fork cannot inherit the pins of whoever cloned first. The working copy still carries the real set, and the CI integration job materializes its copy with the repository's own dependencies because the repository's application needs PySide6 to run. The E2E fixture rewrites the same key and now anchors its pattern to the start of the line, so a commented-out example can never win over the declaration that is in effect.

### Verification
- macOS arm64 with PySide6 6.11.0: full build + `--run` exits 0 and prints `GUI OK offscreen`; `--verify` reports 40 PASS / 0 FAIL; `codesign --verify --deep --strict` passes.
- Rename probe: the packaged launcher copied to `Renamed.app/Contents/MacOS/Renamed`, with no `Renamed.int` present, starts through `_py_upper_static.int` (exit 0).
- Failure probes: an entry that raises prints the traceback, exits 1 and leaves no report file behind; a missing entry exits 3 and lists the candidate names.
- App-name probe: a bundle whose entry prints `PY_UPPER_APP_NAME` reports `MyApp`; the same bundle with the executable renamed to `Renamed` and no `Renamed.int` still reports `MyApp` through `_py_upper_static.int`, and `utils.paths.app_name()` inside the packaged bundle resolves to the same value.
- Missing-configuration probe: with `app/pyproject.toml` deleted, `--doctor` and the test suite both stop with `app/pyproject.toml is missing` plus the copy command; recreating it from the template restores a clean run. `git check-ignore -v app/pyproject.toml` reports the `.gitignore` rule.
- Template probe: the CI step's script was run against the tracked template and produced an `app/pyproject.toml` that parses with the repository's dependency set; `test_tracked_pyproject_template_is_complete` fails if the template ever pins a dependency again.
- `python -m pytest app/tests -q`: 70 passed, 2 skipped (the E2E pair, skipped without `PY_UPPER_E2E=1`); with `PY_UPPER_E2E=1` both pass as well.

### Limitations
- The Windows launcher is exercised by CI only; the version resource deliberately carries no icon because the repository has no `.ico`/`.icns` asset yet.

## 0.17.4

### Problem
- The build tool had grown to 27 modules, several of which were thin layers around the same concept: `runtime.py`/`runtime_provider.py`/`runtime_optimize.py`, `pbs_assets.py`/`pbs_sdk.py`, `verify.py`/`smoke.py`, `package.py`/`launcher_build.py`/`release_artifacts.py`, plus a `wheel.py` facade nothing imported and a four-times-duplicated `sha256` implementation.

### Root cause
- Each fix landed as its own module instead of extending the module that already owned the concept.

### Changes
- Merge the PBS asset resolver and the SDK acquisition into `pbs.py`.
- Merge runtime provider dispatch, PBS runtime acquisition and release-safe optimization into `runtime.py`.
- Merge the static verification with the target-runtime and launcher smoke tests into `verify.py`.
- Merge the launcher build, bundle assembly and release manifest into `package.py`.
- Move the Unix target extension compiler into `python_build.py`, which already owned the Windows path.
- Delete the unused `wheel.py` compatibility facade and the four duplicate `sha256` implementations; hashing now lives in `fs.sha256`.
- Rename the test modules that followed the removed names (`test_smoke.py` to `test_verify.py`, `test_release_artifacts.py` to `test_package.py`).
- Clear every unused import and variable reported by pyflakes across `tools/`, `app/src` and `app/tests`.

### Verification
- Module count 27 to 19, total lines 3597 to ~3550 with no behaviour change.
- pyflakes reports no undefined names, unused imports or redefinitions.
- Full suite on macOS arm64 with `PY_UPPER_E2E=1`: 62 passed.
- Full PySide6 6.11.0 build on macOS arm64 after the merge: `--run` exit 0, Qt pruning frees 1101 MB, `--verify` 39 PASS / 0 FAIL, `codesign --verify --deep --strict` passes, and the real cocoa window opens.

### Limitations
- The merge is mechanical: public entry points are unchanged, but any external script importing the removed modules must switch to `pbs`/`runtime`/`verify`/`package`.

## 0.17.3

### Problem
- Import-driven Qt pruning removed the macOS cocoa platform plugin, so a packaged application failed to start with `qt.qpa.plugin: Could not find the Qt platform plugin "cocoa"` and only `offscreen`/`minimal` remained.
- The build's Qt smoke test only exercised the offscreen platform, so the regression passed every gate.

### Root cause
- The plugin filter compared a plugin's dependencies against the Qt modules reachable from the kept wrappers, but the dependency-to-module mapping also classified system frameworks (`CoreVideo`, `IOSurface`, `ColorSync`, ...) as Qt modules. `libqcocoa.dylib` links those system frameworks, so it looked like it needed Qt modules the application never imports and was dropped.

### Changes
- Only treat Qt-prefixed frameworks and Qt-prefixed libraries as Qt modules; system frameworks no longer participate in the decision.
- Keep the whole `platforms` plugin category unconditionally: platform plugins decide whether the application starts at all.
- Add regression tests for the system-framework mapping and for a platform plugin that references an extra Qt module.

### Verification
- macOS arm64 with `PySide6==6.11.0`: the packaged launcher reports `PLATFORM cocoa`, the application opens a real window, `--verify` passes (39 PASS / 0 FAIL), `codesign --verify --deep --strict` accepts the bundle, and the pruned payload still measures 154 MB (pruning frees 1101 MB).
- Full suite: 60 passed, 2 skipped.

### Limitations
- The offscreen smoke cannot prove the cocoa/windows/xcb platform plugin loads; the unconditional platform-plugin rule is what guarantees it.

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
