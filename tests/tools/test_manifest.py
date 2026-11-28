# 中文维护说明：验证清单摘要和文件覆盖；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
import json

from py_upper.config import RuntimeSpec, TARGETS
from py_upper.manifest import build_manifest, canonical_json, manifest_hash, validate_manifest


def test_manifest_round_trip_is_portable_and_hashed(tmp_path):
    target = TARGETS["linux-x86_64"]
    runtime = tmp_path / "runtime"; sdk = tmp_path / "sdk"
    runtime.mkdir(); (sdk / "bin").mkdir(parents=True); (sdk / "include").mkdir()
    (sdk / "bin" / "python3.13").write_text("", encoding="utf-8")
    (sdk / "include" / "Python.h").write_text("", encoding="utf-8")
    spec = RuntimeSpec("local", "3.13.5", target, runtime, sdk)
    data = build_manifest(spec, {"python":"3.13.5","python_tag":"cp313","python_platform_tag":"manylinux_2_17_x86_64","python_executable":sdk/"bin/python3.13","include_dir":sdk/"include"})
    validate_manifest(data, target, spec)
    assert data["python_executable"] == "bin/python3.13"
    assert str(tmp_path) not in canonical_json(data).decode()
    assert len(manifest_hash(data)) == 64


def test_manifest_hash_is_deterministic():
    data = {"b": 2, "a": 1}
    assert manifest_hash(data) == manifest_hash(json.loads(json.dumps(data)))
