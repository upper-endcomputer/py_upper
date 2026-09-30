from __future__ import annotations

import json


def _target():
    from py_upper.config import TARGETS
    return TARGETS["macos-arm64"]


def test_exact_pbs_metadata_entry_maps_31110_to_20241016():
    from py_upper.pbs_assets import metadata_asset, metadata_key

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


def test_pbs_auto_resolves_one_release_and_requires_sdk():
    from py_upper.pbs_assets import PBS_RELEASE_API, PBS_RUNTIME_METADATA_URL, metadata_key, resolve_pbs_inputs

    target = _target()
    runtime_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10%2B20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"
    sdk_name = "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst"
    metadata = {metadata_key(target, "3.11.10"): {"url": runtime_url, "sha256": "runtime"}}
    release = {"assets": [
        {"name": sdk_name, "browser_download_url": "https://example.invalid/" + sdk_name},
        {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz", "browser_download_url": runtime_url},
    ]}
    calls=[]
    from py_upper import pbs_assets
    pbs_assets._METADATA_CACHE.unlink(missing_ok=True)
    def fetch(url):
        calls.append(url)
        if url == PBS_RUNTIME_METADATA_URL:
            return metadata
        if url == PBS_RELEASE_API.format(tag="20241016"):
            return release
        raise AssertionError(url)
    result = resolve_pbs_inputs(fetch, "", target, "3.11.10")
    assert result.tag == "20241016"
    assert result.sdk.name == sdk_name
    assert calls == [PBS_RUNTIME_METADATA_URL, PBS_RELEASE_API.format(tag="20241016")]


def test_pbs_explicit_release_reports_available_versions():
    from py_upper.pbs_assets import resolve_pbs_inputs, PBS_RELEASE_API

    release = {"assets": [
        {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz", "browser_download_url": "u1"},
        {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst", "browser_download_url": "u2"},
    ]}
    def fetch(url): return release if url == PBS_RELEASE_API.format(tag="20241016") else None
    result = resolve_pbs_inputs(fetch, "20241016", _target(), "3.11.10")
    assert result.runtime.name.endswith("install_only_stripped.tar.gz")


def test_no_runtime_asset_error_is_actionable():
    from py_upper.pbs_assets import no_asset_error

    release = {"assets": [
        {"name": "cpython-3.10.15+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"},
        {"name": "cpython-3.10.15+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
    ]}
    error = no_asset_error("20241016", _target(), "3.11.10", release, missing_runtime=True, missing_sdk=True)
    text = str(error)
    assert "Requested Python: 3.11.10" in text
    assert "Available runtime Python versions: 3.10.15" in text
