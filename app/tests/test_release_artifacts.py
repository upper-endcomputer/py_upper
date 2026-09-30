import json

from pstand.config import TARGETS
from pstand.release_artifacts import write_release_manifest


def test_release_manifest_is_deterministic_and_hashed(tmp_path):
    out = tmp_path / "dist"
    (out / "MyApp").mkdir(parents=True)
    (out / "MyApp" / "MyApp").write_bytes(b"launcher")
    path = write_release_manifest(TARGETS["linux-x86_64"], out)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "0.16.0"
    assert data["target"] == "linux-x86_64"
    assert data["files"][0]["path"] == "MyApp/MyApp"
    assert len(data["files"][0]["sha256"]) == 64
