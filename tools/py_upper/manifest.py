from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import ROOT, RuntimeSpec, Target

FORMAT = 2
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
    runtime_root = spec.root.resolve()
    sdk_root = spec.sdk_root.resolve()
    if not runtime_root.exists():
        raise RuntimeError(f"Runtime directory missing: {runtime_root}")
    if not sdk_root.exists():
        raise RuntimeError(f"SDK directory missing: {sdk_root}")
    runtime_info = data.get("runtime")
    sdk_info = data.get("sdk")
    if not isinstance(runtime_info, dict) or runtime_info.get("root") != ".":
        raise RuntimeError("Runtime manifest runtime.root must be '.'")
    if not isinstance(sdk_info, dict) or sdk_info.get("root") != "configured":
        raise RuntimeError("Runtime manifest sdk.root must be 'configured'")
    for field in ("python_executable", "include_dir"):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"Runtime manifest missing {field}")
        if Path(value).is_absolute():
            raise RuntimeError(f"Runtime manifest {field} must be relative to the SDK root")


def _relative_to_sdk(value: Any, sdk_root: Path) -> str:
    if not value:
        return ""
    p = Path(str(value))
    if p.is_absolute():
        try:
            return p.resolve().relative_to(sdk_root.resolve()).as_posix()
        except ValueError:
            return p.name
    return p.as_posix()


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
        # Keep the manifest portable: physical cache/workspace paths are build
        # inputs, not runtime identity. The configured provider resolves them.
        "runtime": {"root": "."},
        "sdk": {"root": "configured"},
        "python_executable": _relative_to_sdk(sdk.get("python_executable"), spec.sdk_root),
        "include_dir": _relative_to_sdk(sdk.get("include_dir"), spec.sdk_root),
        "pbs": ({"release": sdk.get("tag"), "asset": sdk.get("asset"), "sha256": sdk.get("sha256")} if spec.provider == "pbs" else None),
    }
