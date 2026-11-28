# 中文维护说明：把应用 TOML 转为目标平台、ABI、路径和有效构建选项；路径以选定应用根为准。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .appdir import selected_app_dir
from .compat import tomllib

ROOT = Path(__file__).resolve().parents[2]


def _default_app_dir() -> Path:
    """选择框架内示例 app；不存在时识别根目录带 appbuild 配置的项目。

    Find the application when no explicit directory was selected.

    The repository keeps its example application under ``app/``. A fork that
    uses the build tool in a normal application repository has no such child
    directory; in that case a root ``pyproject.toml`` carrying a
    ``[tool.appbuild]`` table is the application.
    """
    bundled = ROOT / "app"
    if (bundled / "pyproject.toml").is_file() or (bundled / "pyproject.toml.example").is_file():
        return bundled
    root_config = ROOT / "pyproject.toml"
    if root_config.is_file():
        try:
            with root_config.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, ValueError):
            data = {}
        tool = data.get("tool")
        if isinstance(tool, dict) and isinstance(tool.get("appbuild"), dict):
            return ROOT
    return bundled


APP = selected_app_dir(_default_app_dir())
CACHE = ROOT / ".cache"
BUILD = ROOT / "build"
DIST = ROOT / "dist"
RUNTIMES = ROOT / "runtimes"
LAUNCHER = ROOT / "launcher"
LOCK = ROOT / "appbuild.lock.json"
MIN_BUILD_PYTHON = (3, 8)
# The application configuration. The bundled example keeps a tracked template
# and a Git-ignored working copy; an application supplied with --app or
# APPBUILD_APP_DIR normally has only pyproject.toml.
APP_CONFIG = APP / "pyproject.toml"
APP_CONFIG_EXAMPLE = APP / "pyproject.toml.example"


@dataclass(frozen=True)
class Target:
    os: str
    arch: str
    triple: str

    @property
    def key(self) -> str:
        """返回操作系统和架构组成的稳定目标标识，用作缓存与输出目录名。"""
        return f"{self.os}-{self.arch}"

    @property
    def python_tag(self) -> str:
        """返回 CPython 主次版本标签；补丁版本不参与 wheel 标签。"""
        return "cp" + "".join(python_version().split(".")[:2])

    @property
    def wheel_platforms(self) -> tuple[str, ...]:
        """按优先顺序生成目标可接受的 wheel 平台标签；未知系统报错。"""
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
        """返回目标首选平台标签，供清单记录使用。"""
        return self.wheel_platforms[0]

    @property
    def extension_platform(self) -> str:
        """返回目标原生扩展文件名的平台片段，与构建宿主无关。"""
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
        """从固定运行时版本提取主次版本。"""
        return ".".join(self.python.split(".")[:2])

    @property
    def abi_tag(self) -> str:
        """由主次版本生成目标 CPython ABI 标签。"""
        return f"cp{self.major_minor.replace('.', '')}"


@dataclass(frozen=True)
class TargetPython:
    """The target interpreter as a *build input*, not as a release runtime.

    ``sdk_root`` is the full SDK tree the compiler is pointed at: headers and
    the import library both live there. The runtime that ships in ``dist/`` is
    a different tree and holds no build input at all — on Windows it is the
    python.org embeddable layout, which has no ``libs/`` directory.

    ``executable`` is the interpreter used to run the target-arch smoke test,
    and ``include_dir`` the directory holding ``Python.h``.
    """

    target: Target
    sdk_root: Path
    executable: Path
    include_dir: Path
    python_version: str
    python_major_minor: str
    abi_tag: str
    extension_suffix: str

    @property
    def platform_tags(self) -> tuple[str, ...]:
        """返回 SDK 所属目标的 wheel 平台集合。"""
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
    """读取整数环境变量；缺失或格式错误时采用默认值。"""
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def manylinux_baselines() -> list[str]:
    """读取并校验 manylinux 基线列表；保留配置顺序供 pip 选择目标 wheel。

    Configured manylinux baselines, newest first.

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
            "[tool.appbuild.dependencies].manylinux must be a manylinux_X_Y string or an array of them"
        )
    cleaned = [baseline.strip() for baseline in baselines if str(baseline).strip()]
    if not cleaned:
        raise RuntimeError("[tool.appbuild.dependencies].manylinux must not be empty")
    for baseline in cleaned:
        if baseline != "manylinux2014" and not baseline.startswith("manylinux_"):
            raise RuntimeError("[tool.appbuild.dependencies].manylinux must be manylinux_X_Y or manylinux2014")
    return cleaned


def _manylinux_tag(baseline: str, arch: str) -> str:
    """把 ABI 基线和 Linux 架构拼成 wheel 平台标签。"""
    return f"manylinux2014_{arch}" if baseline == "manylinux2014" else f"{baseline}_{arch}"


def _readable_path(path: Path) -> str:
    """生成相对仓库的诊断路径；Windows 跨盘无法相对化时保留绝对路径。

    A path for error messages: project-relative when that is expressible.

    ``os.path.relpath`` raises ``ValueError`` when the two paths live on
    different Windows drives, which happens whenever the configuration is read
    from a temporary directory. A missing-configuration error must not turn into
    a path-arithmetic crash, so fall back to the absolute path.
    """
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return str(path)


def app_config_label() -> str:
    """返回当前应用配置的可读诊断路径。

    The active application configuration path for diagnostics."""
    return _readable_path(APP_CONFIG)


def load_app_config() -> dict:
    """读取当前应用 TOML；缺失时给出模板复制或应用选择提示。"""
    if not APP_CONFIG.is_file():
        target = _readable_path(APP_CONFIG)
        if APP_CONFIG_EXAMPLE.is_file():
            example = _readable_path(APP_CONFIG_EXAMPLE)
            raise RuntimeError(
                f"{target} is missing.\n"
                "Create your local configuration from the tracked template:\n"
                f"    cp {example} {target}"
            )
        example = _readable_path(APP_CONFIG_EXAMPLE)
        raise RuntimeError(
            f"{target} is missing.\n"
            f"Create it from {example} when that template exists, or point "
            "--app (APPBUILD_APP_DIR) at a directory containing pyproject.toml "
            "and src/."
        )
    with APP_CONFIG.open("rb") as f:
        return tomllib.load(f)


def appbuild_config() -> dict:
    """读取唯一的 tool.appbuild 配置表；缺失或非表时返回空字典。"""
    value = load_app_config().get("tool", {}).get("appbuild", {})
    return value if isinstance(value, dict) else {}


def project_config() -> dict:
    """读取项目元信息表；不存在时返回空字典。"""
    value = load_app_config().get("project", {})
    return value if isinstance(value, dict) else {}


def project_version() -> str:
    """读取必填项目版本；空值抛出 RuntimeError。"""
    value = str(project_config().get("version") or "").strip()
    if not value:
        raise RuntimeError("[project].version is required")
    return value


def app_config() -> dict:
    """读取应用名称及标识配置子表。"""
    value = appbuild_config().get("app", {})
    return value if isinstance(value, dict) else {}


def app_name() -> str:
    """返回应用显示和输出名称；拒绝空值、目录分隔符及点目录。"""
    value = str(app_config().get("name") or "Upper").strip()
    if not value or value in {".", ".."} or any(sep in value for sep in ("/", "\\")):
        raise RuntimeError("[tool.appbuild.app].name must be a single application name")
    return value


def app_identifier() -> str:
    """返回应用缓存与平台标识；显式空值报错。"""
    value = str(app_config().get("identifier") or "com.example.pyupper").strip()
    if not value:
        raise RuntimeError("[tool.appbuild.app].identifier must not be empty")
    return value


def entry_path() -> Path:
    """解析 src 内的 Python 入口；拒绝越界路径、错误后缀和缺失文件。"""
    value = str(appbuild_config().get("entry") or "main.py").strip()
    root = (APP / "src").resolve()
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("[tool.appbuild].entry must stay under the application src/ directory") from exc
    if path.suffix != ".py" or not path.exists():
        raise RuntimeError(f"Application entry file does not exist: {path}")
    return path


def entry_module() -> str:
    """把已校验入口转换为可导入的点分模块名。"""
    return ".".join(entry_path().relative_to(APP / "src").with_suffix("").parts)


def entry_source() -> str:
    """生成 launcher 调用 main 的入口源码；构建和校验共享此唯一来源。

    The program the launcher runs, generated from the configured entry module.

    It is not authored anywhere: the packaged entry only hands control to
    ``entry_module()``, and a hand-written copy would be a second place that
    names it. The packaging step compiles this text into the launcher, and
    ``--verify`` reads it back out of the launcher binary, so the two sides
    cannot drift.
    """
    return f'from {entry_module()} import main\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'


def user_agent() -> str:
    """返回含项目版本的公开下载请求标识。"""
    return f"appbuild/{project_version()}"


def host_target() -> Target:
    """规范化当前系统和 CPU 名称；不支持的宿主抛出 RuntimeError。"""
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
    """解析显式目标或采用宿主；未知名称以 SystemExit 报错。"""
    if not name:
        return host_target()
    target = TARGETS.get(name)
    if target is None:
        raise SystemExit(f"Unknown target: {name}; available: {', '.join(TARGETS)}")
    return target


def runtime_config() -> dict:
    """读取目标运行时配置表。"""
    value = appbuild_config().get("runtime", {})
    return value if isinstance(value, dict) else {}


def python_version() -> str:
    """返回精确的 X.Y.Z 目标版本；不接受缺少补丁号的版本。"""
    value = str(runtime_config().get("python") or "").strip()
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise RuntimeError("[tool.appbuild.runtime].python must be an exact X.Y.Z Python version")
    return value


def runtime_provider() -> str:
    """读取并校验 pbs 或 local 运行时提供方。"""
    provider = str(runtime_config().get("provider") or "pbs").strip().lower()
    if provider not in {"pbs", "local"}:
        raise RuntimeError(f"Unsupported runtime provider: {provider!r}; use 'pbs' or 'local'")
    return provider


def runtime_spec(target: Target) -> RuntimeSpec:
    """汇总目标版本、提供方和 runtime/SDK 路径，不执行下载。"""
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
    """解析 local 提供方的必填 runtime 路径。"""
    value = runtime_config().get("runtime")
    if not value:
        raise RuntimeError("[tool.appbuild.runtime].runtime is required when provider = 'local'")
    return _expand_target_path(str(value), target)


def local_sdk_path(target: Target) -> Path:
    """解析 local 提供方的必填 SDK 路径。"""
    value = runtime_config().get("sdk")
    if not value:
        raise RuntimeError("[tool.appbuild.runtime].sdk is required when provider = 'local'")
    return _expand_target_path(str(value), target)


def _expand_target_path(value: str, target: Target) -> Path:
    """展开目标占位符并相对框架根目录解析路径。"""
    return (ROOT / value.format(target=target.key, os=target.os, arch=target.arch, python=python_version())).resolve()


def pbs_release() -> str:
    """读取固定 PBS release；空字符串表示自动解析。"""
    cfg = appbuild_config().get("pbs", {})
    return str(cfg.get("release") or "").strip() if isinstance(cfg, dict) else ""


def cython_config() -> dict:
    """读取 Cython 源码选择和模块合并配置。"""
    value = appbuild_config().get("cython", {})
    return value if isinstance(value, dict) else {}


MERGE_MODES = ("none", "directory", "all")

# Merged extension bundles live under this directory. The build packs them into
# the egg as stored entries and the launcher extracts and registers them before
# Py_Initialize; the Python egg loader never extracts this prefix.
BUNDLE_DIR = "appbuild_bundles"


def cython_merge_mode() -> str:
    """规范化模块合并粒度；未知值阻止构建。

    How application modules are packaged as native extensions.

    ``none`` keeps one extension per module. ``directory`` links leaf modules by
    top-level package so localized changes only rebuild that bundle.
    ``all`` links all leaves into one image to minimize native loads at startup.
    """
    value = cython_config().get("merge", "none")
    if isinstance(value, bool):
        return "directory" if value else "none"
    text = str(value or "").strip().lower()
    if text in ("", "none", "off", "false", "no", "0"):
        return "none"
    if text in ("directory", "dir", "package", "on", "true", "yes", "1"):
        return "directory"
    if text == "all":
        return text
    raise RuntimeError(
        "[tool.appbuild.cython].merge must be one of: " + ", ".join(MERGE_MODES)
    )


def optimize_config() -> dict:
    """读取 runtime 和第三方依赖的裁剪选项。"""
    value = appbuild_config().get("optimize", {})
    return value if isinstance(value, dict) else {}


def trim_config() -> dict:
    """读取声明式删除及保留规则。

    ``[tool.appbuild.trim]``: declarative payload removal patterns."""
    value = appbuild_config().get("trim", {})
    return value if isinstance(value, dict) else {}


def trim_policy(preset=None):
    """在触碰产物前验证裁剪规则，并叠加本次 CLI 预设。

    The configured trim policy, validated before a build touches a tree.

    ``preset`` lets ``--trim-preset`` select a named rule bundle for one build
    without editing the project configuration.
    """
    from .trim import TrimPolicy

    return TrimPolicy.from_config(trim_config(), preset)


def native_config() -> dict:
    """读取原生库排除与部署配置。"""
    value = appbuild_config().get("native", {})
    return value if isinstance(value, dict) else {}


def native_exclude_patterns() -> list[str]:
    """返回非空原生库排除模式；不接受非数组配置。

    Glob patterns (relative to the bundled site-packages) to drop.

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
        raise RuntimeError("[tool.appbuild.native].exclude must be an array of glob patterns")
    return [str(item).strip() for item in value if str(item).strip()]


def dependency_config() -> dict:
    """读取 wheel 解析、manylinux 及下载选项。"""
    value = appbuild_config().get("dependencies", {})
    return value if isinstance(value, dict) else {}


PIP_TIMEOUT_DEFAULT = 120
PIP_RETRIES_DEFAULT = 10


def _positive_int(value, fallback: int, name: str) -> int:
    """读取非负整数配置；空值取默认，负数或无法转换时报错。"""
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
    """生成 pip 超时和重试参数；配置优先于环境变量和内置默认值。

    pip transfer settings for large wheels over unreliable links.

    The default 15 second socket timeout aborts a multi-hundred-megabyte wheel
    such as PySide6 Addons on any single stall, which then discards the whole
    download. ``[tool.appbuild.dependencies].timeout`` / ``.retries`` and the
    APPBUILD_PIP_TIMEOUT / APPBUILD_PIP_RETRIES environment variables override
    the defaults.
    """
    cfg = dependency_config()
    timeout = _positive_int(cfg.get("timeout"), _env_int("APPBUILD_PIP_TIMEOUT", PIP_TIMEOUT_DEFAULT), "[tool.appbuild.dependencies].timeout")
    retries = _positive_int(cfg.get("retries"), _env_int("APPBUILD_PIP_RETRIES", PIP_RETRIES_DEFAULT), "[tool.appbuild.dependencies].retries")
    return ["--timeout", str(timeout), "--retries", str(retries)]


def pip_cache_dir() -> Path:
    """返回框架共享 pip 缓存目录，不创建目录。

    appbuild-owned pip download cache, reused across builds and CI runs."""
    return CACHE / "pip"


def target_runtime_dir(target: Target) -> Path:
    """返回选定提供方的目标 runtime 根目录。"""
    return runtime_spec(target).root


def staging_dir(target: Target) -> Path:
    """返回按目标隔离的构建暂存目录。"""
    return BUILD / "staging" / target.key


def publish_dir(target: Target) -> Path:
    """返回按目标及进程区分的候选包目录，避免直接改写已发布包。

    Assembly tree for a bundle, unique per build.

    A bundle carries a working interpreter, and the editor resolves every
    ``python`` it finds under the workspace by running it, so a bundle must
    never be assembled in the published path. The per-build name also keeps a
    path the editor has already resolved from pointing at a later build's tree.
    """
    return BUILD / "publish" / f"{target.key}.{os.getpid()}"


def wheel_dir(target: Target) -> Path:
    """返回目标依赖 wheel 的存放目录。"""
    return BUILD / "wheels" / target.key


def pbs_sdk_dir(target: Target) -> Path:
    """返回目标 SDK 路径；名称不限定运行时提供方。"""
    return runtime_spec(target).sdk_root


def target_extension_suffix(target: Target, python_major_minor: str, abi_tag: str) -> str:
    """根据目标平台和 Python ABI 生成扩展后缀，禁止使用宿主后缀。"""
    digits = python_major_minor.replace(".", "")
    if target.os == "windows":
        return f".{abi_tag}-{target.extension_platform}.pyd"
    if target.os == "linux":
        return f".cpython-{digits}-{target.extension_platform}.so"
    return f".cpython-{digits}-darwin.so"


def validate_build_python_version(version_info=None) -> None:
    """校验构建解释器最低版本；可传入版本对象供测试隔离。"""
    info = version_info or sys.version_info
    actual = (int(info.major), int(info.minor))
    if actual < MIN_BUILD_PYTHON:
        raise RuntimeError(
            f"appbuild build tool requires Python {MIN_BUILD_PYTHON[0]}.{MIN_BUILD_PYTHON[1]}+; "
            f"current interpreter is {info.major}.{info.minor}"
        )


def python_executable() -> Path:
    """依次查找显式 APPBUILD_PYTHON、应用 venv、当前解释器及 PATH。"""
    explicit = os.environ.get("APPBUILD_PYTHON")
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
    raise RuntimeError("No development Python found. Set APPBUILD_PYTHON or install Python.")


def require_local_python() -> Path:
    """运行开发解释器探针并检查最低版本；路径或版本无效时阻止构建。"""
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
    """查找目标 runtime 自身解释器；Windows PBS 使用 embeddable 平铺布局。

    Locate the target runtime's own interpreter, when it is present locally."""
    root = runtime_spec(target).root
    if target.os == "windows" and runtime_provider() == "pbs":
        executable = root / "python.exe"
        if not executable.is_file():
            raise RuntimeError(f"Windows embeddable interpreter missing: {executable}")
        return executable
    return _find_target_executable(root, target)


def host_description() -> str:
    """返回用于诊断的系统、架构及宿主 Python 版本。"""
    return f"{platform.system()} {platform.machine()} Python {platform.python_version()}"


def _find_target_executable(root: Path, target: Target) -> Path:
    """优先查找常见 runtime 布局，再递归查找目标解释器；找不到时报错。"""
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
    """查找含 Python.h 的 SDK include 目录；无法定位时抛出 RuntimeError。"""
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
    """准备 SDK 并解析目标编译输入；校验目标版本后生成 ABI 和扩展后缀。"""
    from .runtime import ensure_sdk, sdk_info

    spec = runtime_spec(target)
    ensure_sdk(target)
    info = sdk_info(target)
    # Both providers name this key the same way, so neither can hand back its
    # release runtime where the compiler expects the SDK.
    sdk_root = Path(str(info.get("sdk_root") or spec.sdk_root)).resolve()
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
    return TargetPython(target, sdk_root, executable, include, actual, major_minor, abi, suffix)


def build_config() -> dict:
    """读取构建并发及增量缓存配置。"""
    value = appbuild_config().get("build", {})
    return value if isinstance(value, dict) else {}


def build_jobs() -> int:
    """返回正并发数；零或缺省使用 CPU 数，负数和非整数配置报错。

    How many Cython/compiler invocations may run at the same time.

    Each module is translated and compiled by its own process and the work is
    CPU-bound, so a sequential rebuild costs the sum of every file. ``jobs = 0``
    (the default) uses one job per CPU.
    """
    value = build_config().get("jobs")
    if value is None or value == "":
        return max(1, os.cpu_count() or 1)
    try:
        jobs = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("[tool.appbuild.build].jobs must be an integer") from exc
    if jobs == 0:
        return max(1, os.cpu_count() or 1)
    if jobs < 0:
        raise RuntimeError("[tool.appbuild.build].jobs must not be negative")
    return jobs


def incremental_build_enabled() -> bool:
    """读取增量构建开关；必须为布尔值。

    Whether a module that did not change reuses its cached build output."""
    value = build_config().get("incremental", True)
    if not isinstance(value, bool):
        raise RuntimeError("[tool.appbuild.build].incremental must be a boolean")
    return value
