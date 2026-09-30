import json

from py_upper.config import TARGETS
from py_upper.release_artifacts import write_release_manifest


def test_release_manifest_is_deterministic_and_hashed(tmp_path):
    out = tmp_path / "dist"
    (out / "DemoApp").mkdir(parents=True)
    (out / "DemoApp" / "DemoApp").write_bytes(b"launcher")
    path = write_release_manifest(TARGETS["linux-x86_64"], out)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "0.17.0"
    assert data["target"] == "linux-x86_64"
    assert data["files"][0]["path"] == "DemoApp/DemoApp"
    assert len(data["files"][0]["sha256"]) == 64
