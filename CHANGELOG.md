# py_upper Changelog

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
