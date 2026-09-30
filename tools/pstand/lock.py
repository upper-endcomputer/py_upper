from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

from .config import ROOT, Target, load_app_config, pbs_release, pbs_sdk_dir, python_version, wheel_dir, target_runtime_dir, runtime_provider
from .runtime_provider import sdk_info
from .manifest import manifest_hash, read_manifest

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
    provider = runtime_provider()
    if provider == "local":
        info = sdk_info(target)
        root = Path(str(info["root"])).resolve()
        sdk_root = Path(str(info["sdk_root"])).resolve()
        return {
            "provider": "local",
            "python": info.get("python"),
            "target_triple": info.get("target_triple"),
            "root": root.relative_to(ROOT.resolve()).as_posix() if root.is_relative_to(ROOT.resolve()) else None,
            "sdk_root": sdk_root.relative_to(ROOT.resolve()).as_posix() if sdk_root.is_relative_to(ROOT.resolve()) else None,
        }
    marker = target_runtime_dir(target) / ".pystand2-runtime.json"
    if not marker.exists():
        return {}
    return json.loads(marker.read_text(encoding="utf-8"))

def write_lock(target: Target) -> Path:
    if LOCK.exists():
        data = json.loads(LOCK.read_text(encoding="utf-8"))
    else:
        data = {
            "format": 2,
            "project": load_app_config()["project"]["name"],
            "python": python_version(),
            "runtime_provider": runtime_provider(),
            "generated_by": "PyStand2",
            "host": {"system": platform.system(), "machine": platform.machine()},
            "targets": {},
        }
    data["format"] = 3
    data["python"] = python_version()
    data["runtime_provider"] = runtime_provider()
    if runtime_provider() == "pbs":
        data["pbs_release"] = pbs_release()
    runtime_record = _runtime_record(target)
    manifest = read_manifest(target_runtime_dir(target))
    target_data = {
        "runtime": runtime_record,
        "runtime_manifest_sha256": manifest_hash(manifest),
        "wheels": _wheel_records(target),
    }
    if runtime_provider() == "pbs":
        info = sdk_info(target)
        target_data["pbs_sdk"] = {
            "tag": info.get("tag"),
            "asset": info.get("asset"),
            "sha256": info.get("sha256"),
        }
    data["targets"][target.key] = target_data
    LOCK.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return LOCK

def verify_lock(target: Target) -> None:
    if not LOCK.exists():
        raise RuntimeError(f"Lock file not found: {LOCK}. Run: python tools/build.py --lock --target {target.key}")
    data = json.loads(LOCK.read_text(encoding="utf-8"))
    if str(data.get("python")) != python_version():
        raise RuntimeError(f"Lock Python {data.get('python')} != app Python {python_version()}")
    if str(data.get("runtime_provider") or "pbs") != runtime_provider():
        raise RuntimeError(f"Lock runtime provider mismatch: {data.get('runtime_provider')} != {runtime_provider()}")
    target_data = data.get("targets", {}).get(target.key)
    if not target_data:
        raise RuntimeError(f"Target {target.key} is not present in {LOCK}")

    if runtime_provider() == "pbs":
        if str(data.get("pbs_release") or "") != pbs_release():
            raise RuntimeError(f"Lock PBS release {data.get('pbs_release')} != app PBS release {pbs_release()}")
        sdk_marker = pbs_sdk_dir(target) / ".pystand2-sdk.json"
        if not sdk_marker.exists():
            raise RuntimeError(f"Locked build requires cached PBS SDK: {pbs_sdk_dir(target)}")
        sdk = json.loads(sdk_marker.read_text(encoding="utf-8"))
        locked_sdk = target_data.get("pbs_sdk", {})
        for key in ("tag", "asset", "sha256"):
            if locked_sdk.get(key) and sdk.get(key) != locked_sdk[key]:
                raise RuntimeError(f"PBS SDK lock mismatch for {target.key}: {key}")
    else:
        info = sdk_info(target)
        locked_runtime = target_data.get("runtime", {})
        if str(locked_runtime.get("provider")) != "local":
            raise RuntimeError(f"Local runtime lock provider mismatch for {target.key}")
        if locked_runtime.get("python") and str(info.get("python")) != str(locked_runtime["python"]):
            raise RuntimeError(f"Local runtime Python lock mismatch for {target.key}")
        if locked_runtime.get("target_triple") and str(info.get("target_triple")) != str(locked_runtime["target_triple"]):
            raise RuntimeError(f"Local runtime target lock mismatch for {target.key}")
        current_root = Path(str(info["root"])).resolve()
        current_sdk = Path(str(info["sdk_root"])).resolve()
        for key, current in (("root", current_root), ("sdk_root", current_sdk)):
            locked_value = locked_runtime.get(key)
            if not locked_value:
                raise RuntimeError(f"Local runtime lock missing {key} for {target.key}")
            locked = (ROOT / str(locked_value)).resolve()
            if current != locked:
                raise RuntimeError(f"Local runtime lock mismatch for {target.key}: {key}")

    locked_manifest_hash = target_data.get("runtime_manifest_sha256")
    manifest = read_manifest(target_runtime_dir(target))
    if locked_manifest_hash and manifest_hash(manifest) != locked_manifest_hash:
        raise RuntimeError(f"Runtime manifest checksum mismatch for {target.key}")

    locked_runtime = target_data.get("runtime", {})
    if runtime_provider() == "pbs":
        runtime_marker = target_runtime_dir(target) / ".pystand2-runtime.json"
        if not runtime_marker.exists():
            raise RuntimeError(f"Locked build requires cached PBS runtime: {target_runtime_dir(target)}")
        runtime = _runtime_record(target)
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
