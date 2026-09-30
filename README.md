# PyStand2

Standalone Python application runtime/packaging for Windows, macOS and Linux.

## One command

The public build interface is intentionally small. **You normally only need `tools/build.py`.**

```bash
python tools/build.py
python tools/build.py --target linux-x86_64
python tools/build.py --run
python tools/build.py --verify
python tools/build.py --doctor
python tools/build.py --lock --target linux-x86_64
python tools/build.py --locked --target linux-x86_64
python tools/build.py --release --target macos-arm64 --identity "Developer ID Application: ..." --notary-profile pystand-notary
python tools/build.py --clean
```

The old positional forms remain accepted for compatibility.

## Development Python vs target Python

These are intentionally independent.

- **Development Python** runs `tools/build.py`, Cython, setuptools and pip. It is selected from `PYSTAND_PYTHON`, `app/.venv`, the current interpreter, or `PATH`.
- **Target Python** is the interpreter bundled into the final application and is configured by `[tool.pstand.runtime].python`.
- The target compiler uses the target Python headers/libs, so a Python 3.13 development machine can build a Python 3.8 target package when the required target SDK is supplied.

Example:

```toml
[tool.pstand.runtime]
provider = "pbs"
python = "3.13"
```

The development environment does not need to be a venv. In VS Code use **Python: Select Interpreter** to choose any supported environment (venv, conda, pyenv, Poetry-managed Python, system Python, etc.). `PYSTAND_PYTHON` is available for explicit builds.

## Runtime manifests

Every target runtime is now accompanied by a `manifest.json`. The manifest is the build contract between the runtime, SDK, target ABI and PyStand. It records the provider, target triple, Python version/ABI/platform tag, runtime root, SDK root and (for PBS) the release asset and SHA256.

The build/verify/lock flow is intentionally closed:

```text
runtime + SDK
    ↓
manifest.json
    ↓
lock (manifest SHA256)
    ↓
build
    ↓
verify
```

A `local` runtime must provide a valid manifest; a PBS runtime gets its manifest generated from the PBS `PYTHON.json` metadata. PBS documents `PYTHON.json` as the machine-readable distribution description intended for downstream consumers.

## Runtime providers

Modern targets normally use `pbs`:

```toml
[tool.pstand.runtime]
provider = "pbs"
python = "3.13"

[tool.pstand.pbs]
release = "20260929"
```

A custom runtime can use `local`:

```toml
[tool.pstand.runtime]
provider = "local"
python = "3.8.10"
runtime = "runtimes/{target}/{python}"
sdk = "runtimes/{target}/{python}-sdk"
```

The local provider is intended for runtimes that PBS does not provide or that require a legacy/custom build. For Windows XP, the bundled interpreter must itself be an XP-compatible CPython build; simply selecting the official CPython 3.8.10 release does not make XP supported.

## Targets

```text
windows-x86
windows-x86_64
windows-arm64
macos-x86_64
macos-arm64
linux-x86_64
linux-arm64
```

Linux uses GNU/glibc targets by default. PBS documents the corresponding LLVM target triples and currently supports Windows 32-bit (`i686-pc-windows-msvc`) as well as Windows x64/ARM64, macOS Intel/ARM64 and Linux x64/ARM64. 

## Build pipeline

```text
pyproject.toml
    ↓
runtime provider
    ↓
target SDK + runtime
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

Generated C is produced by the development Python/Cython environment, but native extensions are compiled against the target Python SDK. Cython's setuptools integration supports generating C during the build and compiling it through the target extension toolchain.

## Locking

`pystand.lock.json` records the target runtime provider, target Python, runtime manifest SHA256, PBS release/assets/checksums when PBS is used, and exact wheel hashes. `--locked` verifies these inputs before doing any build work and is intentionally offline.

## VS Code

The repository includes workspace settings, tasks and launch configurations under `.vscode/`. No absolute Python interpreter path is committed. Select the interpreter from VS Code or set `PYSTAND_PYTHON` before running a task.

## Validation

CI covers native Linux x86_64, Windows x86_64, macOS Intel and macOS ARM64, plus Linux ARM64 cross-build verification. A host can only execute its native target; cross-built artifacts are verified without execution.
