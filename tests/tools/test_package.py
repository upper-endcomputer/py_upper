# 中文维护说明：验证候选目录组装和发布替换；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import json

from py_upper.config import TARGETS, project_version
from py_upper.package import write_release_manifest


def test_release_manifest_is_deterministic_and_hashed(tmp_path):
    out = tmp_path / "dist"
    (out / "DemoApp").mkdir(parents=True)
    (out / "DemoApp" / "DemoApp").write_bytes(b"launcher")
    path = write_release_manifest(TARGETS["linux-x86_64"], out)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == project_version()
    assert data["target"] == "linux-x86_64"
    assert data["files"][0]["path"] == "DemoApp/DemoApp"
    assert len(data["files"][0]["sha256"]) == 64
