from __future__ import annotations

import json
import os

import pytest


def test_verify_authenticates_each_artifact_once_without_reusing_prior_results(tmp_path, monkeypatch, capsys):
    from py_upper import egg_loader, verify
    from py_upper.config import TARGETS, runtime_spec, target_extension_suffix
    from py_upper.egg import EGG_DIR, EGG_NAME, EggConfig, egg_loader_source, pack_egg
    from test_native_deployment import _elf

    target = TARGETS["linux-arm64"]
    stage = tmp_path / "stage"
    package = tmp_path / "package"
    site = tmp_path / "payload"
    (stage / "site-packages").mkdir(parents=True)
    spec = runtime_spec(target)
    suffix = target_extension_suffix(target, spec.major_minor, spec.abi_tag)
    _elf(site / ("main" + suffix), machine=183)
    config = EggConfig()
    staged = pack_egg(site, stage / EGG_DIR / EGG_NAME, ["main" + suffix], config)
    packaged = package / EGG_DIR / EGG_NAME
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(staged.read_bytes())
    launcher = _elf(package / "VerifyApp", machine=183, file_type=2)
    launcher.write_bytes(launcher.read_bytes() + verify.entry_source().encode() + egg_loader_source().encode())
    monkeypatch.setattr(verify, "app_name", lambda: "VerifyApp")
    monkeypatch.setattr(verify, "entry_module", lambda: "main")
    monkeypatch.setattr(verify, "cython_merge_mode", lambda: "none")
    monkeypatch.setattr(verify, "read_manifest", lambda root: {"format": 4})
    monkeypatch.setattr(verify, "validate_manifest", lambda manifest, target, spec: None)
    monkeypatch.setattr(verify, "dependency_specs", lambda: [])
    monkeypatch.setattr(verify, "dependency_names", lambda path: [])
    monkeypatch.setattr(verify, "staging_dir", lambda target: stage)
    monkeypatch.setattr(verify, "_stage_egg_config", lambda stage: config)
    monkeypatch.setattr(verify, "egg_config", lambda: config)
    monkeypatch.setattr(verify, "_package_root", lambda target: package)
    monkeypatch.setattr(verify, "selected_sources", lambda: [verify.APP / "src" / "main.py"])

    calls = []
    original = egg_loader.open_egg

    def observe(raw, key):
        calls.append(raw)
        return original(raw, key)

    monkeypatch.setattr(egg_loader, "open_egg", observe)
    assert verify.verify(target) == 0
    output = capsys.readouterr().out
    assert len(calls) == 2
    assert "PASS encrypted egg authenticated" in output
    assert "PASS packaged egg authenticated" in output
    assert "PASS egg native dependencies portable" in output
    assert output.count("PASS egg manifest covers every member") == 2

    stamp = packaged.stat()
    data = bytearray(packaged.read_bytes())
    data[-1] ^= 1
    packaged.write_bytes(data)
    os.utime(packaged, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert verify.verify(target) == 1
    output = capsys.readouterr().out
    assert len(calls) == 4
    assert "PASS encrypted egg authenticated" in output
    assert "FAIL packaged egg" in output
    assert "egg authentication failed" in output


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


@pytest.mark.parametrize("member", ["module.pyd", "py_upper_bundles/py_upper_demo.pyd"])
def test_egg_cannot_hide_custom_native_dependencies(tmp_path, monkeypatch, member):
    from py_upper import verify
    from py_upper.config import TARGETS
    from py_upper.egg import EggConfig, open_egg_archive, pack_egg, remove_packed_files
    from py_upper.native import deps
    from test_native_deployment import _pe

    monkeypatch.setattr(deps, "_dumpbin_dependencies", lambda path, env=None: [])
    site = tmp_path / "site-packages"
    _pe(site / member, ["vendor_driver.dll"])
    config = EggConfig()
    egg = pack_egg(site, tmp_path / "py_upper.dat", [member], config)
    remove_packed_files(site, [member])

    with open_egg_archive(egg, config.key_hex) as archive:
        checks = verify._verify_egg_native_dependencies(archive, TARGETS["windows-x86_64"], tmp_path / "runtime")

    assert len(checks) == 1
    assert checks[0][0] is False
    assert "vendor_driver.dll" in checks[0][1]
    assert "--no-egg" in checks[0][1]


def test_egg_windows_runtime_dependency_must_be_bundled(tmp_path, monkeypatch):
    from py_upper import verify
    from py_upper.config import TARGETS
    from py_upper.egg import EggConfig, open_egg_archive, pack_egg
    from py_upper.native import deps
    from test_native_deployment import _pe

    monkeypatch.setattr(deps, "_dumpbin_dependencies", lambda path, env=None: [])
    site = tmp_path / "site-packages"
    _pe(site / "module.pyd", ["vcruntime140.dll", "kernel32.dll"])
    config = EggConfig()
    egg = pack_egg(site, tmp_path / "py_upper.dat", ["module.pyd"], config)
    runtime = tmp_path / "runtime"
    target = TARGETS["windows-x86_64"]

    with open_egg_archive(egg, config.key_hex) as archive:
        assert not all(ok for ok, _ in verify._verify_egg_native_dependencies(archive, target, runtime))
        _pe(runtime / "vcruntime140.dll")
        assert not all(ok for ok, _ in verify._verify_egg_native_dependencies(archive, target, runtime))
        _pe(runtime / "python313.dll", ["vcruntime140.dll"])
        assert all(ok for ok, _ in verify._verify_egg_native_dependencies(archive, target, runtime))
