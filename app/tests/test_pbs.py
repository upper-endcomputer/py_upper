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
