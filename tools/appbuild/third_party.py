# 中文维护说明：按目标 ABI 获取 wheel 并安装到暂存目录；跨目标宿主 marker 求值存在边界。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path, PureWindowsPath

from .fs import sha256

from .config import APP, BUILD, LOCK, ROOT, Target, app_config_label, dependency_config, host_target, load_app_config, optimize_config, pip_cache_dir, pip_transfer_args, python_version, require_local_python, runtime_executable, wheel_dir

WHEEL_MANIFEST = "manifest.json"
MANIFEST_FORMAT = 4


def dependency_specs() -> list[str]:
    """读取项目直接依赖并去除空项；非数组配置抛出 RuntimeError。"""
    value = load_app_config().get("project", {}).get("dependencies", [])
    if not isinstance(value, list):
        raise RuntimeError("[project].dependencies must be an array of requirement strings")
    return [str(item).strip() for item in value if str(item).strip()]


def _find_links() -> list[str]:
    """规范化离线 wheel 来源；相对目录以框架根目录解析。"""
    value = dependency_config().get("find_links", [])
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise RuntimeError("[tool.appbuild.dependencies].find_links must be an array")
    links: list[str] = []
    for item in value:
        value = str(item).strip()
        if not value:
            continue
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) and not os.path.isabs(value):
            value = str((ROOT / value).resolve())
        links.append(value)
    return links


def _tag_args(target: Target) -> list[str]:
    """为跨解释器下载显式指定目标 Python ABI 及可接受平台标签。

    Wheel compatibility tags, needed only for a foreign interpreter.

    pip already knows the platform and Python version of the interpreter that
    runs it, so these overrides are only meaningful when that interpreter is
    not the target runtime.
    """
    major_minor = ".".join(python_version().split(".")[:2])
    abi = "cp" + major_minor.replace(".", "")
    args = [
        "--implementation", "cp",
        "--python-version", major_minor,
        "--abi", abi,
        "--abi", "abi3",
        "--abi", "none",
    ]
    for platform_tag in target.wheel_platforms:
        args.extend(["--platform", platform_tag])
    return args


def _resolver_args() -> list[str]:
    """生成索引和 find-links 参数；本机及跨目标解析均须遵守相同来源配置。

    Wheel sources from ``[tool.appbuild.dependencies]``.

    These describe where wheels may come from, not what counts as compatible,
    so they apply to every resolution: an offline wheelhouse has to be honored
    whether pip runs under the target runtime or under the host interpreter.
    """
    args: list[str] = []
    cfg = dependency_config()
    if bool(cfg.get("no_index", False)):
        args.append("--no-index")
    index_url = str(cfg.get("index_url") or "").strip()
    if index_url:
        args.extend(["--index-url", index_url])
    for extra in cfg.get("extra_index_urls", []) if isinstance(cfg.get("extra_index_urls", []), list) else []:
        if str(extra).strip():
            args.extend(["--extra-index-url", str(extra).strip()])
    for link in _find_links():
        args.extend(["--find-links", link])
    return args


def _pip_args(target: Target) -> list[str]:
    """组合目标标签与下载来源参数。"""
    return _tag_args(target) + _resolver_args()


def _has_pip(python: Path) -> bool:
    """运行 pip 版本探针，仅根据退出码判断是否可用。"""
    probe = subprocess.run(
        [str(python), "-m", "pip", "--version"], capture_output=True, text=True, check=False,
    )
    return probe.returncode == 0


def resolution_python(target: Target) -> tuple[Path, list[str]]:
    """本机目标优先用目标解释器求值依赖 marker；跨目标回退宿主并明确限制。

    Pick the interpreter that resolves target wheels, plus extra pip arguments.

    pip evaluates environment markers (``sys_platform``, ``python_full_version``,
    ...) against the interpreter running it; ``--platform`` and
    ``--python-version`` only override wheel compatibility tags and the
    Requires-Python check. Resolving with the host interpreter therefore pulls
    the wrong dependency set whenever the target platform or Python version
    differs from the host, so native builds use the target runtime itself.
    Cross targets cannot execute the target interpreter and keep the host
    interpreter with explicit tag overrides.
    """
    if target == host_target():
        # The official Windows embed ZIP has no pip. The matching PBS full SDK
        # contains a runnable interpreter with pip for correct marker evaluation;
        # it is a build input and is never copied into the final bundle.
        if target.os == "windows":
            from .runtime import sdk_info

            sdk_python = Path(sdk_info(target)["python_executable"])
            if not _has_pip(sdk_python):
                raise RuntimeError(f"Target SDK Python has no pip: {sdk_python}")
            return sdk_python, _resolver_args()
        try:
            runtime = runtime_executable(target)
        except RuntimeError:
            runtime = None
        if runtime is not None and _has_pip(runtime):
            return runtime, _resolver_args()
    print("Dependency resolution: host fallback; target environment markers are not guaranteed. Prefer a target-native lock and wheelhouse.")
    return require_local_python(), _pip_args(target)


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    """输出并执行依赖解析命令；非零退出转换为带命令的 RuntimeError。"""
    print("+", " ".join(map(str, cmd)))
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Third-party resolver failed ({exc.returncode}): {' '.join(map(str, cmd))}") from exc



def _wheel_files(root: Path) -> list[Path]:
    """列出目录直属 wheel 文件并稳定排序。"""
    return sorted(path for path in root.glob("*.whl") if path.is_file())


def _manifest_path(target: Target) -> Path:
    """返回目标 wheel 输入清单路径。"""
    return wheel_dir(target) / WHEEL_MANIFEST


def _requirement_name(spec: str) -> str:
    """提取并规范化 requirement 的发行包名，供直接依赖归属匹配。"""
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", spec)
    if not match:
        raise RuntimeError(f"Could not parse dependency name: {spec}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _dist_name(dist_info: Path) -> str:
    """优先读取 METADATA 中发行名；缺失时从 dist-info 目录名推导。"""
    metadata = dist_info / "METADATA"
    if metadata.exists():
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:"):
                return re.sub(r"[-_.]+", "-", line.split(":", 1)[1].strip()).lower()
    stem = dist_info.name.removesuffix(".dist-info")
    return re.sub(r"[-_.]+", "-", re.sub(r"-\d.*$", "", stem)).lower()


def _record_import_roots(dist_info: Path) -> set[str]:
    """从该发行包的 RECORD 识别导入根，避免误归属其他包的模块。

    The import roots RECORD says this distribution installed.

    RECORD lists exactly the files the distribution installed. Scanning the
    whole site-packages directory instead would attribute every other
    distribution's modules (and unrelated empty directories) to this one, so a
    smoke test would try to import unrelated test packages.
    """
    names: set[str] = set()
    record = dist_info / "RECORD"
    if not record.exists():
        return names
    for line in record.read_text(encoding="utf-8", errors="replace").splitlines():
        entry = line.split(",", 1)[0].strip()
        if not entry or entry.endswith("/") or entry.startswith(".."):
            continue
        head, _, tail = entry.partition("/")
        if head.endswith((".dist-info", ".data")) or head.startswith("."):
            continue
        if tail:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", head):
                names.add(head)
        elif entry.endswith(".py") and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", entry):
            names.add(entry[:-3])
    return names


def _top_level_imports(dist_info: Path) -> list[str]:
    # top_level.txt is optional, and it is a claim about the wheel rather than a
    # record of it. Wheels do get it wrong: PySide2's and shiboken2's both list a
    # bare ``QtCore`` that neither installs, because their modules live under
    # ``PySide2/`` and ``shiboken2/``. Trusting the claim makes the build's own
    # smoke test import a module no distribution provides, so an intact PySide2
    # payload fails [6/6] with "No module named 'QtCore'". Every declared name
    # therefore has to appear in RECORD to count.
    """用安装记录验证声明的顶层模块；没有记录时才采用发行包命名回退。"""
    declared: set[str] = set()
    top = dist_info / "top_level.txt"
    if top.exists():
        for line in top.read_text(encoding="utf-8", errors="replace").splitlines():
            value = line.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                declared.add(value)

    installed = _record_import_roots(dist_info)
    if installed:
        # A name RECORD does not mention is not importable from this
        # distribution. When nothing declared survives, RECORD is the only
        # remaining description of what the distribution installs.
        return sorted(declared & installed) or sorted(installed)
    if declared:
        return sorted(declared)

    fallback = _dist_name(dist_info).replace("-", "_")
    return [fallback] if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", fallback) else []


def installed_import_names(site: Path) -> list[str]:
    """合并已安装发行包的顶层导入名并去重排序。"""
    result: set[str] = set()
    for dist in site.glob("*.dist-info"):
        if dist.is_dir():
            result.update(_top_level_imports(dist))
    return sorted(result)


def _safe_member(name: str) -> Path:
    # Wheel members use POSIX paths regardless of the build host. Reject a
    # Windows path even on Unix, where pathlib would treat its drive as a name.
    """拒绝 wheel 中的绝对路径、盘符、反斜线和父目录段。"""
    parts = name.rstrip("/").split("/")
    if (not name or name.startswith("/") or "\\" in name
            or PureWindowsPath(name).drive or any(part in {"", ".", ".."} for part in parts)):
        raise RuntimeError(f"Unsafe wheel member path: {name}")
    return Path(*parts)


def _wheel_records(path: Path) -> tuple[list[str], Path]:
    """验证 wheel 路径并要求唯一 METADATA，返回成员表与 dist-info 目录。"""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        dist_infos = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(dist_infos) != 1:
            raise RuntimeError(f"Wheel must contain exactly one .dist-info/METADATA: {path.name}")
        for name in names:
            _safe_member(name)
        return names, Path(dist_infos[0]).parent


def _destination_for_member(member: Path, site: Path, staging: Path, data_root: str) -> Path | None:
    """把 wheel 各 data bucket 映射到 staging；头文件不进入发布包。"""
    parts = member.parts
    if parts[0] != data_root:
        if parts[0].endswith(".data"):
            raise RuntimeError(f"Invalid wheel .data member: {'/'.join(parts)}")
        return site.joinpath(*parts)
    if len(parts) < 3:
        raise RuntimeError(f"Invalid wheel .data member: {'/'.join(parts)}")
    bucket = parts[1]
    relative = parts[2:]
    if bucket in {"purelib", "platlib"}:
        return site.joinpath(*relative)
    if bucket == "data":
        return staging.joinpath("runtime-data", *relative)
    if bucket == "scripts":
        return staging.joinpath("runtime-data", "bin", *relative)
    if bucket == "headers":
        return None
    raise RuntimeError(f"Unsupported wheel data bucket {bucket!r} in {'/'.join(parts)}")


def _extract_wheel(wheel: Path, site: Path, staging: Path | None = None) -> list[Path]:
    """先验证全部目标路径再写文件；同名相同内容可复用，不同内容报冲突。"""
    staging = staging or site.parent
    _, dist_info = _wheel_records(wheel)
    data_root = dist_info.name[:-len(".dist-info")] + ".data"
    installed: list[Path] = []
    with zipfile.ZipFile(wheel) as archive:
        # Validate every destination before writing any payload, including a
        # malformed .data bucket that appears late in the archive.
        entries = [
            (info, _destination_for_member(_safe_member(info.filename), site, staging, data_root))
            for info in archive.infolist() if not info.is_dir()
        ]
        for info, destination in entries:
            if destination is None:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = archive.read(info)
            if destination.exists():
                if destination.read_bytes() != payload:
                    raise RuntimeError(f"Wheel file collision with different content: {destination}")
            else:
                destination.write_bytes(payload)
            installed.append(destination)
    return installed


def _direct_imports_from_requirements(site: Path) -> list[str]:
    """从已安装元数据提取直接依赖可导入名，供冒烟脚本使用。"""
    wanted = {_requirement_name(spec) for spec in dependency_specs()}
    result: set[str] = set()
    for dist in site.glob("*.dist-info"):
        if dist.is_dir() and _dist_name(dist) in wanted:
            result.update(_top_level_imports(dist))
    return sorted(result)


def _write_manifest(
    target: Target,
    wheels: list[Path],
    site: Path,
    imports: list[str] | None = None,
) -> Path:
    """记录目标、依赖、wheel 摘要及导入名；可传入裁剪前保留的导入列表。"""
    data = {
        "format": MANIFEST_FORMAT,
        "python": python_version(),
        "target": target.key,
        "target_triple": target.triple,
        "requirements": dependency_specs(),
        "wheels": [
            {"file": path.name, "sha256": sha256(path), "size": path.stat().st_size}
            for path in wheels
        ],
        "imports": _direct_imports_from_requirements(site) if imports is None else list(imports),
    }
    path = _manifest_path(target)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def resolve_wheels(target: Target) -> Path:
    """锁定模式核验现有 wheel 集合与摘要；非锁定模式重新解析目标二进制依赖。"""
    out = wheel_dir(target)
    manifest = _manifest_path(target)
    locked = os.environ.get("APPBUILD_LOCKED") == "1"
    if locked:
        records = {}
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("format") != MANIFEST_FORMAT or data.get("python") != python_version() or data.get("target") != target.key:
                raise RuntimeError("Locked wheel manifest does not match configured target")
            if data.get("requirements", []) != dependency_specs():
                raise RuntimeError(
                    f"Locked wheel requirements do not match {app_config_label()}"
                )
            records = {str(item["file"]): item for item in data.get("wheels", []) if isinstance(item, dict) and item.get("file")}
        elif LOCK.exists():
            lock = json.loads(LOCK.read_text(encoding="utf-8"))
            target_data = lock.get("targets", {}).get(target.key, {}) if isinstance(lock, dict) else {}
            if isinstance(target_data, dict):
                if target_data.get("requirements", []) != dependency_specs():
                    raise RuntimeError(
                        f"Locked wheel requirements do not match {app_config_label()}"
                    )
                records = {str(item["file"]): item for item in target_data.get("wheels", []) if isinstance(item, dict) and item.get("file")}
        if not records and dependency_specs():
            raise RuntimeError(f"Locked build has no recorded wheels for {target.key}: {LOCK}")
        actual = {path.name: path for path in _wheel_files(out)}
        if set(actual) != set(records):
            raise RuntimeError(f"Locked wheel set mismatch: expected {sorted(records)}, found {sorted(actual)}")
        for name, record in records.items():
            expected = str(record.get("sha256") or "").lower()
            if expected and sha256(actual[name]).lower() != expected:
                raise RuntimeError(f"Wheel checksum mismatch: {name}")
        return out

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    specs = dependency_specs()
    if not specs:
        _write_manifest(target, [], BUILD / "staging" / target.key / "site-packages")
        return out

    python, tag_args = resolution_python(target)
    # Downloads go through a appbuild-owned cache so repeated builds and CI runs
    # do not refetch multi-hundred-megabyte wheels such as PySide6 Addons.
    cmd = [
        str(python), "-m", "pip", "download",
        "--disable-pip-version-check", "--only-binary=:all:", "--prefer-binary",
        "--cache-dir", str(pip_cache_dir()),
        "--dest", str(out),
    ] + pip_transfer_args() + tag_args + specs
    _run(cmd, cwd=APP)
    wheels = _wheel_files(out)
    if not wheels:
        raise RuntimeError(f"No compatible binary wheel resolved for {target.key} / Python {python_version()}")
    for wheel in wheels:
        _wheel_records(wheel)
    stage_site = BUILD / "staging" / target.key / "site-packages"
    _write_manifest(target, wheels, stage_site)
    return out


def optimize_site(site: Path, target: Target | None = None, trim=None) -> dict[str, int]:
    """按显式策略移除缓存、开发负载及 Qt 冗余，再应用声明式裁剪规则。"""
    cfg = optimize_config()
    profile = str(cfg.get("profile") or "safe").lower()
    if profile not in {"safe", "aggressive"}:
        raise RuntimeError("[tool.appbuild.optimize].profile must be 'safe' or 'aggressive'")
    removed_files = removed_dirs = 0
    remove_caches = bool(cfg.get("remove_python_caches", True))
    for path in sorted(site.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if remove_caches and path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path)
            removed_dirs += 1
        elif remove_caches and path.is_file() and path.suffix.lower() in {".pyc", ".pyo"}:
            path.unlink()
            removed_files += 1
    # Debug symbol bundles are not runtime payload: they bloat the app and are
    # not loadable, so they are always dropped regardless of the profile.
    for path in sorted(site.rglob("*.dSYM"), key=lambda value: len(value.parts), reverse=True):
        if path.is_dir():
            shutil.rmtree(path)
            removed_dirs += 1

    remove_tests = bool(cfg.get("remove_tests", False)) or profile == "aggressive"
    remove_docs = bool(cfg.get("remove_docs", False)) or profile == "aggressive"
    removable_dirs: set[str] = set()
    if remove_tests:
        removable_dirs.update({"tests", "test"})
    if remove_docs:
        removable_dirs.update({"docs", "examples", "benchmarks"})
    for path in sorted(site.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if path.is_dir() and path.name.lower() in removable_dirs:
            shutil.rmtree(path)
            removed_dirs += 1

    if target is not None:
        from .qt_prune import prune as prune_qt

        qt = prune_qt(site, target)
        if qt:
            print(
                f"Qt pruning: removed {qt['files']} file(s) and {qt['dirs']} director(ies), "
                f"freed {qt['bytes'] / 1_000_000:.0f} MB"
            )
            removed_files += qt["files"]
            removed_dirs += qt["dirs"]

    if trim is not None and trim.has_site_rules:
        from .trim import trim_tree

        report = trim_tree(site, trim.site_packages, trim.site_packages_keep)
        if report:
            print(report.describe("site-packages"))
            removed_files += report.removed_files
            removed_dirs += report.removed_dirs
    return {"removed_files": removed_files, "removed_dirs": removed_dirs}


def install_wheels(target: Target, staging: Path, trim=None) -> Path:
    """安装目标 wheel 并在裁剪前记录导入名，最后写入完整输入清单。"""
    out = resolve_wheels(target)
    site = staging / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for wheel in _wheel_files(out):
        _extract_wheel(wheel, site, staging)
    # Record the distributions' import names before any trim runs: the release
    # preset removes ``*.dist-info``/``*.egg-info``, and once the metadata is
    # gone the manifest has nothing left to derive the smoke-test imports from.
    imports = _direct_imports_from_requirements(site)
    optimize = optimize_site(site, target, trim)
    _write_manifest(target, _wheel_files(out), site, imports)
    if optimize["removed_files"] or optimize["removed_dirs"]:
        print(f"Third-party optimization: removed {optimize['removed_files']} files and {optimize['removed_dirs']} directories")
    return out
