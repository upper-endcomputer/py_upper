from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .compat import tomllib

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
CACHE = ROOT / ".cache"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
RUNTIMES = ROOT / "runtimes"
LAUNCHER = ROOT / "launcher"
LOCK = ROOT / "py_upper.lock.json"
MIN_BUILD_PYTHON = (3, 8)
# Entry file that does not depend on the executable name, so a renamed launcher
# still finds its application. The launcher searches for it as a fallback after
# the executable-derived names; keep both sides in sync (launcher/src/PyUpper.cpp).
STATIC_ENTRY = "_py_upper_static.int"
# The tracked template and the Git-ignored working copy of the application
# configuration. Everything reads the working copy; the template is what a
# fresh clone starts from.
APP_CONFIG = APP / "pyproject.toml"
APP_CONFIG_EXAMPLE = APP / "pyproject.toml.example"


@dataclass(frozen=True)
class Target:
    os: str
    arch: str
    triple: str

    @property
    def key(self) -> str:
        return f"{self.os}-{self.arch}"

    @property
    def python_tag(self) -> str:
        return "cp" + python_version().replace(".", "")[:3]

    @property
    def wheel_platforms(self) -> tuple[str, ...]:
        if self.os == "windows":
            return {
                "x86": ("win32",),
                "x86_64": ("win_amd64",),
                "arm64": ("win_arm64",),
            }[self.arch]
        if self.os == "macos":
            if self.arch == "arm64":
                return (
                    "macosx_13_0_arm64",
                    "macosx_12_0_arm64",
                    "macosx_11_0_arm64",
                    "macosx_13_0_universal2",
                    "macosx_12_0_universal2",
                    "macosx_11_0_universal2",
                )
            return (
                "macosx_13_0_x86_64",
                "macosx_12_0_x86_64",
                "macosx_11_0_x86_64",
                "macosx_10_15_x86_64",
                "macosx_13_0_universal2",
                "macosx_12_0_universal2",
                "macosx_11_0_universal2",
            )
        if self.os == "linux":
            arch = {"x86_64": "x86_64", "arm64": "aarch64"}[self.arch]
            return tuple(_manylinux_tag(baseline, arch) for baseline in manylinux_baselines())
        raise RuntimeError(f"Unsupported target: {self.key}")

    @property
    def primary_wheel_platform(self) -> str:
        return self.wheel_platforms[0]

    @property
    def extension_platform(self) -> str:
        if self.os == "windows":
            return {"x86": "win32", "x86_64": "win_amd64", "arm64": "win_arm64"}[self.arch]
        if self.os == "linux":
            return "x86_64-linux-gnu" if self.arch == "x86_64" else "aarch64-linux-gnu"
        return "darwin"


@dataclass(frozen=True)
class RuntimeSpec:
    provider: str
    python: str
    target: Target
    root: Path
    sdk_root: Path

    @property
    def major_minor(self) -> str:
        return ".".join(self.python.split(".")[:2])

    @property
    def abi_tag(self) -> str:
        return f"cp{self.major_minor.replace('.', '')}"


@dataclass(frozen=True)
class TargetPython:
    target: Target
    root: Path
    executable: Path
    include_dir: Path
    python_version: str
    python_major_minor: str
    abi_tag: str
    extension_suffix: str

    @property
    def platform_tags(self) -> tuple[str, ...]:
        return self.target.wheel_platforms


TARGETS = {
    "windows-x86": Target("windows", "x86", "i686-pc-windows-msvc"),
    "windows-x86_64": Target("windows", "x86_64", "x86_64-pc-windows-msvc"),
    "windows-arm64": Target("windows", "arm64", "aarch64-pc-windows-msvc"),
    "macos-x86_64": Target("macos", "x86_64", "x86_64-apple-darwin"),
    "macos-arm64": Target("macos", "arm64", "aarch64-apple-darwin"),
    "linux-x86_64": Target("linux", "x86_64", "x86_64-unknown-linux-gnu"),
    "linux-arm64": Target("linux", "arm64", "aarch64-unknown-linux-gnu"),
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def manylinux_baselines() -> list[str]:
    """Configured manylinux baselines, newest first.

    A single baseline is not enough for modern projects: PySide6 publishes
    ``manylinux_2_34_x86_64`` and ``manylinux_2_39_aarch64`` wheels, so the
    per-arch requirement differs. Listing several baselines lets pip pick the
    best match for each target while keeping the older ones as fallback.
    """
    value = dependency_config().get("manylinux")
    if value in (None, ""):
        return ["manylinux_2_17"]
    if isinstance(value, str):
        baselines = [value]
    elif isinstance(value, list):
        baselines = [str(item) for item in value]
    else:
        raise RuntimeError(
            "[tool.py_upper.dependencies].manylinux must be a manylinux_X_Y string or an array of them"
        )
    cleaned = [baseline.strip() for baseline in baselines if str(baseline).strip()]
    if not cleaned:
        raise RuntimeError("[tool.py_upper.dependencies].manylinux must not be empty")
    for baseline in cleaned:
        if baseline != "manylinux2014" and not baseline.startswith("manylinux_"):
            raise RuntimeError("[tool.py_upper.dependencies].manylinux must be manylinux_X_Y or manylinux2014")
    return cleaned


def _manylinux_tag(baseline: str, arch: str) -> str:
    return f"manylinux2014_{arch}" if baseline == "manylinux2014" else f"{baseline}_{arch}"


def _readable_path(path: Path) -> str:
    """A path for error messages: project-relative when that is expressible.

    ``os.path.relpath`` raises ``ValueError`` when the two paths live on
    different Windows drives, which happens whenever the configuration is read
    from a temporary directory. A missing-configuration error must not turn into
    a path-arithmetic crash, so fall back to the absolute path.
    """
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return str(path)


def load_app_config() -> dict:
    if not APP_CONFIG.is_file():
        target = _readable_path(APP_CONFIG)
        example = _readable_path(APP_CONFIG_EXAMPLE)
        raise RuntimeError(
            f"{target} is missing.\n"
            "Create your local configuration from the tracked template:\n"
            f"    cp {example} {target}"
        )
    with APP_CONFIG.open("rb") as f:
        return tomllib.load(f)


def py_upper_config() -> dict:
    value = load_app_config().get("tool", {}).get("py_upper", {})
    return value if isinstance(value, dict) else {}


def project_config() -> dict:
    value = load_app_config().get("project", {})
    return value if isinstance(value, dict) else {}


def project_version() -> str:
    value = str(project_config().get("version") or "").strip()
    if not value:
        raise RuntimeError("[project].version is required")
    return value


def app_config() -> dict:
    value = py_upper_config().get("app", {})
    return value if isinstance(value, dict) else {}


def app_name() -> str:
    value = str(app_config().get("name") or "MyApp").strip()
    if not value or value in {".", ".."} or any(sep in value for sep in ("/", "\\")):
        raise RuntimeError("[tool.py_upper.app].name must be a single application name")
    return value


def app_identifier() -> str:
    value = str(app_config().get("identifier") or "com.example.pyupper").strip()
    if not value:
        raise RuntimeError("[tool.py_upper.app].identifier must not be empty")
    return value


def entry_path() -> Path:
    value = str(py_upper_config().get("entry") or "main.py").strip()
    root = (APP / "src").resolve()
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("[tool.py_upper].entry must stay under app/src") from exc
    if path.suffix != ".py" or not path.exists():
        raise RuntimeError(f"Application entry file does not exist: {path}")
    return path


def entry_module() -> str:
    return ".".join(entry_path().relative_to(APP / "src").with_suffix("").parts)


def user_agent() -> str:
    return f"py_upper/{project_version()}"


def host_target() -> Target:
    system = platform.system()
    machine = platform.machine().lower()
    aliases = {
        ("Windows", "amd64"): "windows-x86_64",
        ("Windows", "x86_64"): "windows-x86_64",
        ("Windows", "arm64"): "windows-arm64",
        ("Windows", "aarch64"): "windows-arm64",
        ("Windows", "x86"): "windows-x86",
        ("Darwin", "arm64"): "macos-arm64",
        ("Darwin", "aarch64"): "macos-arm64",
        ("Darwin", "x86_64"): "macos-x86_64",
        ("Darwin", "amd64"): "macos-x86_64",
        ("Linux", "arm64"): "linux-arm64",
        ("Linux", "aarch64"): "linux-arm64",
        ("Linux", "x86_64"): "linux-x86_64",
        ("Linux", "amd64"): "linux-x86_64",
    }
    name = aliases.get((system, machine))
    if not name:
        raise RuntimeError(f"Unsupported host: {system} {machine}")
    return TARGETS[name]


def validate_target(name: str | None) -> Target:
    if not name:
        return host_target()
    target = TARGETS.get(name)
    if target is None:
        raise SystemExit(f"Unknown target: {name}; available: {', '.join(TARGETS)}")
    return target


def runtime_config() -> dict:
    value = py_upper_config().get("runtime", {})
    return value if isinstance(value, dict) else {}


def python_version() -> str:
    value = str(runtime_config().get("python") or "").strip()
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise RuntimeError("[tool.py_upper.runtime].python must be an exact X.Y.Z Python version")
    return value


def runtime_provider() -> str:
    provider = str(runtime_config().get("provider") or "pbs").strip().lower()
    if provider not in {"pbs", "local"}:
        raise RuntimeError(f"Unsupported runtime provider: {provider!r}; use 'pbs' or 'local'")
    return provider


def runtime_spec(target: Target) -> RuntimeSpec:
    provider = runtime_provider()
    python = python_version()
    if provider == "local":
        root = local_runtime_path(target)
        sdk = local_sdk_path(target)
    else:
        root = RUNTIMES / target.key / python
        sdk = CACHE / "pbs-sdk" / target.key / python
    return RuntimeSpec(provider, python, target, root, sdk)


def local_runtime_path(target: Target) -> Path:
    value = runtime_config().get("runtime")
    if not value:
        raise RuntimeError("[tool.py_upper.runtime].runtime is required when provider = 'local'")
    return _expand_target_path(str(value), target)


def local_sdk_path(target: Target) -> Path:
    value = runtime_config().get("sdk")
    if not value:
        raise RuntimeError("[tool.py_upper.runtime].sdk is required when provider = 'local'")
    return _expand_target_path(str(value), target)


def _expand_target_path(value: str, target: Target) -> Path:
    return (ROOT / value.format(target=target.key, os=target.os, arch=target.arch, python=python_version())).resolve()


def pbs_release() -> str:
    cfg = py_upper_config().get("pbs", {})
    return str(cfg.get("release") or "").strip() if isinstance(cfg, dict) else ""


def cython_config() -> dict:
    value = py_upper_config().get("cython", {})
    return value if isinstance(value, dict) else {}


def optimize_config() -> dict:
    value = py_upper_config().get("optimize", {})
    return value if isinstance(value, dict) else {}


def native_config() -> dict:
    value = py_upper_config().get("native", {})
    return value if isinstance(value, dict) else {}


def native_exclude_patterns() -> list[str]:
    """Glob patterns (relative to the bundled site-packages) to drop.

    Third-party wheels occasionally ship native files whose dependencies cannot
    exist in the bundle: PySide6's macOS wheel links the ODBC/Mimer/PostgreSQL
    Qt SQL drivers against build-machine absolute paths and references a
    QtQuickShapesDesignHelpers framework it does not ship. Those plugins are
    optional, so they are removed explicitly instead of failing the closure.
    """
    value = native_config().get("exclude", [])
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise RuntimeError("[tool.py_upper.native].exclude must be an array of glob patterns")
    return [str(item).strip() for item in value if str(item).strip()]


def dependency_config() -> dict:
    value = py_upper_config().get("dependencies", {})
    return value if isinstance(value, dict) else {}


PIP_TIMEOUT_DEFAULT = 120
PIP_RETRIES_DEFAULT = 10


def _positive_int(value, fallback: int, name: str) -> int:
    if value is None or value == "":
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise RuntimeError(f"{name} must not be negative")
    return parsed


def pip_transfer_args() -> list[str]:
    """pip transfer settings for large wheels over unreliable links.

    The default 15 second socket timeout aborts a multi-hundred-megabyte wheel
    such as PySide6 Addons on any single stall, which then discards the whole
    download. ``[tool.py_upper.dependencies].timeout`` / ``.retries`` and the
    PY_UPPER_PIP_TIMEOUT / PY_UPPER_PIP_RETRIES environment variables override
    the defaults.
    """
    cfg = dependency_config()
    timeout = _positive_int(cfg.get("timeout"), _env_int("PY_UPPER_PIP_TIMEOUT", PIP_TIMEOUT_DEFAULT), "[tool.py_upper.dependencies].timeout")
    retries = _positive_int(cfg.get("retries"), _env_int("PY_UPPER_PIP_RETRIES", PIP_RETRIES_DEFAULT), "[tool.py_upper.dependencies].retries")
    return ["--timeout", str(timeout), "--retries", str(retries)]


def pip_cache_dir() -> Path:
    """py_upper-owned pip download cache, reused across builds and CI runs."""
    return CACHE / "pip"


def target_runtime_dir(target: Target) -> Path:
    return runtime_spec(target).root


def staging_dir(target: Target) -> Path:
    return BUILD / "staging" / target.key


def wheel_dir(target: Target) -> Path:
    return BUILD / "wheels" / target.key


def pbs_sdk_dir(target: Target) -> Path:
    return runtime_spec(target).sdk_root


def target_extension_suffix(target: Target, python_major_minor: str, abi_tag: str) -> str:
    digits = python_major_minor.replace(".", "")
    if target.os == "windows":
        return f".{abi_tag}-{target.extension_platform}.pyd"
    if target.os == "linux":
        return f".cpython-{digits}-{target.extension_platform}.so"
    return f".cpython-{digits}-darwin.so"


def validate_build_python_version(version_info=None) -> None:
    info = version_info or sys.version_info
    actual = (int(info.major), int(info.minor))
    if actual < MIN_BUILD_PYTHON:
        raise RuntimeError(
            f"py_upper build tool requires Python {MIN_BUILD_PYTHON[0]}.{MIN_BUILD_PYTHON[1]}+; "
            f"current interpreter is {info.major}.{info.minor}"
        )


def python_executable() -> Path:
    explicit = os.environ.get("PY_UPPER_PYTHON") or os.environ.get("PYSTAND_PYTHON")
    if explicit:
        return Path(explicit).expanduser().resolve()
    local = APP / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if local.exists():
        return local.resolve()
    current = Path(sys.executable).resolve()
    if current.exists():
        return current
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    raise RuntimeError("No development Python found. Set PY_UPPER_PYTHON or install Python.")


def require_local_python() -> Path:
    path = python_executable()
    if not path.exists():
        raise RuntimeError(f"Development Python not found: {path}")
    probe = subprocess.run(
        [str(path), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split()
    if len(probe) != 2:
        raise RuntimeError(f"Could not determine development Python version: {path}")
    major, minor = int(probe[0]), int(probe[1])
    if (major, minor) < MIN_BUILD_PYTHON:
        raise RuntimeError(f"Development Python {major}.{minor} is too old; Python 3.8+ is required: {path}")
    return path


def runtime_executable(target: Target) -> Path:
    """Locate the target runtime's own interpreter, when it is present locally."""
    return _find_target_executable(runtime_spec(target).root, target)


def host_description() -> str:
    return f"{platform.system()} {platform.machine()} Python {platform.python_version()}"


def _find_target_executable(root: Path, target: Target) -> Path:
    names = ["python.exe", "python3.exe"] if target.os == "windows" else [
        f"python{python_version()}", f"python{'.'.join(python_version().split('.')[:2])}", "python3", "python"
    ]
    candidates = []
    for name in names:
        candidates.extend((root / "bin" / name, root / "install" / "bin" / name))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    for name in names:
        for candidate in root.rglob(name):
            if candidate.is_file():
                return candidate
    raise RuntimeError(f"Target Python executable not found under {root}")


def _find_target_include(sdk_root: Path) -> Path:
    for candidate in (
        sdk_root / "python" / "include",
        sdk_root / "python" / "install" / "include",
        sdk_root / "include",
        sdk_root / "install" / "include",
    ):
        if candidate.exists() and (candidate / "Python.h").exists():
            return candidate
    matches = sorted(p.parent for p in sdk_root.rglob("Python.h"))
    if matches:
        return matches[0]
    raise RuntimeError(f"Target Python include directory not found under {sdk_root}")


def resolve_target_python(target: Target) -> TargetPython:
    from .runtime import ensure_sdk, sdk_info

    spec = runtime_spec(target)
    ensure_sdk(target)
    info = sdk_info(target)
    sdk_root = Path(str(info.get("root") or spec.sdk_root)).resolve()
    executable = Path(str(info.get("python_executable") or ""))
    if not executable.exists():
        executable = _find_target_executable(spec.root, target)
    include = Path(str(info.get("include_dir") or ""))
    if not include.exists():
        include = _find_target_include(sdk_root)
    actual = str(info.get("python") or spec.python)
    if actual != spec.python:
        raise RuntimeError(f"Target Python {actual} does not match configured runtime Python {spec.python}")
    major_minor = ".".join(actual.split(".")[:2])
    abi = f"cp{major_minor.replace('.', '')}"
    suffix = target_extension_suffix(target, major_minor, abi)
    return TargetPython(target, spec.root, executable, include, actual, major_minor, abi, suffix)
