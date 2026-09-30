from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import ROOT, RuntimeSpec, Target

FORMAT = 1
FILENAME = "manifest.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def manifest_hash(data: dict[str, Any]) -> str:
    return _sha256_bytes(canonical_json(data))


def write_manifest(root: Path, data: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / FILENAME
    path.write_bytes(canonical_json(data))
    return path


def read_manifest(root: Path) -> dict[str, Any]:
    path = root / FILENAME
    if not path.exists():
        raise RuntimeError(f"Runtime manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid runtime manifest: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid runtime manifest root: {path}")
    return data


def validate_manifest(data: dict[str, Any], target: Target, spec: RuntimeSpec) -> None:
    if int(data.get("format", -1)) != FORMAT:
        raise RuntimeError(f"Unsupported runtime manifest format: {data.get('format')!r}")
    if data.get("provider") != spec.provider:
        raise RuntimeError(f"Runtime manifest provider {data.get('provider')!r} != {spec.provider!r}")
    if data.get("target") != target.key:
        raise RuntimeError(f"Runtime manifest target {data.get('target')!r} != {target.key!r}")
    if data.get("target_triple") != target.triple:
        raise RuntimeError(f"Runtime manifest target triple {data.get('target_triple')!r} != {target.triple!r}")
    if str(data.get("python_major_minor")) != spec.major_minor:
        raise RuntimeError(f"Runtime manifest Python {data.get('python_major_minor')!r} != {spec.major_minor!r}")
    if data.get("python_abi") != spec.abi_tag:
        raise RuntimeError(f"Runtime manifest ABI {data.get('python_abi')!r} != {spec.abi_tag!r}")
    for field in ("runtime", "sdk"):
        value = data.get(field)
        if not isinstance(value, dict) or not value.get("root"):
            raise RuntimeError(f"Runtime manifest missing {field}.root")
    runtime_root = Path(data["runtime"]["root"]).resolve()
    sdk_root = Path(data["sdk"]["root"]).resolve()
    if runtime_root != spec.root.resolve():
        raise RuntimeError(f"Runtime manifest runtime.root {runtime_root} != configured {spec.root.resolve()}")
    if sdk_root != spec.sdk_root.resolve():
        raise RuntimeError(f"Runtime manifest sdk.root {sdk_root} != configured {spec.sdk_root.resolve()}")
    if not runtime_root.exists():
        raise RuntimeError(f"Runtime directory missing: {runtime_root}")
    if not sdk_root.exists():
        raise RuntimeError(f"SDK directory missing: {sdk_root}")
    if not data.get("python_executable"):
        raise RuntimeError("Runtime manifest missing python_executable")
    if not data.get("include_dir"):
        raise RuntimeError("Runtime manifest missing include_dir")


def build_manifest(spec: RuntimeSpec, sdk: dict[str, Any], runtime_root: Path) -> dict[str, Any]:
    py = str(sdk.get("python") or spec.python)
    major_minor = str(sdk.get("python_major_minor") or ".".join(py.split(".")[:2]))
    python_abi = str(sdk.get("python_tag") or f"cp{major_minor.replace('.', '')}")
    platform_tag = sdk.get("python_platform_tag")
    if not platform_tag:
        platform_tag = {
            "windows-x86": "win32",
            "windows-x86_64": "win_amd64",
            "windows-arm64": "win_arm64",
            "linux-x86_64": "manylinux_2_17_x86_64",
            "linux-arm64": "manylinux_2_17_aarch64",
            "macos-x86_64": "macosx_10_15_x86_64",
            "macos-arm64": "macosx_11_0_arm64",
        }[spec.target.key]
    return {
        "format": FORMAT,
        "provider": spec.provider,
        "target": spec.target.key,
        "target_triple": spec.target.triple,
        "python": py,
        "python_major_minor": major_minor,
        "python_abi": python_abi,
        "python_platform_tag": platform_tag,
        "python_implementation": sdk.get("python_implementation_name", "cpython"),
        "runtime": {"root": str(runtime_root.resolve())},
        "sdk": {"root": str(spec.sdk_root.resolve())},
        "python_executable": str(sdk.get("python_executable") or ""),
        "include_dir": str(sdk.get("include_dir") or ""),
        "pbs": ({"release": sdk.get("tag"), "asset": sdk.get("asset"), "sha256": sdk.get("sha256")} if spec.provider == "pbs" else None),
    }
