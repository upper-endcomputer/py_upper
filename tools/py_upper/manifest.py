from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import RuntimeSpec, Target

FORMAT = 4
FILENAME = "manifest.json"


def canonical_json(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def manifest_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(data)).hexdigest()


def write_manifest(root: Path, data: dict[str, Any]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / FILENAME
    path.write_bytes(canonical_json(data))
    return path


def read_manifest(root: Path) -> dict[str, Any]:
    path = root / FILENAME
    if not path.exists():
        raise RuntimeError(f"Runtime manifest not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid runtime manifest root: {path}")
    return data


def _relative(value: str | Path | None, root: Path) -> str:
    if not value:
        return ""
    path = Path(str(value))
    if not path.is_absolute():
        if ".." in path.parts:
            raise RuntimeError(f"Path {path} must not escape {root}")
        return path.as_posix()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"Path {path} must be inside {root} for a portable manifest") from exc


def build_manifest(spec: RuntimeSpec, sdk: dict[str, Any]) -> dict[str, Any]:
    python = str(sdk.get("python") or spec.python)
    major_minor = ".".join(python.split(".")[:2])
    abi = str(sdk.get("python_tag") or f"cp{major_minor.replace('.', '')}")
    data: dict[str, Any] = {
        "format": FORMAT,
        "provider": spec.provider,
        "target": spec.target.key,
        "target_triple": spec.target.triple,
        "python": python,
        "python_major_minor": major_minor,
        "python_abi": abi,
        "python_platform_tag": str(sdk.get("python_platform_tag") or spec.target.primary_wheel_platform),
        "python_implementation": "cpython",
        "runtime": {"root": "."},
        "sdk": {"root": "configured"},
        "python_executable": _relative(sdk.get("python_executable"), spec.sdk_root),
        "include_dir": _relative(sdk.get("include_dir"), spec.sdk_root),
    }
    if spec.provider == "pbs":
        data["pbs"] = {
            "release": sdk.get("tag"),
            "asset": sdk.get("asset"),
            "sha256": sdk.get("sha256"),
            "url": sdk.get("url"),
        }
    return data


def validate_manifest(data: dict[str, Any], target: Target, spec: RuntimeSpec) -> None:
    expected = {
        "format": FORMAT,
        "provider": spec.provider,
        "target": target.key,
        "target_triple": target.triple,
        "python": spec.python,
        "python_major_minor": spec.major_minor,
        "python_abi": spec.abi_tag,
    }
    for key, value in expected.items():
        if str(data.get(key)) != str(value):
            raise RuntimeError(f"Runtime manifest {key} {data.get(key)!r} != {value!r}")
    if not spec.root.exists() or not spec.sdk_root.exists():
        raise RuntimeError(f"Runtime/SDK missing for {target.key}: runtime={spec.root} sdk={spec.sdk_root}")
    for key in ("python_executable", "include_dir"):
        value = data.get(key)
        if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
            raise RuntimeError(f"Runtime manifest {key} must be a relative SDK path without '..'")
        if not (spec.sdk_root / value).exists():
            raise RuntimeError(f"Runtime manifest {key} does not exist: {spec.sdk_root / value}")
