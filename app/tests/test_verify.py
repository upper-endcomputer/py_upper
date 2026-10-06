from __future__ import annotations

import json


def test_smoke_modules_include_entry_and_direct_dependencies(tmp_path, monkeypatch):
    import py_upper.verify as smoke

    monkeypatch.setattr(smoke, "selected_sources", lambda: [])
    monkeypatch.setattr(smoke, "entry_module", lambda: "main")
    manifest_dir = tmp_path / "wheels" / "linux-x86_64"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(json.dumps({"imports": ["PySide6"]}), encoding="utf-8")
    monkeypatch.setattr(smoke, "wheel_dir", lambda target: manifest_dir)
    from py_upper.config import TARGETS
    assert smoke.smoke_modules(TARGETS["linux-x86_64"]) == ["PySide6", "main"]
