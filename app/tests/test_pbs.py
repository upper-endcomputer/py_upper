from __future__ import annotations



def _target():
    from py_upper.config import TARGETS
    return TARGETS["macos-arm64"]


def test_exact_pbs_metadata_entry_maps_31110_to_20241016():
    from py_upper.pbs import metadata_asset, metadata_key

    target = _target()
    data = {
        metadata_key(target, "3.11.10"): {
            "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10%2B20241016-aarch64-apple-darwin-install_only_stripped.tar.gz",
            "sha256": "a5a224138a526acecfd17210953d76a28487968a767204902e2bde809bb0e759",
        }
    }
    asset = metadata_asset(data, target, "3.11.10")
    assert asset is not None
    assert asset.name == "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"
    assert asset.sha256.startswith("a5a224")


def test_pbs_auto_resolves_one_release_and_requires_sdk(tmp_path, monkeypatch):
    from py_upper.pbs import PBS_RELEASE_API, PBS_RUNTIME_METADATA_URLS, metadata_key, resolve_pbs_inputs

    target = _target()
    runtime_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10%2B20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"
    sdk_name = "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst"
    metadata = {metadata_key(target, "3.11.10"): {"url": runtime_url, "sha256": "runtime"}}
    release = {"assets": [
        {"name": sdk_name, "browser_download_url": "https://example.invalid/" + sdk_name},
        {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz", "browser_download_url": runtime_url},
    ]}
    calls=[]
    from py_upper import pbs
    # The metadata index is cached on disk. A test must never write its fixture
    # into the project's real cache: a leftover fake index then makes every
    # later build fail with a misleading "No PBS runtime metadata" error.
    monkeypatch.setattr(pbs, "_METADATA_CACHE", tmp_path / "uv-download-metadata.json")
    def fetch(url):
        calls.append(url)
        if url in PBS_RUNTIME_METADATA_URLS:
            return metadata
        if url == PBS_RELEASE_API.format(tag="20241016"):
            return release
        raise AssertionError(url)
    result = resolve_pbs_inputs(fetch, "", target, "3.11.10")
    assert result.tag == "20241016"
    assert result.sdk.name == sdk_name
    assert calls == [PBS_RUNTIME_METADATA_URLS[0], PBS_RELEASE_API.format(tag="20241016")]


def test_pbs_metadata_index_falls_back_when_uv_moves_the_file(tmp_path, monkeypatch):
    """uv reorganised its tree once (crates/uv-python -> crates/uv-python-managed)
    and the old raw URL started returning 404, which took down every build with a
    cold cache. The next candidate must be used instead of failing the build."""
    from py_upper import pbs
    from py_upper.pbs import PBS_RELEASE_API, PBS_RUNTIME_METADATA_URLS, metadata_key, resolve_pbs_inputs

    target = _target()
    runtime_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10%2B20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"
    sdk_name = "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst"
    metadata = {metadata_key(target, "3.11.10"): {"url": runtime_url, "sha256": "runtime"}}
    release = {"assets": [{"name": sdk_name, "browser_download_url": "https://example.invalid/" + sdk_name}]}

    monkeypatch.setattr(pbs, "_METADATA_CACHE", tmp_path / "uv-download-metadata.json")
    moved, calls = PBS_RUNTIME_METADATA_URLS[0], []

    def fetch(url):
        calls.append(url)
        if url == moved:
            raise OSError("HTTP Error 404: Not Found")
        if url in PBS_RUNTIME_METADATA_URLS:
            return metadata
        if url == PBS_RELEASE_API.format(tag="20241016"):
            return release
        raise AssertionError(url)

    result = resolve_pbs_inputs(fetch, "", target, "3.11.10")
    assert result.tag == "20241016"
    assert result.sdk.name == sdk_name
    assert calls == [moved, PBS_RUNTIME_METADATA_URLS[1], PBS_RELEASE_API.format(tag="20241016")]


def test_pbs_metadata_index_error_names_every_candidate(tmp_path, monkeypatch):
    from py_upper import pbs
    from py_upper.pbs import PBS_RUNTIME_METADATA_URLS, resolve_pbs_inputs

    monkeypatch.setattr(pbs, "_METADATA_CACHE", tmp_path / "uv-download-metadata.json")

    def fetch(url):
        raise OSError("HTTP Error 404: Not Found")

    try:
        resolve_pbs_inputs(fetch, "", _target(), "3.11.10")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("resolve_pbs_inputs should fail when no index is reachable")
    for url in PBS_RUNTIME_METADATA_URLS:
        assert url in message


def test_pbs_explicit_release_reports_available_versions():
    from py_upper.pbs import resolve_pbs_inputs, PBS_RELEASE_API

    release = {"assets": [
        {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz", "browser_download_url": "u1"},
        {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst", "browser_download_url": "u2"},
    ]}
    def fetch(url): return release if url == PBS_RELEASE_API.format(tag="20241016") else None
    result = resolve_pbs_inputs(fetch, "20241016", _target(), "3.11.10")
    assert result.runtime.name.endswith("install_only_stripped.tar.gz")


def test_no_runtime_asset_error_is_actionable():
    from py_upper.pbs import no_asset_error

    release = {"assets": [
        {"name": "cpython-3.10.15+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"},
        {"name": "cpython-3.10.15+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
    ]}
    error = no_asset_error("20241016", _target(), "3.11.10", release, missing_runtime=True, missing_sdk=True)
    text = str(error)
    assert "Requested Python: 3.11.10" in text
    assert "Available runtime Python versions: 3.10.15" in text


def _windows_target():
    from py_upper.config import TARGETS
    return TARGETS["windows-x86_64"]


def _fake_sdk(root, python_exe: str, include: str) -> None:
    """A PBS SDK tree reduced to the two things the layout helpers resolve."""
    import json

    (root / "install").mkdir(parents=True, exist_ok=True)
    executable = root / python_exe
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"")
    include_dir = root / include
    include_dir.mkdir(parents=True, exist_ok=True)
    (include_dir / "Python.h").write_text("", encoding="utf-8")
    (root / "PYTHON.json").write_text(
        json.dumps({"python_exe": python_exe, "python_paths": {"include": include}}), encoding="utf-8"
    )


def test_sdk_info_finds_the_windows_interpreter_that_python_json_records(tmp_path, monkeypatch):
    """A Windows PBS tree has no install/bin: python.exe sits next to python3XX.dll.
    Searching for POSIX names reported a complete SDK as missing an interpreter."""
    import json
    from py_upper import pbs

    root = tmp_path / "sdk"
    _fake_sdk(root, "install/python.exe", "install/include")
    (root / pbs.SDK_MARKER).write_text(json.dumps({"format": 2, "python": "3.11.13"}), encoding="utf-8")
    monkeypatch.setattr(pbs, "ensure_pbs_sdk", lambda target: root)
    monkeypatch.setattr(pbs, "python_version", lambda: "3.11.13")

    info = pbs.sdk_info(_windows_target())

    assert info["python_executable"] == str(root / "install" / "python.exe")
    assert info["include_dir"] == str(root / "install" / "include")
    assert info["python_tag"] == "cp311"


def test_sdk_info_still_reads_the_posix_layout(tmp_path, monkeypatch):
    import json
    from py_upper import pbs

    root = tmp_path / "sdk"
    _fake_sdk(root, "install/bin/python3.11", "install/include/python3.11")
    (root / pbs.SDK_MARKER).write_text(json.dumps({"format": 2, "python": "3.11.13"}), encoding="utf-8")
    monkeypatch.setattr(pbs, "ensure_pbs_sdk", lambda target: root)
    monkeypatch.setattr(pbs, "python_version", lambda: "3.11.13")

    info = pbs.sdk_info(_target())

    assert info["python_executable"] == str(root / "install" / "bin" / "python3.11")
    assert info["include_dir"] == str(root / "install" / "include" / "python3.11")


def test_sdk_interpreter_refuses_a_recorded_path_outside_the_sdk(tmp_path):
    from py_upper import pbs

    root = tmp_path / "sdk"
    (root / "install").mkdir(parents=True)
    try:
        pbs._sdk_interpreter(root, _windows_target(), {"python_exe": "../../etc/passwd"})
    except RuntimeError as exc:
        assert "python_exe" in str(exc)
    else:
        raise AssertionError("a recorded path escaping the SDK must not be used")


def test_sdk_include_refuses_an_ambiguous_tree(tmp_path):
    """Four Python.h directories used to be resolved by picking matches[0]."""
    from py_upper import pbs

    root = tmp_path / "sdk"
    for name in ("a", "b"):
        (root / name).mkdir(parents=True)
        (root / name / "Python.h").write_text("", encoding="utf-8")
    try:
        pbs._sdk_include(root, {})
    except RuntimeError as exc:
        assert "several Python.h" in str(exc)
    else:
        raise AssertionError("an ambiguous Python.h search must fail instead of guessing")


def test_best_sdk_follows_the_runtime_build_flavour():
    """Windows releases of that era shipped shared-pgo and static-noopt SDKs under
    names that sort the static one first. Extension modules link the versioned
    import library, which only the shared flavour carries."""
    from py_upper.pbs import _best_sdk

    assets = [
        {"name": "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-shared-pgo-full.tar.zst"},
        {"name": "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-static-noopt-full.tar.zst"},
    ]
    shared_runtime = "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-shared-install_only.tar.gz"
    static_runtime = "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-static-install_only.tar.gz"

    assert _best_sdk(assets, shared_runtime)["name"].endswith("-shared-pgo-full.tar.zst")
    assert _best_sdk(assets, static_runtime)["name"].endswith("-static-noopt-full.tar.zst")

    try:
        _best_sdk([assets[1]], shared_runtime)
    except RuntimeError as exc:
        assert "shared" in str(exc)
    else:
        raise AssertionError("a flavoured runtime without a matching SDK must fail loudly")


def test_best_sdk_keeps_pgo_lto_when_the_runtime_records_no_flavour():
    from py_upper.pbs import _best_sdk

    assets = [
        {"name": "cpython-3.11.13+20251007-aarch64-apple-darwin-full.tar.zst"},
        {"name": "cpython-3.11.13+20251007-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
    ]
    runtime = "cpython-3.11.13+20251007-aarch64-apple-darwin-install_only_stripped.tar.gz"
    assert _best_sdk(assets, runtime)["name"].endswith("-pgo+lto-full.tar.zst")


def test_windows_metadata_resolution_pairs_a_shared_sdk_with_a_shared_runtime(tmp_path, monkeypatch):
    from py_upper import pbs
    from py_upper.pbs import PBS_RELEASE_API, metadata_key, resolve_pbs_inputs

    target = _windows_target()
    runtime_name = "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-shared-install_only.tar.gz"
    runtime_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20230507/" + runtime_name
    metadata = {metadata_key(target, "3.10.11"): {"url": runtime_url, "sha256": "runtime"}}
    release = {"assets": [
        {"name": "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-shared-pgo-full.tar.zst", "browser_download_url": "u1"},
        {"name": "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-static-noopt-full.tar.zst", "browser_download_url": "u2"},
    ]}

    monkeypatch.setattr(pbs, "_METADATA_CACHE", tmp_path / "uv-download-metadata.json")

    def fetch(url):
        if url in pbs.PBS_RUNTIME_METADATA_URLS:
            return metadata
        if url == PBS_RELEASE_API.format(tag="20230507"):
            return release
        raise AssertionError(url)

    result = resolve_pbs_inputs(fetch, "", target, "3.10.11")
    assert result.runtime.name == runtime_name
    assert result.sdk.name.endswith("-shared-pgo-full.tar.zst")
