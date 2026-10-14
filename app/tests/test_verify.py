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


def test_windows_pbs_package_requires_embeddable_layout(tmp_path, monkeypatch):
    from py_upper import verify
    from py_upper.config import TARGETS, runtime_spec

    target = TARGETS["windows-x86_64"]
    # The version-dependent names follow the configured pin; the check reads its
    # digits from the same runtime spec, so the test must not hardcode one.
    digits = runtime_spec(target).major_minor.replace(".", "")
    versioned = (f"python{digits}.dll", f"python{digits}.zip", f"python{digits}._pth")
    monkeypatch.setattr(verify, "DIST", tmp_path)
    monkeypatch.setattr(verify, "app_name", lambda: "MyApp")
    monkeypatch.setattr(verify, "selected_sources", lambda: [])
    root = tmp_path / "MyApp" / "runtime"
    root.mkdir(parents=True)
    labels = {label: ok for ok, label in verify._checks_for_package(target)}
    assert labels["Windows embed runtime is flat"]
    assert labels[f"Windows embed runtime {versioned[1]}"] is False
    for name in ("python3.dll", *versioned):
        (root / name).write_bytes(b"runtime")
    labels = {label: ok for ok, label in verify._checks_for_package(target)}
    assert all(labels[f"Windows embed runtime {name}"] for name in ("python3.dll", *versioned))


def test_windows_pbs_stage_smoke_uses_sdk_interpreter(tmp_path, monkeypatch):
    from py_upper import verify, runtime
    from py_upper.config import TARGETS

    target = TARGETS["windows-x86_64"]
    sdk_python = tmp_path / "sdk" / "install" / "python.exe"
    calls = []
    monkeypatch.setattr(verify, "host_target", lambda: target)
    monkeypatch.setattr(verify, "BUILD", tmp_path)
    monkeypatch.setattr(verify, "write_smoke_script", lambda target, path: path)
    monkeypatch.setattr(runtime, "sdk_info", lambda target: {"python_executable": str(sdk_python)})
    monkeypatch.setattr(verify.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))

    verify.run_target_python_smoke(target)
    assert calls[0][0][0] == str(sdk_python)
    assert "PYTHONHOME" not in calls[0][1]["env"]
    assert calls[0][1]["env"]["PYTHONPATH"].endswith("site-packages")
