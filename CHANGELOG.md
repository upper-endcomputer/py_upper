## 0.17.12

### Problem
- The VS Code launch configuration could not build the packaged application on a Windows host: `python tools/build.py --run` stopped at `[4/6] Package + native dependency closure` with `ERROR: PermissionError: [WinError 5] access denied: '...\dist\Upper' -> '...\build\retired\Upper.37880'`, and every stage that leads to it had failed at least once on the way.
- The Qt pruning step died before it could report anything: on Windows the staged target interpreter aborted in preinit with `Fatal Python error: _Py_HashRandomization_Init: failed to get random numbers to initialize Python`.
- The target extensions and the launcher did not compile against the SDK's headers: Cython's own ABI guard, `1 / (int)(SIZEOF_VOID_P == sizeof(void*))`, failed, and `std::snprintf` resolved to a `std::_snprintf` that the GNU C++ library does not have.
- CMake configuration ended in `Invalid character escape` before the resource compiler was ever invoked.
- The payload's architecture guard rejected the whole `windows-x86_64` bundle over a 32-bit `ucrtbase.dll` that no bundled image can load.
- The native dependency closure refused the bundle over Qt libraries the packager had already deleted, because the only executable linking them had been left behind.
- `[6/6]` ran the packaged application and reported `No module named 'QtCore'`, on a payload that was intact.
- Retiring the previous bundle at `[4/6]` read "another process still has this tree open" as "this tree may not be touched", which turns an ordinary rebuild over a running application into a hard failure that names no culprit.

### Root cause
- `qt_prune._runtime_closure()` handed its child probe a hand-built environment block: `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE` and `PATH=/usr/bin:/bin:/usr/local/bin`. That is a POSIX environment, not a smaller copy of the host's, and the interpreter being started is a Windows CPython: without `SystemRoot` it aborts in preinit while initializing hash randomization. Every Windows host build with `qt = "imports"` failed inside pruning, before the launcher was even configured.
- The SDK ships the MSVC-shaped `pyconfig.h` of the official Windows build, and two of its statements describe the wrong ABI for a MinGW-w64 driver. The pointer width is defined only on the branch `pyconfig.h` reaches through `_MSC_VER`, which a GNU driver never defines, so a 64-bit target fell through to the 32-bit view of a runtime whose `PyConfig` the launcher shares. Separately, `pyerrors.h` renames `snprintf` to `_snprintf` under `MS_WIN32 && !HAVE_SNPRINTF` -- right for MSVC's pre-C99 CRT, wrong for MinGW-w64's C99 one.
- CMake copies some `-D` values verbatim into the cache-reload scripts it generates. `CMAKE_RC_COMPILER` lands in `CMakeRCCompiler.cmake` as `set(CMAKE_RC_COMPILER "...")`, where a Windows path is re-read as a CMake string literal and `D:\XY_Project\...` is the invalid escape `\X`. A compiler path CMake finds for itself is normalized; one handed in through `-D` is not, so the tool has to spell it.
- The `win_amd64` PySide2/shiboken2 wheels ship a 32-bit `ucrtbase.dll`. The UCRT belongs to Windows: it is on the closure's host-library list (`native/deps.py`), so it is never resolved and copied, the loader takes it from `System32`, and no image in either wheel maps the copy the wheel carries. Leaving it in makes the arch guard reject an otherwise correct bundle.
- PySide ships each tool it does not import twice, once bare and once under a binding-prefixed name: `lupdate.exe` beside `pyside2-lupdate.exe`. The prefixed copy is a full build of the same tool, and it links the Qt modules that tool needs (`lupdate` reads `Qt5Xml`) rather than the ones the application imports, so the reachability sweep removed those libraries while leaving the only executable that referenced them.
- `top_level.txt` is a claim about a wheel rather than a record of it, and wheels do get it wrong: PySide2's and shiboken2's both declare a bare `QtCore` that neither distribution installs, because their modules live under `PySide2/` and `shiboken2/`. Trusting the claim made the build's own smoke test import a module no distribution provides, so an intact payload failed `[6/6]`.
- Windows reports "another process still holds this tree" as a bare access-denied on the tree itself. At the call site that is indistinguishable from a real permission problem, and the holders are ordinary and short-lived: the application the previous `--run` is still closing, an editor or indexer that resolved and executed `dist/**/python`, an antivirus scan of a freshly written payload.

### Changes
- `qt_prune._runtime_closure()` inherits `os.environ` and overrides only the two variables the probe actually needs, the way every other subprocess in the build tool does; the comment states why a hand-built block is not a smaller host environment.
- `toolchain.windows_gnu_cpython_defines(target)` holds the ABI correction in one place: `-DHAVE_SNPRINTF` always, plus `-DMS_WIN64` for `x86_64` and `arm64` (the 32-bit `pyconfig.h` already reaches the `MS_WIN32` branch, so adding it there would be a second, wrong story). `ensure_zig()` spells them into both the `CC`/`CXX` frontends and `Toolchain.compiler_args`, so the launcher's CMake build and the Cython extensions compile against one view of the headers instead of two.
- `python_build.compile_windows_extensions_with_zig()` forwards `tc.compiler_args`; without it the defines reach the launcher but not the extensions.
- `package._cmake_path()` spells every path a `-D` value carries for CMake (`CMAKE_RC_COMPILER`, `PYSTAND_PYTHON_INCLUDE`, `PY_UPPER_EMBEDDED_SOURCE`, `PY_UPPER_RESOURCE_RC`). `Path.as_posix()` is the identity on POSIX, so only Windows changes.
- `app/pyproject.toml.example`'s `[tool.py_upper.native].exclude` drops `PySide6/ucrtbase.dll` and `shiboken6/ucrtbase.dll` beside the `vccorlib140.dll` entries, with the reason written above them and a test pinning both lines. The patterns match nothing on Linux or macOS: these are wheel defects, not platform requirements.
- `native/inspect.py` appends `_ARCH_MISMATCH_REMEDY` to both architecture-mismatch messages. The guard is still the right answer for a foreign payload, but the reader has to be told that `[tool.py_upper.native].exclude` is the door out of it, or the tempting move is to weaken the guard.
- `qt_prune.prune()` drops the binding-prefixed spelling beside the bare one, from the same `binding.lower()` prefix the rest of the module already derives.
- `third_party._record_import_roots()` is the RECORD scan the old fallback already had, extracted and promoted to the deciding one: a declared name now counts only if RECORD installs it, and RECORD alone describes the distribution when nothing declared survives. The dist-name fallback stays for distributions with no RECORD at all, and the scan stays platform-neutral because RECORD entries are always `/`-separated.
- `fs.retire_tree()` runs its three steps through `_housekeeping()`, which retries only `winerror` 5/32/33 (access denied, sharing violation, lock violation) up to 30 times at 0.25 s, re-raises anything else immediately, and fails with a message naming the processes that plausibly hold the tree. A POSIX `PermissionError` carries no `winerror`, so nothing is retried there and that behavior is unchanged.
- Tests: `test_runtime_closure_probe_inherits_the_host_environment`, `test_zig_windows_frontend_carries_the_cpython_header_corrections`, `test_zig_compiled_windows_extensions_name_the_64_bit_abi`, `test_launcher_configure_spells_every_path_for_cmake`, `test_template_drops_the_32_bit_ucrt_orphan`, `test_arch_mismatch_names_the_documented_remedy`, `test_windows_pruning_drops_the_binding_prefixed_tool_copy`, `test_direct_import_discovery_rejects_a_declared_module_the_wheel_does_not_install`, and the three retirement cases (`test_retire_tree_waits_out_a_tree_windows_still_holds`, `test_retire_tree_names_what_holds_a_tree_it_cannot_retire`, `test_retire_tree_does_not_retry_a_real_permission_error`). Each was confirmed to fail against the unfixed source.
- `docs/DEVELOPMENT_GUIDE.md` §13 maps the retirement `WinError 5` failure to what holds the tree and to the wait that now precedes the failure.

### Verification
1. `python -m pytest app/tests -q`: 192 passed, 7 skipped (at 0.17.11: 183 passed, 5 skipped; 11 cases were added). `pyflakes` over `app/src`, `app/tests` and `tools/py_upper`, `compileall` over `tools/py_upper` and `tools/build.py`, and `git diff --check` are clean.
2. The launch configuration that was failing now completes end to end: `python tools/build.py --run` on a Windows host with the PBS build SDK (CPython 3.8.10) and `PySide2==5.15.2.1` runs `[1/6]`…`[6/6]`, launches the packaged application, reports `Application exit code: 0`, and leaves `dist/Upper` behind.
3. `python tools/build.py doctor` exits 0.
4. Red before green, per fix: with the change reverted, the corresponding test fails on the unmodified source (the extension build reports `SIZEOF_VOID_P 4`, the generated configure command carries a backslash in a `-D` value, the arch guard rejects the wheel's `ucrtbase.dll`, the prefixed tool copy survives pruning and its Qt5Xml dependency is therefore swept, the smoke test dies on `QtCore`, and the retirement step raises on its first attempt).

## 0.17.11

### Problem
- A packaged Windows application died the moment its window was built: `AttributeError: 'PySide2.QtWidgets.QApplication' object has no attribute 'exec'`, raised from `mvvm/mvvm_bootstrap.py:65` in `MVVMQtApplication.run`. The launcher started, the interpreter came up, Qt imported, `QApplication` was constructed — and then the process ended before anything was shown, with only a native crash dialog to explain it.
- The Qt5 side of the desktop had a second silent defect behind that one: the high-DPI attributes were set on the `QApplication` instance after it existed, which Qt5 ignores once the application is built.
- A packaged bundle carried three `.int` files next to the launcher: `<App>.int` (the entry), `_py_upper_static.int` (a byte-identical copy of the same entry under a name that survives renaming the executable) and `<App>.smoke.int` (the program the build runs under `PY_UPPER_SMOKE=1`). One of them was the application and two were copies of it, and the third was build-verification data that no user installs.
- `py_quote()` in `launcher/src/PyUpper.cpp` wrapped every value it handed to Python in a raw literal (`r'...'`) and then escaped the backslash as `\\`. A raw literal keeps both characters, so a Windows path reached Python with every separator written twice. Nothing depended on it being right — the launcher only passes UTF-8 paths and Python accepts the doubled form — so it sat there looking like correct escaping while doing the opposite.
- The application name was the placeholder `MyApp` in the tracked template and in both fallback defaults, and `app/tests/test_e2e_local.py` spelled `dist/MyApp.app` by hand. The name is configuration, but the local end-to-end test carried a second copy of it, so renaming the application did not mean changing one line: it made that test point at a directory that no longer exists.

### Root cause
- `MVVMQtApplication.run()` named the event-loop entry itself: `self.qt_app.exec()`. `exec` is the Qt6 spelling; the Qt5 wrappers were generated while `exec` was still a Python 2 statement, so PySide2 and PyQt5 only publish `exec_`. Writing either name binds the application to one Qt major, and the failure appears only when the loop is actually entered.
- Nothing exercised that line. The build's smoke step, the unit tests and the manual runs all took the headless branch, which calls `processEvents()` and never opens a loop, and every real-Qt leg on this host had PySide6 installed — the binding where `exec` happens to be right. The first Qt5 runtime to run the interactive path was the user's packaged application, which is why a one-word difference reached `dist/`.
- The high-DPI call had the same shape of bug: `app.setAttribute(...)` after `QApplication(...)` is a no-op on Qt5, which reads those attributes while the application object is constructed and logs `Attribute Qt::AA_EnableHighDpiScaling must be set before QCoreApplication is created`. The setting looked present, the warning was easy to read past, and the scaling never applied.
- Entry resolution was inherited from PyStand, whose layout is "the entry is a file next to the executable". That makes renaming the executable a problem for the launcher, and the fix was to write the same program a second time under a name that does not follow the executable. The smoke program reused the same mechanism — write a script, let the launcher run it — which is why verification data ended up in `dist/`.
- The three files were three copies of one program. Both reasons they existed (rename safety, and a smoke program the launcher can run) are answered by putting the programs *in* the launcher, which is where the entry effectively already lived: the packaged entry is generated from `[tool.py_upper].entry` and has no hand-written copy to keep in sync.
- `py_quote` mixed two escaping schemes in one function. A raw literal means "no escapes at all", and the loop below it escaped anyway; only one of the two can own the job.

### Changes
- `utils/qt_compat.py` gained `exec_loop(target, *args, **kwargs)`: it probes the object for `exec` then `exec_`, calls whichever the binding ships, forwards arguments and the return code, and raises `AttributeError: <type> has neither exec nor exec_ (binding X, Qt N)` when neither exists. It is the same capability probe `Signal`/`Slot` already use, and it makes the shim the one place that knows the difference — the module docstring now says so.
- `mvvm/mvvm_bootstrap.py` enters the loop through `exec_loop(self.qt_app)`, so the bootstrap no longer names a Qt major anywhere.
- The Qt5 high-DPI branch sets the attribute where Qt5 reads it: `QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)` — the static, before `QApplication(...)` is constructed. The comment states why the order matters instead of leaving the next reader to rediscover the warning.
- `app/tests/test_qt_compat.py` covers `exec_loop` dispatching on the binding's spelling, argument/return-code pass-through, the missing-entry error, and a new AST invariant (`test_only_the_shim_names_the_event_loop_entry`) that fails on any `.exec(`/`.exec_(` call in `app/src` outside the shim — the same guard shape as the existing single-file binding rule.
- `app/tests/test_mvvm_example.py` splits the fake into `FakeApplicationSurface` (the shared surface, with a class-level `setAttribute` that records into `CREATED["qt_attributes"]`) plus `FakeApplication` (Qt6, `exec`) and `FakeQt5Application` (Qt5, only `exec_`, whose constructor asserts the high-DPI attribute was already set). Two tests use them: `test_run_gui_enters_the_loop_the_binding_ships` drives the whole `run_gui()` path against a PySide2-shaped binding — the historical version reproduces the reported `AttributeError` there — and `test_the_qt5_high_dpi_attribute_is_set_before_the_window` pins the ordering.
- `docs/DEVELOPMENT_GUIDE.md` §9.1 gained rule 6 (the loop is entered through `exec_loop`, enforced by the AST invariant), §9.4's `MVVMQtApplication` paragraph names `exec_loop`, and the §13 table maps the `AttributeError` to ≥0.17.11.
- `package.write_embedded_source()` generates `py_upper_embedded.cpp`: `py_upper_embedded::kEntry` and `::kSmoke` as C++ raw string literals carrying the entry program and the smoke program. `build_launcher()` writes it into the build directory and passes `-DPY_UPPER_EMBEDDED_SOURCE=`, and `launcher/CMakeLists.txt` compiles it in and defines `PY_UPPER_HAS_EMBEDDED_SOURCE` so `PyUpper.cpp` knows the symbols exist. `write_entry_scripts()` and `_entry_script()` are gone and `package()` writes no `.int` file at all.
- `config.entry_source()` is the single source of the entry program, shared by `package.py` and `verify.py` (which cannot import the packager without a cycle). `STATIC_ENTRY` is removed from the Python side; the launcher keeps its own constant for hand-made bundles.
- `_embedded_literal()` refuses a payload containing the raw-string delimiter's closing form or a triple double quote instead of quietly switching to another delimiter. The second is what the launcher's bootstrap wraps the same text in, so a payload carrying one would end that Python literal early; both are generator invariants, so they raise.
- Resolution order is now embedded first, file chain second. A packaged launcher never looks next to itself, so a leftover sidecar from the old layout cannot shadow the entry the binary was built with. The file chain (`<stem>.int` → `.py` → `.pyw` → `_py_upper_static.int`) remains for a launcher built by hand, which has nothing embedded to prefer; `PY_UPPER_SMOKE=1` runs the embedded smoke program and falls back to `<stem>.smoke.int` on that same build.
- The embedded entry runs through `exec(compile(source, ENTRY_FILE, "exec"))` with `ENTRY_FILE` set to the launcher; a file entry still goes through `runpy.run_path()`. One consequence is recorded in the guide: `runpy`'s `_ModifiedArgv0` temporarily rewrites `sys.argv[0]` to the script path, so an embedded entry now keeps `sys.argv[0]` as the launcher path.
- `PY_UPPER_SCRIPT` is exported only when the entry really is a file. Naming a path that does not exist is worse than saying nothing, and a packaged launcher has no such file.
- `py_quote()` is a normal single-quoted literal with real escapes now (`\\`, `\'`, `\n`, `\r`, `\t`, and `\xNN` for control bytes), so one function owns escaping again.
- `verify.py` replaces the two file-existence checks with "no `.int` sidecar next to the launcher" and "the launcher's bytes contain `entry_source()`". The second is the check the first cannot do: it proves the binary that was built carries the entry the configuration names. `run_launcher_smoke()` no longer looks for `<stem>.smoke.int` before running — the run itself is what proves the embedded smoke program.
- The generated declarations start at file scope in `PyUpper.cpp`, not inside its anonymous namespace: inside it they named a different, internal symbol and the launcher link failed with `variable '(anonymous namespace)::py_upper_embedded::kEntry' has internal linkage but is not defined`. The real build is what caught it; the unit tests only read the source text.
- The tracked template, the working copy and both mirroring fallbacks ship `Upper` now: `app/pyproject.toml` / `app/pyproject.toml.example` (`[tool.py_upper.app].name`, annotated in place as the single source of truth), `app/src/utils/paths.py` (`_DEFAULT_APP_NAME`) and `tools/py_upper/config.py` (`or "Upper"`). The last two answer the same question — "the name, when neither a launcher nor a `pyproject.toml` is reachable" — from the two sides of the packaging boundary, which is why they move together.
- `app/tests/test_e2e_local.py::_packaged_root()` reads the name out of the fixture's own `app/pyproject.toml` (through `py_upper.compat.tomllib`) instead of hardcoding `dist/MyApp.app`, and the package-verification test in `app/tests/test_build_tool_compat.py` pins `verify.app_name` to the name its fixture directories were built with, the way the bundle tests in the same file already do.
- README「App 名称」is an operating procedure now: which single line to edit, that the tracked template has to move with it, what the name reaches (bundle / executable / `Info.plist` names / Windows version resource / `PY_UPPER_APP_NAME` / window title) and what it does not (renaming the executable, `identifier`, the project name), and that a rename leaves the old bundle in `dist/` because only `--clean` removes it. The guide's layout diagram, configuration example, pruned-size measurement and rename example follow, plus a troubleshooting row for "renamed it and the bundle still has the old name".

### Verification
1. Tests: `python3 -m pytest app/tests -q` is 183 passed, 5 skipped (at 0.17.9: 175 passed, 5 skipped; the Qt5 line added 6 — the 4 `exec_loop` cases plus the 2 example cases — and the embedded-entry line dropped one and added three: the generated translation unit, `_embedded_literal` refusing a payload, and the `py_quote` escaping pin). The matrix pair, `pytest app/tests/test_build_tool_compat.py app/tests/test_py_upper_config.py -q`, is 77 passed, 2 skipped. pyflakes over `app/src`, `app/tests` and `tools/py_upper`, `compileall` and `git diff --check` are clean. `PY_UPPER_E2E=1 python3 -m pytest app/tests/test_e2e_local.py -q` is 2 passed — the test that used to carry the application name itself.
2. Red before green: the two new example tests reproduce `AttributeError: 'FakeQt5Application' object has no attribute 'exec'` from `mvvm_bootstrap.py:65` on the unmodified source and pass after it.
3. Real Qt runtimes: a real Qt5 runtime ran the interactive path — PyQt5 5.15.11 (arm64 wheel, installed under `/tmp/qt5venv`) reports `binding: PyQt5 qt_major: 5` and `run() exit code: 0`, and the `AA_EnableHighDpiScaling must be set before` warning is gone with the attribute set statically. Real PySide6 (staged `site-packages`, offscreen, `QTimer.singleShot(0, app.quit)`) also exits 0 through `run()`.
4. macOS arm64 full build, `python3 tools/build.py --target macos-arm64`: `[1/6]`…`[6/6]`, 100 PASS / 0 FAIL, `READY: dist/Upper.app` (`--verify` at 100 PASS / 0 FAIL), launcher smoke exit 0. `find dist -name '*.int'` is empty, `Resources/` holds `runtime` and `site-packages` only, `PY_UPPER_SMOKE=1 …/Upper.app/Contents/MacOS/Upper` prints `SMOKE PASS launcher` and exits 0, `codesign --verify --deep --strict` passes, and the interactive path is up (the process is still alive after 6 s, the window was built, `Metal API Validation Enabled` on stderr). `Info.plist` reports `CFBundleName` / `CFBundleDisplayName` / `CFBundleExecutable` = `Upper` while `CFBundleIdentifier` stays `com.example.pyupper`, the launcher bytes contain `Upper` and zero occurrences of `MyApp`, and the real window title is `Upper`: an offscreen probe against the packaged bundle prints `windowTitle()` as `Upper`, and as `Whatever` when run with `PY_UPPER_APP_NAME=Whatever`. The same build was also run before the rename and reported `READY: dist/MyApp.app`, which is why some figures below still name the old bundle.
5. Windows cross build, `PY_UPPER_WINDOWS_TOOLCHAIN=zig python3 tools/build.py --target windows-x86_64`: `READY: dist/Upper` with `--verify` at 106 PASS / 0 FAIL, `launcher subsystem=gui`, no `.int` in the bundle, both programs found in the PE bytes (`b'from main import main'`, `b'SMOKE PASS launcher'`), and a generated `py_upper_resource.rc` naming `Upper` in FileDescription / InternalName / ProductName and `Upper.exe` in OriginalFilename. Both smokes print `SKIP … cross target` as before.
6. Behaviour probes, all on copies under `/tmp` so `dist/` stays untouched:
  - Rename plus poisoned sidecars: `Contents/MacOS/MyApp` renamed to `Renamed`, with `MyApp.int` and `_py_upper_static.int` planted in `Resources/` that print a marker and `raise SystemExit(99)`. The application starts normally (`Hello from py_upper`, `GUI OK offscreen`, exit 0) and neither sidecar runs, which is both halves of the claim in one run.
  - Hand-made launcher: `cmake -S launcher -B …` without `PY_UPPER_EMBEDDED_SOURCE` builds an 85 KB launcher. Placed as `Contents/MacOS/Manual` with a `Manual.int` beside it in `Resources/`, it runs that file and reports `sys.argv[0]` as the sidecar path (exit 7); with no sidecar it fails with `Entry script missing in …/Resources` / `Expected Manual.int, Manual.py, Manual.pyw, _py_upper_static.int or an entry compiled into the launcher` and exit 3.
  - Embedded identity: deleting the packaged `main` extension makes the run fail, and the reported traceback's innermost frame is `File "./MyApp.app/Contents/MacOS/MyApp", line 1, in <module>` — `ENTRY_FILE` is the launcher itself, and the crash-report path still reports it.
  - `PY_UPPER_SCRIPT`: a `sitecustomize.py` in the packaged `site-packages` prints the environment on startup. With an embedded entry `PY_UPPER_SCRIPT` is `None` while `PY_UPPER_HOME` and `PY_UPPER_APP_NAME` are unchanged.
- Rename leftovers: both stale artifacts from the old name — `dist/MyApp.app` and `dist/MyApp` — survived the rename exactly as the new troubleshooting row says they would, and were deleted by hand.

### Limitations
- The Windows bundle still needs a Windows machine to be started: the cross build proves the entry bytes are inside the PE and the subsystem is the GUI one, not that Explorer can double-click it. The Windows legs of CI remain the end-to-end evidence.
- This host cannot run the binding that failed: PySide2 publishes no macOS arm64 wheel and Rosetta is unavailable, so the Qt5 leg rests on the PySide2-shaped fake-binding tests and on `exec_loop`'s probe against real PyQt5 (which ships both spellings). The end-to-end confirmation is a repackage on the Windows machine that hit this — `python tools/build.py --target windows-x86_64`, then double-click `dist\Upper\Upper.exe`.
- The generator refuses a program text carrying the raw-string delimiter's closing form or a triple double quote. The entry is three lines and the smoke program two, so the fixed delimiter is enough today; an entry that needed to carry such text should move to a searched delimiter or a byte array rather than have this check relaxed.
- `PY_UPPER_SCRIPT` is no longer exported by a packaged launcher. Anything deriving the entry script path from it now reads `None` instead of a path that does not exist; the guide and the environment table say so.

## 0.17.9

### Problem
- Windows targets could only be produced on a Windows host with Visual Studio: `resolve_toolchain` raised `Windows targets require a Windows/MSVC build host` on macOS and Linux before the first compiler step, so the only way to exercise a Windows bundle was CI.
- Qt was addressed by one hard-wired binding. Application code had to write `from PySide6.QtWidgets import ...`, and the pruning pass matched that literal prefix; another binding meant either an import error at run time or a prune pass that quietly kept the full 1.2 GB payload.
- The MVVM layer was a second, incomplete copy: `app/src/mvvm/models/app_model.py` imported four modules that do not exist anywhere in the tree, `PropertyFactory` and `ViewModelFactory` were each defined twice, and Cython was still compiling the dead model.
- The MVVM package could not carry an application on its own. The container had no ViewModel or View registry (an `add_view()` list sat beside them), its service locator was class-level, nothing ever called `start()`/`stop()`, and there was no example that closed the loop from a click to a window, so every gap was a guess rather than a failing test.
- The tree still carried folders that are not part of the architecture: `app/src/models/` held the dead `AppState`, `app/src/plugins/` was an empty package, and the service layer lived outside the MVVM package in `app/src/services/`.

### Root cause
- The toolchain resolver treated "can this host build Windows at all" and "which compiler do we use" as one decision (`vswhere` → `vcvarsall.bat`), so there was no code path in which a non-Windows host emits PE objects.
- The Qt binding was implicit at every import site. Nothing in the tool stated which binding the application uses, so switching bindings was a source-wide edit, and `qt_prune` failed open: its regex matched nothing, it printed `app/src does not reference a Qt binding, keeping the full Qt payload`, and the build continued to ship everything.
- The MVVM port copied a directory rather than the module graph, so files that were dead in the source tree arrived as-is and the factories were duplicated instead of moved.
- The port stopped at the layers that happened to compile. With no window ever driving the framework, a container whose registries were missing, whose `run()` duplicated `add_view()`, and whose locator was shared by every application looked fine; the same reason kept the leftovers in place — nobody imported them, so nothing broke.
- Qt names were imported at module level in `views/main_window.py` and `mvvm_bootstrap.py`. That made "import the application entry point" mean "install Qt", so the binding-free import the framework is supposed to guarantee was never actually exercised.

### Changes
- `PY_UPPER_WINDOWS_TOOLCHAIN=zig` lets a macOS or Linux host build Windows targets. The host-native Zig release named by `PY_UPPER_ZIG_VERSION` (default `0.16.0`) is resolved from `ziglang.org/download/index.json`, verified against the SHA256 in that index, unpacked under `build/host-tools/zig/<version>-<host>` behind a `.verified` marker, and invoked as `zig cc`/`zig c++` with `-target <arch>-windows-gnu`. The MSVC triple is rewritten to the GNU one because Zig 0.16 bundles no MSVC cross CRT, and the packaging triple's `i686` is translated to the architecture name Zig actually knows (`x86`). Archive members are validated (no traversal, no absolute paths, no duplicates) before unpacking, and a checksum mismatch aborts the build.
- `Toolchain.compiler_args` carries the arguments that turn a compiler path into a usable frontend, so a cross compiler is a first-class toolchain rather than a string baked into `CC`. Windows extension compilation gained an executable path that does not need the host setuptools driver: `zig cc -shared -I<include> -L<libs> -lpython3XX` against the SDK import library. The launcher's CMake configure receives generated `zig-cc`/`zig-cxx`/`zig-rc` wrappers plus `CMAKE_SYSTEM_NAME=Windows`, `CMAKE_RC_COMPILER`, `-municode` and `CMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY`.
- Cross targets no longer pretend they ran the target: `run_launcher_smoke` prints `SKIP launcher smoke: cross target` when the target is not the host, matching the runtime smoke that already skipped.
- The macOS architecture no longer appears twice in the toolchain environment: extensions pass `-arch <arch>` themselves and the launcher uses `CMAKE_OSX_ARCHITECTURES`, so `CFLAGS`/`CXXFLAGS`/`LDFLAGS`/`ARCHFLAGS` carry no second copy.
- The launcher disables the embeddable runtime's `python3XX._pth` at startup (renamed to `.disabled`), because that file cuts `site-packages` out of the search path the launcher configures itself.
- `utils/qt_compat.py` is a module proxy instead of a function table: `from utils.qt_compat import QtWidgets` hands back a module object built by PEP 562 attribute resolution, so an IDE completes `QtWidgets.QMainWindow` and a type checker reads the `PySide6` stubs through the `TYPE_CHECKING` import, while no binding is imported at module import time. `Signal`/`Slot` keep their spelling on either binding (`Signal`/`pyqtSignal`, `Slot`/`pyqtSlot`), a misspelled `Qt*` module fails on the import line, a missing attribute reports the binding, and a test asserts that only this file names a binding.
- `qt_prune` reads imports with `ast` instead of one regex: it detects the binding in use (`pyside2`/`pyside6`/`pyqt5`/`pyqt6`), follows imports that come through `utils.qt_compat`, and raises with the offending path when a source file cannot be parsed, instead of failing open.
- The MVVM package lives in `app/src/mvvm/` without the dead model, with one home per factory (`mvvm/core/factory.py`) and unused imports removed; the template gains the Windows-shaped PySide6 plugin/QML exclude prefixes so both wheel layouts prune the same payload.
- `MVVMApplication` owns its registries (`register_viewmodel`/`get_viewmodel`, `register_view`/`get_view`, `register_service`/`resolve_service`) plus `start()` and `stop()`; `ServiceLocator` is per instance, and the redundant `add_view()`/`run()` pair is gone. `MVVMQtApplication.prepare()` fixes the order once (container → viewmodels → views → `start()`), and subclasses only fill in `initialize_viewmodels()`/`initialize_views()`.
- The Qt names move into the functions that actually build a window (`create_application()`, `MainWindow.__init__`), so `import application` loads no binding — pytest, pyflakes and Cython compile the application without a Qt install, and a test asserts it.
- `ViewBase` binds correctly: a binding whose target resolves to a callable calls it (`QLabel.setText`) instead of `setattr`-ing over the method name, and a two-way binding has to name its getter (`getter_name=`) rather than have one guessed from the setter — the guess silently read the wrong value for every Qt pair whose names differ (`setText`/`text`, `setWindowTitle`/`windowTitle`).
- The desktop gains a working example: a `关于 MVVM 架构` button binds to `MainViewModel.show_about_command`, which flips the `about_visible` property; the main window opens the About window on that notification and the About window's close button binds to `hide_about_command`. One ViewModel serves both windows and `about_visible` is the single source of truth.
- The Python 3.8 leg of the compatibility matrix failed while 3.9 through 3.13 stayed green: that job runs `app/tests/test_build_tool_compat.py` and `app/tests/test_py_upper_config.py`, which loads `app/tests/conftest.py`, and the new fake-binding helper annotated its parameter as `dict[str, dict[str, dict[str, object]]]` without `from __future__ import annotations`. A PEP 585 generic in a signature is evaluated when the module is defined, so 3.8 raised `TypeError: 'type' object is not subscriptable` while importing conftest, before a single test ran. The missing future imports are added, and a new guard test walks `app/src`, `app/tests` and `tools` and fails on any module that evaluates a PEP 585 generic or a PEP 604 union at definition time.
- `app/src/models/` and `app/src/plugins/` are deleted (dead state object, empty package), and the service layer moves into the architecture it belongs to: `app/src/services/hello.py` becomes `mvvm/services/greeting.py` with a `GreetingService` the container registers and injects. `app/src/utils/` stays — `utils.paths.app_name()` is the launcher contract.
- The Zig cross path produced a launcher that was not a GUI one. CMake spells `WIN32_EXECUTABLE` as `-mwindows`, and clang's driver — the one Zig goes through — reports it as an unused argument and drops it, so every cross-built launcher linked the console subsystem: an Explorer double-click opened a console window beside the application and the `AttachConsole(ATTACH_PARENT_PROCESS)` path was never reached. `launcher/CMakeLists.txt` now asks its non-MSVC linkers for `-Wl,--subsystem,windows` on the target itself (MSVC keeps using `WIN32_EXECUTABLE`, whose linker does not accept `-Wl,...`), and the console escape hatch is untouched because it is a different branch. `BinaryInfo` gained a `subsystem` field read from `IMAGE_OPTIONAL_HEADER.Subsystem` (PE32 and PE32+ agree on that offset), and `verify.py` fails a Windows package whose launcher is not the expected subsystem — GUI by default, console only under `PY_UPPER_LAUNCHER_CONSOLE=1` — so the defect can no longer pass silently.
- Zig does cross-compile `windows-x86`; the guard claiming otherwise was wrong. `zig cc -target x86-windows-gnu` links the real launcher and a `python313.lib`-linked extension as PE32 (`machine 0x14c`, `subsystem 2`), the same command the MSVC path uses, so "Zig 0.16 ships no 32-bit Windows cross toolchain" only blocked a build that works. The guard is gone along with the test that pinned it, `CMAKE_SYSTEM_PROCESSOR` comes from the packaging target (`x86`/`AMD64`/`ARM64`) instead of an arm64/AMD64 ternary that reported every 32-bit configure as AMD64, and `ensure_zig` translates the triple's `i686` into the `x86` Zig names.
- `app/src/core/` is gone, because it collided with the framework's own `app/src/mvvm/core/`. The composition root — the one file that imports all four MVVM layers — is now `app/src/application.py`, and the Qt shim joins the launcher contract in `app/src/utils/qt_compat.py`, where `utils` stays a leaf package that imports no application module. `main.py` is still the entry file and only hands control to the composition root.

### Verification
- `python -m pytest app/tests -q`: 175 passed, 5 skipped (at the previous commit: 141 passed, 5 skipped; 30 new tests); the same suite is green on CPython 3.8.20, and `pytest app/tests/test_build_tool_compat.py app/tests/test_py_upper_config.py -q` — the exact pair the 3.8 matrix job runs — is 71 passed, 2 skipped; `PY_UPPER_E2E=1 python -m pytest app/tests/test_e2e_local.py -q`: 2 passed; pyflakes clean over `app/src`, `app/tests` and `tools/py_upper`, `compileall` and `git diff --check` clean.
- macOS arm64 full build, `[1/6]`…`[6/6]`: 100 PASS, `Qt pruning: removed 315 file(s) and 158 director(ies), freed 1101 MB`; staging `site-packages` fell from 1.2 GB to 109 MB (PySide6 1.2 GB → 105 MB) and `dist/MyApp.app` is 100 MB with 58 MB of PySide6 inside it. The same build before the prune fix printed `keeping the full Qt payload`. `--run` exits 0 with `GUI OK offscreen` and `codesign --verify --deep --strict` passes. The staged `site-packages` now holds `application`, `main`, `mvvm`, `utils` and the wheels only — no `models/`, `plugins/` or top-level `services/`.
- The example was exercised against a real PySide6 6.11.0 runtime on the offscreen platform: the window lists `Hello from py_upper` and the `关于 MVVM 架构` button, clicking it opens the About dialog (title `关于 MVVM 架构`, body describing the four layers), and the dialog's close button hides it again.
- `PY_UPPER_WINDOWS_TOOLCHAIN=zig python tools/build.py --target windows-x86_64` finishes `[1/6]`…`[6/6]` on macOS arm64 and prints `READY: dist/MyApp` with 433 native binaries checked; both runtime smokes report `SKIP ... cross target`.
- New tests cover the Qt proxy (module and attribute resolution, `Signal`/`Slot` spelling on both bindings, the single-file binding invariant), the prune pass reading the shim (including a parenthesised multi-line import) and refusing an unparseable source, the whole MVVM module graph importing against a fake binding, the container registries/`start`/`stop` and per-application service scoping, the binding rules (setter call vs. attribute assignment, the explicit getter a two-way binding needs), the example's whole chain — button click → command → `about_visible` → window shown/hidden — against fake Qt components, and the Zig archive/host guards. The subsystem work is covered by parsing the optional header of all three cross-built launchers, which report `subsystem=2 (gui)` for windows-x86_64, windows-arm64 and windows-x86 while the `PY_UPPER_LAUNCHER_CONSOLE=1` build reports `subsystem=3 (console)`; the dist launcher built before this fix fails the new check with `launcher subsystem: console, expected gui` and the freshly built one passes it. `compile_windows_extensions_with_zig` on windows-x86 emits `probe_module.cp313-win32.pyd` with `machine=0x14c` importing `python313.dll`.

### Limitations
- The Zig path emits MinGW-w64 (`*-windows-gnu`) binaries, which is not the ABI a Visual Studio build produces. Windows hosts keep the MSVC path unchanged, and the Windows CI legs remain the only end-to-end evidence for the PE bundle.
- Dynamic Qt imports (`importlib`, QML `import`) stay invisible to the prune pass; such an application must list its modules explicitly.

## 0.17.8

### Problem
- A Windows build stopped at the first compiler step with `RuntimeError: Target Python import library not found under ...\runtimes\windows-x86_64\3.13.5`. Every Linux and macOS leg built, so the failure belonged to the shape of the Windows runtime rather than to the target: the import library the linker needs was being read out of the release runtime, which since 0.17.7 is the python.org embeddable ZIP and carries no `libs/` directory at all.

### Root cause
- The tree the compiler reads and the tree that ships were addressed by one field. `TargetPython.root` held the *runtime* root while the include directory beside it already came from the SDK through the runtime manifest; the import-library lookup was the one consumer that kept walking the packaged tree, and it only worked by accident — before 0.17.7 the release runtime *was* the PBS install tree, which happens to carry `libs/python3XX.lib`.
- The SDK info dict exposed the compiler-facing tree under `root`, and the two providers filled that key with different trees: PBS with the SDK, `local` with the runtime. Reading it decided which of the two a build got.

### Changes
- `TargetPython.root` is `sdk_root` now, and `resolve_target_python()` reads `info["sdk_root"]`. Both providers name the SDK tree the same way; where the packaged tree is reported it is called `runtime_root`, so no key means two different directories.
- `_windows_import_library()` searches `libs/` and `install/libs/` under the SDK. A missing versioned import library now reports the SDK path and states that the library is a compiler input and is never taken from the runtime.
- The development guide states which tree each Windows build input comes from and lists the failure in the troubleshooting table.
- The local end-to-end fixture follows the same split: the import libraries it links against sit in the SDK half, not in the runtime half it copies.

### Verification
- `python -m pytest app/tests -q`: 141 passed, 5 skipped.
- The import-library test now plants a `libs/python313.lib` in the *runtime* tree and leaves only the shim in the SDK: the build must fail there, and the successful case asserts the linker's search path is the SDK path and never the runtime path. On the old lookup this test passes silently instead.
- `PY_UPPER_E2E=1 python -m pytest app/tests/test_e2e_local.py -q`: 2 passed.
- Full macOS arm64 PySide6 build on the same pin: `[1/6]`…`[6/6]` green, 40 PASS, `--run` exit 0, `codesign --verify --deep --strict` valid.

### Limitations
- The Windows legs of CI remain the only end-to-end evidence for the Windows bundle; this machine cannot execute a PE binary.

## 0.17.7

### Problem
- The Windows package shipped the same PBS runtime as every other platform: a regular CPython install tree (`Lib/`, `DLLs/`, `include/`, `libs/`, `tcl/`, `Scripts/`, 3 271 files and 65 MB for 3.13.5 x86_64) even though the launcher embeds the interpreter and nothing ever runs `python.exe`. The embedding layout python.org publishes for Windows was not used at all.
- The target Python pin was only ever checked against PBS. Python 3.11.13 is a valid PBS version with no official Windows embeddable ZIP, so a pin that cannot produce a Windows package looked correct until a Windows build ran.

### Root cause
- `runtime.py` had one acquisition path per provider, and the PBS path is the POSIX shape: unpack `install_only_stripped` into the runtime root. Windows needs the same interpreter in a different shape, published by a different project than the one publishing the build SDK.
- The Windows compiler-side needs are not the release runtime's needs: resolving target wheels and probing the Qt import closure must run an interpreter with pip, while the embeddable interpreter has no pip and ignores `PYTHONPATH` through its `._pth`.

### Changes
- Windows with `provider = "pbs"` takes its release runtime from the official python.org embeddable ZIP (`python-<version>-embed-{win32,amd64,arm64}.zip`). It is cached under `.cache/python.org/<python>/<target>/`, validated before installation (flat single-level members, no traversal, no duplicates, required members present), unpacked unmodified into `runtimes/<target>/<python>`, and recorded in the lock file as `runtime.source = "python.org"` with its SHA256.
- PBS keeps supplying the full SDK for the same target Python: extension compilation, `pip download`/target resolution and the Qt import-closure probe all use the SDK interpreter. The SDK stays a build input and never enters `dist/`.
- `runtime_executable()` returns `<runtime>/python.exe` for Windows PBS, since the embeddable layout has no `bin/`.
- `_checks_for_package()` asserts the packaged Windows runtime is the embeddable shape: `python3.dll`, `python3XX.dll`, `python3XX.zip`, `python3XX._pth` present, `Lib/` and `DLLs/` absent.
- Launcher: an embeddable runtime keeps its standard extensions beside `python3XX.dll` instead of in `DLLs/`, so the Windows search path falls back to the runtime root when `DLLs/` is missing.
- `verify_lock()` rejects a locked Windows PBS target whose runtime was not recorded as coming from python.org, so an existing lock cannot silently rebuild the old shape.
- A pinned version python.org publishes no embeddable ZIP for now fails with the version, the target and the URL instead of a bare HTTP 404.
- `app/pyproject.toml.example` moves to `3.13.5`, the version available both as three Windows embeddable ZIPs and across all seven PBS SDKs; README, the development guide and the CI cache key follow.

### Verification
- `python -m pytest app/tests -q`: 140 passed, 5 skipped.
- All seven target triples resolve PBS inputs at 3.13.5 (tag 20250723).
- The three real python.org ZIPs were downloaded and unpacked: 34/36/35 entries, flat layout, required members present, and every image passes the target architecture guard (29/30/30 images, 0 failures; 17.2/20.2/20.8 MB).
- windows-x86_64 end to end: PBS SDK 3.13.5 downloaded (270 MB, `install/python.exe`), runtime manifest format 4 written and validated, lock written (format 5, `source: python.org`) and `verify_lock()` passes.
- Full macOS arm64 PySide6 build on the new pin: `[1/6]`…`[6/6]` green, 40 PASS including `native binaries checked=48`, launcher smoke, `--run` exit 0, `codesign --verify --deep --strict` valid.

### Impact
- The Windows runtime payload drops from 65 MB / 3 271 files to 20 MB / 36 files (x86_64, 3.13.5) and no longer contains `Lib/`, `DLLs/`, `include/`, `libs/`, `tcl/`, `Scripts/` or a pip to prune. The interpreter version does not change; only the runtime shape and its publisher do.

### Limitations
- The Windows bundle can only be executed on Windows, so the three Windows CI legs (packaged launcher import smoke) remain the only end-to-end evidence for it.

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
