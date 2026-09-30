import sys
import stat
from pathlib import Path


def test_supported_build_python_version():
    assert sys.version_info >= (3, 8)


def test_toml_compatibility_layer():
    from py_upper.compat import tomllib

    assert callable(tomllib.loads)
    data = tomllib.loads('name = "py_upper"\n[tool.py_upper]\nentry = "main.py"\n')
    assert data["name"] == "py_upper"
    assert data["tool"]["py_upper"]["entry"] == "main.py"


def test_runtime_imports_user_agent():
    from py_upper import runtime
    assert callable(runtime.user_agent)


def test_cython_bootstrap_uses_project_cache_when_missing(monkeypatch, tmp_path):
    from py_upper import python_build

    calls = []
    versions = iter([None, "3.1.3"])

    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "_cython_version", lambda host, env=None: next(versions))
    monkeypatch.setattr(python_build, "run", lambda cmd, cwd=None, env=None: calls.append((cmd, cwd, env)))

    env = python_build._cython_env(Path(sys.executable))

    assert calls
    assert calls[0][0][0] == str(Path(sys.executable))
    assert calls[0][0][3] == "install"
    assert "Cython>=3.1,<3.2" in calls[0][0]
    assert "--upgrade" not in calls[0][0]
    assert env["PYTHONPATH"].startswith(str(tmp_path / "build" / "host-tools"))


def test_cython_bootstrap_reuses_existing_project_cache(monkeypatch, tmp_path):
    from py_upper import python_build

    cache = tmp_path / "build" / "host-tools" / "cython-python3.10-3.10.10"
    cache.mkdir(parents=True)
    calls = []
    versions = iter([None, "3.1.3"])

    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "_cython_cache_dir", lambda host: cache)
    monkeypatch.setattr(python_build, "_cython_version", lambda host, env=None: next(versions))
    monkeypatch.setattr(python_build, "run", lambda cmd, cwd=None, env=None: calls.append(cmd))

    env = python_build._cython_env(Path(sys.executable))

    assert not calls
    assert env["PYTHONPATH"].startswith(str(cache))


def test_selected_sources_compile_all_application_modules_except_package_markers():
    from py_upper import python_build

    sources = {python_build.module_name(p) for p in python_build.selected_sources()}
    assert sources == {"main", "core.app", "models.state", "services.hello", "utils.paths"}


def test_cythonize_keeps_generated_c_out_of_src(monkeypatch, tmp_path):
    from py_upper import python_build

    source = tmp_path / "src" / "core" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run_application():\n    return 1\n", encoding="utf-8")
    calls = []

    def fake_run(cmd, cwd=None, env=None):
        calls.append((cmd, cwd))
        assert cwd == source.parent
        assert "--force" in cmd
        assert cmd[-1] == "app.py"
        output = cwd / cmd[-2]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("/* generated */\n", encoding="utf-8")

    monkeypatch.setattr(python_build, "APP", tmp_path)
    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "_cython_env", lambda host: {"PYTHONPATH": "cached-cython"})
    monkeypatch.setattr(python_build, "run", fake_run)
    generated = python_build.cythonize_to_c([source], Path(sys.executable))

    assert calls
    assert source not in generated or generated[source].exists()
    assert generated[source].parent == tmp_path / "build" / "cython" / "core"
    assert generated[source].exists()
    assert not (source.parent / "app.c").exists()


def test_launcher_loads_libpython_with_global_symbols():
    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    text = launcher.read_text(encoding="utf-8")
    assert "RTLD_NOW | RTLD_GLOBAL" in text
    assert "RTLD_LOCAL" not in text


def test_macho_arm64_header_is_detected_with_correct_byte_order(tmp_path):
    from py_upper.native.inspect import inspect

    # Mach-O arm64 little-endian header: magic bytes CF FA ED FE,
    # cputype 0x0100000c encoded little-endian.
    binary = tmp_path / "libarm64.dylib"
    binary.write_bytes(bytes.fromhex("cffaedfe0c0000010000000000000000"))

    info = inspect(binary)
    assert info.format == "Mach-O"
    assert info.arch == "arm64"


def test_macho_universal_header_must_contain_target_arch(tmp_path):
    from py_upper.config import TARGETS
    from py_upper.native.inspect import verify_arch

    binary = tmp_path / "universal.dylib"
    # FAT_MAGIC, two slices: x86_64 then arm64.
    header = bytearray()
    header += bytes.fromhex("cafebabe")
    header += (2).to_bytes(4, "big")
    for cputype in (0x01000007, 0x0100000C):
        header += cputype.to_bytes(4, "big")
        header += (3).to_bytes(4, "big")  # CPU subtype
        header += (0).to_bytes(4, "big")  # offset
        header += (0).to_bytes(4, "big")  # size
        header += (0).to_bytes(4, "big")  # alignment
    binary.write_bytes(header)

    assert verify_arch(binary, TARGETS["macos-arm64"]).arch == "universal"

    x86_only = tmp_path / "x86-universal.dylib"
    x86_only.write_bytes(header[:4] + (1).to_bytes(4, "big") + header[8:28])
    try:
        verify_arch(x86_only, TARGETS["macos-arm64"])
    except RuntimeError as exc:
        assert "expected arm64" in str(exc)
    else:
        raise AssertionError("x86_64-only universal binary was accepted for arm64")


def test_remove_cython_sources_leaves_only_package_markers(tmp_path, monkeypatch):
    from py_upper import python_build

    src = tmp_path / "src"
    site = tmp_path / "site"
    (src / "core").mkdir(parents=True)
    (src / "utils").mkdir(parents=True)
    (src / "core" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (src / "core" / "__init__.py").write_text("", encoding="utf-8")
    (src / "utils" / "paths.py").write_text("x = 1\n", encoding="utf-8")
    (src / "utils" / "__init__.py").write_text("", encoding="utf-8")
    (site / "core").mkdir(parents=True)
    (site / "utils").mkdir(parents=True)
    (site / "core" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (site / "core" / "__init__.py").write_text("", encoding="utf-8")
    (site / "utils" / "paths.py").write_text("x = 1\n", encoding="utf-8")
    (site / "utils" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(python_build, "APP", tmp_path)
    monkeypatch.setattr(python_build, "selected_sources", lambda: [src / "core" / "app.py", src / "utils" / "paths.py"])

    python_build.remove_cython_source_py(site)

    assert not (site / "core" / "app.py").exists()
    assert not (site / "utils" / "paths.py").exists()
    assert (site / "core" / "__init__.py").exists()
    assert (site / "utils" / "__init__.py").exists()


def test_package_tree_copy_does_not_replay_source_modes_or_metadata(monkeypatch, tmp_path):
    from py_upper import fs
    from py_upper import package

    source = tmp_path / "runtime"
    destination = tmp_path / "bundle-runtime"
    (source / "lib" / "pkg").mkdir(parents=True)
    readonly = source / "lib" / "pkg" / "README.rst"
    readonly.write_text("read me\n", encoding="utf-8")
    readonly.chmod(0o444)

    calls = []
    real_chmod = fs.os.chmod

    def record_chmod(*args, **kwargs):
        calls.append(args[0])
        return real_chmod(*args, **kwargs)

    monkeypatch.setattr(fs.os, "chmod", record_chmod)
    package._copy_tree_contents(source, destination)

    copied = destination / "lib" / "pkg" / "README.rst"
    assert copied.read_text(encoding="utf-8") == "read me\n"
    assert copied not in calls
    assert stat.S_IMODE(copied.stat().st_mode) & 0o111 == 0


def test_package_tree_copy_preserves_symlinks_and_launcher_executable_mode(tmp_path):
    from py_upper import package

    source = tmp_path / "runtime"
    destination = tmp_path / "bundle-runtime"
    source.mkdir()
    executable = source / "python"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    link = source / "python3"
    link.symlink_to("python")

    package._copy_tree_contents(source, destination)

    assert (destination / "python").read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert (destination / "python3").is_symlink()
    assert (destination / "python3").readlink() == Path("python")


def test_make_executable_adds_only_execute_bits(tmp_path):
    from py_upper import package

    path = tmp_path / "launcher"
    path.write_bytes(b"x")
    path.chmod(0o644)

    package._make_executable(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_native_dependency_resolver_finds_nested_libraries(tmp_path):
    from py_upper.native.deps import resolve_dependency

    runtime = tmp_path / "runtime"
    nested = runtime / "lib" / "tcl9.0"
    nested.mkdir(parents=True)
    names = [
        "libtcl9thread3.0.6.dylib",
        "libthread3.0.6.dylib",
        "libitcl4.3.8.dylib",
        "libtcl9itcl4.3.8.dylib",
    ]
    for name in names:
        (nested / name).write_bytes(b"dylib")

    owner = tmp_path / "site-packages" / "_tkinter.cpython.so"
    owner.parent.mkdir(parents=True)
    owner.write_bytes(b"dylib")

    for name in names:
        assert resolve_dependency(name, [tmp_path / "site-packages", runtime], owner) == (nested / name).resolve()


def test_native_dependency_resolver_uses_macos_rpath(monkeypatch, tmp_path):
    from py_upper.native.deps import resolve_dependency

    runtime_lib = tmp_path / "runtime" / "lib"
    runtime_lib.mkdir(parents=True)
    lib = runtime_lib / "libitcl4.3.8.dylib"
    lib.write_bytes(b"dylib")
    owner = tmp_path / "runtime" / "lib" / "itcl4.3.8" / "libtcl9itcl4.3.8.dylib"
    owner.parent.mkdir(parents=True)
    owner.write_bytes(b"dylib")

    monkeypatch.setattr("py_upper.native.deps._mac_rpaths", lambda path: ["@loader_path/../"])
    assert resolve_dependency(
        "@rpath/libitcl4.3.8.dylib",
        [tmp_path / "runtime"],
        owner,
    ) == lib.resolve()


def test_source_tree_copies_native_libraries_and_symlinks(monkeypatch, tmp_path):
    from py_upper import python_build

    src = tmp_path / "src"
    site = tmp_path / "site"
    native = src / "native"
    native.mkdir(parents=True)
    dylib = native / "libcustom.dylib"
    dylib.write_bytes(b"macho-like")
    alias = native / "libcustom-current.dylib"
    alias.symlink_to("libcustom.dylib")
    (native / "helper.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(python_build, "APP", tmp_path)
    python_build.copy_python_tree(site)

    assert (site / "native" / "libcustom.dylib").read_bytes() == b"macho-like"
    assert (site / "native" / "libcustom-current.dylib").is_symlink()
    assert (site / "native" / "libcustom-current.dylib").readlink() == Path("libcustom.dylib")
    assert (site / "native" / "helper.txt").read_text(encoding="utf-8") == "keep"


def test_source_dylib_install_name_is_not_treated_as_dependency(monkeypatch, tmp_path):
    from py_upper.native import deps

    binary = tmp_path / "libcustom.dylib"
    binary.write_bytes(b"macho-like")
    monkeypatch.setattr(deps.shutil, "which", lambda name: "/usr/bin/otool" if name == "otool" else None)

    def fake_run(cmd, capture_output, text, check):
        if cmd[1] == "-D":
            return type("Result", (), {"returncode": 0, "stdout": f"{binary}:\n@rpath/libcustom.dylib\n", "stderr": ""})()
        return type("Result", (), {
            "returncode": 0,
            "stdout": f"{binary}:\n    @rpath/libcustom.dylib (compatibility version 1.0.0, current version 1.0.0)\n    @rpath/libhelper.dylib (compatibility version 1.0.0, current version 1.0.0)\n",
            "stderr": "",
        })()

    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    assert deps.dependency_names(binary) == ["@rpath/libhelper.dylib"]


def test_source_native_dependency_is_resolved_inside_app_tree(tmp_path):
    from py_upper.native.deps import resolve_dependency

    root = tmp_path / "site-packages"
    owner = root / "native" / "plugin" / "libplugin.dylib"
    dependency = root / "native" / "libs" / "libcustom.dylib"
    owner.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    owner.write_bytes(b"owner")
    dependency.write_bytes(b"dependency")

    assert resolve_dependency("@rpath/libcustom.dylib", [root], owner) == dependency.resolve()


def test_macos_bundle_rewrites_dylib_id_and_dependency(monkeypatch, tmp_path):
    from py_upper import config
    from py_upper.native import bundle

    root = tmp_path / "MyApp.app"
    binary = root / "Contents" / "Resources" / "site-packages" / "native" / "libcustom.dylib"
    helper = binary.parent / "libhelper.dylib"
    launcher = root / "Contents" / "MacOS" / "MyApp"
    binary.parent.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    for path in (binary, helper, launcher):
        path.write_bytes(b"native")

    target = config.TARGETS["macos-arm64"]
    commands = []
    monkeypatch.setattr(bundle, "verify_arch", lambda path, target: None)
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env=None: ["/old/libhelper.dylib"] if path == binary else [])
    monkeypatch.setattr(bundle, "_system_dependency", lambda name, target: False)
    monkeypatch.setattr(bundle, "resolve_dependency", lambda name, roots, source: helper if source == binary else None)
    monkeypatch.setattr(bundle.shutil, "which", lambda name: "/usr/bin/install_name_tool" if name == "install_name_tool" else None)
    monkeypatch.setattr(bundle, "_mac_install_name", lambda path: "/old/libcustom.dylib")
    monkeypatch.setattr(bundle.subprocess, "run", lambda cmd, check: commands.append(cmd))

    bundle.bundle_native_dependencies(root, target)

    assert ["install_name_tool", "-id", "@loader_path/libcustom.dylib", str(binary)] in commands
    assert ["install_name_tool", "-id", "@loader_path/libhelper.dylib", str(helper)] in commands
    assert [
        "install_name_tool", "-change", "/old/libhelper.dylib",
        "@loader_path/libhelper.dylib", str(binary),
    ] in commands


def test_verify_does_not_require_hardcoded_core_package(monkeypatch, tmp_path, capsys):
    from py_upper import verify as verify_module

    target = verify_module.Target("macos", "arm64", "aarch64-apple-darwin")
    runtime = tmp_path / "runtime"
    stage = tmp_path / "staging"
    site = stage / "site-packages"
    out = tmp_path / "dist" / "MyApp.app"
    runtime.mkdir(parents=True)
    (runtime / ".pystand-runtime.json").write_text("{}\n", encoding="utf-8")
    (site / "config").mkdir(parents=True)
    (site / "config" / "plugin.py").write_text("x = 1\n", encoding="utf-8")
    launcher = out / "Contents" / "MacOS" / "MyApp"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"native")
    (out / "Contents" / "Resources").mkdir(parents=True)
    (out / "Contents" / "Resources" / "MyApp.int").write_text("", encoding="utf-8")

    monkeypatch.setattr(verify_module, "target_runtime_dir", lambda t: runtime)
    monkeypatch.setattr(verify_module, "staging_dir", lambda t: stage)
    monkeypatch.setattr(verify_module, "runtime_spec", lambda t: object())
    monkeypatch.setattr(verify_module, "read_manifest", lambda path: {"format": 2})
    monkeypatch.setattr(verify_module, "validate_manifest", lambda manifest, t, spec: None)
    monkeypatch.setattr(verify_module, "selected_sources", lambda: [])
    monkeypatch.setattr(verify_module, "app_name", lambda: "MyApp")
    monkeypatch.setattr(verify_module, "DIST", tmp_path / "dist")
    monkeypatch.setattr(verify_module, "verify_arch", lambda path, t: type("Info", (), {"arch": t.arch})())

    rc = verify_module.verify(target)
    output = capsys.readouterr().out

    assert rc == 0
    assert "PASS application site-packages" in output
    assert "core" not in output.lower()



def test_pbs_sdk_error_reports_requested_and_available_versions():
    from py_upper.config import TARGETS
    from py_upper.pbs_sdk import select_full_asset

    release = {
        "assets": [
            {"name": "cpython-3.10.21+20260929-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
            {"name": "cpython-3.11.16+20260929-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
            {"name": "cpython-3.12.14+20260929-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
        ]
    }

    try:
        select_full_asset(
            release, TARGETS["macos-arm64"], "3.11.11", "20260929",
            lambda pyver, target, kind: "20250317",
        )
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("missing PBS SDK asset was accepted")

    assert "Requested Python: 3.11.11" in message
    assert "PBS release: 20260929" in message
    assert "Target: aarch64-apple-darwin" in message
    assert "Available Python versions for this target in this release: 3.10.21, 3.11.16, 3.12.14" in message
    assert "Suggested exact-match PBS release: 20250317" in message
    assert "will not silently substitute another version" in message or "auto-select a release containing the exact Python version" in message


def test_pbs_runtime_error_reports_requested_and_available_versions():
    from py_upper.config import TARGETS
    from py_upper.runtime import select_asset

    release = {
        "assets": [
            {"name": "cpython-3.10.21+20260929-aarch64-apple-darwin-install_only_stripped.tar.gz"},
            {"name": "cpython-3.11.16+20260929-aarch64-apple-darwin-install_only_stripped.tar.gz"},
        ]
    }

    try:
        select_asset(release, TARGETS["macos-arm64"], "3.11.11", "20260929")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("missing PBS runtime asset was accepted")

    assert "No PBS install_only_stripped runtime asset" in message
    assert "Requested Python: 3.11.11" in message
    assert "PBS release: 20260929" in message
    assert "Available Python versions for this target in this release: 3.10.21, 3.11.16" in message


def test_runtimes_directory_is_gitignored():
    ignore = Path(__file__).parents[2] / ".gitignore"
    assert "runtimes/" in ignore.read_text(encoding="utf-8").splitlines()


def test_native_binaries_under_app_src_are_not_globally_gitignored():
    ignore_lines = (Path(__file__).parents[2] / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.dylib" not in ignore_lines
    assert "*.so" not in ignore_lines
    assert "*.pyd" not in ignore_lines



def test_find_matching_pbs_release_from_candidates():
    from py_upper.config import TARGETS
    from py_upper.pbs_assets import find_matching_release_from_candidates

    releases = [
        {
            "tag_name": "20260929",
            "assets": [
                {"name": "cpython-3.11.16+20260929-aarch64-apple-darwin-pgo+lto-full.tar.zst"}
            ],
        },
        {
            "tag_name": "20250317",
            "assets": [
                {"name": "cpython-3.11.11+20250317-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
                {"name": "cpython-3.11.11+20250317-aarch64-apple-darwin-install_only_stripped.tar.gz"},
            ],
        },
    ]

    assert find_matching_release_from_candidates(
        releases, "20260929", TARGETS["macos-arm64"], "3.11.11", kind="sdk"
    ) == "20250317"
    assert find_matching_release_from_candidates(
        releases, "20260929", TARGETS["macos-arm64"], "3.11.11", kind="runtime"
    ) == "20250317"



def test_find_matching_pbs_release_paginates_and_ignores_current_release():
    from py_upper.config import TARGETS
    from py_upper.pbs_assets import find_matching_release

    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("page=1"):
            page = [
                {
                    "tag_name": "20260929",
                    "assets": [{"name": "cpython-3.11.11+20260929-aarch64-apple-darwin-pgo+lto-full.tar.zst"}],
                }
            ]
            page.extend({"tag_name": f"filler-{i}", "assets": []} for i in range(99))
            return page
        return [{
            "tag_name": "20250317",
            "assets": [{"name": "cpython-3.11.11+20250317-aarch64-apple-darwin-pgo+lto-full.tar.zst"}],
        }]

    assert find_matching_release(
        fetch, "20260929", TARGETS["macos-arm64"], "3.11.11", kind="sdk", max_pages=2
    ) == "20250317"
    assert len(calls) == 2



def test_runtime_selector_accepts_exact_pbs_asset_name():
    from py_upper.config import TARGETS
    from py_upper.runtime import select_asset

    asset = select_asset(
        {
            "assets": [
                {
                    "name": "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz",
                    "browser_download_url": "https://example.invalid/runtime",
                }
            ]
        },
        TARGETS["macos-arm64"],
        "3.11.10",
        "20241016",
    )
    assert asset["name"].startswith("cpython-3.11.10+")


def test_auto_pbs_release_requires_both_runtime_and_sdk():
    from py_upper.config import TARGETS
    from py_upper.pbs_assets import resolve_pbs_release

    page = [
        {
            "tag_name": "newer-runtime-only",
            "assets": [
                {"name": "cpython-3.11.10+newer-aarch64-apple-darwin-install_only_stripped.tar.gz"},
            ],
        },
        {
            "tag_name": "20241016",
            "assets": [
                {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"},
                {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
            ],
        },
    ]

    calls = []
    def fetch(url):
        calls.append(url)
        return page

    tag, release = resolve_pbs_release(fetch, "", TARGETS["macos-arm64"], "3.11.10", max_pages=1)
    assert tag == "20241016"
    assert release["tag_name"] == "20241016"
    assert len(calls) == 2
    assert calls[0].startswith("https://raw.githubusercontent.com/astral-sh/uv/main/")


def test_explicit_pbs_release_does_not_auto_search():
    from py_upper.config import TARGETS
    from py_upper.pbs_assets import resolve_pbs_release

    calls = []
    def fetch(url):
        calls.append(url)
        return {"tag_name": "20241016", "assets": []}

    tag, release = resolve_pbs_release(fetch, "20241016", TARGETS["macos-arm64"], "3.11.10")
    assert tag == "20241016"
    assert release["tag_name"] == "20241016"
    assert len(calls) == 1



def test_pbs_release_is_optional(monkeypatch):
    from py_upper import config

    monkeypatch.setattr(config, "py_upper_config", lambda: {"runtime": {"provider": "pbs"}})
    assert config.pbs_release() == ""

    monkeypatch.setattr(
        config, "py_upper_config", lambda: {"pbs": {"release": "20241016"}, "runtime": {"provider": "pbs"}}
    )
    assert config.pbs_release() == "20241016"


def test_pbs_runtime_selector_and_available_versions_use_same_match_rule():
    from py_upper.config import TARGETS
    from py_upper.pbs_assets import available_python_versions, matching_assets
    from py_upper.runtime import select_asset

    release = {
        "assets": [
            {
                "name": "cpython-3.11.10-20241016-aarch64-apple-darwin-install_only_stripped.tar.gz",
                "browser_download_url": "https://example.invalid/runtime",
            },
            {
                "name": "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz",
                "browser_download_url": "https://example.invalid/runtime-plus",
            },
        ]
    }
    target = TARGETS["macos-arm64"]
    assert available_python_versions(release, target, kind="runtime") == ["3.11.10"]
    assert len(matching_assets(release, target, "3.11.10", kind="runtime")) == 2
    selected = select_asset(release, target, " 3.11.10 ", "20241016")
    assert selected in matching_assets(release, target, "3.11.10", kind="runtime")


def test_pbs_sdk_selector_and_available_versions_use_same_match_rule():
    from py_upper.config import TARGETS
    from py_upper.pbs_assets import available_python_versions, matching_assets
    from py_upper.pbs_sdk import select_full_asset

    release = {
        "assets": [
            {
                "name": "cpython-3.11.10-20241016-aarch64-apple-darwin-full.tar.zst",
                "browser_download_url": "https://example.invalid/sdk",
            },
            {
                "name": "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst",
                "browser_download_url": "https://example.invalid/sdk-pgo",
            },
        ]
    }
    target = TARGETS["macos-arm64"]
    assert available_python_versions(release, target, kind="sdk") == ["3.11.10"]
    assert len(matching_assets(release, target, "3.11.10", kind="sdk")) == 2
    selected = select_full_asset(release, target, "3.11.10", "20241016")
    assert selected in matching_assets(release, target, "3.11.10", kind="sdk")


def test_auto_pbs_release_uses_exact_runtime_metadata_index_before_release_listing():
    from py_upper.config import TARGETS
    from py_upper.pbs_assets import PBS_RUNTIME_METADATA_URL, resolve_pbs_release

    calls = []
    def fetch(url):
        calls.append(url)
        if url == PBS_RUNTIME_METADATA_URL:
            return {
                "cpython-3.11.10-darwin-aarch64-none": {
                    "url": "https://github.com/astral-sh/python-build-standalone/releases/download/20241016/"
                          "cpython-3.11.10%2B20241016-aarch64-apple-darwin-install_only_stripped.tar.gz",
                    "build": "20241016",
                }
            }
        assert url.endswith("/releases/tags/20241016")
        return {
            "tag_name": "20241016",
            "assets": [
                {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-install_only_stripped.tar.gz"},
                {"name": "cpython-3.11.10+20241016-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
            ],
        }

    tag, release = resolve_pbs_release(fetch, "", TARGETS["macos-arm64"], "3.11.10")
    assert tag == "20241016"
    assert release["tag_name"] == "20241016"
    assert calls == [
        PBS_RUNTIME_METADATA_URL,
        "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20241016",
    ]


def test_auto_pbs_release_does_not_use_large_release_list_when_metadata_matches():
    from py_upper.config import TARGETS
    from py_upper.pbs_assets import PBS_RUNTIME_METADATA_URL, PBS_RELEASES_API, resolve_pbs_release

    def fetch(url):
        if url == PBS_RUNTIME_METADATA_URL:
            return {
                "cpython-3.13.15-darwin-aarch64-none": {"build": "20260929"}
            }
        if url.endswith("/releases/tags/20260929"):
            return {
                "tag_name": "20260929",
                "assets": [
                    {"name": "cpython-3.13.15+20260929-aarch64-apple-darwin-install_only_stripped.tar.gz"},
                    {"name": "cpython-3.13.15+20260929-aarch64-apple-darwin-pgo+lto-full.tar.zst"},
                ],
            }
        raise AssertionError(f"Unexpected paginated PBS request: {url}")

    tag, _ = resolve_pbs_release(fetch, "", TARGETS["macos-arm64"], "3.13.15")
    assert tag == "20260929"


def test_http_json_retries_gateway_timeout(monkeypatch):
    import urllib.error
    from py_upper import net

    calls = []
    responses = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, *args):
            return b'{"ok": true}'
    def fake_urlopen(request, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 504, "Gateway Timeout", {}, None)
        responses.append(True)
        return Response()

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(net.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("PY_UPPER_HTTP_RETRIES", "2")
    result = net.http_json("https://example.invalid/metadata")
    assert result == {"ok": True}
    assert len(calls) == 2
    assert responses == [True]
