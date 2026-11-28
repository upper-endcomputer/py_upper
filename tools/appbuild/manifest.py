# 中文维护说明：生成运行时清单及构建来源信息，验证摘要与路径边界。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import APP, APP_CONFIG, ROOT, RuntimeSpec, Target, python_version

FORMAT = 4
FILENAME = "manifest.json"


def canonical_json(data: dict[str, Any]) -> bytes:
    """按稳定键顺序编码 UTF-8 JSON，末尾保留换行以得到确定字节。"""
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def manifest_hash(data: dict[str, Any]) -> str:
    """对规范化清单字节计算 SHA-256。"""
    return hashlib.sha256(canonical_json(data)).hexdigest()


def write_manifest(root: Path, data: dict[str, Any]) -> Path:
    """创建目标目录并写入规范化清单，返回清单路径。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / FILENAME
    path.write_bytes(canonical_json(data))
    return path


def read_manifest(root: Path) -> dict[str, Any]:
    """读取 runtime 清单对象；缺失或顶层类型错误时报错。"""
    path = root / FILENAME
    if not path.exists():
        raise RuntimeError(f"Runtime manifest not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid runtime manifest root: {path}")
    return data


def _relative(value: str | Path | None, root: Path) -> str:
    """把 SDK 内路径转换为可迁移相对路径；拒绝越界和父目录引用。"""
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
    """依据运行时规格和 SDK 元信息生成清单，路径相对 SDK 保存。"""
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
    """核对清单格式、目标、ABI 和 SDK 路径存在性；不一致时抛出 RuntimeError。"""
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


def write_build_info(root, target, options):
    """向候选包写入构建来源与输入摘要；macOS 写入 Contents/Resources，避免把配置秘密落盘。"""
    digest = hashlib.sha256()
    for path in sorted((APP / 'src').rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts:
            digest.update(path.relative_to(APP).as_posix().encode())
            digest.update(path.read_bytes())
    git = shutil.which('git')
    result = subprocess.run([git, 'rev-parse', 'HEAD'], cwd=ROOT,
                            capture_output=True, text=True, check=False) if git else None
    dirty = subprocess.run([git, 'diff', '--quiet'], cwd=ROOT, check=False).returncode != 0 if git else None
    data = {
        'format': 1, 'builder_commit': result.stdout.strip() if result and result.returncode == 0 else None,
        'builder_dirty': dirty,
        'build_python': sys.version.split()[0], 'target': target.key,
        'target_python': python_version(),
        'source_sha256': digest.hexdigest(),
        'config_sha256': hashlib.sha256(APP_CONFIG.read_bytes()).hexdigest(),
        'options': options,
        'reproducibility': 'input provenance; not a byte-identical build guarantee',
    }
    resources = root / 'Contents/Resources' if target.os == 'macos' else root
    resources.mkdir(parents=True, exist_ok=True)
    (resources / 'build-info.json').write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
