# PyStand2

Standalone Python application packaging for Windows, macOS and Linux.

## Use

One public entry point:

```bash
python tools/build.py
```

Useful options:

```bash
python tools/build.py --target linux-x86_64
python tools/build.py --run
python tools/build.py --verify
python tools/build.py --lock --target linux-x86_64
python tools/build.py --locked --target linux-x86_64
python tools/build.py --doctor
python tools/build.py --clean
```

## Python environments

Development Python and bundled target Python are independent.

Development Python is selected in this order:

1. `PYSTAND_PYTHON`
2. `app/.venv` when present
3. the Python running `tools/build.py`
4. `python` / `python3` on `PATH`

VS Code uses **Python: Select Interpreter**; no interpreter path is committed to the repository.

The bundled Python is configured separately:

```toml
[tool.pstand.runtime]
provider = "pbs"
python = "3.13"

[tool.pstand.pbs]
release = "20260929"
```

A custom runtime uses:

```toml
[tool.pstand.runtime]
provider = "local"
python = "3.8.10"
runtime = "runtimes/{target}/{python}"
sdk = "runtimes/{target}/{python}-sdk"
```

A local runtime must provide a matching `manifest.json`. This is intended for legacy/custom CPython builds, including an XP-compatible build. Official CPython 3.8.10 itself is not an XP runtime.

## Targets

- Windows: x86, x86_64, arm64
- macOS: x86_64, arm64
- Linux: x86_64, arm64

PBS currently documents these target families and uses `PYTHON.json` as machine-readable distribution metadata. Linux GNU distributions generally require glibc 2.17 or newer; CPython 3.13 and earlier Windows distributions require Windows 8.1 or newer.

## Build flow

```text
runtime + SDK
    -> manifest
    -> lock
    -> target wheels
    -> Cython -> C
    -> target compiler -> native extension
    -> launcher + runtime
    -> package
    -> verify
```

Cython generates C using the development environment; the native extension is compiled against the target Python SDK. 

## Reproducibility

`pystand.lock.json` records the target runtime provider, runtime manifest hash, PBS artifacts/checksums when applicable, and wheel hashes. `--locked` validates these inputs before the build and does not resolve or download replacements.

## VS Code

The workspace contains only the useful development/debug configuration under `.vscode/`. Select your own Python interpreter; no `.venv` is required.

## Validation

CI covers Linux x86_64, Linux ARM64, Windows x86_64, macOS Intel and macOS ARM64. Each CI target is built and launched natively.

## Integration validation

CI runs the same build entry point used locally. Linux x86_64, Linux ARM64, Windows x86_64, macOS Intel, and macOS ARM64 are built and launched on native runners.

A runtime manifest is portable: it describes runtime identity and ABI rather than embedding a developer machine's absolute cache paths. `--locked` verifies the manifest hash and all pinned artifacts before building.

## CI artifacts

Every integration job uploads the resulting `dist/` directory as a workflow artifact. The same `python tools/build.py --run` command is used in CI and local development.
