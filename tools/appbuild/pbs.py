# 中文维护说明：解析 PBS 运行时和 SDK 资产并验证解包布局；宿主工具与目标产物不可混用。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""PBS (python-build-standalone) resolution: release metadata, assets, runtime and SDK."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from .config import CACHE, Target, pbs_release, pbs_sdk_dir, python_version
from .fs import copy_tree_contents, sha256
from .net import download, http_json, http_text


PBS_RELEASE_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/{tag}"
# uv maintains the exact build index for every python-build-standalone release.
# Upstream reorganises its source tree without notice: the file moved from
# crates/uv-python to crates/uv-python-managed in uv commit 00e49e7e, which
# turned the old path into a hard 404 for every build that had a cold cache.
# Known locations are therefore tried in order instead of pinning one path on a
# branch that moves.
PBS_RUNTIME_METADATA_URLS = (
    "https://raw.githubusercontent.com/astral-sh/uv/main/crates/uv-python-managed/download-metadata.json",
    "https://raw.githubusercontent.com/astral-sh/uv/main/crates/uv-python/download-metadata.json",
)
_METADATA_CACHE = CACHE / "pbs" / "uv-download-metadata.json"


@dataclass(frozen=True)
class PBSAsset:
    name: str
    url: str
    sha256: str | None = None


@dataclass(frozen=True)
class PBSInputs:
    tag: str
    runtime: PBSAsset
    sdk: PBSAsset
    source: str


def metadata_key(target: Target, pyver: str) -> str:
    """把目标平台和精确 Python 版本转换为 PBS 元数据索引键。"""
    platform = {
        ("macos", "arm64"): "darwin-aarch64-none",
        ("macos", "x86_64"): "darwin-x86_64-none",
        ("windows", "x86"): "windows-i686-none",
        ("windows", "x86_64"): "windows-x86_64-none",
        ("windows", "arm64"): "windows-aarch64-none",
        ("linux", "x86_64"): "linux-x86_64-gnu",
        ("linux", "arm64"): "linux-aarch64-gnu",
    }[(target.os, target.arch)]
    return f"cpython-{pyver.strip()}-{platform}"


def windows_embed_asset(target: Target, pyver: str) -> PBSAsset:
    """构造官方 Windows embeddable ZIP 的目标架构名称与下载地址。

    Official CPython embeddable runtime; PBS remains the build-only SDK."""
    if target.os != "windows":
        raise ValueError(f"No Windows embeddable runtime for {target.key}")
    architecture = {"x86": "win32", "x86_64": "amd64", "arm64": "arm64"}[target.arch]
    name = f"python-{pyver}-embed-{architecture}.zip"
    return PBSAsset(name, f"https://www.python.org/ftp/python/{pyver}/{name}")


def asset_python_version(name: str) -> str | None:
    """从 PBS 资产名提取 X.Y.Z 版本；不匹配时返回 None。"""
    match = re.match(r"^cpython-(\d+\.\d+\.\d+)(?:\+|-)", str(name).strip())
    return match.group(1) if match else None


def release_assets(checksums: str, tag: str) -> list[PBSAsset]:
    """严格解析公开 SHA256SUMS；拒绝空清单、重复文件和格式错误。

    Read the public release inventory and hashes without the GitHub API."""
    base_url = PBS_RELEASE_URL.format(tag=quote(tag, safe=""))
    assets: dict[str, PBSAsset] = {}
    for number, line in enumerate(checksums.splitlines(), 1):
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *]([A-Za-z0-9_][A-Za-z0-9_+.-]*)", line)
        if not match:
            raise RuntimeError(f"Invalid PBS SHA256SUMS for release {tag} at line {number}")
        digest, name = match.groups()
        if name in assets:
            raise RuntimeError(f"Duplicate PBS SHA256SUMS entry for release {tag}: {name}")
        assets[name] = PBSAsset(name, f"{base_url}/{quote(name, safe='')}", digest.lower())
    if not assets:
        raise RuntimeError(f"Empty PBS SHA256SUMS for release {tag}")
    return list(assets.values())


def _release_assets(fetch_text: Callable[[str], str], tag: str) -> list[PBSAsset]:
    """下载指定 release 的公开摘要清单并解析资产列表。"""
    url = PBS_RELEASE_URL.format(tag=quote(tag, safe="")) + "/SHA256SUMS"
    return release_assets(fetch_text(url), tag)


def is_matching_asset(name: str, target: Target, pyver: str, *, kind: str) -> bool:
    """筛选精确版本及目标 triple 的 runtime 或 SDK，排除 debug/free-threaded 构建。"""
    name = str(name).strip()
    if asset_python_version(name) != pyver.strip() or target.triple not in name:
        return False
    if "freethreaded" in name or "debug" in name:
        return False
    if kind == "runtime":
        return name.endswith("-install_only_stripped.tar.gz")
    if kind == "sdk":
        return name.endswith(("-pgo+lto-full.tar.zst", "-pgo-full.tar.zst", "-full.tar.zst"))
    raise ValueError(f"Unknown PBS asset kind: {kind}")


def matching_assets(assets: list[PBSAsset], target: Target, pyver: str, *, kind: str) -> list[PBSAsset]:
    """保留符合目标、版本和资产类别的条目。"""
    return [
        asset for asset in assets
        if is_matching_asset(asset.name, target, pyver, kind=kind)
    ]


def available_python_versions(assets: list[PBSAsset], target: Target, *, kind: str) -> list[str]:
    """列出当前 release 对目标及类别可用的版本，按数字版本排序。"""
    values = {
        version
        for asset in assets
        for version in [asset_python_version(asset.name)]
        if version and is_matching_asset(asset.name, target, version, kind=kind)
    }
    return sorted(values, key=lambda x: tuple(int(part) for part in x.split(".")))


def available_metadata_python_versions(data: dict[str, Any], target: Target) -> list[str]:
    """从元数据索引提取当前目标存在下载地址的版本列表。"""
    suffix = metadata_key(target, "0.0.0").split("0.0.0-", 1)[1]
    prefix = "cpython-"
    values: set[str] = set()
    for key, value in data.items():
        if not isinstance(key, str) or not key.startswith(prefix) or not key.endswith("-" + suffix):
            continue
        version = key[len(prefix): -len(suffix) - 1]
        if re.fullmatch(r"\d+\.\d+\.\d+", version) and isinstance(value, dict) and value.get("url"):
            values.add(version)
    return sorted(values, key=lambda x: tuple(int(part) for part in x.split(".")))


def _fetch_metadata_index(fetch_json: Callable[[str], Any]) -> dict[str, Any]:
    """按候选地址读取元数据对象；全部失败时合并来源诊断。"""
    failures: list[str] = []
    for url in PBS_RUNTIME_METADATA_URLS:
        try:
            value = fetch_json(url)
        except (OSError, ValueError) as exc:
            failures.append(f"{url}: {exc}")
            continue
        if isinstance(value, dict):
            return value
        failures.append(f"{url}: expected a JSON object, got {type(value).__name__}")
    raise RuntimeError(
        "Could not fetch the uv python-build-standalone metadata index.\n"
        + "\n".join(failures)
        + "\nuv moves this file between releases; update PBS_RUNTIME_METADATA_URLS in tools/appbuild/pbs.py."
    )


def _load_metadata(fetch_json: Callable[[str], Any], *, force: bool = False) -> dict[str, Any]:
    """优先读取有效本地 JSON 缓存；强制刷新或缓存无效时重新获取并保存。"""
    if not force and _METADATA_CACHE.exists():
        try:
            value = json.loads(_METADATA_CACHE.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (OSError, ValueError):
            pass
    value = _fetch_metadata_index(fetch_json)
    _METADATA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _METADATA_CACHE.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return value


def metadata_asset(data: dict[str, Any], target: Target, pyver: str) -> PBSAsset | None:
    """按精确版本与目标读取下载条目；缺少地址时返回 None。"""
    entry = data.get(metadata_key(target, pyver))
    if not isinstance(entry, dict):
        return None
    url = str(entry.get("url") or "").strip()
    if not url:
        return None
    return PBSAsset(
        name=unquote(url.rsplit("/", 1)[-1]),
        url=url,
        sha256=str(entry.get("sha256") or "").strip() or None,
    )


def asset_flavor(name: str) -> str:
    """识别 shared/static 构建风味，用于配对包含正确 import library 的 SDK。

    The build flavour a PBS asset name records, or "" when it records none.

    Windows releases used to ship a shared and a static flavour of every
    artifact, and the two are not interchangeable: extension modules link the
    versioned import library (python3XX.lib), which only the shared flavour
    carries. Newer releases dropped the flavour from the name altogether.
    """
    for flavor in ("shared", "static"):
        if f"-{flavor}-" in name:
            return flavor
    return ""


def is_full_build_asset(name: str) -> bool:
    """判断是否为含 SDK 和 install 树的完整构建，排除仅运行时和调试变体。

    Whether a PBS asset is a full build rather than an install-only runtime.

    A full build carries the build tree, the SDK (headers and import
    libraries) and an ``install/`` runtime; an install-only archive carries
    the runtime alone. PBS releases before 20210724 publish full builds only,
    which is why uv's metadata entry for them points at a full build.
    """
    name = str(name).strip()
    return (
        name.endswith(".tar.zst")
        and "install_only" not in name
        and "debug" not in name
        and "freethreaded" not in name
    )


def _best_sdk(assets: list[PBSAsset], runtime_name: str) -> PBSAsset:
    """匹配 runtime 构建风味，并优先选择 pgo+lto SDK；无匹配时报错。

    The SDK matching the runtime's build flavour, best optimisation first."""
    flavor = asset_flavor(runtime_name)
    candidates = assets
    if flavor:
        candidates = [asset for asset in assets if asset_flavor(asset.name) == flavor]
        if not candidates:
            raise RuntimeError(
                f"No PBS SDK asset shares the {flavor} flavour of runtime {runtime_name}.\n"
                f"SDK assets offered for this release: {', '.join(asset.name for asset in assets)}"
            )
    return max(candidates, key=lambda asset: ("pgo+lto-full" in asset.name, asset.name))


def no_asset_error(release_tag: str, target: Target, pyver: str, assets: list[PBSAsset], *, missing_runtime: bool, missing_sdk: bool) -> RuntimeError:
    """构造包含请求版本、目标和可用资产的诊断异常，不自动换版本。"""
    missing = []
    if missing_runtime:
        missing.append("install_only_stripped runtime")
    if missing_sdk:
        missing.append("full SDK")
    return RuntimeError(
        f"No PBS {' / '.join(missing)} asset for Python {pyver} / {target.triple} in release {release_tag}.\n"
        f"Requested Python: {pyver}\n"
        f"PBS release: {release_tag}\n"
        f"Target: {target.triple}\n"
        f"Available runtime Python versions: {', '.join(available_python_versions(assets, target, kind='runtime')) or 'none'}\n"
        f"Available SDK Python versions: {', '.join(available_python_versions(assets, target, kind='sdk')) or 'none'}\n"
        "Set [tool.appbuild.pbs].release to a release containing the requested exact Python version; "
        "appbuild will not silently substitute another version."
    )


def resolve_pbs_inputs(
    fetch_json: Callable[[str], Any], configured_tag: str, target: Target, pyver: str,
    *, fetch_text: Callable[[str], str],
) -> PBSInputs:
    """解析同一 release 的精确 runtime/SDK 配对；自动模式先从元数据定位版本。

    Resolve one exact PBS runtime + SDK pair.

    Automatic mode uses the uv-generated exact runtime metadata to determine the
    build tag. Both modes select the SDK from that release's public SHA256SUMS,
    avoiding the authenticated/rate-limited GitHub API entirely.
    """
    if configured_tag:
        try:
            assets = _release_assets(fetch_text, configured_tag)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            raise RuntimeError(
                f"PBS release {configured_tag} publishes no SHA256SUMS. Releases before "
                "20210724 predate that inventory; leave [tool.appbuild.pbs].release empty "
                "so the uv metadata index can select the exact full build."
            ) from exc
        runtime_assets = matching_assets(assets, target, pyver, kind="runtime")
        sdk_assets = matching_assets(assets, target, pyver, kind="sdk")
        if not runtime_assets or not sdk_assets:
            raise no_asset_error(configured_tag, target, pyver, assets, missing_runtime=not runtime_assets, missing_sdk=not sdk_assets)
        runtime = runtime_assets[0]
        sdk = _best_sdk(sdk_assets, runtime.name)
        return PBSInputs(configured_tag, runtime, sdk, "release")

    data = _load_metadata(fetch_json)
    runtime = metadata_asset(data, target, pyver)
    if runtime is None:
        available = available_metadata_python_versions(data, target)
        if available and pyver not in available:
            raise RuntimeError(
                f"No PBS runtime metadata for exact Python {pyver} / {target.triple}.\n"
                f"Requested Python: {pyver}\n"
                f"Target: {target.triple}\n"
                f"Available Python versions from PBS metadata for this target: {', '.join(available)}\n"
                "appbuild will not silently substitute another Python version."
            )
        data = _load_metadata(fetch_json, force=True)
        runtime = metadata_asset(data, target, pyver)
    if runtime is None:
        raise RuntimeError(f"No PBS runtime metadata for exact Python {pyver} / {target.triple}")

    match = re.search(r"/releases/download/([^/]+)/", runtime.url)
    tag = unquote(match.group(1)) if match else ""
    if not tag:
        raise RuntimeError(f"PBS runtime metadata has no release build tag: {runtime.url}")

    try:
        assets = _release_assets(fetch_text, tag)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        # Old releases predate SHA256SUMS and publish full builds only, so the
        # metadata entry already points at the only asset carrying the SDK.
        # It is a runtime too: ensure_pbs_runtime copies its install/ tree.
        if not is_full_build_asset(runtime.name):
            raise RuntimeError(
                f"PBS release {tag} publishes no SHA256SUMS and its metadata asset is "
                f"not a full build: {runtime.name}\n"
                "appbuild cannot resolve a matching SDK for this release."
            ) from exc
        return PBSInputs(tag, runtime, runtime, "metadata-full")

    sdk_assets = matching_assets(assets, target, pyver, kind="sdk")
    if not sdk_assets:
        raise no_asset_error(tag, target, pyver, assets, missing_runtime=False, missing_sdk=True)
    sdk = _best_sdk(sdk_assets, runtime.name)
    return PBSInputs(tag, runtime, sdk, "metadata")


SDK_MARKER = ".appbuild-sdk.json"


def _safe_extract_zst(archive: Path, destination: Path) -> None:
    """用系统 tar 解压已校验 SDK；必要时先调用 zstd，随后删除中间 tar。"""
    destination.mkdir(parents=True, exist_ok=True)
    tar = shutil.which("tar")
    if not tar:
        raise RuntimeError("Extracting PBS SDK requires tar on PATH")
    result = subprocess.run([tar, "-axf", str(archive), "-C", str(destination)], capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return
    zstd = shutil.which("zstd")
    if not zstd:
        raise RuntimeError(f"Unable to extract {archive.name}: tar could not read zstd archive and zstd is not installed")
    raw = archive.with_suffix("")
    subprocess.run([zstd, "-d", "-f", str(archive), "-o", str(raw)], check=True)
    try:
        subprocess.run([tar, "-xf", str(raw), "-C", str(destination)], check=True)
    finally:
        raw.unlink(missing_ok=True)


def _sdk_metadata(root: Path) -> dict:
    """读取 SDK 自带 PYTHON.json；缺失元数据立即报错。

    The SDK's own PYTHON.json, which is the authoritative layout record."""
    path = root / "PYTHON.json"
    if not path.is_file():
        raise RuntimeError(f"PBS full SDK has no PYTHON.json: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _recorded_path(root: Path, value: Any) -> Path | None:
    """解析并约束 SDK 内记录路径；允许文件或目录，越界或不存在返回 None。

    Resolve one path recorded in PYTHON.json, refusing to escape the SDK."""
    if not isinstance(value, str) or not value:
        return None
    candidate = (root / value).resolve()
    if root.resolve() not in candidate.parents:
        return None
    return candidate if candidate.exists() else None


def _sdk_include(root: Path, metadata: dict) -> Path:
    """优先采用元数据 include 目录；仅无有效记录时扫描 Python.h，多个候选时报歧义。

    The include directory holding Python.h.

    PYTHON.json records it, and the value differs per platform:
    install/include/python3.10 on POSIX, install/include on Windows. Searching
    the tree is only a fallback for metadata without the field, and an ambiguous
    tree is an error rather than a silent pick between candidates.
    """
    recorded = _recorded_path(root, (metadata.get("python_paths") or {}).get("include"))
    if recorded is not None and (recorded / "Python.h").is_file():
        return recorded
    matches = sorted(path.parent for path in root.rglob("Python.h"))
    if not matches:
        raise RuntimeError(f"PBS full SDK contains no Python.h: {root}")
    if len(matches) > 1:
        raise RuntimeError(
            "PBS full SDK holds several Python.h and its PYTHON.json records none: "
            + ", ".join(str(path) for path in matches)
        )
    return matches[0]


def _sdk_interpreter(root: Path, target: Target, metadata: dict) -> Path:
    """优先使用元数据记录的解释器文件；缺失时按目标布局寻找候选。

    The target interpreter, at the path PYTHON.json records.

    PBS does not use one layout everywhere: POSIX builds put the interpreter in
    install/bin, Windows builds put python.exe next to python3XX.dll with no bin
    directory at all. Reading the recorded path is what makes one implementation
    cover both; the name guesses below only serve metadata without the field.
    """
    recorded = _recorded_path(root, metadata.get("python_exe"))
    if recorded is not None and recorded.is_file():
        return recorded
    if target.os == "windows":
        names, directories = ("python.exe", "python3.exe"), (root / "install", root)
    else:
        version = python_version()
        names = (f"python{version}", f"python{'.'.join(version.split('.')[:2])}", "python3", "python")
        directories = (root / "install" / "bin", root / "bin")
    for directory in directories:
        for name in names:
            if (directory / name).is_file():
                return directory / name
    raise RuntimeError(
        f"Target Python executable not found in PBS SDK: {root}\n"
        f"PYTHON.json python_exe: {metadata.get('python_exe')!r}\n"
        "Looked for " + ", ".join(names) + " in " + ", ".join(str(directory) for directory in directories)
    )


def _validate_layout(root: Path, target: Target) -> tuple[Path, dict]:
    """在缓存发布前验证完整 SDK 的 install 树、解释器和头文件。"""
    python_root = root / "python"
    metadata = _sdk_metadata(python_root)
    install_root = python_root / "install"
    if not install_root.is_dir():
        raise RuntimeError(f"PBS full SDK has no python/install directory: {install_root}")
    # Resolve both now: a truncated or renamed archive has to fail here, not
    # after the tree has been copied into the cache.
    _sdk_interpreter(python_root, target, metadata)
    _sdk_include(python_root, metadata)
    return python_root, metadata


def resolve_inputs(target: Target) -> PBSInputs:
    """将当前配置和公开下载函数交给 PBS 精确配对解析器。"""
    return resolve_pbs_inputs(http_json, pbs_release(), target, python_version(), fetch_text=http_text)


def ensure_pbs_sdk(target: Target) -> Path:
    """复用有效 SDK 标记，或下载校验、解压并验证布局后写入新标记。"""
    out = pbs_sdk_dir(target)
    marker_path = out / SDK_MARKER
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if (
                marker.get("format") == 2
                and marker.get("target") == target.key
                and marker.get("python") == python_version()
                and out.exists()
            ):
                return out
        except (OSError, ValueError):
            pass

    inputs = resolve_inputs(target)
    archive = CACHE / "pbs" / inputs.tag / target.key / inputs.sdk.name
    download(inputs.sdk.url, archive)
    actual = sha256(archive)
    if inputs.sdk.sha256 and actual.lower() != inputs.sdk.sha256.lower():
        raise RuntimeError(f"PBS SDK SHA256 mismatch: expected {inputs.sdk.sha256}, got {actual}")

    extract = CACHE / "pbs" / inputs.tag / target.key / "sdk-extract"
    if extract.exists():
        shutil.rmtree(extract)
    _safe_extract_zst(archive, extract)
    python_root, metadata = _validate_layout(extract, target)
    if out.exists():
        shutil.rmtree(out)
    copy_tree_contents(python_root, out)
    marker = {
        "format": 2,
        "provider": "pbs",
        "tag": inputs.tag,
        "asset": inputs.sdk.name,
        "url": inputs.sdk.url,
        "sha256": actual,
        "target": target.key,
        "python": str(metadata.get("python_version") or python_version()),
    }
    (out / SDK_MARKER).write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def sdk_info(target: Target) -> dict:
    """返回 SDK 路径、解释器、头文件及实际 ABI 元信息供构建和清单使用。"""
    root = ensure_pbs_sdk(target)
    marker = json.loads((root / SDK_MARKER).read_text(encoding="utf-8"))
    metadata = _sdk_metadata(root)
    include = _sdk_include(root, metadata)
    executable = _sdk_interpreter(root, target, metadata)

    actual_python = str(marker.get("python") or python_version())
    return {
        "provider": "pbs",
        "sdk_root": str(root),
        "tag": marker.get("tag"),
        "asset": marker.get("asset"),
        "url": marker.get("url"),
        "sha256": marker.get("sha256"),
        "python": actual_python,
        "python_executable": str(executable),
        "include_dir": str(include),
        "python_major_minor": ".".join(actual_python.split(".")[:2]),
        "python_tag": "cp" + "".join(actual_python.split(".")[:2]),
        "python_platform_tag": target.primary_wheel_platform,
        "python_implementation_name": "cpython",
    }
