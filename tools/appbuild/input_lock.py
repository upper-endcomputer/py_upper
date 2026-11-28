# 中文维护说明：记录和检查 runtime、SDK 与 wheel 输入；依赖可重放不等于产物字节级可复现。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import json
from pathlib import Path

from .fs import sha256

from .config import LOCK, Target, app_config_label, load_app_config, python_version, runtime_provider, runtime_spec, wheel_dir
from .manifest import manifest_hash, read_manifest
from .wheels import dependency_specs

FORMAT = 5



def _wheel_records(target: Target) -> list[dict]:
    """按文件名稳定排序记录 wheel 的名称、内容摘要和大小。"""
    return [
        {"file": path.name, "sha256": sha256(path), "size": path.stat().st_size}
        for path in sorted(wheel_dir(target).glob("*.whl"))
    ]


def _marker(root: Path, name: str) -> dict:
    """读取输入目录中的 JSON 标记；不存在或顶层不是对象时返回空字典。"""
    path = root / name
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def write_lock(target: Target) -> Path:
    """更新当前目标的输入锁记录；只保留与当前项目和运行时契约兼容的其他目标。"""
    spec = runtime_spec(target)
    manifest = read_manifest(spec.root)
    project = str(load_app_config().get("project", {}).get("name") or "appbuild")
    data = {
        "format": FORMAT,
        "project": project,
        "python": python_version(),
        "runtime_provider": runtime_provider(),
        "targets": {},
    }
    if LOCK.exists():
        try:
            existing = json.loads(LOCK.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if (
            isinstance(existing, dict)
            and existing.get("format") == FORMAT
            and existing.get("python") == python_version()
            and existing.get("runtime_provider") == runtime_provider()
        ):
            existing_targets = existing.get("targets", {})
            if isinstance(existing_targets, dict):
                data["targets"].update(existing_targets)
    target_data: dict = {
        "runtime_manifest_sha256": manifest_hash(manifest),
        "requirements": dependency_specs(),
        "wheels": _wheel_records(target),
    }
    if runtime_provider() == "pbs":
        runtime = _marker(spec.root, ".appbuild-runtime.json")
        sdk = _marker(spec.sdk_root, ".appbuild-sdk.json")
        target_data["runtime"] = {key: runtime.get(key) for key in ("source", "tag", "asset", "url", "sha256", "python", "target") if runtime.get(key) is not None}
        target_data["sdk"] = {key: sdk.get(key) for key in ("tag", "asset", "url", "sha256", "python", "target") if sdk.get(key) is not None}
    data["targets"][target.key] = target_data
    LOCK.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return LOCK


def verify_lock(target: Target) -> None:
    """对照锁文件检查目标 runtime、SDK 与 wheel；输入不一致时阻止继续构建。"""
    if not LOCK.exists():
        raise RuntimeError(f"Lock file not found: {LOCK}; run python tools/build.py --lock")
    data = json.loads(LOCK.read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise RuntimeError(f"Unsupported lock format: {data.get('format')!r}")
    if data.get("python") != python_version() or data.get("runtime_provider") != runtime_provider():
        raise RuntimeError("Lock Python/runtime provider does not match app configuration")
    target_data = data.get("targets", {}).get(target.key)
    if not isinstance(target_data, dict):
        raise RuntimeError(f"Target {target.key} is not present in {LOCK}")
    if target_data.get("requirements", []) != dependency_specs():
        raise RuntimeError(
            f"Lock dependency requirements do not match {app_config_label()}"
        )

    spec = runtime_spec(target)
    manifest = read_manifest(spec.root)
    expected_manifest = target_data.get("runtime_manifest_sha256")
    if expected_manifest and manifest_hash(manifest) != expected_manifest:
        raise RuntimeError(f"Runtime manifest checksum mismatch for {target.key}")

    if runtime_provider() == "pbs":
        recorded_runtime = target_data.get("runtime", {})
        if target.os == "windows" and (not isinstance(recorded_runtime, dict)
                                       or recorded_runtime.get("source") != "python.org"):
            raise RuntimeError("Locked Windows PBS build requires an official python.org embeddable runtime; recreate lock")
        for label, root, marker_name in (
            ("runtime", spec.root, ".appbuild-runtime.json"),
            ("sdk", spec.sdk_root, ".appbuild-sdk.json"),
        ):
            current = _marker(root, marker_name)
            if not current:
                raise RuntimeError(f"Locked build requires cached PBS {label}: {root}")
            for key, expected in target_data.get(label, {}).items():
                if expected and current.get(key) != expected:
                    raise RuntimeError(f"Locked PBS {label} mismatch for {target.key}: {key}")

    actual = {path.name: path for path in wheel_dir(target).glob("*.whl")}
    expected_wheels = {str(record["file"]): record for record in target_data.get("wheels", []) if isinstance(record, dict) and record.get("file")}
    if set(actual) != set(expected_wheels):
        raise RuntimeError(f"Locked wheel set mismatch: expected {sorted(expected_wheels)}, found {sorted(actual)}")
    for name, record in expected_wheels.items():
        if sha256(actual[name]).lower() != str(record["sha256"]).lower():
            raise RuntimeError(f"Locked wheel checksum mismatch: {name}")
