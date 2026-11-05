from __future__ import annotations

import io
import json
import urllib.error
import zipfile

import pytest


DIGEST = "a" * 64


def _checksums(*names):
    return "".join(f"{DIGEST}  {name}\n" for name in names)


def _target():
    from py_upper.config import TARGETS
    return TARGETS["macos-arm64"]


@pytest.mark.parametrize("token", [None, "unused-stale-token"])
@pytest.mark.parametrize("target_name", [
    "windows-x86", "windows-x86_64", "windows-arm64", "linux-x86_64", "linux-arm64",
    "macos-x86_64", "macos-arm64",
])
def test_cold_resolution_does_not_need_github_api_or_login(tmp_path, monkeypatch, token, target_name):
    from py_upper import net, pbs
    from py_upper.config import TARGETS

    target = TARGETS[target_name]
    tag = "20250723"
    prefix = f"https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/"
    runtime = f"cpython-3.13.5+{tag}-{target.triple}-install_only_stripped.tar.gz"
    sdk = f"cpython-3.13.5+{tag}-{target.triple}-pgo-full.tar.zst"
    digest = "a" * 64
    metadata = {pbs.metadata_key(target, "3.13.5"): {"url": prefix + runtime, "sha256": "b" * 64}}
    monkeypatch.setattr(pbs, "_METADATA_CACHE", tmp_path / "metadata.json")
    monkeypatch.setattr(pbs, "python_version", lambda: "3.13.5")
    monkeypatch.setattr(pbs, "pbs_release", lambda: "")
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        if token is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, token)
    requests = []

    def public_downloads_only(request, timeout):
        url = request.full_url
        requests.append(url)
        if url.startswith("https://api.github.com/"):
            raise urllib.error.HTTPError(url, 403, "API rate limit exceeded", {}, None)
        assert "Authorization" not in request.headers
        if url == pbs.PBS_RUNTIME_METADATA_URLS[0]:
            return io.BytesIO(json.dumps(metadata).encode())
        if url == prefix + "SHA256SUMS":
            return io.BytesIO(f"{digest}  {sdk}\n".encode())
        raise AssertionError(url)

    monkeypatch.setattr(net.urllib.request, "urlopen", public_downloads_only)
    result = pbs.resolve_inputs(target)

    assert result.sdk.name == sdk
    assert result.sdk.sha256 == digest
    assert requests == [pbs.PBS_RUNTIME_METADATA_URLS[0], prefix + "SHA256SUMS"]


def test_release_checksums_preserve_hashes_and_encode_download_names():
    from py_upper.pbs import release_assets

    name = "cpython-3.13.5+20250723-aarch64-apple-darwin-pgo+lto-full.tar.zst"
    # Both text and binary sha256sum markers, CRLF, and uppercase hex are valid.
    assets = release_assets(f"{DIGEST.upper()} *{name}\r\n{DIGEST}  extra.txt\r\n", "20250723")

    assert assets[0].name == name
    assert assets[0].sha256 == DIGEST
    assert assets[0].url.endswith("cpython-3.13.5%2B20250723-aarch64-apple-darwin-pgo%2Blto-full.tar.zst")
    assert assets[1].name == "extra.txt"


@pytest.mark.parametrize("checksums", [
    "", "\n", "<html>login required</html>", "a" * 63 + "  asset.tar.zst",
    f"{DIGEST}  ../escape", f"{DIGEST}  C:\\escape", _checksums("duplicate", "duplicate"),
])
def test_release_checksums_reject_corruption(checksums):
    from py_upper.pbs import release_assets

    with pytest.raises(RuntimeError, match="PBS SHA256SUMS.*20250723"):
        release_assets(checksums, "20250723")


def test_release_download_failure_is_visible_without_api_fallback():
    from py_upper.pbs import resolve_pbs_inputs

    def fail_download(url):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    with pytest.raises(urllib.error.HTTPError) as error:
        resolve_pbs_inputs(
            lambda _: pytest.fail("must not fall back to GitHub API"),
            "20250723", _target(), "3.13.5", fetch_text=fail_download,
        )
    assert error.value.code == 403
    assert error.value.url.endswith("/20250723/SHA256SUMS")


def test_explicit_release_never_substitutes_a_different_version():
    from py_upper.pbs import resolve_pbs_inputs

    with pytest.raises(RuntimeError, match="Requested Python: 3.13.5") as error:
        resolve_pbs_inputs(
            lambda _: pytest.fail("explicit releases must not use metadata"),
            "20241016", _target(), "3.13.5",
            fetch_text=lambda _: _checksums(
                "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz",
                "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst",
            ),
        )
    assert "Available SDK Python versions: 3.11.10" in str(error.value)


def test_sdk_download_verifies_release_checksum_before_extraction(tmp_path, monkeypatch):
    from py_upper import pbs

    sdk_name = "cpython-3.13.5+20250723-x86_64-pc-windows-msvc-pgo-full.tar.zst"
    sdk = pbs.release_assets(_checksums(sdk_name), "20250723")[0]
    monkeypatch.setattr(pbs, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(pbs, "pbs_sdk_dir", lambda _: tmp_path / "sdk")
    monkeypatch.setattr(pbs, "resolve_inputs", lambda _: pbs.PBSInputs("20250723", sdk, sdk, "release"))

    def corrupted_download(url, destination):
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"corrupted archive")

    monkeypatch.setattr(pbs, "download", corrupted_download)
    monkeypatch.setattr(pbs, "_safe_extract_zst", lambda *_: pytest.fail("must validate before extraction"))
    with pytest.raises(RuntimeError, match="PBS SDK SHA256 mismatch"):
        pbs.ensure_pbs_sdk(_windows_target())
    assert not (tmp_path / "sdk").exists()


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
    from py_upper.pbs import PBS_RELEASE_URL, PBS_RUNTIME_METADATA_URLS, metadata_key, resolve_pbs_inputs

    target = _target()
    runtime_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10%2B20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"
    sdk_name = "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst"
    metadata = {metadata_key(target, "3.11.10"): {"url": runtime_url, "sha256": "runtime"}}
    checksums_url = PBS_RELEASE_URL.format(tag="20241016") + "/SHA256SUMS"
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
        if url == checksums_url:
            return _checksums(sdk_name)
        raise AssertionError(url)
    result = resolve_pbs_inputs(fetch, "", target, "3.11.10", fetch_text=fetch)
    assert result.tag == "20241016"
    assert result.sdk.name == sdk_name
    assert result.sdk.sha256 == DIGEST
    assert calls == [PBS_RUNTIME_METADATA_URLS[0], checksums_url]


def test_pbs_metadata_index_falls_back_when_uv_moves_the_file(tmp_path, monkeypatch):
    """uv reorganised its tree once (crates/uv-python -> crates/uv-python-managed)
    and the old raw URL started returning 404, which took down every build with a
    cold cache. The next candidate must be used instead of failing the build."""
    from py_upper import pbs
    from py_upper.pbs import PBS_RELEASE_URL, PBS_RUNTIME_METADATA_URLS, metadata_key, resolve_pbs_inputs

    target = _target()
    runtime_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10%2B20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"
    sdk_name = "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst"
    metadata = {metadata_key(target, "3.11.10"): {"url": runtime_url, "sha256": "runtime"}}
    checksums_url = PBS_RELEASE_URL.format(tag="20241016") + "/SHA256SUMS"

    monkeypatch.setattr(pbs, "_METADATA_CACHE", tmp_path / "uv-download-metadata.json")
    moved, calls = PBS_RUNTIME_METADATA_URLS[0], []

    def fetch(url):
        calls.append(url)
        if url == moved:
            raise OSError("HTTP Error 404: Not Found")
        if url in PBS_RUNTIME_METADATA_URLS:
            return metadata
        if url == checksums_url:
            return _checksums(sdk_name)
        raise AssertionError(url)

    result = resolve_pbs_inputs(fetch, "", target, "3.11.10", fetch_text=fetch)
    assert result.tag == "20241016"
    assert result.sdk.name == sdk_name
    assert calls == [moved, PBS_RUNTIME_METADATA_URLS[1], checksums_url]


def test_pbs_metadata_index_error_names_every_candidate(tmp_path, monkeypatch):
    from py_upper import pbs
    from py_upper.pbs import PBS_RUNTIME_METADATA_URLS, resolve_pbs_inputs

    monkeypatch.setattr(pbs, "_METADATA_CACHE", tmp_path / "uv-download-metadata.json")

    def fetch(url):
        raise OSError("HTTP Error 404: Not Found")

    try:
        resolve_pbs_inputs(fetch, "", _target(), "3.11.10", fetch_text=fetch)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("resolve_pbs_inputs should fail when no index is reachable")
    for url in PBS_RUNTIME_METADATA_URLS:
        assert url in message


def test_pbs_explicit_release_uses_only_public_checksums():
    from py_upper.pbs import resolve_pbs_inputs, PBS_RELEASE_URL

    def fetch(url):
        assert url == PBS_RELEASE_URL.format(tag="20241016") + "/SHA256SUMS"
        return _checksums(
            "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz",
            "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst",
        )
    result = resolve_pbs_inputs(
        lambda _: pytest.fail("explicit release must not fetch metadata/API"),
        "20241016", _target(), "3.11.10", fetch_text=fetch,
    )
    assert result.runtime.name.endswith("install_only_stripped.tar.gz")
    assert result.runtime.sha256 == DIGEST
    assert result.sdk.sha256 == DIGEST


def test_no_runtime_asset_error_is_actionable():
    from py_upper.pbs import no_asset_error, release_assets

    release = release_assets(_checksums(
        "cpython-3.10.15+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz",
        "cpython-3.10.15+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst",
    ), "20241016")
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
    from py_upper.pbs import _best_sdk, release_assets

    assets = release_assets(_checksums(
        "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-shared-pgo-full.tar.zst",
        "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-static-noopt-full.tar.zst",
    ), "20230507")
    shared_runtime = "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-shared-install_only.tar.gz"
    static_runtime = "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-static-install_only.tar.gz"

    assert _best_sdk(assets, shared_runtime).name.endswith("-shared-pgo-full.tar.zst")
    assert _best_sdk(assets, static_runtime).name.endswith("-static-noopt-full.tar.zst")

    try:
        _best_sdk([assets[1]], shared_runtime)
    except RuntimeError as exc:
        assert "shared" in str(exc)
    else:
        raise AssertionError("a flavoured runtime without a matching SDK must fail loudly")


def test_best_sdk_keeps_pgo_lto_when_the_runtime_records_no_flavour():
    from py_upper.pbs import _best_sdk, release_assets

    assets = release_assets(_checksums(
        "cpython-3.11.13+20251007-aarch64-apple-darwin-full.tar.zst",
        "cpython-3.11.13+20251007-aarch64-apple-darwin-pgo+lto-full.tar.zst",
    ), "20251007")
    runtime = "cpython-3.11.13+20251007-aarch64-apple-darwin-install_only_stripped.tar.gz"
    assert _best_sdk(assets, runtime).name.endswith("-pgo+lto-full.tar.zst")


def test_windows_metadata_resolution_pairs_a_shared_sdk_with_a_shared_runtime(tmp_path, monkeypatch):
    from py_upper import pbs
    from py_upper.pbs import PBS_RELEASE_URL, metadata_key, resolve_pbs_inputs

    target = _windows_target()
    runtime_name = "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-shared-install_only.tar.gz"
    runtime_url = "https://github.com/astral-sh/python-build-standalone/releases/download/20230507/" + runtime_name
    metadata = {metadata_key(target, "3.10.11"): {"url": runtime_url, "sha256": "runtime"}}

    monkeypatch.setattr(pbs, "_METADATA_CACHE", tmp_path / "uv-download-metadata.json")

    def fetch(url):
        if url in pbs.PBS_RUNTIME_METADATA_URLS:
            return metadata
        if url == PBS_RELEASE_URL.format(tag="20230507") + "/SHA256SUMS":
            return _checksums(
                "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-shared-pgo-full.tar.zst",
                "cpython-3.10.11+20230507-x86_64-pc-windows-msvc-static-noopt-full.tar.zst",
            )
        raise AssertionError(url)

    result = resolve_pbs_inputs(fetch, "", target, "3.10.11", fetch_text=fetch)
    assert result.runtime.name == runtime_name
    assert result.sdk.name.endswith("-shared-pgo-full.tar.zst")


def test_windows_embeddable_asset_uses_official_architecture_names():
    from py_upper.pbs import windows_embed_asset
    from py_upper.config import TARGETS

    for arch, suffix in (("x86", "win32"), ("x86_64", "amd64"), ("arm64", "arm64")):
        asset = windows_embed_asset(TARGETS[f"windows-{arch}"], "3.13.5")
        assert asset.name == f"python-3.13.5-embed-{suffix}.zip"
        assert asset.url == f"https://www.python.org/ftp/python/3.13.5/{asset.name}"


def test_windows_embed_archive_is_checked_before_install(tmp_path, monkeypatch):
    from py_upper import runtime
    from py_upper.config import TARGETS

    target = TARGETS["windows-x86_64"]
    out = tmp_path / "runtime"
    cache = tmp_path / "cache"
    monkeypatch.setattr(runtime, "target_runtime_dir", lambda _: out)
    monkeypatch.setattr(runtime, "CACHE", cache)
    monkeypatch.setattr(runtime, "python_version", lambda: "3.13.5")
    def download_fixture(url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("LICENSE.txt", b"license")
            archive.writestr("python313.dll", b"dll")
            archive.writestr("python3.dll", b"stable abi")
            archive.writestr("python313.zip", b"stdlib")
            archive.writestr("python313._pth", "python313.zip\n.\n")
            archive.writestr("_socket.pyd", b"extension")
            archive.writestr("python.exe", b"python")
    monkeypatch.setattr(runtime, "download", download_fixture)
    result = runtime.ensure_windows_embed_runtime(target)
    assert result == out
    assert (out / "_socket.pyd").is_file()
    assert (out / "python313.zip").is_file()
    assert (out / "python313._pth").read_text() == "python313.zip\n.\n"
    assert not (out / "Lib").exists()
    marker = json.loads((out / runtime.RUNTIME_MARKER).read_text())
    assert marker["source"] == "python.org"
    assert marker["sha256"] == runtime.sha256(cache / "python.org" / "3.13.5" / target.key / marker["asset"])


def test_windows_embed_archive_rejects_path_traversal_without_writing(tmp_path, monkeypatch):
    from py_upper import runtime
    from py_upper.config import TARGETS

    out = tmp_path / "runtime"
    monkeypatch.setattr(runtime, "target_runtime_dir", lambda _: out)
    monkeypatch.setattr(runtime, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(runtime, "python_version", lambda: "3.13.5")
    def download_fixture(url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("../escape", b"bad")
    monkeypatch.setattr(runtime, "download", download_fixture)
    with pytest.raises(RuntimeError, match="Unsafe embed archive path"):
        runtime.ensure_windows_embed_runtime(TARGETS["windows-x86_64"])
    assert not out.exists()
    assert not (tmp_path / "escape").exists()


def test_windows_embed_version_without_official_zip_fails_with_the_version(tmp_path, monkeypatch):
    import urllib.error

    from py_upper import runtime
    from py_upper.config import TARGETS

    target = TARGETS["windows-arm64"]
    monkeypatch.setattr(runtime, "target_runtime_dir", lambda _: tmp_path / "runtime")
    monkeypatch.setattr(runtime, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(runtime, "python_version", lambda: "3.11.13")

    def download_missing(url, destination):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(runtime, "download", download_missing)
    with pytest.raises(RuntimeError, match="3.11.13.*no official embeddable ZIP.*windows-arm64"):
        runtime.ensure_windows_embed_runtime(target)
