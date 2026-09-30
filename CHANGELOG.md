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
