from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import LOCK, Target, load_app_config, python_version, runtime_provider, runtime_spec, wheel_dir
from .manifest import manifest_hash, read_manifest
from .third_party import dependency_specs

FORMAT = 5


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_records(target: Target) -> list[dict]:
    return [
        {"file": path.name, "sha256": _sha256(path), "size": path.stat().st_size}
        for path in sorted(wheel_dir(target).glob("*.whl"))
    ]


def _marker(root: Path, name: str) -> dict:
    path = root / name
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def write_lock(target: Target) -> Path:
    spec = runtime_spec(target)
    manifest = read_manifest(spec.root)
    project = str(load_app_config().get("project", {}).get("name") or "py_upper")
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
        runtime = _marker(spec.root, ".py_upper-runtime.json")
        sdk = _marker(spec.sdk_root, ".py_upper-sdk.json")
        target_data["runtime"] = {key: runtime.get(key) for key in ("tag", "asset", "url", "sha256", "python", "target") if runtime.get(key) is not None}
        target_data["sdk"] = {key: sdk.get(key) for key in ("tag", "asset", "url", "sha256", "python", "target") if sdk.get(key) is not None}
    data["targets"][target.key] = target_data
    LOCK.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return LOCK


def verify_lock(target: Target) -> None:
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
        raise RuntimeError("Lock dependency requirements do not match app/pyproject.toml")

    spec = runtime_spec(target)
    manifest = read_manifest(spec.root)
    expected_manifest = target_data.get("runtime_manifest_sha256")
    if expected_manifest and manifest_hash(manifest) != expected_manifest:
        raise RuntimeError(f"Runtime manifest checksum mismatch for {target.key}")

    if runtime_provider() == "pbs":
        for label, root, marker_name in (
            ("runtime", spec.root, ".py_upper-runtime.json"),
            ("sdk", spec.sdk_root, ".py_upper-sdk.json"),
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
        if _sha256(actual[name]).lower() != str(record["sha256"]).lower():
            raise RuntimeError(f"Locked wheel checksum mismatch: {name}")
