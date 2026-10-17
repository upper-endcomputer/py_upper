from __future__ import annotations

from pathlib import Path

import pytest

from py_upper.trim import TrimPolicy, trim_tree


def _tree(root: Path) -> Path:
    files = {
        "pkg/__init__.py": b"",
        "pkg/module.pyi": b"stub",
        "pkg/nested/deep.pyi": b"stub",
        "pkg/nested/keep.py": b"keep",
        "pkg/examples/demo.py": b"demo",
        "pkg/examples/keep.txt": b"keep",
        "pkg/include/header.h": b"header",
        "pkg/data.txt": b"data",
    }
    for name, payload in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


def test_single_star_does_not_cross_directories(tmp_path):
    root = _tree(tmp_path / "site-packages")

    trim_tree(root, ["pkg/*.pyi"])

    assert not (root / "pkg" / "module.pyi").exists()
    # The pattern names exactly one directory level, so the nested stub stays.
    assert (root / "pkg" / "nested" / "deep.pyi").exists()


def test_double_star_matches_at_any_depth(tmp_path):
    root = _tree(tmp_path / "site-packages")

    trim_tree(root, ["pkg/**/*.pyi"])

    assert not (root / "pkg" / "module.pyi").exists()
    assert not (root / "pkg" / "nested" / "deep.pyi").exists()
    assert (root / "pkg" / "nested" / "keep.py").exists()


def test_a_matched_directory_removes_its_whole_subtree(tmp_path):
    root = _tree(tmp_path / "site-packages")

    report = trim_tree(root, ["pkg/examples"])

    assert not (root / "pkg" / "examples").exists()
    assert (root / "pkg" / "include" / "header.h").exists()
    assert report.removed_files == 2
    assert report.removed_dirs == 1
    assert report.removed_bytes == len(b"demo") + len(b"keep")
    assert report.as_dict()["removed_files"] == 2


def test_keep_wins_over_a_parent_removal(tmp_path):
    """A kept file must survive even when its parent directory is named."""
    root = _tree(tmp_path / "site-packages")

    trim_tree(root, ["pkg"], keep=["pkg/nested/keep.py"])

    assert (root / "pkg" / "nested" / "keep.py").exists()
    assert (root / "pkg" / "nested").is_dir()
    assert not (root / "pkg" / "__init__.py").exists()
    assert not (root / "pkg" / "module.pyi").exists()
    assert not (root / "pkg" / "examples").exists()


def test_a_kept_directory_protects_everything_below_it(tmp_path):
    root = _tree(tmp_path / "site-packages")

    trim_tree(root, ["pkg"], keep=["pkg/examples"])

    assert (root / "pkg" / "examples" / "demo.py").exists()
    assert (root / "pkg" / "examples" / "keep.txt").exists()
    assert not (root / "pkg" / "include").exists()


@pytest.mark.parametrize("pattern", [
    "/absolute/path",
    "C:/windows/path",
    "..",
    "../outside",
    "pkg/../outside",
    "pkg//double",
    "  ",
])
def test_patterns_must_stay_relative_to_the_trim_root(pattern):
    with pytest.raises(RuntimeError, match="trim"):
        TrimPolicy.from_config({"site_packages": [pattern]})


def test_policy_accepts_one_pattern_and_merges_command_line_entries():
    policy = TrimPolicy.from_config({
        "site_packages": "**/*.pyi",
        "runtime": ["**/tests"],
        "runtime_keep": ["**/tests/keep.py"],
    })

    assert policy.site_packages == ("**/*.pyi",)
    assert policy.has_site_rules and policy.has_runtime_rules

    merged = policy.merge(site_packages=["PySide2/resources"], runtime_keep=["**/keep.py"])
    assert merged.site_packages == ("**/*.pyi", "PySide2/resources")
    assert merged.runtime_keep == ("**/tests/keep.py", "**/keep.py")
    # The configured policy is immutable, so a CLI override cannot leak into it.
    assert policy.site_packages == ("**/*.pyi",)

    # A single string is one pattern, not a sequence of characters.
    single = policy.merge(runtime="**/idlelib")
    assert single.runtime == ("**/tests", "**/idlelib")


def test_missing_trim_section_is_an_empty_policy():
    policy = TrimPolicy.from_config({})
    assert not policy.has_site_rules
    assert not policy.has_runtime_rules


def test_runtime_optimizer_applies_the_runtime_patterns(tmp_path, monkeypatch):
    from py_upper import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "optimize_config", lambda: {})
    root = tmp_path / "runtime"
    for name in (
        "lib/python3.8/tkinter/__init__.py",
        "lib/python3.8/tkinter/tcl/init.tcl",
        "lib/python3.8/lib-dynload/_tkinter.so",
        "lib/python3.8/site-packages/pkg/tests/test_pkg.py",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload")

    result = runtime_module.optimize_runtime_tree(
        root,
        TrimPolicy(runtime=["**/tkinter", "**/tests"]),
    )

    assert not (root / "lib" / "python3.8" / "tkinter").exists()
    assert not (root / "lib" / "python3.8" / "site-packages" / "pkg" / "tests").exists()
    # Only the named trees go: the tkinter extension is a separate path.
    assert (root / "lib" / "python3.8" / "lib-dynload" / "_tkinter.so").exists()
    assert result["removed_files"] == 3
    assert result["removed_dirs"] == 3


def test_site_optimizer_applies_the_site_patterns(tmp_path, monkeypatch):
    from py_upper import third_party

    monkeypatch.setattr(third_party, "optimize_config", lambda: {"remove_python_caches": False})
    site = tmp_path / "site-packages"
    (site / "PySide2" / "examples").mkdir(parents=True)
    (site / "PySide2" / "examples" / "demo.py").write_text("", encoding="utf-8")
    (site / "PySide2" / "__init__.py").write_text("", encoding="utf-8")

    result = third_party.optimize_site(
        site,
        trim=TrimPolicy(site_packages=["PySide2/examples"]),
    )

    assert not (site / "PySide2" / "examples").exists()
    assert (site / "PySide2" / "__init__.py").exists()
    assert result["removed_files"] == 1
    assert result["removed_dirs"] == 1


def test_release_preset_only_adds_distribution_metadata():
    policy = TrimPolicy.from_config({"preset": "release"})

    assert policy.site_packages == ("**/*.dist-info", "**/*.egg-info")
    assert not policy.runtime


def test_minimal_preset_adds_the_optional_payload_rules():
    policy = TrimPolicy.from_config({"preset": "minimal"})

    assert "PySide*/**/opengl32sw.dll" in policy.site_packages
    assert "PyQt*/**/opengl32sw.dll" in policy.site_packages
    assert "PySide*/**/plugins/platforms/*qdirect2d*" in policy.site_packages
    assert "PyQt*/**/plugins/platforms/*qminimal*" in policy.site_packages
    assert "PySide*/**/translations/*.qm" in policy.site_packages
    assert "PyQt*/**/translations/*.qm" in policy.site_packages
    assert "PySide*/**/plugins/imageformats/*qwebp*" in policy.site_packages
    assert "PyQt*/**/plugins/imageformats/*qtiff*" in policy.site_packages
    assert "**/*.dist-info" in policy.site_packages
    assert "**/*.egg-info" in policy.site_packages
    assert "**/tkinter" in policy.runtime
    assert policy.has_site_rules and policy.has_runtime_rules


def test_minimal_preset_matches_every_supported_qt_layout(tmp_path):
    """The same preset has to prune flat PySide and nested PySide/PyQt wheels.

    The 0.17.14 patterns started with a single ``*``: they matched
    ``PySide2/plugins/...`` on Windows but not ``PySide6/Qt/plugins/...`` or
    ``PyQt6/Qt6/plugins/...``, so the preset was a no-op on macOS, Linux and
    PyQt on every platform.
    """
    policy = TrimPolicy.from_config({"preset": "minimal"})
    root = tmp_path / "site-packages"
    removed = {
        # PySide2 on Windows: payload beside the package.
        "PySide2/opengl32sw.dll",
        "PySide2/plugins/platforms/qdirect2d.dll",
        "PySide2/plugins/platforms/qminimal.dll",
        "PySide2/translations/qtbase_zh_CN.qm",
        "PySide2/plugins/imageformats/qwebp.dll",
        "PySide2/plugins/imageformats/qtiff.dll",
        "PySide2/examples/demo.py",
        "PySide2/include/QtCore/qobject.h",
        # PySide6 on macOS/Linux: everything under Qt/.
        "PySide6/Qt/plugins/platforms/libqminimal.dylib",
        "PySide6/Qt/translations/qtbase_zh_CN.qm",
        "PySide6/Qt/translations/qt_en.qm",
        "PySide6/Qt/plugins/imageformats/libqwebp.dylib",
        "PySide6/Qt/plugins/imageformats/libqtiff.dylib",
        "PySide6/Qt/examples/demo.py",
        "PySide6/Qt/include/QtCore/qobject.h",
        # PyQt6: everything under Qt6/.
        "PyQt6/Qt6/plugins/platforms/qminimal.dll",
        "PyQt6/Qt6/translations/qtbase_zh_CN.qm",
        "PyQt6/Qt6/plugins/imageformats/qwebp.dll",
        "PyQt6/Qt6/plugins/imageformats/qtiff.dll",
        "PyQt6/Qt6/examples/demo.py",
        "PyQt6/Qt6/include/QtCore/qobject.h",
        # PyQt5: everything under Qt5/.
        "PyQt5/Qt5/plugins/platforms/libqminimal.so",
        "PyQt5/Qt5/translations/qtbase_zh_CN.qm",
        "PyQt5/Qt5/plugins/imageformats/libqwebp.so",
    }
    kept = {
        # The desktop platform plugin is what makes the window appear.
        "PySide2/plugins/platforms/qwindows.dll",
        "PySide6/Qt/plugins/platforms/libqcocoa.dylib",
        "PyQt6/Qt6/plugins/platforms/qwindows.dll",
        "PyQt5/Qt5/plugins/platforms/libqxcb.so",
        # Formats the preset does not name.
        "PySide2/plugins/imageformats/qgif.dll",
        "PySide6/Qt/plugins/imageformats/libqgif.dylib",
    }
    for name in removed | kept:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload")

    trim_tree(root, policy.site_packages)

    missing = sorted(name for name in removed if (root / name).exists())
    assert not missing, f"minimal left layout-specific payload behind: {missing}"
    lost = sorted(name for name in kept if not (root / name).exists())
    assert not lost, f"minimal removed payload it must keep: {lost}"


def test_preset_patterns_are_deduplicated_against_the_configured_ones():
    policy = TrimPolicy.from_config({
        "preset": "minimal",
        "site_packages": ["**/*.pyi", "PySide2/examples"],
        "runtime": ["**/tkinter"],
    })

    assert policy.site_packages.count("**/*.pyi") == 1
    assert policy.site_packages.count("PySide2/examples") == 1
    assert policy.runtime.count("**/tkinter") == 1


def test_a_command_line_preset_overrides_the_configured_one():
    policy = TrimPolicy.from_config({"preset": "minimal"}, preset="safe")

    assert "*/opengl32sw.dll" not in policy.site_packages
    assert "**/tkinter" not in policy.runtime
    assert not policy.has_site_rules
    assert not policy.has_runtime_rules


def test_an_unknown_preset_is_rejected():
    with pytest.raises(RuntimeError, match="preset"):
        TrimPolicy.from_config({"preset": "tiny"})


def test_template_declares_the_trim_interface():
    from py_upper.compat import tomllib
    from py_upper.config import APP_CONFIG_EXAMPLE

    data = tomllib.loads(APP_CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    trim = data["tool"]["py_upper"]["trim"]
    assert trim["preset"] == "safe"
    assert "**/*.pyi" in trim["site_packages"]
    # The tracked defaults must not encode one wheel layout either.
    assert "PySide*/**/examples" in trim["site_packages"]
    assert "PyQt*/**/include" in trim["site_packages"]
    assert "**/tkinter" in trim["runtime"]
    assert trim["site_packages_keep"] == []
    assert trim["runtime_keep"] == []


def test_the_cli_exposes_repeatable_trim_options(capsys):
    import build

    with pytest.raises(SystemExit) as exit_info:
        build.main(["--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    for option in (
        "--trim-preset",
        "--trim-site",
        "--trim-site-keep",
        "--trim-runtime",
        "--trim-runtime-keep",
    ):
        assert option in help_text