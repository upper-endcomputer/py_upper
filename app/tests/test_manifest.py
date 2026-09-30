import json
import sys
from pathlib import Path

from pstand.config import TARGETS, RuntimeSpec
from pstand.manifest import build_manifest, canonical_json, manifest_hash, validate_manifest


def test_manifest_is_portable_and_validates(tmp_path):
    target = TARGETS["linux-x86_64"]
    runtime = tmp_path / "runtime"
    sdk = tmp_path / "sdk"
    runtime.mkdir(); sdk.mkdir()
    spec = RuntimeSpec("local", "3.13", target, runtime, sdk)
    data = build_manifest(spec, {
        "python": "3.13.5",
        "python_executable": str(sdk / "bin/python3"),
        "include_dir": str(sdk / "include/python3.13"),
    }, runtime)
    validate_manifest(data, target, spec)
    assert data["runtime"]["root"] == "."
    assert data["sdk"]["root"] == "configured"
    assert str(tmp_path) not in canonical_json(data).decode()
    assert not Path(data["python_executable"]).is_absolute()
    assert not Path(data["include_dir"]).is_absolute()
    assert len(manifest_hash(data)) == 64


def test_manifest_hash_is_deterministic():
    data = {"b": 2, "a": 1}
    assert manifest_hash(data) == manifest_hash(json.loads(json.dumps(data)))
