from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

from .config import ROOT, Target, load_app_config, pbs_sdk_dir, python_version, wheel_dir, target_runtime_dir
from .pbs_sdk import sdk_info

LOCK = ROOT / "pystand.lock.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _wheel_records(target: Target) -> list[dict]:
    out = wheel_dir(target)
    return [
        {"file": p.name, "sha256": _sha256(p), "size": p.stat().st_size}
        for p in sorted(out.glob("*.whl"))
    ]


def _runtime_record(target: Target) -> dict:
    marker = target_runtime_dir(target) / ".pystand2-runtime.json"
    if not marker.exists():
        return {}
    return json.loads(marker.read_text(encoding="utf-8"))


def write_lock(target: Target) -> Path:
    if LOCK.exists():
        data = json.loads(LOCK.read_text(encoding="utf-8"))
    else:
        data = {
            "format": 1,
            "project": load_app_config()["project"]["name"],
            "python": python_version(),
            "generated_by": "PyStand2",
            "host": {"system": platform.system(), "machine": platform.machine()},
            "targets": {},
        }
    data["python"] = python_version()
    data["targets"][target.key] = {
        "pbs_sdk": {
            "tag": sdk_info(target).get("tag"),
            "asset": sdk_info(target).get("asset"),
            "sha256": sdk_info(target).get("sha256"),
        },
        "runtime": _runtime_record(target),
        "wheels": _wheel_records(target),
    }
    LOCK.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return LOCK


def verify_lock(target: Target) -> None:
    if not LOCK.exists():
        raise RuntimeError(f"Lock file not found: {LOCK}. Run: python tools/build.py lock --target {target.key}")
    data = json.loads(LOCK.read_text(encoding="utf-8"))
    if str(data.get("python")) != python_version():
        raise RuntimeError(f"Lock Python {data.get('python')} != app Python {python_version()}")
    target_data = data.get("targets", {}).get(target.key)
    if not target_data:
        raise RuntimeError(f"Target {target.key} is not present in {LOCK}")

    sdk_marker = pbs_sdk_dir(target) / ".pystand2-sdk.json"
    if not sdk_marker.exists():
        raise RuntimeError(f"Locked build requires cached PBS SDK: {pbs_sdk_dir(target)}")
    sdk = json.loads(sdk_marker.read_text(encoding="utf-8"))
    locked_sdk = target_data.get("pbs_sdk", {})
    for key in ("tag", "asset", "sha256"):
        if locked_sdk.get(key) and sdk.get(key) != locked_sdk[key]:
            raise RuntimeError(f"PBS SDK lock mismatch for {target.key}: {key}")

    runtime_marker = target_runtime_dir(target) / ".pystand2-runtime.json"
    if not runtime_marker.exists():
        raise RuntimeError(f"Locked build requires cached PBS runtime: {target_runtime_dir(target)}")
    runtime = _runtime_record(target)
    locked_runtime = target_data.get("runtime", {})
    for key in ("tag", "asset", "sha256", "python"):
        if locked_runtime.get(key) and runtime.get(key) != locked_runtime[key]:
            raise RuntimeError(f"PBS runtime lock mismatch for {target.key}: {key}")

    records = {x["file"]: x for x in target_data.get("wheels", [])}
    out = wheel_dir(target)
    actual = {p.name: p for p in out.glob("*.whl")}
    if set(actual) != set(records):
        raise RuntimeError(f"Wheel lock mismatch for {target.key}: locked={sorted(records)}, actual={sorted(actual)}")
    for name, rec in records.items():
        if _sha256(actual[name]).lower() != rec["sha256"].lower():
            raise RuntimeError(f"Wheel checksum mismatch: {name}")
