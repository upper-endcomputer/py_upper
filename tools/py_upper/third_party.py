from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from .config import APP, BUILD, LOCK, ROOT, Target, dependency_config, host_target, load_app_config, optimize_config, pip_cache_dir, pip_transfer_args, python_version, require_local_python, runtime_executable, wheel_dir

WHEEL_MANIFEST = "manifest.json"
MANIFEST_FORMAT = 4


def dependency_specs() -> list[str]:
    value = load_app_config().get("project", {}).get("dependencies", [])
    if not isinstance(value, list):
        raise RuntimeError("[project].dependencies must be an array of requirement strings")
    return [str(item).strip() for item in value if str(item).strip()]


def _find_links() -> list[str]:
    value = dependency_config().get("find_links", [])
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise RuntimeError("[tool.py_upper.dependencies].find_links must be an array")
    links: list[str] = []
    for item in value:
        value = str(item).strip()
        if not value:
            continue
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) and not os.path.isabs(value):
            value = str((ROOT / value).resolve())
        links.append(value)
    return links


def _pip_args(target: Target) -> list[str]:
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


def _has_pip(python: Path) -> bool:
    probe = subprocess.run(
        [str(python), "-m", "pip", "--version"], capture_output=True, text=True, check=False,
    )
    return probe.returncode == 0


def resolution_python(target: Target) -> tuple[Path, list[str]]:
    """Pick the interpreter that resolves target wheels, plus extra pip arguments.

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
        try:
            runtime = runtime_executable(target)
        except RuntimeError:
            runtime = None
        if runtime is not None and _has_pip(runtime):
            return runtime, []
    return require_local_python(), _pip_args(target)


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(map(str, cmd)))
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Third-party resolver failed ({exc.returncode}): {' '.join(map(str, cmd))}") from exc


def wheel_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_files(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("*.whl") if path.is_file())


def _manifest_path(target: Target) -> Path:
    return wheel_dir(target) / WHEEL_MANIFEST


def _requirement_name(spec: str) -> str:
    match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)", spec)
    if not match:
        raise RuntimeError(f"Could not parse dependency name: {spec}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _dist_name(dist_info: Path) -> str:
    metadata = dist_info / "METADATA"
    if metadata.exists():
        for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name:"):
                return re.sub(r"[-_.]+", "-", line.split(":", 1)[1].strip()).lower()
    stem = dist_info.name.removesuffix(".dist-info")
    return re.sub(r"[-_.]+", "-", re.sub(r"-\d.*$", "", stem)).lower()


def _top_level_imports(dist_info: Path) -> list[str]:
    top = dist_info / "top_level.txt"
    names: set[str] = set()
    if top.exists():
        for line in top.read_text(encoding="utf-8", errors="replace").splitlines():
            value = line.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                names.add(value)
    if names:
        return sorted(names)

    # top_level.txt is optional. Derive the import roots from RECORD, which
    # lists exactly the files this distribution installed. Scanning the whole
    # site-packages directory instead would attribute every other
    # distribution's modules (and unrelated empty directories) to this one, so
    # a smoke test would try to import unrelated test packages.
    record = dist_info / "RECORD"
    if record.exists():
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
    if names:
        return sorted(names)

    fallback = _dist_name(dist_info).replace("-", "_")
    return [fallback] if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", fallback) else []


def installed_import_names(site: Path) -> list[str]:
    result: set[str] = set()
    for dist in site.glob("*.dist-info"):
        if dist.is_dir():
            result.update(_top_level_imports(dist))
    return sorted(result)


def _safe_member(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe wheel member path: {name}")
    return path


def _wheel_records(path: Path) -> tuple[list[str], Path]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        dist_infos = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(dist_infos) != 1:
            raise RuntimeError(f"Wheel must contain exactly one .dist-info/METADATA: {path.name}")
        for name in names:
            _safe_member(name)
        return names, Path(dist_infos[0]).parent


def _destination_for_member(member: Path, site: Path, staging: Path) -> Path | None:
    if not member.parts:
        return None
    parts = member.parts
    data_index = next((i for i, part in enumerate(parts) if part.endswith(".data")), None)
    if data_index is None:
        return site.joinpath(*parts)
    if data_index != len(parts) - 2:
        raise RuntimeError(f"Invalid wheel .data member: {'/'.join(parts)}")
    bucket = parts[data_index + 1]
    relative = parts[data_index + 2 :]
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
    staging = staging or site.parent
    installed: list[Path] = []
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            member = _safe_member(info.filename)
            destination = _destination_for_member(member, site, staging)
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
    wanted = {_requirement_name(spec) for spec in dependency_specs()}
    result: set[str] = set()
    for dist in site.glob("*.dist-info"):
        if dist.is_dir() and _dist_name(dist) in wanted:
            result.update(_top_level_imports(dist))
    return sorted(result)


def _write_manifest(target: Target, wheels: list[Path], site: Path) -> Path:
    data = {
        "format": MANIFEST_FORMAT,
        "python": python_version(),
        "target": target.key,
        "target_triple": target.triple,
        "requirements": dependency_specs(),
        "wheels": [
            {"file": path.name, "sha256": wheel_sha256(path), "size": path.stat().st_size}
            for path in wheels
        ],
        "imports": _direct_imports_from_requirements(site),
    }
    path = _manifest_path(target)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def resolve_wheels(target: Target) -> Path:
    out = wheel_dir(target)
    manifest = _manifest_path(target)
    locked = os.environ.get("PY_UPPER_LOCKED") == "1"
    if locked:
        records = {}
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("format") != MANIFEST_FORMAT or data.get("python") != python_version() or data.get("target") != target.key:
                raise RuntimeError("Locked wheel manifest does not match configured target")
            if data.get("requirements", []) != dependency_specs():
                raise RuntimeError("Locked wheel requirements do not match app/pyproject.toml")
            records = {str(item["file"]): item for item in data.get("wheels", []) if isinstance(item, dict) and item.get("file")}
        elif LOCK.exists():
            lock = json.loads(LOCK.read_text(encoding="utf-8"))
            target_data = lock.get("targets", {}).get(target.key, {}) if isinstance(lock, dict) else {}
            if isinstance(target_data, dict):
                if target_data.get("requirements", []) != dependency_specs():
                    raise RuntimeError("Locked wheel requirements do not match app/pyproject.toml")
                records = {str(item["file"]): item for item in target_data.get("wheels", []) if isinstance(item, dict) and item.get("file")}
        if not records and dependency_specs():
            raise RuntimeError(f"Locked build has no recorded wheels for {target.key}: {LOCK}")
        actual = {path.name: path for path in _wheel_files(out)}
        if set(actual) != set(records):
            raise RuntimeError(f"Locked wheel set mismatch: expected {sorted(records)}, found {sorted(actual)}")
        for name, record in records.items():
            expected = str(record.get("sha256") or "").lower()
            if expected and wheel_sha256(actual[name]).lower() != expected:
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
    # Downloads go through a py_upper-owned cache so repeated builds and CI runs
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


def optimize_site(site: Path, target: Target | None = None) -> dict[str, int]:
    cfg = optimize_config()
    profile = str(cfg.get("profile") or "safe").lower()
    if profile not in {"safe", "aggressive"}:
        raise RuntimeError("[tool.py_upper.optimize].profile must be 'safe' or 'aggressive'")
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
    return {"removed_files": removed_files, "removed_dirs": removed_dirs}


def install_wheels(target: Target, staging: Path) -> Path:
    out = resolve_wheels(target)
    site = staging / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for wheel in _wheel_files(out):
        _extract_wheel(wheel, site, staging)
    optimize = optimize_site(site, target)
    _write_manifest(target, _wheel_files(out), site)
    if optimize["removed_files"] or optimize["removed_dirs"]:
        print(f"Third-party optimization: removed {optimize['removed_files']} files and {optimize['removed_dirs']} directories")
    return out
