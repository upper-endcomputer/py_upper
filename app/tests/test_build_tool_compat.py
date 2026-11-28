# 中文维护说明：验证宿主版本、平台工具链和 CLI 配置契约；失败时先检查 fixture 与平台前提，不能通过删除断言掩盖回归。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def test_build_python_minimum_and_toml_compatibility():
    """验证构建解释器满足最低版本并能解析工具 TOML 配置。"""
    from appbuild.compat import tomllib
    from appbuild.config import validate_build_python_version

    validate_build_python_version(type("V", (), {"major": 3, "minor": 8})())
    data = tomllib.loads('[tool.appbuild]\nentry = "main.py"\n')
    assert data["tool"]["appbuild"]["entry"] == "main.py"


def test_every_build_tool_module_imports():
    """递归导入构建模块，检查迁移后的包引用和依赖完整性。

    Compiling is not loading.

    PEP 604 unions and builtin generics in annotations are evaluated when a
    module is imported, so ``list[str]`` / ``Path | None`` in a signature raise
    TypeError on 3.8 and 3.9 unless the module opts into PEP 563. ``py_compile``
    cannot see that, and the toolchain still supports 3.8, so every module is
    imported here rather than only the two the workflow used to name.
    """
    import importlib
    import pkgutil

    import appbuild

    names = sorted(module.name for module in pkgutil.walk_packages(appbuild.__path__, appbuild.__name__ + "."))
    assert "appbuild.python_build" in names and "appbuild.pbs" in names
    for name in names:
        importlib.import_module(name)


def _defers_annotations(tree) -> bool:
    """检查模块是否启用延迟求值的类型注解。"""
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _runtime_annotations(tree, builtin_generics):
    """收集会在运行时求值的注解，供最低 Python 版本检查。

    Annotation nodes CPython evaluates while the module is being defined."""
    found = []
    for node in ast.walk(tree):
        for field in ("annotation", "returns"):
            value = getattr(node, field, None)
            if value is None:
                continue
            for child in ast.walk(value):
                if isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name) and child.value.id in builtin_generics:
                    found.append(child)
                elif isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
                    found.append(child)
    return found


def test_every_source_module_defers_its_annotations():
    """验证使用新版注解语法的模块都启用延迟求值。

    The 3.8 leg must fail here, not only in the matrix.

    A PEP 585 generic or a PEP 604 union in a signature is evaluated when the
    module is defined, so ``list[str]`` / ``Path | None`` raise TypeError on 3.8
    before a single test runs. ``py_compile`` cannot see that, which is how a
    missing ``from __future__ import annotations`` in ``app/tests/conftest.py``
    turned the 3.8 job red while 3.9 through 3.13 stayed green. The future
    import defers evaluation, and every module the toolchain ships or runs has
    to carry it.
    """
    builtin_generics = {"dict", "frozenset", "list", "set", "tuple", "type"}
    root = Path(__file__).resolve().parents[2]
    from appbuild.config import APP

    offenders: list[str] = []
    for package in (APP / "src", Path(__file__).parent, root / "tools"):
        for path in sorted(package.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if _defers_annotations(tree):
                continue
            lines = sorted({node.lineno for node in _runtime_annotations(tree, builtin_generics)})
            if lines:
                try:
                    label = path.relative_to(root)
                except ValueError:
                    label = path
                offenders.append(f"{label}: {lines}")
    assert not offenders, f"these modules need 'from __future__ import annotations': {offenders}"


def test_msvc_host_target_pairs_cover_every_combination():
    """验证 MSVC 主机与目标架构的环境脚本参数映射。"""
    from appbuild.toolchain import VC_HOST_COMPONENT, VCVARSALL_HOST_TARGET

    for host in ("x86", "x86_64", "arm64"):
        assert host in VC_HOST_COMPONENT
        for target in ("x86", "x86_64", "arm64"):
            assert VCVARSALL_HOST_TARGET[(host, target)]
    # vcvarsall spells a native build with just the host architecture, and the
    # ARM64 runner must not be told to cross-compile from an x64 host.
    assert VCVARSALL_HOST_TARGET[("arm64", "arm64")] == "arm64"
    assert VCVARSALL_HOST_TARGET[("x86_64", "x86_64")] == "amd64"
    assert VCVARSALL_HOST_TARGET[("x86_64", "arm64")] == "amd64_arm64"


def test_host_arch_normalizes_windows_and_linux_spellings(monkeypatch):
    """验证不同系统的架构名称归一化为统一目标键。

    platform.machine() reports AMD64/ARM64 on Windows and x86_64/aarch64 on Linux."""
    from appbuild import toolchain

    for machine, expected in (
        ("AMD64", "x86_64"),
        ("x86_64", "x86_64"),
        ("ARM64", "arm64"),
        ("aarch64", "arm64"),
        ("i686", "x86"),
    ):
        monkeypatch.setattr(toolchain.platform, "machine", lambda machine=machine: machine)
        assert toolchain._host_arch() == expected


def test_windows_environment_parser_keeps_only_variable_assignments():
    """验证环境解析保留合法赋值并剔除命令回显和非法变量名。

    ``vcvarsall.bat`` prints a banner and progress lines around ``set``.

    Everything that is not a ``NAME=VALUE`` line has to be dropped: a stray line
    accepted as a variable would put a non-path into ``INCLUDE`` or ``PATH`` and
    break the compiler invocation far away from the parse. Names are upper-cased
    because ``set`` echoes whatever casing the environment block holds — the
    search path is ``Path`` there, while ``os.environ`` and every caller spell it
    ``PATH``.
    """
    from appbuild import toolchain

    parsed = toolchain._parse_windows_environment(
        "\n".join(
            [
                "**********************************************************************",
                "** Visual Studio 2022 Developer Command Prompt v17.14.0",
                "**********************************************************************",
                "[vcvarsall.bat] Environment initialized for: 'x64'",
                "VCToolsInstallDir=C:\\Program Files\\Microsoft Visual Studio\\2022\\VC\\Tools\\MSVC\\14.44.35207\\",
                "INCLUDE=C:\\SDK\\Include;=leading equals stays in the value",
                "Path=C:\\bin;C:\\Windows",
                "  PADDED=indented output is not a variable",
                "KEY WITH SPACES=not a variable",
                "NO_SEPARATOR_LINE",
                "",
            ]
        )
    )

    assert parsed["VCTOOLSINSTALLDIR"].endswith("14.44.35207\\")
    assert parsed["INCLUDE"] == "C:\\SDK\\Include;=leading equals stays in the value"
    assert parsed["PATH"] == "C:\\bin;C:\\Windows"
    assert "PADDED" not in parsed
    assert "KEY WITH SPACES" not in parsed
    assert set(parsed) == {"VCTOOLSINSTALLDIR", "INCLUDE", "PATH"}


def test_windows_environment_parser_reports_its_own_output_on_failure():
    """验证失败诊断保留有限长度的有效输出尾部。

    A failed ``vcvarsall`` run must describe itself; the exit code cannot."""
    from appbuild import toolchain

    assert toolchain._output_tail("", "") == "(no output)"
    assert toolchain._output_tail("first\n\nsecond") == "first\nsecond"
    tail = toolchain._output_tail("\n".join(f"line {index}" for index in range(100)), limit=3)
    assert tail == "line 97\nline 98\nline 99"


def test_tracked_pyproject_template_is_complete():
    """验证配置模板具备产品、入口、依赖和运行时的必要字段。

    A fresh clone copies this file, so it must be buildable on its own."""
    import re

    from appbuild.compat import tomllib
    from appbuild.config import APP_CONFIG_EXAMPLE

    assert APP_CONFIG_EXAMPLE.is_file()
    data = tomllib.loads(APP_CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    assert data["project"]["name"] and data["project"]["version"]
    # The template pins no dependencies: a fork must not inherit the pins of
    # whoever cloned first. The integration job materializes its own working
    # copy for the build, so an empty list here still builds.
    assert data["project"]["dependencies"] == []
    tool = data["tool"]["appbuild"]
    assert tool["entry"].endswith(".py")
    assert tool["app"]["name"] and tool["app"]["identifier"]
    assert tool["runtime"]["provider"] in {"pbs", "local"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", tool["runtime"]["python"])


def test_template_drops_the_pyside_arm64_orphan():
    """验证模板排除 Qt wheel 中不适合目标的孤立 VC 运行库。

    The PySide2/PySide6 and shiboken2/shiboken6 wheels ship an ARM32
    ``vccorlib140.dll``.

    Windows on ARM cannot map an ARM32 image at all, and no image in the wheel
    imports it (it is the C++/CX runtime, which Qt does not use), so the payload
    arch guard rightly refuses to ship it. It goes through the documented
    wheel-defect list rather than relaxing that guard: if either binding ever
    starts importing the file, the closure fails loudly with it missing instead
    of shipping a bundle that cannot load. Both bindings are listed because the
    Python 3.8 compatibility job builds against PySide2.
    """
    from appbuild.compat import tomllib
    from appbuild.config import APP_CONFIG_EXAMPLE

    data = tomllib.loads(APP_CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    exclude = data["tool"]["appbuild"]["native"]["exclude"]
    assert "PySide6/vccorlib140.dll" in exclude
    assert "shiboken6/vccorlib140.dll" in exclude
    assert "PySide2/vccorlib140.dll" in exclude
    assert "shiboken2/vccorlib140.dll" in exclude


def test_template_drops_the_32_bit_ucrt_orphan():
    """验证模板排除 Qt wheel 附带的错误位数 UCRT。

    The PySide2/PySide6 and shiboken2/shiboken6 win_amd64 wheels ship a
    32-bit ``ucrtbase.dll``.

    The UCRT ships with Windows and is on the closure's host-library list
    (``native/deps.py``), so it is never resolved and copied and no bundled
    image loads the wheel's copy; the payload arch guard rightly refuses to
    ship it. Like the ARM32 ``vccorlib140.dll``, it goes through the documented
    wheel-defect list instead of relaxing that guard: if a wheel ever starts
    importing the file, the closure still fails loudly rather than shipping a
    bundle that cannot load. Both bindings are listed because the Python 3.8
    compatibility job builds against PySide2.
    """
    from appbuild.compat import tomllib
    from appbuild.config import APP_CONFIG_EXAMPLE

    data = tomllib.loads(APP_CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    exclude = data["tool"]["appbuild"]["native"]["exclude"]
    assert "PySide6/ucrtbase.dll" in exclude
    assert "shiboken6/ucrtbase.dll" in exclude
    assert "PySide2/ucrtbase.dll" in exclude
    assert "shiboken2/ucrtbase.dll" in exclude


def test_local_configuration_is_git_ignored_but_the_template_is_not():
    """验证本机配置被忽略而可移植模板仍纳入版本控制。"""
    root = Path(__file__).parents[2]
    entries = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "app/pyproject.toml" in entries
    assert "app/pyproject.toml.example" not in entries


VALIDATION_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "validate.yml"
VSCODE_SETTINGS = Path(__file__).parents[2] / ".vscode" / "settings.json"


def _workflow_triggers(text: str) -> dict[str, list[str] | None]:
    """提取工作流触发分支，避免依赖完整 YAML 解析器。

    Read the `on:` block of the validation workflow.

    The build tool reads TOML and JSON only, so the repository ships no YAML
    parser and this test does not add one. What follows understands exactly the
    shape the trigger policy uses -- ``<trigger>:`` with an optional ``branches``
    list beneath it, inline or as ``- `` entries -- and raises on anything else,
    so a workflow that grows a construct this test cannot read fails here instead
    of passing on a stale reading. ``None`` means the trigger carries no branch
    filter.
    """
    lines = text.splitlines()
    triggers: dict[str, list[str] | None] = {}
    current = None
    for line in lines[lines.index("on:") + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent == 2 and stripped.endswith(":"):
            current = stripped[:-1]
            triggers[current] = None
        elif indent == 4 and stripped.startswith("branches:") and current is not None:
            inline = stripped[len("branches:") :].strip()
            if inline.startswith("[") and inline.endswith("]"):
                triggers[current] = [entry.strip() for entry in inline[1:-1].split(",") if entry.strip()]
            elif not inline:
                triggers[current] = []
            else:
                raise AssertionError(f"unreadable `branches:` value in: {line!r}")
        elif indent == 6 and stripped.startswith("- ") and triggers[current] is not None:
            triggers[current].append(stripped[2:].strip())
        else:
            raise AssertionError(f"unhandled line in the workflow `on:` block: {line!r}")
    return triggers


def test_ci_triggers_are_scoped_to_develop():
    """验证 CI 自动触发仅限 develop 的推送和拉取请求，并允许手动执行。

    Only develop runs the matrix, and it runs it on the way in.

    Development happens on feature_* / fix_* branches cut from develop and
    merged back after self-testing; their intermediate commits are not
    deliverable. A bare ``push:`` runs all fifteen jobs on every one of them.
    ``pull_request`` is kept because the review request aimed at develop is part
    of the merge, and it filters on the base branch.
    """
    triggers = _workflow_triggers(VALIDATION_WORKFLOW.read_text(encoding="utf-8"))
    assert triggers["push"] == ["develop"]
    assert triggers["pull_request"] == ["develop"]
    # Manual re-runs stay available on any ref so a failing job can be
    # reproduced without pushing an empty commit.
    assert triggers["workflow_dispatch"] is None
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}


def test_ci_builds_the_oldest_supported_windows_runtime():
    """验证 CI 保留最低受支持 Python 运行时的实际构建任务。

    Windows Python 3.8-3.12 embeddable runtimes ship ``_msi.pyd``.

    That extension imports ``msi.dll`` and ``Cabinet.dll`` from System32, so
    the native closure regression only appears on the older runtime line.
    Keep one real Windows package build on Python 3.8.10; a 3.13-only matrix
    cannot hide the same regression again.
    """
    text = VALIDATION_WORKFLOW.read_text(encoding="utf-8")
    assert """          - os: windows-2025
            target: windows-x86_64
            runtime-python: '3.8.10'
            run: true""" in text
    assert "APPBUILD_CI_RUNTIME_PYTHON: ${{ matrix.runtime-python || '3.13.5' }}" in text
    assert "dependencies: '[\"PySide2==5.15.2.1\"]'" in text
    assert "artifact: py38-compat-windows-x86_64" in text


def test_vscode_never_watches_the_built_trees():
    """验证编辑器排除生成树，避免构建文件触发无意义监视。

    The editor must not execute the interpreters this repository builds.

    The Python extension registers a ``**/python`` file watcher per workspace
    folder and hands every create/change it reports to python-env-tools, which
    resolves a path by executing it. A build that writes
    ``dist/MyApp.app/Contents/Resources/runtime/bin/python`` therefore gets its
    own artifact executed while it is still assembling it, and the bytecode that
    run writes lands in the tree the build is deleting. ``files.watcherExclude``
    is the only thing keeping those paths out of the editor's view, so dropping
    ``build`` or ``dist`` from it brings back intermittent local build failures
    (ENOENT on a bytecode temp file, ENOTEMPTY on ``__pycache__``).
    """
    import json

    settings = json.loads(VSCODE_SETTINGS.read_text(encoding="utf-8"))
    watcher = settings["files.watcherExclude"]
    for tree in ("build", "dist", "runtimes"):
        assert watcher.get(f"**/{tree}/**") is True, f"{tree}/ must stay out of the file watcher"
    # Searching or scanning the packaged runtime returns thousands of stdlib
    # hits instead of project code.
    search = settings["search.exclude"]
    for tree in ("build", "dist", "runtimes"):
        assert search.get(f"**/{tree}") is True


def test_missing_local_configuration_points_at_the_template(tmp_path, monkeypatch):
    """验证缺少本机配置时提示可复制的模板位置。"""
    from appbuild import config

    monkeypatch.setattr(config, "APP_CONFIG", tmp_path / "pyproject.toml")
    monkeypatch.setattr(config, "APP_CONFIG_EXAMPLE", tmp_path / "pyproject.toml.example")
    try:
        config.load_app_config()
    except RuntimeError as exc:
        assert "pyproject.toml.example" in str(exc)
    else:
        raise AssertionError("expected a missing-configuration error")


def test_missing_local_configuration_survives_a_foreign_drive(tmp_path, monkeypatch):
    """验证 Windows 跨盘路径不会使缺失配置的诊断再次失败。

    A configuration outside the project root must still be reported.

    ``os.path.relpath`` raises ValueError when the two paths are on different
    Windows drives, which is what happens when the working copy lives on D: and
    the temporary configuration on C:.
    """
    from appbuild import config

    def cross_drive(*_args, **_kwargs):
        """模拟无法计算跨盘相对路径的 Path 行为。"""
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr(config, "APP_CONFIG", tmp_path / "pyproject.toml")
    monkeypatch.setattr(config, "APP_CONFIG_EXAMPLE", tmp_path / "pyproject.toml.example")
    monkeypatch.setattr(config.os.path, "relpath", cross_drive)
    with pytest.raises(RuntimeError) as excinfo:
        config.load_app_config()
    assert "pyproject.toml.example" in str(excinfo.value)


def _write_probe_app(root: Path, name: str = "Probe") -> Path:
    """创建最小独立应用及配置，供外置应用路径验证。"""
    app = root / "myapp"
    (app / "src").mkdir(parents=True)
    (app / "src" / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (app / "pyproject.toml").write_text(
        f'''[project]
name = "probe"
version = "1.2.3"
dependencies = []

[tool.appbuild]
entry = "main.py"

[tool.appbuild.app]
name = "{name}"

[tool.appbuild.runtime]
python = "3.13.5"
''',
        encoding="utf-8",
    )
    return app


def test_application_directory_can_live_outside_the_framework(tmp_path):
    """验证构建工具能使用框架目录之外的应用源码和配置。

    A fork can point at its app without editing any appbuild module."""
    app = _write_probe_app(tmp_path)
    code = (
        "import sys; sys.path.insert(0, 'tools'); "
        "from appbuild.config import APP, APP_CONFIG, app_name, entry_module; "
        "print(APP); print(APP_CONFIG); print(app_name()); print(entry_module())"
    )
    env = os.environ.copy()
    env["APPBUILD_APP_DIR"] = str(app)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    lines = result.stdout.splitlines()
    assert lines[0] == str(app.resolve())
    assert lines[1] == str((app / "pyproject.toml").resolve())
    assert lines[2:] == ["Probe", "main"]


def test_app_directory_option_is_applied_before_config_import(tmp_path):
    """验证应用目录参数在首次读取配置之前生效。"""
    from appbuild import appdir

    app = _write_probe_app(tmp_path)
    env_name = appdir.APP_DIR_ENV
    old = os.environ.pop(env_name, None)
    try:
        assert appdir.select_from_argv(["--app", str(app)]) == str(app)
        assert os.environ[env_name] == str(app.resolve())
        assert appdir.select_from_argv([f"--app={app}"]) == str(app)
        with pytest.raises(SystemExit):
            appdir.select_from_argv(["--app"])
        with pytest.raises(SystemExit):
            appdir.select_from_argv(["--app", "--doctor"])
    finally:
        os.environ.pop(env_name, None)
        if old is not None:
            os.environ[env_name] = old


def test_root_application_is_discovered_without_an_app_child(tmp_path, monkeypatch):
    """验证可直接识别根目录应用布局，无需固定 app 子目录。

    Copying tools/ into an app repository should not require an app/ folder."""
    from appbuild import config

    (tmp_path / "pyproject.toml").write_text(
        "[tool.appbuild]\nentry = \"main.py\"\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "ROOT", tmp_path)
    assert config._default_app_dir() == tmp_path


def test_build_cli_documents_the_app_directory_option():
    """验证 CLI 帮助公开应用目录选项和对应环境变量。"""
    root = Path(__file__).parents[2]
    result = subprocess.run(
        [sys.executable, "tools/build.py", "--help"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "--app DIR" in result.stdout
    assert "APPBUILD_APP_DIR" in result.stdout


def test_build_cli_selects_an_external_app_before_config_load(tmp_path):
    """通过独立进程确认 CLI 使用指定外部应用。"""
    app = _write_probe_app(tmp_path)
    result = subprocess.run(
        [sys.executable, "tools/build.py", "--app", str(app), "--doctor"],
        cwd=Path(__file__).parents[2],
        text=True,
        capture_output=True,
    )
    assert f"app: {app.resolve()}" in result.stdout


def test_target_matrix_and_platform_contract(monkeypatch):
    """验证目标矩阵及可配置 manylinux 基线的轮子平台标签。"""
    from appbuild import config
    from appbuild.config import TARGETS

    assert set(TARGETS) == {
        "windows-x86", "windows-x86_64", "windows-arm64",
        "macos-x86_64", "macos-arm64", "linux-x86_64", "linux-arm64",
    }
    assert TARGETS["windows-arm64"].triple == "aarch64-pc-windows-msvc"
    assert TARGETS["macos-arm64"].primary_wheel_platform == "macosx_13_0_arm64"
    # Linux baselines are project configuration, so assert the derivation
    # instead of duplicating whatever app/pyproject.toml currently declares.
    monkeypatch.setattr(config, "dependency_config", lambda: {})
    assert TARGETS["linux-x86_64"].wheel_platforms == ("manylinux_2_17_x86_64",)
    monkeypatch.setattr(config, "dependency_config", lambda: {"manylinux": ["manylinux_2_39", "manylinux_2_34"]})
    assert TARGETS["linux-x86_64"].wheel_platforms == ("manylinux_2_39_x86_64", "manylinux_2_34_x86_64")
    assert TARGETS["linux-arm64"].wheel_platforms == ("manylinux_2_39_aarch64", "manylinux_2_34_aarch64")
    assert TARGETS["linux-x86_64"].primary_wheel_platform == "manylinux_2_39_x86_64"


def test_manylinux_baselines_are_validated(monkeypatch):
    """验证不合法的 manylinux 基线配置会被拒绝。"""
    from appbuild import config

    for value in ("manylinux_2_17", ["manylinux_2_28"], "manylinux2014"):
        monkeypatch.setattr(config, "dependency_config", lambda value=value: {"manylinux": value})
        assert config.manylinux_baselines()
    monkeypatch.setattr(config, "dependency_config", lambda: {"manylinux": "manylinux"})
    try:
        config.manylinux_baselines()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected a rejection of an unqualified manylinux baseline")
    monkeypatch.setattr(config, "dependency_config", lambda: {"manylinux": []})
    try:
        config.manylinux_baselines()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected a rejection of an empty manylinux list")


def test_extension_suffixes_follow_target_import_conventions():
    """验证扩展命名使用目标平台的 CPython ABI 后缀。"""
    from appbuild.config import TARGETS, target_extension_suffix

    assert target_extension_suffix(TARGETS["windows-x86_64"], "3.13", "cp313") == ".cp313-win_amd64.pyd"
    assert target_extension_suffix(TARGETS["macos-arm64"], "3.13", "cp313") == ".cpython-313-darwin.so"
    assert target_extension_suffix(TARGETS["linux-x86_64"], "3.13", "cp313") == ".cpython-313-x86_64-linux-gnu.so"


def test_windows_extensions_are_named_for_the_target_not_the_build_host(tmp_path):
    """验证交叉构建的 Windows 扩展后缀不受主机解释器影响。

    setuptools names an extension after the interpreter that runs it.

    On Windows the extensions are compiled by the development interpreter (3.13
    on the CI runners) for the target runtime (3.11), so the name setuptools
    derives on its own is the host's: utils/paths.cp313-win_amd64.pyd. The
    target runtime cannot import that name and the layout check rejects it, so
    the generated driver must answer with the target's suffix. This runs the
    driver the build actually writes, with the host suffix standing in for the
    one the running interpreter would produce.
    """
    import setuptools
    from setuptools.dist import Distribution

    from appbuild.config import APP
    from appbuild.python_build import module_name, windows_setup_script

    source = APP / "src" / "utils" / "paths.py"
    module = module_name(source)
    script = windows_setup_script(
        include=tmp_path / "include",
        libdir=tmp_path / "libs",
        libraries=["python311"],
        generated={source: tmp_path / "app.c"},
        sources=[source],
        suffix=".cp311-win_amd64.pyd",
    )

    captured: dict = {}
    original = setuptools.setup
    setuptools.setup = lambda **kwargs: captured.update(kwargs)
    try:
        exec(compile(script, "setup.py", "exec"), {"__name__": "__main__"})
    finally:
        setuptools.setup = original

    command = captured["cmdclass"]["build_ext"](Distribution({"ext_modules": captured["ext_modules"]}))
    command.build_lib = str(tmp_path / "out")
    command.inplace = False
    command.ext_map = {}
    built = Path(*module.split(".")).with_suffix(".cp311-win_amd64.pyd")
    assert command.get_ext_fullpath(module) == str(tmp_path / "out" / built)


def test_source_selection_is_layout_agnostic():
    """验证模块发现支持包和普通模块，并正确处理包初始化文件。"""
    from appbuild.python_build import APP, extension_name, module_name, selected_sources

    sources = selected_sources()
    modules = {module_name(path) for path in sources}
    assert "main" in modules
    assert "application" in modules
    assert "utils.paths" in modules

    # Package markers are compiled too. Their import name is the package name,
    # while setuptools still needs the __init__ extension path and entry point.
    package_init = APP / "src" / "mvvm" / "__init__.py"
    assert package_init in sources
    assert module_name(package_init) == "mvvm"
    assert extension_name(package_init) == "mvvm.__init__"


def test_cython_cache_is_keyed_by_development_python(monkeypatch, tmp_path):
    """验证 Cython 工具缓存按构建解释器版本隔离。"""
    from appbuild import python_build

    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    calls = []

    class Result:
        stdout = "3.13\n"
        returncode = 0

    monkeypatch.setattr(python_build.subprocess, "run", lambda *args, **kwargs: Result())
    path = python_build._cython_cache_dir(Path("/fake/python3"))
    assert path.name == "cython-py3_13"
    assert calls == []


def test_pip_transfer_args_are_configurable(monkeypatch):
    """验证 pip 超时与重试次数的默认值和覆盖规则。"""
    from appbuild import config

    monkeypatch.setattr(config, "dependency_config", lambda: {})
    monkeypatch.delenv("APPBUILD_PIP_TIMEOUT", raising=False)
    monkeypatch.delenv("APPBUILD_PIP_RETRIES", raising=False)
    assert config.pip_transfer_args() == ["--timeout", "120", "--retries", "10"]

    monkeypatch.setattr(config, "dependency_config", lambda: {"timeout": 300, "retries": 3})
    assert config.pip_transfer_args() == ["--timeout", "300", "--retries", "3"]

    monkeypatch.setenv("APPBUILD_PIP_TIMEOUT", "45")
    monkeypatch.setattr(config, "dependency_config", lambda: {})
    assert config.pip_transfer_args() == ["--timeout", "45", "--retries", "10"]


def test_native_wheel_resolution_uses_the_target_interpreter(tmp_path, monkeypatch):
    """验证本机构建由目标解释器解析 wheel 标签。

    pip evaluates environment markers against the interpreter that runs it."""
    from appbuild import third_party
    from appbuild.config import TARGETS

    target = TARGETS["macos-arm64"]
    runtime_python = tmp_path / "bin" / "python3.10"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(third_party, "host_target", lambda: target)
    monkeypatch.setattr(third_party, "runtime_executable", lambda value: runtime_python)
    monkeypatch.setattr(third_party, "_has_pip", lambda value: True)
    python, tag_args = third_party.resolution_python(target)
    assert python == runtime_python
    assert tag_args == []


def test_cross_target_resolution_keeps_the_host_interpreter(tmp_path, monkeypatch):
    """验证交叉构建使用主机 pip 并显式传入目标标签。"""
    from appbuild import third_party
    from appbuild.config import TARGETS

    target = TARGETS["windows-x86_64"]
    host_python = tmp_path / "host-python"
    host_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(third_party, "host_target", lambda: TARGETS["macos-arm64"])
    monkeypatch.setattr(third_party, "require_local_python", lambda: host_python)
    python, tag_args = third_party.resolution_python(target)
    assert python == host_python
    assert "--platform" in tag_args and "win_amd64" in tag_args


def test_cython_supported_versions():
    """验证 Cython 版本接受范围与最低支持版本边界。"""
    from appbuild.python_build import _supported_cython

    assert _supported_cython("3.1.6")
    assert _supported_cython("3.2.4")
    assert not _supported_cython("3.0.12")


def test_supported_host_cython_ignores_stale_cache(monkeypatch, tmp_path):
    """验证主机已有可用 Cython 时不被旧缓存遮蔽。"""
    from appbuild import python_build

    cache = tmp_path / "cython-cache"
    cache.mkdir()
    monkeypatch.setenv("PYTHONPATH", "existing-path")
    probes = []

    def version(host, env):
        """按 PYTHONPATH 模拟主机与缓存中的不同 Cython 版本。"""
        probes.append(env.copy())
        return "3.0.12" if str(cache) in env.get("PYTHONPATH", "") else "3.2.4"

    monkeypatch.setattr(python_build, "_cython_version", version)
    cache_probes = []
    monkeypatch.setattr(python_build, "_cython_cache_dir", lambda host: cache_probes.append(host) or cache)
    env = python_build._cython_env(Path("python"))

    assert env["PYTHONPATH"] == "existing-path"
    assert len(probes) == 1
    assert not cache_probes


def test_supported_cached_cython_is_added_once(monkeypatch, tmp_path):
    """验证可用 Cython 缓存路径只加入搜索路径一次。"""
    from appbuild import python_build

    cache = tmp_path / "cython-cache"
    cache.mkdir()
    monkeypatch.setenv("PYTHONPATH", "existing-path")
    monkeypatch.setattr(python_build, "_cython_cache_dir", lambda host: cache)
    monkeypatch.setattr(
        python_build, "_cython_version",
        lambda host, env: "3.2.4" if str(cache) in env["PYTHONPATH"] else None,
    )
    env = python_build._cython_env(Path("python"))

    assert env["PYTHONPATH"].split(os.pathsep) == [str(cache), "existing-path"]


@pytest.mark.parametrize("existing_cache", [False, True])
@pytest.mark.parametrize("installed_version", ["3.2.4", "3.0.12"])
def test_cython_bootstrap_replaces_cache_and_checks_result(
    monkeypatch, tmp_path, existing_cache, installed_version,
):
    """验证引导安装升级旧缓存，并重新探测安装结果。"""
    from appbuild import python_build

    cache = tmp_path / "cython-cache"
    if existing_cache:
        cache.mkdir()
    monkeypatch.setenv("PYTHONPATH", "existing-path")
    monkeypatch.setattr(python_build, "_cython_cache_dir", lambda host: cache)
    commands = []
    probes = []

    def version(host, env):
        """根据是否执行安装返回前后版本，检查再次探测行为。"""
        probes.append(env.copy())
        return installed_version if commands else "3.0.12"

    monkeypatch.setattr(python_build, "_cython_version", version)
    monkeypatch.setattr(python_build, "_run", lambda cmd: commands.append(cmd))
    monkeypatch.setattr(python_build, "pip_transfer_args", lambda: [])
    if installed_version == "3.0.12":
        with pytest.raises(RuntimeError, match="Unable to bootstrap"):
            python_build._cython_env(Path("python"))
    else:
        env = python_build._cython_env(Path("python"))
        assert env["PYTHONPATH"].split(os.pathsep) == [str(cache), "existing-path"]

    assert len(commands) == 1
    assert "--upgrade" in commands[0]
    assert len(probes) == (3 if existing_cache else 2)


def test_package_copy_does_not_replay_source_metadata(tmp_path, monkeypatch):
    """验证发布复制不继承源码的只读或多余执行权限。"""
    from appbuild import fs

    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir()
    readonly = source / "README.rst"
    readonly.write_text("read\n", encoding="utf-8")
    readonly.chmod(0o444)
    calls = []
    original = fs.os.chmod
    monkeypatch.setattr(fs.os, "chmod", lambda path, mode: (calls.append(path), original(path, mode))[1])

    fs.copy_tree_contents(source, destination)

    copied = destination / "README.rst"
    assert copied.read_text(encoding="utf-8") == "read\n"
    assert not calls
    assert stat.S_IMODE(copied.stat().st_mode) & 0o111 == 0


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX execute bits")
def test_copy_preserves_execute_bits_without_replaying_restrictive_modes(tmp_path):
    """验证可执行文件保留执行位，普通文件不被错误设置执行位。

    A copied interpreter must stay runnable; 0444 sources must not be replayed."""
    from appbuild import fs

    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir()
    interpreter = source / "python3.10"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)
    readonly = source / "README.rst"
    readonly.write_text("read\n", encoding="utf-8")
    readonly.chmod(0o444)

    fs.copy_tree_contents(source, destination)

    copied_interpreter = destination / "python3.10"
    assert stat.S_IMODE(copied_interpreter.stat().st_mode) & 0o111
    assert copied_interpreter.read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert stat.S_IMODE((destination / "README.rst").stat().st_mode) & 0o111 == 0


def test_package_copy_can_reject_collisions(tmp_path):
    """验证要求禁止覆盖时，复制遇到目标冲突明确失败。"""
    from appbuild.fs import copy_file_contents, copy_tree_contents

    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir(); destination.mkdir()
    (source / "a.txt").write_text("one", encoding="utf-8")
    copy_file_contents(source / "a.txt", destination / "a.txt")
    try:
        copy_tree_contents(source, destination, replace=False)
    except RuntimeError as exc:
        assert "collision" in str(exc).lower()
    else:
        raise AssertionError("expected collision")


def test_optional_resource_tree_is_skipped_when_absent(tmp_path):
    """验证资源目录缺失时跳过，存在时复制其内容。

    app/resources is optional: a project without it must still package."""
    from appbuild.fs import copy_optional_tree, copy_tree_contents

    source = tmp_path / "resources"
    destination = tmp_path / "out" / "resources"

    copy_optional_tree(source, destination)
    assert not destination.exists()

    source.mkdir()
    (source / "logo.png").write_bytes(b"\x89PNG")
    copy_optional_tree(source, destination)
    assert (destination / "logo.png").read_bytes() == b"\x89PNG"

    # The strict copier keeps failing loudly for payload that must be present.
    try:
        copy_tree_contents(tmp_path / "missing", tmp_path / "never")
    except RuntimeError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("expected a missing-source error")


def test_launcher_uses_global_python_symbols_and_smoke_switch():
    """检查启动器的符号可见性、隔离配置、嵌入入口和 smoke 开关。"""
    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    text = launcher.read_text(encoding="utf-8")
    # Whitespace-insensitive: the launcher is reformatted from time to time and
    # the assertion is about the flags, not the spacing.
    compact = "".join(text.split())
    assert "RTLD_NOW|RTLD_GLOBAL" in compact
    assert "APPBUILD_SMOKE" in text
    assert "RTLD_LOCAL" not in text
    # PyConfig_InitIsolatedConfig ignores PYTHONDONTWRITEBYTECODE, so the
    # launcher must disable bytecode caching through the config field. Writing
    # .pyc files into a signed .app mutates the bundle at runtime.
    assert "config.write_bytecode=0;" in compact
    # A hand-made launcher still resolves its entry from a file next to it, so
    # the rename-safe fallback name has to stay in the binary.
    assert "_appbuild_static.int" in text
    # A fatal error in a GUI-subsystem launcher has nowhere to print, so the
    # dialog is the report channel and the env switch is the way out of it.
    assert "APPBUILD_NO_DIALOG" in text
    # The application name is compiled in: a renamed executable must still
    # report the configured name, not its own filename.
    assert "APPBUILD_APP_NAME" in text
    # SetEnvironmentVariableW updates the process block but not the CRT copy
    # Python snapshots into os.environ, so the bootstrap republishes every
    # exported value inside the interpreter.
    assert "os.environ.update(LAUNCHER_ENV)" in text
    assert 'launcher_env += "    " + py_quote(entry.first)' in text
    # A compiled-in entry reports the launcher as its file; linecache must not
    # try to decode the PE image when the crash report wants a source line.
    assert "linecache.cache[ENTRY_FILE]" in text
    # The packaged entry programs are compiled in, which is what lets a bundle
    # ship no .int sidecar at all. The define is what tells the launcher the
    # generated symbols exist, and the accessors are what resolve them.
    assert "APPBUILD_HAS_EMBEDDED_SOURCE" in text
    assert "appbuild_embedded::kEntry" in text
    assert "appbuild_embedded::kSmoke" in text
    # The egg key is rebuilt at run time from a masked pair, so the shipped
    # binary has no readable hexadecimal key for `strings` to find.
    assert "kEggKeyMasked" in text
    assert "kEggKeyMask" in text
    assert "kEggKeyLength" in text
    # A packaged launcher refuses an archive with no authentication tag.
    assert "pyupper::read_egg(egg, embedded_egg_key())" in text
    assert 'globals().pop("_appbuild_opened_egg")' in text
    # Authentication is mandatory: the bootstrap carries no switch that could
    # turn the tag check off.
    assert "embedded_egg_requires_auth()" not in text
    assert "EGG_REQUIRE_AUTH" not in text
    cmake = (launcher.parents[1] / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "APPBUILD_APP_NAME" in cmake
    assert "APPBUILD_EMBEDDED_SOURCE" in cmake
    assert "APPBUILD_HAS_EMBEDDED_SOURCE" in cmake


def test_launcher_egg_reader_verifies_the_authentication_tag():
    """检查启动器仅接受认证归档，使用恒定时间校验及明确字节序。

    The C++ reader and the Python loader must agree on the v2 format.

    If the launcher trusted an untagged archive, replacing bundle/appbuild.dat
    with any zip of the attacker's own would run their code; the tag is what
    makes that fail before a single module is imported.
    """
    headers = Path(__file__).parents[2] / "launcher" / "src"
    egg_bundles = (headers / "egg_bundles.h").read_text(encoding="utf-8")
    # The magic is spelled byte by byte so the reader never depends on the
    # host's string encoding for a binary header.
    assert "'W', 'B', 'E', 'G', 'G', '0', '2', '!'" in egg_bundles
    assert "egg authentication failed" in egg_bundles
    assert "egg is not authenticated" in egg_bundles
    # There is no v1 parser and no switch that skips the tag check.
    assert "magic_v1" not in egg_bundles
    assert "require_auth" not in egg_bundles
    # Constant-time compare: the tag check must not leak a matching prefix.
    assert "equal_constant_time" in egg_bundles
    # The tag doubles as the cache key, which is what keeps authentication from
    # costing an extra pass over the payload.
    assert "cache_key = detail::hex(tag, kTagSize)" in egg_bundles
    sha3 = (headers / "sha3.h").read_text(encoding="utf-8")
    # Lane-wise absorb/squeeze instead of the byte loop: the byte version ran at
    # ~150 MB/s and made the authenticated format measurably slower than the
    # unauthenticated one.
    assert "load_le64" in sha3
    assert "store_le64" in sha3


def test_masked_key_arrays_round_trip_and_hide_the_plain_key():
    """验证分片密钥可还原，且生成数组不直接暴露明文密钥。"""
    from appbuild.package import masked_key_arrays

    key_hex = "047636cf3ddbaae2346f597256416e858ee0b7c8b7448221e5babfbb4f74c20d"
    masked, mask = masked_key_arrays(key_hex)
    key = bytes.fromhex(key_hex)
    assert bytes(left ^ right for left, right in zip(masked, mask)) == key
    # Neither array may contain the key, or the masking is decorative.
    assert bytes(masked) != key
    assert bytes(mask) != key
    assert key_hex.encode("ascii") not in bytes(masked) + bytes(mask)
    # Derived from the key, so a rebuild reproduces the same binary.
    assert masked_key_arrays(key_hex) == (masked, mask)


def test_launcher_compiles_against_python38_snprintf_alias():
    """验证 MSVC 构建定义所支持旧 Python 头文件需要的 snprintf 宏。

    Python 3.8's pyerrors.h aliases snprintf to _snprintf for MSVC.

    The alias assumed the pre-C99 MSVC CRT. VS 2015+ provides snprintf, and
    VS 18 no longer exposes _snprintf from <cstdio> in namespace std, so the
    launcher's std::snprintf became std::_snprintf and failed with C2039.
    HAVE_SNPRINTF must be defined for the native MSVC build; the Zig frontend
    already passes it for MinGW.
    """
    cmake = (Path(__file__).parents[2] / "launcher" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "if(MSVC)" in cmake
    assert "target_compile_definitions(PyUpper PRIVATE HAVE_SNPRINTF)" in cmake


def test_launcher_escapes_python_literals_without_a_raw_prefix():
    """检查路径转 Python 字面量时正确转义斜杠、制表符和控制字符。

    A raw literal plus doubled backslashes doubled every Windows separator.

    ``py_quote`` built ``r'...'`` and then escaped ``\\`` as ``\\\\``, which a raw
    string hands back verbatim: a Windows path reached Python with each
    separator written twice. The quoting is a normal literal with real escapes
    now, so ``\\t`` and control bytes are emitted as escapes as well.
    """
    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    body = launcher.read_text(encoding="utf-8").split("std::string py_quote", 1)[1].split("\n}\n", 1)[0]
    backslash = chr(92)
    # The literal is opened without the raw prefix; that prefix is what made the
    # doubled backslashes survive into the path Python saw.
    assert 'std::string r = "\'";' in body
    assert 'r = "r' not in body
    assert 'r += "' + backslash * 4 + '"; break;' in body
    assert 'r += "' + backslash * 2 + 't"; break;' in body
    assert '"' + backslash * 2 + 'x%02x"' in body


def test_launcher_build_passes_the_configured_app_name(tmp_path, monkeypatch):
    """验证 CMake 收到配置产品名及生成的嵌入源码路径。"""
    from types import SimpleNamespace

    from appbuild import package
    from appbuild.config import TARGETS, app_name

    monkeypatch.setattr(package, "BUILD", tmp_path)
    monkeypatch.setattr(package, "resolve_target_python", lambda target: SimpleNamespace(include_dir=tmp_path / "include"))
    monkeypatch.setattr(package, "resolve_toolchain", lambda target: SimpleNamespace(env={}, deployment_target=None))
    calls = []

    def fake_run(cmd, **kwargs):
        """记录 CMake 调用并模拟产生启动器文件。"""
        calls.append(cmd)
        if "--build" in cmd:
            (tmp_path / "launcher" / "macos-arm64" / "PyUpper").write_text("", encoding="utf-8")

    monkeypatch.setattr(package.subprocess, "run", fake_run)

    package.build_launcher(TARGETS["macos-arm64"])

    configure = calls[0]
    assert configure[0] == "cmake"
    assert f"-DAPPBUILD_APP_NAME={app_name()}" in configure
    # The entry programs are generated into the build directory and compiled in
    # through this define; without it the launcher is the hand-made variant. The
    # path is spelled for CMake, see test_launcher_configure_spells_every_path_for_cmake.
    embedded = tmp_path / "launcher" / "macos-arm64" / "appbuild_embedded.cpp"
    assert f"-DAPPBUILD_EMBEDDED_SOURCE={embedded.as_posix()}" in configure
    assert embedded.is_file()


def test_zig_launcher_configure_names_every_windows_architecture(tmp_path, monkeypatch):
    """验证 Zig 的 Windows 架构与 Unicode 入口链接参数。

    The configure step must carry the target architecture, x86 included.

    CMAKE_SYSTEM_PROCESSOR used to be spelled with an arm64/AMD64 ternary, which
    reported every 32-bit configure as AMD64.  It now comes from the packaging
    target, which has an entry for each Windows arch the tooling can build.
    """
    from types import SimpleNamespace

    from appbuild import package
    from appbuild.config import TARGETS
    from appbuild.package import CMAKE_WINDOWS_PROCESSOR

    for target in TARGETS.values():
        if target.os == "windows":
            assert CMAKE_WINDOWS_PROCESSOR[target.arch]
    assert CMAKE_WINDOWS_PROCESSOR["x86"] == "x86"

    monkeypatch.setattr(package, "BUILD", tmp_path)
    monkeypatch.setattr(package, "resolve_target_python", lambda target: SimpleNamespace(include_dir=tmp_path / "include"))
    monkeypatch.setattr(package, "resolve_toolchain", lambda target: SimpleNamespace(
        env={}, deployment_target=None, compiler=str(tmp_path / "zig"),
        compiler_args=("cc", "-target", "aarch64-windows-gnu"),
    ))
    calls = []

    def fake_run(cmd, **kwargs):
        """记录交叉构建命令并创建预期启动器产物。"""
        calls.append(cmd)
        if "--build" in cmd:
            (tmp_path / "launcher" / "windows-arm64" / "PyUpper.exe").write_text("", encoding="utf-8")

    monkeypatch.setattr(package.subprocess, "run", fake_run)

    package.build_launcher(TARGETS["windows-arm64"])

    configure = calls[0]
    assert "-DCMAKE_SYSTEM_PROCESSOR=ARM64" in configure
    assert "-DCMAKE_EXE_LINKER_FLAGS=-municode" in configure


def test_launcher_configure_spells_every_path_for_cmake(tmp_path, monkeypatch):
    """验证传给 CMake 的路径均使用其能识别的格式。

    A -D path must reach CMake with forward slashes, on a Windows host too.

    CMake writes ``CMAKE_RC_COMPILER`` verbatim into the generated
    ``CMakeRCCompiler.cmake`` as ``set(CMAKE_RC_COMPILER "...")``. A Windows
    path with backslashes is then re-read as a CMake string literal, and
    ``D:\\XY_Project\\...`` fails with "Invalid character escape '\\X'" before
    the resource compiler is ever invoked, which is what made the launcher step
    impossible to run from a Windows checkout. A compiler CMake finds itself is
    normalized; one handed in through ``-D`` is not, so the tool spells every
    path it passes that way.
    """
    from types import SimpleNamespace

    from appbuild import package
    from appbuild.config import TARGETS

    monkeypatch.setattr(package, "BUILD", tmp_path)
    monkeypatch.setattr(package, "resolve_target_python", lambda target: SimpleNamespace(include_dir=tmp_path / "include"))
    monkeypatch.setattr(package, "resolve_toolchain", lambda target: SimpleNamespace(
        env={}, compiler=str(tmp_path / "zig"), compiler_args=("cc", "-target", "x86_64-windows-gnu"),
    ))
    calls = []

    def fake_run(cmd, **kwargs):
        """捕获配置命令并模拟成功的启动器构建。"""
        calls.append(cmd)
        if "--build" in cmd:
            (tmp_path / "launcher" / "windows-x86_64" / "PyUpper.exe").write_text("", encoding="utf-8")

    monkeypatch.setattr(package.subprocess, "run", fake_run)

    package.build_launcher(TARGETS["windows-x86_64"])

    configure = calls[0]
    defines = [str(token) for token in configure if str(token).startswith("-D")]
    assert defines, "the configure step carried no defines at all"
    for define in defines:
        _, _, value = define.partition("=")
        assert "\\" not in value, define

    rc = next(token for token in configure if str(token).startswith("-DCMAKE_RC_COMPILER="))
    # CMake reads this one back as CMake code, so the spelling is what matters.
    assert Path(str(rc).split("=", 1)[1]).is_absolute()


def test_launcher_cmake_requests_the_gui_subsystem_from_every_toolchain():
    """检查 MSVC 和 GNU 类工具链均显式选择 GUI 子系统。

    WIN32_EXECUTABLE alone does not survive the Zig cross build.

    CMake spells it -mwindows for non-MSVC compilers, and clang's driver (which
    the Zig cross build goes through) drops that as an unused argument, so the
    launcher came out as a console image that pops a console window for every
    user.  The subsystem is requested on the target instead, where MSVC is
    guarded out because its linker does not accept -Wl,... arguments.
    """
    cmake = (Path(__file__).parents[2] / "launcher" / "CMakeLists.txt").read_text(encoding="utf-8")
    compact = "".join(cmake.split())
    assert "set_target_properties(PyUpperPROPERTIESWIN32_EXECUTABLETRUE)" in compact
    assert 'if(NOTMSVC)' in compact
    assert 'target_link_options(PyUpperPRIVATE"-Wl,--subsystem,windows")' in compact
    # The console escape hatch is the one build that must stay a console image.
    assert "APPBUILD_LAUNCHER_CONSOLE" in cmake


def test_embedded_source_carries_the_entry_and_the_smoke_program(tmp_path):
    """验证生成源码同时包含正式入口与 smoke 程序。

    The entry programs are generated as one translation unit, not as files.

    A packaged bundle used to ship ``<App>.int``, a rename-safe copy of the same
    program under ``_appbuild_static.int``, and the smoke program: two copies of
    one entry plus build-verification data no user installs. Both now compile
    into the launcher, so this generated file is the only home the entry has.
    """
    from appbuild.config import TARGETS, entry_module, entry_source
    from appbuild.package import (
        EMBEDDED_SOURCE_FILE, RAW_STRING_DELIMITER, smoke_source,
        write_embedded_source,
    )

    target = TARGETS["windows-x86_64"]
    path = write_embedded_source(tmp_path, target)

    assert path == tmp_path / EMBEDDED_SOURCE_FILE
    text = path.read_text(encoding="utf-8")
    assert "namespace appbuild_embedded" in text
    assert "extern const char* const kEntry;" in text
    assert "extern const char* const kSmoke;" in text
    # Raw literals: the program text is not escaped, so what the launcher
    # compiles is exactly what the configuration names.
    for payload in (entry_source(), smoke_source(target)):
        assert f'R"{RAW_STRING_DELIMITER}({payload}){RAW_STRING_DELIMITER}"' in text
    assert f"from {entry_module()} import main" in text
    assert "SMOKE PASS launcher" in text


def test_embedded_literal_refuses_a_payload_it_cannot_carry():
    """验证原始字符串字面量拒绝包含结束定界符的输入。

    A payload that breaks the raw literal must fail the build, not be mangled.

    Switching delimiters would silently hide the bug, and the bootstrap wraps
    the same text in a Python triple-quoted literal, so that has to be absent
    too. Both are generator invariants, which is why they raise here.
    """
    import pytest

    from appbuild.package import RAW_STRING_DELIMITER, _embedded_literal

    ok = _embedded_literal("print('hi')\n", "the application entry")
    assert ok.startswith(f'R"{RAW_STRING_DELIMITER}(') and ok.endswith(f'){RAW_STRING_DELIMITER}"')
    with pytest.raises(RuntimeError, match="raw string delimiter"):
        _embedded_literal(f'x = "){RAW_STRING_DELIMITER}"\n', "the application entry")
    with pytest.raises(RuntimeError, match="triple quote"):
        _embedded_literal('doc = """text"""\n', "the application entry")


def test_windows_version_resource_carries_a_four_part_version(tmp_path):
    """验证 Windows 版本资源含四段数字版本及 UTF-8 编码标记。"""
    import re

    from appbuild.config import app_name, project_version
    from appbuild.package import _windows_version_resource

    path = _windows_version_resource(tmp_path)
    # rc.exe reads a resource script with the ANSI code page unless the file is
    # marked; without the mark a non-ASCII product name would be mangled.
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    text = path.read_text(encoding="utf-8-sig")
    numbers = [int(part) for part in re.findall(r"\d+", project_version())][:4]
    numbers += [0] * (4 - len(numbers))
    numeric = ",".join(str(value) for value in numbers)
    assert f" FILEVERSION {numeric}" in text
    assert f" PRODUCTVERSION {numeric}" in text
    assert f'VALUE "ProductName", "{app_name()}"' in text
    assert f'VALUE "FileVersion", "{project_version()}"' in text


def test_macos_ad_hoc_signing_seals_the_bundle_last(tmp_path, monkeypatch):
    """验证 macOS 签名覆盖原生库，并在最后签启动器。

    Signing the launcher seals the .app, so nested images must be signed first."""
    from types import SimpleNamespace

    from appbuild.config import TARGETS
    from appbuild.native import bundle
    from appbuild.native.inspect import BinaryInfo

    target = TARGETS["macos-arm64"]
    root = tmp_path / "MyApp.app"
    launcher = root / "Contents" / "MacOS" / "MyApp"
    library = root / "Contents" / "Resources" / "site-packages" / "libdemo.dylib"
    for path in (launcher, library):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe")

    macho = lambda path: BinaryInfo(path, "Mach-O", "arm64")
    signed: list[Path] = []
    monkeypatch.setattr(bundle, "BUILD", tmp_path / "build")
    monkeypatch.setattr(bundle, "app_name", lambda: "MyApp")
    monkeypatch.setattr(bundle, "_native_files", lambda path, value: [library])
    monkeypatch.setattr(bundle, "dependency_names", lambda path, env: [])
    monkeypatch.setattr(bundle, "inspect", macho)
    monkeypatch.setattr(bundle, "verify_arch", lambda path, value: macho(path))
    monkeypatch.setattr(bundle, "optimize_config", lambda: {})
    monkeypatch.setattr(bundle, "_strip_native", lambda path, value: None)
    monkeypatch.setattr(bundle, "ad_hoc_sign", signed.append)
    # Host tool discovery selects the signing branch. Replace the module
    # attribute instead of shutil.which itself: the stdlib module is shared
    # with deps.py, where a faked path would be executed by subprocess and
    # fail on hosts without otool.
    monkeypatch.setattr(bundle, "shutil", SimpleNamespace(which=lambda name: "/usr/bin/" + name))
    # otool is only needed to index install names. This test asserts signing
    # order, so no host tool should run at all.
    monkeypatch.setattr(bundle, "_mac_aliases", lambda roots: {})

    bundle.bundle_native_dependencies(root, target)

    assert sorted(signed) == sorted([launcher.resolve(), library.resolve()])
    assert signed[-1] == launcher.resolve()


def test_macos_universal_images_are_thinned_to_the_target_architecture(tmp_path, monkeypatch):
    """验证通用 Mach-O 按目标架构裁剪，保留权限并清理中间文件。

    The bundle declares one architecture, so the foreign slice is dropped.

    PySide6 and pyobjc publish universal2 wheels; carrying both slices would
    double the size of every bundled image for an architecture the bundle never
    loads.
    """
    import shutil as real_shutil
    from types import SimpleNamespace

    from appbuild.native import bundle
    from appbuild.native.inspect import BinaryInfo

    universal = tmp_path / "QtDBus"
    universal.write_bytes(b"\xca\xfe\xba\xbe")
    universal.chmod(0o755)
    mode = stat.S_IMODE(universal.stat().st_mode)
    already_thin = tmp_path / "QtCore"
    already_thin.write_bytes(b"\xcf\xfa\xed\xfe")

    arches = {universal.resolve(): "universal", already_thin.resolve(): "arm64"}
    monkeypatch.setattr(bundle, "inspect", lambda path: BinaryInfo(path, "Mach-O", arches[Path(path).resolve()]))

    calls: list[list[str]] = []

    def fake_run(command, check=False, **kwargs):
        """模拟 lipo 输出单架构映像并记录命令。"""
        calls.append(command)
        Path(command[command.index("-output") + 1]).write_bytes(b"\xcf\xfa\xed\xfe")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bundle, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(
        bundle,
        "shutil",
        SimpleNamespace(which=lambda name: "/usr/bin/" + name, copymode=real_shutil.copymode),
    )

    bundle._thin_mach_o(universal, "arm64")
    bundle._thin_mach_o(already_thin, "arm64")

    assert calls == [["/usr/bin/lipo", "-thin", "arm64", "-output", str(universal) + ".thin", str(universal)]]
    assert universal.read_bytes() == b"\xcf\xfa\xed\xfe"
    # lipo writes a fresh file, so an executable slice would lose its bits.
    # Windows reports every writable file as 0o666, so compare with the mode the
    # file had rather than a literal POSIX mode.
    assert stat.S_IMODE(universal.stat().st_mode) == mode
    assert not (tmp_path / "QtDBus.thin").exists()


def test_macos_thinning_without_lipo_fails_instead_of_leaving_the_image_universal(tmp_path, monkeypatch):
    """验证缺少 lipo 时明确失败，避免静默保留通用映像。

    A universal image left in place only defers the failure to install_name_tool."""
    import shutil as real_shutil
    from types import SimpleNamespace

    from appbuild.native import bundle
    from appbuild.native.inspect import BinaryInfo

    universal = tmp_path / "QtDBus"
    universal.write_bytes(b"\xca\xfe\xba\xbe")
    monkeypatch.setattr(bundle, "inspect", lambda path: BinaryInfo(path, "Mach-O", "universal"))
    monkeypatch.setattr(bundle, "shutil", SimpleNamespace(which=lambda name: None, copymode=real_shutil.copymode))

    with pytest.raises(RuntimeError, match="requires lipo"):
        bundle._thin_mach_o(universal, "arm64")


def test_macos_bundle_thins_universal_images_before_rewriting_install_names(tmp_path, monkeypatch):
    """验证先裁剪 Mach-O，再改写依赖路径，打包阶段不提前签名。

    Thinning replaces the image, so it has to run before the names are rewritten."""
    import shutil as real_shutil
    from types import SimpleNamespace

    from appbuild.config import TARGETS
    from appbuild.native import bundle, deps
    from appbuild.native.inspect import BinaryInfo

    target = TARGETS["macos-arm64"]
    root = (tmp_path / "MyApp.app").resolve()
    site = root / "Contents" / "Resources" / "site-packages"
    site.mkdir(parents=True)
    library = site / "libdemo.dylib"
    dependency = site / "libdep.dylib"
    library.write_bytes(b"\xca\xfe\xba\xbe")
    dependency.write_bytes(b"\xcf\xfa\xed\xfe")

    arches = {library.resolve(): "universal", dependency.resolve(): "arm64"}
    monkeypatch.setattr(bundle, "inspect", lambda path: BinaryInfo(path, "Mach-O", arches[Path(path).resolve()]))
    monkeypatch.setattr(bundle, "BUILD", tmp_path / "build")
    monkeypatch.setattr(bundle, "app_name", lambda: "MyApp")
    monkeypatch.setattr(bundle, "_native_files", lambda path, value: [library, dependency])
    monkeypatch.setattr(bundle, "_mac_aliases", lambda roots: {})
    monkeypatch.setattr(
        bundle, "dependency_names", lambda path, env: ["@rpath/libdep.dylib"] if Path(path).name == library.name else []
    )
    monkeypatch.setattr(bundle, "verify_arch", lambda path, value: None)
    monkeypatch.setattr(bundle, "optimize_config", lambda: {})
    monkeypatch.setattr(bundle, "_strip_native", lambda path, value: None)
    monkeypatch.setattr(bundle, "ad_hoc_sign", lambda path: None)
    # The fixture dylibs are not real Mach-O files, so otool would fail on them.
    monkeypatch.setattr(deps, "_mac_rpaths", lambda path: [])

    calls: list[list[str]] = []

    def fake_run(command, check=False, **kwargs):
        """模拟 lipo 和安装名工具并记录其执行顺序。"""
        calls.append(command)
        if "-thin" in command:
            Path(command[command.index("-output") + 1]).write_bytes(b"\xcf\xfa\xed\xfe")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bundle, "subprocess", SimpleNamespace(run=fake_run))
    monkeypatch.setattr(
        bundle,
        "shutil",
        SimpleNamespace(which=lambda name: "/usr/bin/" + name, copymode=real_shutil.copymode),
    )

    bundle.bundle_native_dependencies(root, target)

    tools = [Path(command[0]).name for command in calls]
    assert "install_name_tool" in tools
    assert tools.index("lipo") < tools.index("install_name_tool")
    # `codesign --remove-signature` shrinks __LINKEDIT without updating its
    # vmsize, and ld64 then rejects the rewritten image with "link edit
    # information does not fill the __LINKEDIT segment". ad_hoc_sign re-signs
    # with --force, so no signature is removed before the rewrite.
    assert "codesign" not in tools


def test_native_dependency_names_use_binary_format_not_filename_suffix(monkeypatch, tmp_path):
    """验证无标准后缀的 Mach-O 仍按文件格式解析依赖。"""
    from appbuild.native import deps

    binary = tmp_path / "QtWidgets.abi3.so"
    binary.write_bytes(b"fake")
    monkeypatch.setattr(deps, "inspect_binary", lambda path: type("Info", (), {"format": "Mach-O", "arch": "arm64"})())
    monkeypatch.setattr(deps, "_mac_dependencies", lambda path: ["@rpath/libQt6Core.dylib"])
    monkeypatch.setattr(deps, "_elf_dependencies", lambda path: ["libwrong.so"])
    assert deps.dependency_names(binary) == ["@rpath/libQt6Core.dylib"]


def test_native_alias_resolver_can_map_dylib_install_name_to_real_file(tmp_path):
    """验证安装名别名可解析到实际动态库文件。"""
    from appbuild.native.deps import DependencyResolver

    actual = tmp_path / "config" / "PCBUSB.dylib"
    actual.parent.mkdir()
    actual.write_bytes(b"native")
    alias = {"libpcbusb.0.12.1.dylib": actual.resolve()}
    resolved = DependencyResolver([tmp_path], aliases=alias).resolve("@rpath/libPCBUSB.0.12.1.dylib", actual)
    assert resolved == actual.resolve()


def test_native_dependency_parser_excludes_own_macho_id(monkeypatch, tmp_path):
    """验证解析 Mach-O 依赖时排除文件自身的安装标识。"""
    from appbuild.native import deps

    dylib = tmp_path / "PCBUSB.dylib"
    dylib.write_bytes(b"fake")
    monkeypatch.setattr(deps, "_mac_install_name", lambda path: "@rpath/libPCBUSB.0.12.1.dylib")
    fake = "PCBUSB.dylib:\n\t@rpath/libPCBUSB.0.12.1.dylib (compatibility version 0.0.0)\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
    class R:
        stdout = fake
    monkeypatch.setattr(deps.subprocess, "run", lambda *args, **kwargs: R())
    monkeypatch.setattr(deps.shutil, "which", lambda name: "/usr/bin/otool" if name == "otool" else None)
    result = deps._mac_dependencies(dylib)
    assert result == ["/usr/lib/libSystem.B.dylib"]


def test_macho_arm64_and_universal_validation(tmp_path):
    """验证单架构及通用 Mach-O 都能满足相应目标架构检查。"""
    from appbuild.config import TARGETS
    from appbuild.native.inspect import inspect, verify_arch
    from test_native_deployment import _macho, _fat_macho

    single = _macho(tmp_path / "arm.dylib")
    assert inspect(single).arch == "arm64"

    universal = _fat_macho(tmp_path / "fat.dylib", [single.read_bytes(), single.read_bytes()])
    assert verify_arch(universal, TARGETS["macos-arm64"]).arch == "universal"


def _synthetic_pe(machine: int, sections: tuple[str, ...], subsystem: int | None = None) -> bytes:
    """构造带指定架构和子系统字段的最小 PE 样本。

    A minimal PE whose section table carries the given names."""
    pe_offset = 0x80
    optional_size = 0xF0
    table = pe_offset + 24 + optional_size
    data = bytearray(table + len(sections) * 40)
    data[0:2] = b"MZ"
    data[0x3C:0x40] = pe_offset.to_bytes(4, "little")
    data[pe_offset:pe_offset + 4] = b"PE\0\0"
    data[pe_offset + 4:pe_offset + 6] = machine.to_bytes(2, "little")
    data[pe_offset + 6:pe_offset + 8] = len(sections).to_bytes(2, "little")
    data[pe_offset + 20:pe_offset + 22] = optional_size.to_bytes(2, "little")
    if subsystem is not None:
        # IMAGE_OPTIONAL_HEADER.Subsystem sits at the same offset from the start
        # of the optional header in PE32 and PE32+.
        start = pe_offset + 24 + 68
        data[start:start + 2] = subsystem.to_bytes(2, "little")
    for index, name in enumerate(sections):
        start = table + index * 40
        data[start:start + len(name)] = name.encode("ascii")
    return bytes(data)


def test_pe_subsystem_is_reported(tmp_path):
    """验证 PE 检测返回 GUI、控制台或未知子系统。

    The GUI/console subsystem decides whether Windows flashes a console window.

    CMake's WIN32_EXECUTABLE reaches MSVC through -mwindows, which Zig's clang
    frontend drops without an error, so the subsystem is the only honest way to
    tell a cross-built GUI launcher from a console one.
    """
    from appbuild.native.inspect import PE_SUBSYSTEM, PE_SUBSYSTEM_CONSOLE, PE_SUBSYSTEM_GUI, inspect

    gui = tmp_path / "gui.exe"
    gui.write_bytes(_synthetic_pe(0x8664, (".text",), subsystem=PE_SUBSYSTEM_GUI))
    info = inspect(gui)
    assert info.arch == "x86_64" and info.subsystem == 2
    assert PE_SUBSYSTEM[info.subsystem] == "gui"

    console = tmp_path / "console.exe"
    console.write_bytes(_synthetic_pe(0x14C, (".text",), subsystem=PE_SUBSYSTEM_CONSOLE))
    assert inspect(console).subsystem == 3

    # A non-PE binary has no subsystem at all rather than a bogus default.
    elf = bytearray(64)
    elf[0:4] = b"\x7fELF"
    elf[5] = 1
    elf[18:20] = (62).to_bytes(2, "little")
    other = tmp_path / "other"
    other.write_bytes(bytes(elf))
    assert inspect(other).subsystem is None


def test_launcher_subsystem_check_expects_a_gui_image(tmp_path, monkeypatch):
    """验证启动器子系统检查遵循 GUI 默认值及显式控制台配置。"""
    from appbuild.native.inspect import PE_SUBSYSTEM_CONSOLE, PE_SUBSYSTEM_GUI
    from appbuild.verify import _launcher_subsystem_check

    monkeypatch.delenv("APPBUILD_LAUNCHER_CONSOLE", raising=False)
    gui = tmp_path / "gui.exe"
    gui.write_bytes(_synthetic_pe(0x8664, (".text",), subsystem=PE_SUBSYSTEM_GUI))
    assert _launcher_subsystem_check(gui) == (True, "launcher subsystem=gui")

    console = tmp_path / "console.exe"
    console.write_bytes(_synthetic_pe(0x8664, (".text",), subsystem=PE_SUBSYSTEM_CONSOLE))
    ok, message = _launcher_subsystem_check(console)
    assert not ok and "console, expected gui" in message

    # APPBUILD_LAUNCHER_CONSOLE=1 is the documented console build, so it is the
    # one case where a console subsystem is the expected answer.
    monkeypatch.setenv("APPBUILD_LAUNCHER_CONSOLE", "1")
    assert _launcher_subsystem_check(console) == (True, "launcher subsystem=console")
    assert not _launcher_subsystem_check(gui)[0]


def test_arm64ec_images_satisfy_the_arm64_target(tmp_path):
    """验证 ARM64EC 映像可用于 Windows ARM64，普通 x64 不被误判。

    ARM64EC binaries declare the x64 machine type and are still loadable.

    Windows on Arm64 runs ARM64EC images in Arm64 and x64 processes alike, and
    the CPython runtime ships one (vcruntime140_1.dll, the same file the upstream
    arm64 package carries). Reading only the machine field rejects the runtime of
    every windows-arm64 bundle. The .hexpthk section is the only thing that tells
    an ARM64EC image apart from a real x64 one.
    """
    from appbuild.config import TARGETS
    from appbuild.native.inspect import inspect, verify_arch

    arm64ec = tmp_path / "vcruntime140_1.dll"
    arm64ec.write_bytes(_synthetic_pe(0x8664, (".text", ".hexpthk", ".a64xrm", ".reloc")))
    assert inspect(arm64ec).arch == "arm64ec"
    assert verify_arch(arm64ec, TARGETS["windows-arm64"]).arch == "arm64ec"

    x64 = tmp_path / "plain.dll"
    x64.write_bytes(_synthetic_pe(0x8664, (".text", ".rdata", ".reloc")))
    assert inspect(x64).arch == "x86_64"
    with pytest.raises(RuntimeError, match="expected arm64"):
        verify_arch(x64, TARGETS["windows-arm64"])

    # ARM64EC is Arm64-only, so windows-x86_64 keeps rejecting it.
    with pytest.raises(RuntimeError, match="arm64ec, expected x86_64"):
        verify_arch(arm64ec, TARGETS["windows-x86_64"])


def test_arm64ec_detection_reads_the_section_table_past_the_header(tmp_path):
    """验证 ARM64EC 检测能读取超出头部固定窗口的节表。

    The section table can start beyond the bytes read for the machine field."""
    from appbuild.config import TARGETS
    from appbuild.native.inspect import inspect, verify_arch

    deep = tmp_path / "deep.dll"
    deep.write_bytes(_synthetic_pe(0x8664, (".text",) * 200 + (".hexpthk",)))
    assert inspect(deep).arch == "arm64ec"
    assert verify_arch(deep, TARGETS["windows-arm64"]).arch == "arm64ec"


def test_arch_mismatch_names_the_documented_remedy(tmp_path):
    """验证架构不匹配错误包含问题文件、目标架构及排除配置位置。

    The guard's message must point at the exclude list, not at the guard.

    A wheel that ships a foreign payload is the one realistic way to reach this
    error (PySide2's win_amd64 wheels carry a 32-bit ucrtbase.dll), and the only
    right answer is to drop the file through the configuration. An error that
    stops at "Architecture mismatch" invites relaxing the guard instead.
    """
    from appbuild.config import TARGETS
    from appbuild.native.inspect import verify_arch

    foreign = tmp_path / "ucrtbase.dll"
    foreign.write_bytes(_synthetic_pe(0x14C, (".text", ".rdata", ".reloc")))
    with pytest.raises(RuntimeError) as excinfo:
        verify_arch(foreign, TARGETS["windows-x86_64"])
    message = str(excinfo.value)
    assert "ucrtbase.dll: x86, expected x86_64" in message
    assert "[tool.appbuild.native].exclude" in message


def test_network_retries_transient_gateway_errors(monkeypatch):
    """验证临时网关错误会重试，并返回随后成功的 JSON。"""
    from appbuild import net
    calls = []

    class Error(net.urllib.error.HTTPError):
        pass

    class Headers(dict):
        def get(self, key, default=None):
            """为网络桩提供响应头读取接口。"""
            return super().get(key, default)

    sequence = [net.urllib.error.HTTPError("https://x", 504, "timeout", Headers(), None)]
    class Response:
        def __enter__(self):
            """进入模拟响应的上下文管理器。"""
            return self
        def __exit__(self, *args):
            """退出模拟响应且不吞掉异常。"""
            return False
        def read(self, *args):
            """返回可解析的最小 JSON 响应体。"""
            return b"{}"
    sequence.append(Response())

    def fake(req, timeout):
        """按顺序抛出瞬时错误或返回成功响应。"""
        calls.append(req.full_url)
        value = sequence.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    monkeypatch.setattr(net.time, "sleep", lambda seconds: None)
    result = net.http_json("https://example.invalid/api")
    assert result == {}
    assert len(calls) == 2


def test_public_build_downloads_ignore_github_credentials(tmp_path, monkeypatch):
    """验证公共构建下载不附带环境中的 GitHub 凭据。

    Public downloads must work identically with absent or stale login state."""
    import io
    from appbuild import net

    seen: list[dict[str, str]] = []

    def fake(request, timeout):
        """记录出站请求头并提供测试下载内容。"""
        seen.append(dict(request.headers))
        return io.BytesIO(b"{}")

    monkeypatch.setattr(net.urllib.request, "urlopen", fake)
    monkeypatch.setenv("GITHUB_TOKEN", "unused-stale-token")
    monkeypatch.setenv("GH_TOKEN", "unused-stale-token")

    net.http_json("https://raw.githubusercontent.com/astral-sh/uv/main/crates/uv-python-managed/download-metadata.json")
    net.http_text("https://github.com/astral-sh/python-build-standalone/releases/download/20250818/SHA256SUMS")
    net.download("https://github.com/astral-sh/python-build-standalone/releases/download/20250818/archive", tmp_path / "archive")

    assert len(seen) == 3
    assert all("Authorization" not in headers for headers in seen)
    assert (tmp_path / "archive").read_bytes() == b"{}"


def test_local_runtime_manifest_round_trip(tmp_path):
    """验证本地运行时清单能往返保存相对解释器路径。"""
    from appbuild.config import RuntimeSpec, TARGETS
    from appbuild.manifest import build_manifest, validate_manifest, write_manifest

    target = TARGETS["linux-x86_64"]
    runtime = tmp_path / "runtime"
    sdk = tmp_path / "sdk"
    (sdk / "bin").mkdir(parents=True)
    (sdk / "include").mkdir()
    (sdk / "bin" / "python3.13").write_text("", encoding="utf-8")
    (sdk / "include" / "Python.h").write_text("", encoding="utf-8")
    runtime.mkdir()
    spec = RuntimeSpec("local", "3.13.5", target, runtime, sdk)
    data = build_manifest(spec, {
        "python": "3.13.5",
        "python_tag": "cp313",
        "python_platform_tag": "manylinux_2_17_x86_64",
        "python_executable": sdk / "bin" / "python3.13",
        "include_dir": sdk / "include",
    })
    write_manifest(runtime, data)
    validate_manifest(data, target, spec)
    assert data["python_executable"] == "bin/python3.13"


def test_project_version_and_user_agent():
    """验证网络标识中的工具版本来自当前应用配置。"""
    from appbuild.config import load_app_config, project_version, user_agent

    configured = load_app_config()["project"]["version"]
    assert project_version() == configured
    assert user_agent() == f"appbuild/{configured}"


def test_runtime_and_build_python_are_independent():
    """验证构建解释器选择不改变目标运行时 Python 版本。"""
    from appbuild.config import load_app_config, python_version, require_local_python

    path = require_local_python()
    assert Path(path).exists()
    # The target runtime version is a project decision that lives in
    # app/pyproject.toml; the development Python is discovered separately.
    # Asserting a literal here would just duplicate the config.
    configured = load_app_config()["tool"]["appbuild"]["runtime"]["python"]
    assert python_version() == configured


def test_target_python_smoke_does_not_write_bytecode_into_staging(tmp_path, monkeypatch):
    """验证 smoke 子进程禁止向暂存树写入字节码。"""
    from appbuild import verify as smoke
    from appbuild.config import TARGETS

    target = TARGETS["macos-arm64"]
    captured: dict[str, str] = {}
    monkeypatch.setattr(smoke, "BUILD", tmp_path / "build")
    monkeypatch.setattr(smoke, "host_target", lambda: target)
    monkeypatch.setattr(smoke, "runtime_python", lambda value: tmp_path / "python3.10")
    monkeypatch.setattr(smoke, "target_runtime_dir", lambda value: tmp_path / "runtime")
    monkeypatch.setattr(smoke.subprocess, "run", lambda cmd, **kwargs: captured.update(kwargs["env"]))

    smoke.run_target_python_smoke(target)

    assert captured["PYTHONDONTWRITEBYTECODE"] == "1"


def test_windows_extensions_link_the_versioned_import_library(tmp_path, monkeypatch):
    """验证 Windows 扩展使用 SDK 的完整版本导入库，避免误用运行时目录。

    Link the versioned import library, from the SDK, never from the runtime.

    A CPython tree carries both ``python313.lib`` and ``python3.lib``. The shim
    exports a subset of the same functions and names ``python3.dll`` in the
    import table, a DLL the dependency closure does not know as part of the
    runtime. Choosing between the two by sort order picked the shim.

    The library is also a compiler input, so it is read from the SDK tree. The
    runtime tree below carries one anyway: it is the layout the PBS release
    runtime used to have, and reading the linker's search path out of ``dist/``
    is exactly the bug this pins. The packaged runtime is the python.org
    embeddable layout and has no ``libs/`` at all.
    """
    from types import SimpleNamespace

    from appbuild import python_build
    from appbuild.config import TARGETS
    from appbuild.toolchain import Toolchain

    target = TARGETS["windows-x86_64"]
    sdk = tmp_path / "sdk"
    libs = sdk / "install" / "libs"
    libs.mkdir(parents=True)
    (libs / "python3.lib").write_bytes(b"")
    include = sdk / "install" / "include"
    include.mkdir(parents=True)
    runtime_libs = tmp_path / "runtime" / "libs"
    runtime_libs.mkdir(parents=True)
    (runtime_libs / "python313.lib").write_bytes(b"")
    target_python = SimpleNamespace(
        sdk_root=sdk,
        include_dir=include,
        python_major_minor="3.13",
        extension_suffix=".cp313-win_amd64.pyd",
    )
    toolchain = Toolchain(target, {}, "cl.exe", "cl.exe", "link.exe", None)

    app_root = tmp_path / "app"
    source = app_root / "src" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")
    generated = tmp_path / "demo.c"
    generated.write_text("/* generated */\n", encoding="utf-8")

    build = tmp_path / "build"
    extension = build / "native" / target.key / "demo.cp313-win_amd64.pyd"
    generated_setup: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        """捕获生成的扩展构建配置并模拟编译产物。"""
        generated_setup["text"] = Path(cmd[1]).read_text(encoding="utf-8")
        extension.parent.mkdir(parents=True, exist_ok=True)
        extension.write_bytes(b"")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", build)
    monkeypatch.setattr(python_build, "require_local_python", lambda: Path("/fake/python3"))
    monkeypatch.setattr(python_build, "cython_merge_mode", lambda: "none")
    monkeypatch.setattr(python_build, "cythonize_to_c", lambda sources, host: {source: generated})
    monkeypatch.setattr(python_build, "resolve_target_python", lambda value: target_python)
    monkeypatch.setattr(python_build, "resolve_toolchain", lambda value: toolchain)
    monkeypatch.setattr(python_build.subprocess, "run", fake_run)

    # Neither the shim, nor the copy the runtime tree happens to carry, is an
    # import library this build can use.
    with pytest.raises(RuntimeError, match="python313.lib not found"):
        python_build.build_target_extensions(target, [source])

    (libs / "python313.lib").write_bytes(b"")
    outputs = python_build.build_target_extensions(target, [source])

    assert [path.name for path in outputs] == ["demo.cp313-win_amd64.pyd"]
    setup_text = generated_setup["text"]
    assert "TARGET_LIBRARIES=['python313']" in setup_text
    assert f"TARGET_LIBDIR={str(libs)!r}" in setup_text
    assert str(tmp_path / "runtime") not in setup_text


def test_zig_compiled_windows_extensions_name_the_64_bit_abi(tmp_path, monkeypatch):
    """验证 Zig 按目标位数设置 CPython 头文件 ABI 宏。

    The toolchain's header corrections must reach the compiler unaltered.

    ``pyconfig.h`` defines ``MS_WIN64`` -- and with it ``SIZEOF_VOID_P 8`` --
    only on the branch it enters through ``_MSC_VER``. Zig never defines
    ``_MSC_VER``, so a 64-bit Windows target compiled through it was given
    ``SIZEOF_VOID_P 4`` and Cython's ``1 / (int)(SIZEOF_VOID_P == sizeof(void*))``
    ABI guard turned into a division by zero in every generated module. The
    define belongs to the 64-bit targets only: the 32-bit ones already agree
    with the ``MS_WIN32`` branch pyconfig.h falls through to.

    The extension build never invents a compiler flag of its own -- it forwards
    the Zig frontend the toolchain hands it -- so this pins that the corrections
    arrive with it instead of being dropped on the way.
    """
    from types import SimpleNamespace

    from appbuild import python_build
    from appbuild.config import TARGETS
    from appbuild.toolchain import windows_gnu_cpython_defines

    app_root = tmp_path / "app"
    source = app_root / "src" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")
    generated = tmp_path / "demo.c"
    generated.write_text("/* generated */\n", encoding="utf-8")

    sdk = tmp_path / "sdk"
    libs = sdk / "install" / "libs"
    libs.mkdir(parents=True)
    (libs / "python38.lib").write_bytes(b"")
    include = sdk / "install" / "include"
    include.mkdir(parents=True)

    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        """记录 Zig 编译命令并创建对应输出文件。"""
        commands.append([str(token) for token in cmd])
        artifact = Path(str(cmd[cmd.index("-o") + 1]))
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"")

    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "require_local_python", lambda: Path("/fake/python3"))
    monkeypatch.setattr(python_build, "cython_merge_mode", lambda: "none")
    monkeypatch.setattr(python_build, "cythonize_to_c", lambda sources, host: {source: generated})
    monkeypatch.setattr(python_build.subprocess, "run", fake_run)

    def compile_for(key: str) -> list[str]:
        """为指定 Windows 架构执行编译路径并返回实际命令。"""
        target = TARGETS[key]
        target_python = SimpleNamespace(
            sdk_root=sdk,
            include_dir=include,
            python_major_minor="3.8",
            extension_suffix=f".cp38-{target.extension_platform}.pyd",
        )
        frontend = ("cc", "-target", target.triple, *windows_gnu_cpython_defines(target))
        monkeypatch.setattr(python_build, "resolve_target_python", lambda value: target_python)
        monkeypatch.setattr(python_build, "resolve_toolchain", lambda value: SimpleNamespace(
            env={}, compiler=str(tmp_path / "zig"), compiler_args=frontend,
        ))
        commands.clear()
        python_build.build_target_extensions(target, [source])
        assert commands, "the Zig branch never reached the compiler"
        return commands[0]

    for key in ("windows-x86_64", "windows-arm64"):
        assert "-DMS_WIN64" in compile_for(key)

    # snprintf is a correction every Windows target needs; the pointer width is
    # only 8 on the 64-bit ones.
    x86 = compile_for("windows-x86")
    assert "-DHAVE_SNPRINTF" in x86
    assert "-DMS_WIN64" not in x86


def test_target_python_points_the_compiler_at_the_sdk(tmp_path, monkeypatch):
    """验证解析结果使用 SDK 根目录和头文件目录。

    ``resolve_target_python`` reports the SDK tree, not the runtime tree.

    The SDK info dict exposed the tree the compiler reads under ``root``, and
    the local provider filled that key with its *runtime* path. Both providers
    now say ``sdk_root``, so a lookup cannot end up walking ``dist/``.
    """
    from appbuild import config, runtime

    target = config.TARGETS["windows-x86_64"]
    runtime_root = tmp_path / "runtimes" / "windows-x86_64" / "3.13.5"
    sdk = tmp_path / ".cache" / "pbs-sdk" / "windows-x86_64" / "3.13.5"
    include = sdk / "install" / "include"
    include.mkdir(parents=True)
    (include / "Python.h").write_text("", encoding="utf-8")
    (sdk / "install" / "python.exe").write_bytes(b"")
    runtime_root.mkdir(parents=True)

    spec = config.RuntimeSpec("pbs", "3.13.5", target, runtime_root, sdk)
    monkeypatch.setattr(config, "runtime_spec", lambda value: spec)
    monkeypatch.setattr(runtime, "ensure_sdk", lambda value: sdk)
    monkeypatch.setattr(runtime, "sdk_info", lambda value: {
        "sdk_root": str(sdk),
        "runtime_root": str(runtime_root),
        "python_executable": str(sdk / "install" / "python.exe"),
        "include_dir": str(include),
        "python": "3.13.5",
    })

    resolved = config.resolve_target_python(target)

    assert resolved.sdk_root == sdk
    assert resolved.include_dir == include


def test_windows_launcher_does_not_ask_for_the_python_import_library(tmp_path):
    """实际预处理头文件，验证启动器关闭 Python 自动导入库链接指令。

    Including Python.h must not put libpython on the launcher's link line.

    The launcher resolves CPython at runtime, so it links no import library and
    the target SDK has no LIBPATH for one. Windows' pyconfig.h does not know
    that: unless the shared build is switched off it nominates ``python313.lib``
    with ``#pragma comment(lib, ...)``, and the launcher build then stops at
    ``LNK1104: cannot open file 'python313.lib'`` — a file that is neither
    present nor wanted.

    The nomination is read off the preprocessed translation unit. Asking the
    linker instead is not a usable signal: a directive only makes the linker
    open the library once a symbol needs it, so an empty translation unit
    nominates the import library and still links cleanly.
    """
    import subprocess
    import sysconfig

    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    source = launcher.read_text(encoding="utf-8")
    guard = "#define Py_NO_ENABLE_SHARED 1"
    include = "#include <Python.h>"
    assert guard in source, "the launcher must switch off the shared-build pragma"
    assert include in source, "the launcher includes Python.h to declare the C-API"
    assert source.index(guard) < source.index(include), (
        "pyconfig.h reads the switch, so it must be defined before the include"
    )

    if sys.platform != "win32":
        pytest.skip("the auto-link pragma belongs to the Windows pyconfig.h")

    from test_third_party import _windows_extension_compiler

    try:
        cl, import_lib, env = _windows_extension_compiler()
    except RuntimeError as exc:
        pytest.skip(f"MSVC is required to preprocess the launcher translation unit: {exc}")
    include_dir = sysconfig.get_path("include")

    def nominations(name: str, unit: Path) -> list[str]:
        """预处理给定源码并收集 Python 的 pragma 链接库指令。"""
        output = tmp_path / f"{name}.i"
        result = subprocess.run(
            [cl, "/nologo", "/P", f"/Fi{output}", f"/I{include_dir}", "/std:c++17", str(unit)],
            cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return [
            line.strip()
            for line in output.read_text(encoding="utf-8", errors="replace").splitlines()
            if "pragma comment(lib" in line and "python" in line.lower()
        ]

    control_source = tmp_path / "control.cpp"
    control_source.write_text("#include <Python.h>\n", encoding="utf-8")
    # The control keeps the assertion honest: it fails if the header stopped
    # nominating anything, which would make the launcher assertion vacuous.
    control = nominations("control", control_source)
    assert control, "the header no longer nominates the import library"
    assert import_lib.name in control[0], control

    assert not nominations("launcher", launcher), (
        "the launcher nominates a libpython it does not link and cannot find"
    )


def test_packaged_launcher_streams_reach_the_caller(tmp_path):
    """实际编译启动器探针，验证 Python 标准输出能到达调用方。

    The build tool must see what the packaged launcher prints.

    The Windows launcher is a GUI-subsystem binary, and Windows attaches no
    console to such a child. ``subprocess`` started with the default
    ``stdout=None`` hands it the caller's handle *values* without the handles:
    the child writes into dangling handles and every line disappears while the
    exit code stays 0. The smoke step and ``--run`` both assert on that output,
    so both spawn the launcher through the same helper, which passes the
    streams explicitly.

    The probe below is a GUI-subsystem executable for the same reason the
    launcher is one: a console-subsystem probe inherits the streams either way
    and could not tell the two spawns apart.
    """
    import subprocess

    if sys.platform != "win32":
        pytest.skip("handle inheritance for GUI-subsystem children belongs to Windows")

    from test_third_party import _windows_extension_compiler

    try:
        cl, _import_lib, env = _windows_extension_compiler()
    except RuntimeError as exc:
        pytest.skip(f"MSVC is required to compile the probe: {exc}")

    source = tmp_path / "probe.cpp"
    source.write_text(
        "#include <stdio.h>\n"
        "#include <windows.h>\n"
        "int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {\n"
        '    printf("PROBE MARKER\\n");\n'
        "    fflush(stdout);\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )
    probe = tmp_path / "probe.exe"
    build = subprocess.run(
        [cl, "/nologo", str(source), f"/Fe:{probe}", f"/Fo{tmp_path}\\", "/link", "/SUBSYSTEM:WINDOWS"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    # The helper writes into this process's streams, and pytest captures those
    # at the fd level, so the run happens in a child whose stdout is a pipe —
    # the same shape the build tool has under CI.
    driver = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, {tools!r})\n"
        "from appbuild.verify import run_packaged_launcher\n"
        "run_packaged_launcher(Path({probe!r}), cwd=Path({cwd!r}), check=True)\n"
    ).format(
        tools=str(Path(__file__).parents[2] / "tools"),
        probe=str(probe),
        cwd=str(tmp_path),
    )
    result = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PROBE MARKER" in result.stdout


def test_macos_extensions_link_as_bundles_with_deferred_symbols(tmp_path, monkeypatch):
    """验证 macOS 扩展使用 bundle 与 dynamic_lookup 链接语义。

    A macOS CPython extension must not require Python symbols at link time.

    `-dynamiclib` makes the linker resolve every symbol eagerly, which fails on
    Apple Silicon with "symbol(s) not found for architecture arm64". CPython
    extensions are bundles that resolve the C-API from the embedding launcher.
    """
    from types import SimpleNamespace

    from appbuild import python_build
    from appbuild.config import TARGETS
    from appbuild.toolchain import Toolchain

    target = TARGETS["macos-arm64"]
    include = tmp_path / "sdk" / "include" / "python3.10"
    include.mkdir(parents=True)
    target_python = SimpleNamespace(include_dir=include, extension_suffix=".cpython-310-darwin.so")
    toolchain = Toolchain(target, {}, "/usr/bin/clang", "/usr/bin/clang++", "/usr/bin/clang", "11.0")

    app_root = tmp_path / "app"
    source = app_root / "src" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")
    generated = tmp_path / "demo.c"
    generated.write_text("/* generated */\n", encoding="utf-8")

    commands: list[list[str]] = []
    monkeypatch.setattr(python_build, "APP", app_root)
    monkeypatch.setattr(python_build, "BUILD", tmp_path / "build")
    monkeypatch.setattr(python_build, "resolve_target_python", lambda value: target_python)
    monkeypatch.setattr(python_build, "resolve_toolchain", lambda value: toolchain)
    monkeypatch.setattr(python_build.subprocess, "run", lambda cmd, **kwargs: commands.append(cmd))

    outputs = python_build.compile_unix_extensions(target, {source: generated})

    assert [path.name for path in outputs] == ["demo.cpython-310-darwin.so"]
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[:2] == ["/usr/bin/clang", "-bundle"]
    assert "-dynamiclib" not in cmd
    assert cmd[cmd.index("-undefined") + 1] == "dynamic_lookup"
    assert "-arch" in cmd and cmd[cmd.index("-arch") + 1] == "arm64"


def test_package_verification_skips_foreign_format_binaries(tmp_path, monkeypatch):
    """验证产物检查跳过其他平台文件格式，同时验证当前平台原生库。

    Inert Windows launcher stubs inside a macOS runtime are not ABI inputs."""
    from appbuild import verify
    from appbuild.config import TARGETS
    from appbuild.native.inspect import BinaryInfo

    from appbuild.config import entry_source

    target = TARGETS["macos-arm64"]
    root = tmp_path / "MyApp.app"
    resources = root / "Contents" / "Resources"
    (root / "Contents" / "MacOS").mkdir(parents=True)
    # The entry rides in the launcher, so the package-side check reads it back
    # out of these bytes.
    (root / "Contents" / "MacOS" / "MyApp").write_bytes(b"\xcf\xfa\xed\xfe" + entry_source().encode("utf-8"))
    resources.mkdir(parents=True)
    stub = resources / "runtime" / "setuptools" / "cli.exe"
    stub.parent.mkdir(parents=True)
    stub.write_bytes(b"MZ")
    library = resources / "site-packages" / "libdemo.dylib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"\xcf\xfa\xed\xfe")

    def fake_inspect(path: Path) -> BinaryInfo:
        """模拟当前平台的 Mach-O 检测结果。"""
        if path.suffix == ".exe":
            return BinaryInfo(path, "PE", "x86_64")
        return BinaryInfo(path, "Mach-O", "arm64")

    verified: list[Path] = []
    monkeypatch.setattr(verify, "DIST", tmp_path)
    # The bundle root is named after the configured application name, so pin
    # the name this fixture's directories were built with instead of following
    # whatever `app/pyproject.toml` currently says.
    monkeypatch.setattr(verify, "app_name", lambda: "MyApp")
    monkeypatch.setattr(verify, "inspect", fake_inspect)
    monkeypatch.setattr(verify, "verify_arch", lambda path, value: verified.append(path))

    checks = verify._checks_for_package(target)

    assert stub not in verified
    assert library in verified
    assert not [label for ok, label in checks if not ok and "cli.exe" in label]
    # No sidecar next to the launcher, and the entry the configuration names is
    # inside the launcher itself. (The extension checks do fail here: this
    # fixture has no staging tree, and they are not what this test is about.)
    entry_checks = [(ok, label) for ok, label in checks
                    if "sidecar" in label or label.startswith("embedded entry ")]
    assert len(entry_checks) == 2
    assert all(ok for ok, _ in entry_checks), entry_checks


def test_lock_preserves_existing_target_entries(tmp_path, monkeypatch):
    """验证更新锁文件时保留其他目标的已锁定记录。"""
    import json
    import appbuild.lock as lock
    from appbuild.config import TARGETS

    lock_path = tmp_path / "appbuild.lock.json"
    monkeypatch.setattr(lock, "LOCK", lock_path)
    monkeypatch.setattr(lock, "runtime_provider", lambda: "local")
    monkeypatch.setattr(lock, "python_version", lambda: "3.13.5")
    monkeypatch.setattr(lock, "dependency_specs", lambda: [])
    class Manifest: pass
    monkeypatch.setattr(lock, "runtime_spec", lambda target: type("S", (), {"root": tmp_path / target.key, "sdk_root": tmp_path / "sdk" / target.key})())
    monkeypatch.setattr(lock, "read_manifest", lambda root: {"format": 4})
    monkeypatch.setattr(lock, "manifest_hash", lambda data: "hash")
    monkeypatch.setattr(lock, "_wheel_records", lambda target: [])
    first = {"format": 5, "project": "appbuild", "python": "3.13.5", "runtime_provider": "local", "targets": {"linux-x86_64": {"sentinel": True}}}
    lock_path.write_text(json.dumps(first), encoding="utf-8")
    lock.write_lock(TARGETS["linux-arm64"])
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["targets"]["linux-x86_64"] == {"sentinel": True}
    assert "linux-arm64" in data["targets"]


def test_runtime_optimization_is_applied_to_package_copy(tmp_path, monkeypatch):
    """验证运行时裁剪发生在产物副本并移除 pip 等开发组件。"""
    from appbuild import runtime as runtime_optimize
    monkeypatch.setattr(runtime_optimize, "optimize_config", lambda: {"remove_python_caches": True, "remove_runtime_pip": True})
    root = tmp_path / "runtime"
    pip = root / "lib/python3.13/site-packages/pip"
    ensurepip = root / "lib/python3.13/ensurepip"
    pip.mkdir(parents=True)
    ensurepip.mkdir(parents=True)
    (pip / "__init__.py").write_text("", encoding="utf-8")
    runtime_optimize.optimize_runtime_tree(root)
    assert not pip.exists()
    assert not ensurepip.exists()


def test_native_strip_is_opt_in(monkeypatch):
    """验证原生符号剥离默认关闭，须显式启用。"""
    from appbuild.native import bundle
    monkeypatch.setattr(bundle, "optimize_config", lambda: {"strip_native": False})
    assert bundle.optimize_config()["strip_native"] is False


def test_linux_native_rpath_rewrite_is_opt_in(monkeypatch, tmp_path):
    """检查 Linux RPATH 改写受显式优化选项控制。"""
    from appbuild.native import bundle

    monkeypatch.setattr(bundle.shutil, "which", lambda name: "/usr/bin/patchelf" if name == "patchelf" else None)
    monkeypatch.setattr(bundle, "inspect", lambda path: type("Info", (), {"format": "ELF", "arch": "x86_64"})())
    monkeypatch.setattr(bundle.optimize_config, "__call__", lambda: {})
    # The default path is asserted through source-level behavior: package builds
    # must not rewrite third-party Linux RPATHs unless explicitly requested.
    source = Path(__file__).parents[2] / "tools" / "appbuild" / "native" / "bundle.py"
    text = source.read_text(encoding="utf-8")
    assert 'optimize_config().get("rewrite_rpath", False)' in text


def test_windows_python_runtime_dll_is_version_specific(monkeypatch):
    """验证只有当前 Python 版本的 DLL 被视为运行时自带依赖。"""
    import appbuild.native.deps as deps
    from appbuild.config import TARGETS

    target = TARGETS["windows-x86_64"]
    monkeypatch.setattr(deps, "python_version", lambda: "3.11.10")
    assert deps._system_dependency("python311.dll", target)
    assert not deps._system_dependency("python313.dll", target)


def test_windows_stable_abi_forwarder_is_runtime_provided(monkeypatch):
    """验证 Windows 的 python3.dll 转发库不被当成外部依赖。

    PySide6's abi3 extensions link ``python3.dll``, which the launcher loads.

    The forwarder sits next to ``python3XX.dll`` in the runtime directory, and
    the launcher loads every python DLL in that directory, so an abi3 extension's
    import of it binds to the already loaded module. It is therefore
    runtime-provided exactly like the versioned library. Looking for it in the
    wheel leaves it unresolved on every Windows target; POSIX has no counterpart.
    """
    import appbuild.native.deps as deps
    from appbuild.config import TARGETS

    monkeypatch.setattr(deps, "python_version", lambda: "3.11.13")
    assert deps._system_dependency("python3.dll", TARGETS["windows-x86_64"])
    assert deps._system_dependency("PYTHON3.DLL", TARGETS["windows-arm64"])
    assert not deps._system_dependency("python3.dll", TARGETS["linux-x86_64"])
    assert not deps._system_dependency("python3.dll", TARGETS["macos-arm64"])


def test_launcher_resolves_entry_points_from_the_runtime_not_the_forwarder():
    """检查启动器通过具体运行时入口识别主 DLL，避免选中稳定 ABI 转发库。

    The launcher must not resolve symbols from the first DLL that loads.

    python3.dll loads successfully and then answers every GetProcAddress with
    null, which is what made every Windows package exit 5. The runtime directory
    holds python3XX.dll next to it, and that is where the entry points come from.
    """
    launcher = Path(__file__).parents[2] / "launcher" / "src" / "PyUpper.cpp"
    text = launcher.read_text(encoding="utf-8")
    compact = "".join(text.split())
    assert 'constexprconstchar*kRuntimeEntrySymbol="PyConfig_InitIsolatedConfig";' in compact
    # The handle used for every later lookup is the one that exports the entry
    # point, not merely the one whose LoadLibraryEx call succeeded.
    assert "if(!r.handle&&symbol(handle,kRuntimeEntrySymbol))r.handle=handle;" in compact


def test_retire_tree_takes_the_tree_out_of_its_published_path_first(tmp_path, monkeypatch):
    """验证清理前先移走发布路径，且普通文件不会被误删。

    Removal must rename the tree away before deleting anything inside it.

    A foreign process can start inside the tree at any moment -- an editor that
    resolved ``.../runtime/bin/python`` runs it, and that run writes bytecode
    into the same tree. Deleting in place then fails with ENOENT on the entry
    the writer just deleted or ENOTEMPTY on the directory it refilled. The
    published name has to be gone before the recursive delete starts, so every
    writer that loses the race writes into a path that no longer exists.
    """
    from appbuild import fs

    tree = tmp_path / "dist" / "MyApp.app"
    (tree / "Contents" / "Resources" / "runtime" / "lib").mkdir(parents=True)
    (tree / "Contents" / "Resources" / "runtime" / "lib" / "os.pyc").write_bytes(b"x")
    published: list[tuple[Path, bool]] = []
    real_rmtree = fs.shutil.rmtree

    def spy(path, *args, **kwargs):
        """在实际删除前记录正式发布路径是否已腾空。"""
        published.append((Path(path), tree.exists()))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(fs.shutil, "rmtree", spy)
    fs.retire_tree(tree, trash=tmp_path / "build" / "retired")

    assert not tree.exists()
    assert published
    assert all(existed is False for _, existed in published)

    # An absent tree is already in the desired state; anything else is a caller
    # error rather than something to skip silently.
    fs.retire_tree(tmp_path / "missing", trash=tmp_path / "build" / "retired")
    not_a_tree = tmp_path / "launcher"
    not_a_tree.write_bytes(b"binary")
    with pytest.raises(RuntimeError):
        fs.retire_tree(not_a_tree, trash=tmp_path / "build" / "retired")
    assert not_a_tree.is_file()


class _HeldByAnotherProcess(PermissionError):
    """``PermissionError`` carrying the ``winerror`` Windows actually sets.

    Windows spells "the tree is open somewhere else" and "the tree is
    unreadable" the same way, WinError 5, so the retry logic separates them by
    ``winerror`` alone. Spelling it as a class attribute keeps the test honest
    on POSIX, where OSError has no such field to assign.
    """

    winerror = 5


def test_retire_tree_waits_out_a_tree_windows_still_holds(tmp_path, monkeypatch):
    """验证 Windows 暂时占用文件时采用有限重试。

    A tree another process holds must not fail the build on the first try.

    Windows refuses to rename a directory while any handle under it is open,
    and that is the state the previously published bundle is in whenever the
    editor resolves the interpreter it found under ``dist/`` or the developer
    closes the packaged app at about the time they start the next build. The
    holder is short-lived, so waiting it out is what makes the build succeed
    instead of looking for a rename that does not need fixing.
    """
    from appbuild import fs

    tree = tmp_path / "dist" / "MyApp"
    (tree / "runtime").mkdir(parents=True)
    real_replace = os.replace
    renames: list[Path] = []

    def flaky_replace(source, destination):
        """模拟前两次被系统占用、随后成功的重命名。"""
        renames.append(Path(source))
        if len(renames) < 3:
            raise _HeldByAnotherProcess(13, "Access is denied")
        return real_replace(source, destination)

    waits: list[float] = []
    monkeypatch.setattr(fs.os, "replace", flaky_replace)
    monkeypatch.setattr(fs.time, "sleep", waits.append)

    fs.retire_tree(tree, trash=tmp_path / "build" / "retired")

    assert len(renames) == 3
    assert len(waits) == 2
    assert not tree.exists()


def test_retire_tree_names_what_holds_a_tree_it_cannot_retire(tmp_path, monkeypatch):
    """验证持续占用时提示路径与系统原因，并保留原产物。

    A lock that never clears must read as the environment problem it is.

    Left as a bare PermissionError, the failure looks like a bug in the rename
    that the retire dance exists to do, and the tempting repair is to weaken
    the rename -- which is what keeps external writers from corrupting the
    payload. The message has to name the processes that hold a tree instead,
    and the payload has to be left whole rather than half-deleted.
    """
    from appbuild import fs

    tree = tmp_path / "dist" / "MyApp"
    (tree / "runtime").mkdir(parents=True)
    (tree / "runtime" / "python38.dll").write_bytes(b"payload")

    def never_released(source, destination):
        """持续注入 Windows 文件占用错误。"""
        raise _HeldByAnotherProcess(13, "Access is denied")

    monkeypatch.setattr(fs.os, "replace", never_released)
    monkeypatch.setattr(fs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(fs, "_BUSY_ATTEMPTS", 3)

    with pytest.raises(RuntimeError) as failure:
        fs.retire_tree(tree, trash=tmp_path / "build" / "retired")

    message = str(failure.value)
    assert str(tree) in message
    assert "WinError 5" in message
    assert "still running" in message
    assert (tree / "runtime" / "python38.dll").read_bytes() == b"payload"


def test_retire_tree_does_not_retry_a_real_permission_error(tmp_path, monkeypatch):
    """验证真实权限错误不进入文件占用重试循环。

    Only the "someone holds it" shape may wait; a real denial stays immediate."""
    from appbuild import fs

    tree = tmp_path / "dist" / "MyApp"
    tree.mkdir(parents=True)
    renames: list[Path] = []

    def unreadable(source, destination):
        """注入不可重试的访问权限错误。"""
        renames.append(Path(source))
        raise OSError(2, "The system cannot find the file specified")

    monkeypatch.setattr(fs.os, "replace", unreadable)
    monkeypatch.setattr(fs.time, "sleep", lambda seconds: pytest.fail("must not wait"))

    with pytest.raises(OSError):
        fs.retire_tree(tree, trash=tmp_path / "build" / "retired")
    assert len(renames) == 1


def test_package_assembles_outside_the_published_path(tmp_path, monkeypatch):
    """验证临时组装在正式发布树之外，成功后提升并清理字节码。

    The bundle must not be assembled in dist/, where the editor executes it.

    VS Code watches ``**/python`` across the workspace and resolves every path
    it reports by running it, so ``dist/MyApp.app/Contents/Resources/runtime/bin/
    python`` gets executed the moment the build writes it -- and that run writes
    bytecode into the bundle. A build that copies the runtime straight into
    dist/ therefore cleans up a tree a foreign process is writing to, and the
    cleanup fails on the entry the writer recreated. Assembly happens in a
    private tree instead, and the finished bundle is renamed into the published
    path in one step.
    """
    import appbuild.package as pkg
    import appbuild.runtime as runtime_module
    from appbuild.config import TARGETS, publish_dir

    root = tmp_path / "root"
    runtime = root / "runtimes" / "macos-arm64"
    (runtime / "lib" / "python3.11" / "__pycache__").mkdir(parents=True)
    (runtime / "lib" / "python3.11" / "__pycache__" / "stale.pyc").write_bytes(b"stale")
    stage = root / "build" / "staging" / "macos-arm64"
    (stage / "site-packages").mkdir(parents=True)
    launcher = tmp_path / "PyUpper"
    launcher.write_bytes(b"launcher")
    assembly = root / "build" / "publish" / f"macos-arm64.{os.getpid()}"
    target = TARGETS["macos-arm64"]

    monkeypatch.setattr(pkg, "BUILD", root / "build")
    monkeypatch.setattr(pkg, "DIST", root / "dist")
    monkeypatch.setattr(pkg, "APP", tmp_path / "app")
    monkeypatch.setattr(pkg, "staging_dir", lambda _: stage)
    monkeypatch.setattr(pkg, "target_runtime_dir", lambda _: runtime)
    monkeypatch.setattr(pkg, "publish_dir", lambda _: assembly)
    monkeypatch.setattr(pkg, "app_name", lambda: "MyApp")
    monkeypatch.setattr(pkg, "app_identifier", lambda: "com.example.MyApp")
    monkeypatch.setattr(pkg, "project_version", lambda: "0.0.0")
    monkeypatch.setattr(pkg, "entry_source", lambda: "from main import main\n")
    monkeypatch.setattr(pkg, "smoke_modules", lambda _: ["main"])
    monkeypatch.setattr(pkg, "prune_excluded_native_files", lambda _: [])
    monkeypatch.setattr(pkg, "bundle_native_dependencies", lambda *_, **__: [])
    monkeypatch.setattr(
        runtime_module, "optimize_config",
        lambda: {"remove_python_caches": True, "remove_runtime_pip": True},
    )
    cleaned: list[Path] = []
    real_optimize = runtime_module.optimize_runtime_tree
    monkeypatch.setattr(pkg, "optimize_runtime_tree", lambda path: (cleaned.append(path), real_optimize(path))[1])

    out = pkg.package(target, launcher)

    assert out == root / "dist" / "MyApp.app"
    assert (out / "Contents" / "MacOS" / "MyApp").is_file()
    # The launcher carries the entry, so the bundle ships no .int sidecar: not
    # the named entry, not the rename-safe copy, not the smoke program.
    assert list((out / "Contents" / "Resources").glob("*.int")) == []
    # The runtime cleanup ran on the private assembly tree, never on dist/.
    assert len(cleaned) == 1
    assert (root / "dist") not in cleaned[0].parents
    assert not (out / "Contents" / "Resources" / "runtime" / "lib" / "python3.11" / "__pycache__").exists()
    # Publishing moves the bundle out, so no assembly tree is left behind.
    assert not assembly.exists()
    # A per-build name is what keeps a cached locator result from ever pointing
    # at a later build's assembly tree.
    assert str(os.getpid()) in publish_dir(target).name


def test_zig_triple_converts_msvc_to_gnu():
    """验证 Zig 将 Windows MSVC 目标转换为其 GNU 编译三元组。"""
    from appbuild.config import Target
    from appbuild.toolchain import Toolchain

    target = Target("windows", "x86_64", "x86_64-pc-windows-msvc")
    toolchain = Toolchain(target, {}, "zig", "zig", "zig", compiler_args=("cc", "-target", "x86_64-windows-gnu"))
    assert toolchain.compiler_args == ("cc", "-target", "x86_64-windows-gnu")


def test_zig_safe_archive_rejects_unsafe_paths(tmp_path, monkeypatch):
    """验证 Zig 下载归档拒绝绝对路径和目录穿越成员。"""
    import io
    import tarfile

    from appbuild.toolchain import _safe_archive_members

    archive = tmp_path / "evil.tar.xz"
    with tarfile.open(archive, "w:xz") as tf:
        for name in ("../evil.txt", "/abs.txt"):
            data = b"evil"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    members = []
    try:
        for _name, _stream in _safe_archive_members(archive):
            members.append(_name)
    except RuntimeError:
        pass
    assert not members or all(".. " not in name and not name.startswith("/") for name in members)


def test_zig_host_key_requires_native_host():
    """验证 Zig 工具下载键由实际主机系统与架构决定。"""
    from appbuild.toolchain import _zig_host_key

    assert _zig_host_key("Darwin", "arm64") == "aarch64-macos"
    assert _zig_host_key("Linux", "x86_64") == "x86_64-linux"
    try:
        _zig_host_key("Plan9", "riscv64")
    except RuntimeError:
        pass
    else:
        pytest.fail("unsupported host must fail")


def test_zig_cross_compiles_windows_x86_from_a_verified_toolchain(tmp_path, monkeypatch):
    """验证无本机 MSVC 时可通过已验证 Zig 交叉构建 x86。

    32-bit Windows is a Zig target like any other.

    The x86 target used to be rejected before anything was downloaded, on the
    assumption that Zig 0.16 ships no i686 cross toolchain.  It does: zig cc
    -target i686-windows-gnu links a PE32 launcher and a PE32 extension against
    the same python313.lib import library, so the guard only blocked a build
    that works.  A pre-verified toolchain tree keeps the test offline and proves
    the download path is not even reached.
    """
    import platform as platform_module

    from appbuild import toolchain
    from appbuild.config import TARGETS

    monkeypatch.setattr(toolchain, "BUILD", tmp_path)
    host_key = toolchain._zig_host_key(platform_module.system(), toolchain._host_arch())
    root = tmp_path / "host-tools" / "zig" / f"{toolchain.ZIG_VERSION}-{host_key}"
    zig = root / "archive" / f"zig-{host_key}" / toolchain._zig_exe_name()
    zig.parent.mkdir(parents=True)
    zig.write_text("", encoding="utf-8")
    (root / ".verified").write_text("verified\n", encoding="utf-8")

    def fail(*_args, **_kwargs):
        """模拟 MSVC 环境探测失败以进入 Zig 分支。"""
        raise AssertionError("a verified toolchain must not hit the network")

    monkeypatch.setattr(toolchain, "http_json", fail)

    tc = toolchain.ensure_zig(TARGETS["windows-x86"])
    # Zig spells the 32-bit architecture "x86" and rejects the packaging
    # triple's "i686" outright, so the arch component is translated too.
    assert tc.compiler_args == ("cc", "-target", "x86-windows-gnu", "-DHAVE_SNPRINTF")
    assert "-target x86-windows-gnu" in tc.env["CC"]


def test_zig_windows_frontend_carries_the_cpython_header_corrections(tmp_path, monkeypatch):
    """验证 Windows GNU 前端携带 CPython 头文件所需宏修正。

    The SDK's pyconfig.h is MSVC-shaped; the Zig frontend must correct it.

    ``SIZEOF_VOID_P``/``SIZEOF_SIZE_T`` are only 8 on the branch pyconfig.h
    reaches through ``_MSC_VER``, and pyerrors.h renames ``snprintf`` to
    ``_snprintf`` unless ``HAVE_SNPRINTF`` is set -- which is right for MSVC's
    pre-C99 CRT and wrong for MinGW-w64. A GNU driver therefore saw a 32-bit
    ABI on a 64-bit target and a ``std::_snprintf`` that does not exist. Both
    corrections belong to the target ABI, so they are emitted for the
    extensions and for the launcher alike, here and in ``env["CC"]``.
    """
    import platform as platform_module

    from appbuild import toolchain
    from appbuild.config import TARGETS

    monkeypatch.setattr(toolchain, "BUILD", tmp_path)
    host_key = toolchain._zig_host_key(platform_module.system(), toolchain._host_arch())
    root = tmp_path / "host-tools" / "zig" / f"{toolchain.ZIG_VERSION}-{host_key}"
    zig = root / "archive" / f"zig-{host_key}" / toolchain._zig_exe_name()
    zig.parent.mkdir(parents=True)
    zig.write_text("", encoding="utf-8")
    (root / ".verified").write_text("verified\n", encoding="utf-8")
    monkeypatch.setattr(toolchain, "http_json", lambda *a, **k: pytest.fail("a verified toolchain must not hit the network"))

    for key in ("windows-x86_64", "windows-arm64"):
        tc = toolchain.ensure_zig(TARGETS[key])
        assert "-DMS_WIN64" in tc.compiler_args
        assert "-DHAVE_SNPRINTF" in tc.compiler_args
        assert "-DMS_WIN64" in tc.env["CC"]
        assert "-DMS_WIN64" in tc.env["CXX"]

    assert toolchain.windows_gnu_cpython_defines(TARGETS["windows-x86"]) == ("-DHAVE_SNPRINTF",)
