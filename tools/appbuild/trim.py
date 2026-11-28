# 中文维护说明：应用声明式删除与保留规则；keep 优先，预计算祖先集合避免重复扫描子树。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Declarative payload trimming for the bundled site-packages and runtime.

Qt pruning removes what the application's imports prove is dead. Every project
also ships payload no import can describe: C++ headers, example trees, type
stubs, and a runtime's development-only stdlib modules. ``[tool.appbuild.trim]``
names those paths, and ``--trim-site`` / ``--trim-runtime`` do the same for one
build.

Patterns are relative globs rather than host paths:

* ``*``, ``?`` and ``[...]`` match within one path segment;
* ``**`` crosses directories, so ``**/*.pyi`` matches at any depth;
* a pattern that matches a directory removes its whole subtree;
* a ``keep`` pattern wins over a removal pattern, including for a directory
  that would otherwise be removed wholesale.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Iterable, Sequence

SITE_SCOPE = "site-packages"
RUNTIME_SCOPE = "runtime"


# Rules that apply to every build, before any preset or project configuration.
# They remove payload a running application never imports and that is not
# specific to a project or a Qt layout:
#
# * type stubs (the interpreter never loads them);
# * Qt binding examples and C++ headers (development-only in every layout).
#
# Distribution metadata stays: importlib.metadata and plugin entry-point
# discovery need it at runtime even when ordinary module imports work.
GLOBAL_SITE_PACKAGES = (
    "**/*.pyi",
    "PySide*/**/examples",
    "PySide*/**/include",
    "PyQt*/**/examples",
    "PyQt*/**/include",
    # Qt bindings ship their build/deployment tooling next to the runtime
    # package. A frozen application never imports these trees, and on Windows
    # they are the bulk of the remaining ``.py`` files inside PySide6.
    # The patterns are anchored at the binding root on purpose: ``PySide6/lib``
    # is CMake metadata, while ``PySide6/Qt/lib`` is the runtime library
    # directory and must stay.
    "PySide*/scripts",
    "PySide*/support",
    "PySide*/doc",
    "PySide*/docs",
    "PySide*/lib",
    "shiboken*/scripts",
    "shiboken*/support",
    "shiboken*/doc",
    "shiboken*/docs",
    "shiboken*/lib",
    "shiboken*/include",
)

# Runtime payload no frozen desktop application needs: CPython's development
# tools/tests and C headers. Tk/Tcl stays project-configurable because an
# application may actually use it.
GLOBAL_RUNTIME = (
    "**/idlelib",
    "**/test",
    "**/tests",
    "**/include",
    "**/pkgconfig",
)

# Metadata removal is explicit in the release/minimal presets. Projects that
# use those presets can still protect individual distributions with keep rules.
METADATA_SITE_PACKAGES = ("**/*.dist-info", "**/*.egg-info")

# Named rule bundles add to the global rules above. ``safe`` adds nothing.
# ``minimal`` also removes optional runtime payload, so neither aggressive
# preset is enabled by default.
PRESETS = {
    "safe": {"site_packages": (), "runtime": ()},
    "release": {"site_packages": METADATA_SITE_PACKAGES, "runtime": ()},
    "minimal": {
        "site_packages": METADATA_SITE_PACKAGES + (
            # Qt wheels disagree about where the payload lives: PySide on
            # Windows keeps it beside the package, PySide on POSIX nests it
            # under ``Qt``, and PyQt nests it under ``Qt5``/``Qt6`` everywhere.
            # ``**/`` between the binding directory and the payload name
            # matches every one of those layouts, so the preset is not tied to
            # the host that produced it.
            "PySide*/**/opengl32sw.dll",
            "PyQt*/**/opengl32sw.dll",
            "PySide*/**/plugins/platforms/*qdirect2d*",
            "PyQt*/**/plugins/platforms/*qdirect2d*",
            "PySide*/**/plugins/platforms/*qminimal*",
            "PyQt*/**/plugins/platforms/*qminimal*",
            "PySide*/**/translations/*.qm",
            "PyQt*/**/translations/*.qm",
            "PySide*/**/plugins/imageformats/*qwebp*",
            "PyQt*/**/plugins/imageformats/*qwebp*",
            "PySide*/**/plugins/imageformats/*qtiff*",
            "PyQt*/**/plugins/imageformats/*qtiff*",
        ),
        "runtime": (
            # Tk/Tcl is app-dependent, so it is not global. The versioned
            # directories are what actually exist (tcl8.6, tk8.6); a pattern of
            # exactly ``tcl`` matches none of them.
            "**/tkinter",
            "**/tcl[0-9]*",
            "**/tk[0-9]*",
            "**/itcl[0-9]*",
            "**/thread[0-9]*",
        ),
    },
}


def _pattern_tuple(value, where: str) -> tuple[str, ...]:
    """把 TOML 或 CLI 值规范为模式元组，逐项校验类型及路径边界。

    A TOML/CLI value as a tuple of glob patterns, rejecting the wrong shape."""
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        values: Iterable = (value,)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise RuntimeError(f"{where} must be a glob string or an array of glob strings")
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise RuntimeError(f"{where} must contain only glob strings")
        result.append(_validate_pattern(item, where))
    return tuple(result)


def _validate_pattern(pattern: str, where: str) -> str:
    """规范分隔符并拒绝绝对路径、盘符、空段及父目录段。

    Normalize one pattern and refuse anything that could escape the tree.

    The tree this runs on is the bundle's own payload, so a host path or a
    ``..`` segment is a configuration bug. Rejecting it before the build starts
    is better than deleting a sibling directory halfway through packaging.
    """
    text = pattern.strip().replace("\\", "/")
    if not text:
        raise RuntimeError(f"{where} contains an empty glob pattern")
    if text.startswith("/") or text.startswith("//") or PureWindowsPath(text).drive:
        raise RuntimeError(f"{where} glob must be relative to the trim root: {pattern!r}")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeError(f"{where} glob must not contain empty, '.' or '..' segments: {pattern!r}")
    return text


def _translate(pattern: str):
    """把相对 glob 编译为正则；单星不跨目录，双星可匹配多级目录。

    Compile a relative glob into a regex over POSIX-style relative paths."""
    parts: list[str] = []
    index = 0
    size = len(pattern)
    while index < size:
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                # Zero or more directory segments: "**/*.pyi" also matches a
                # stub at the root, and "a/**/b" matches "a/b".
                parts.append("(?:.*/)?")
                index += 3
            elif pattern.startswith("**", index):
                parts.append(".*")
                index += 2
            else:
                parts.append("[^/]*")
                index += 1
        elif char == "?":
            parts.append("[^/]")
            index += 1
        elif char == "[":
            end = index + 1
            if end < size and pattern[end] in "!^":
                end += 1
            if end < size and pattern[end] == "]":
                end += 1
            while end < size and pattern[end] != "]":
                end += 1
            if end >= size:
                # An unterminated class is a literal '[' rather than an error:
                # filenames are allowed to contain it.
                parts.append(r"\[")
                index += 1
            else:
                body = pattern[index + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                parts.append("[" + body + "]")
                index = end + 1
        else:
            parts.append(re.escape(char))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    """保持首次出现顺序去重，避免预设与显式规则重复。

    Preserve order while removing a preset pattern the config already lists."""
    return tuple(dict.fromkeys(values))


def _preset_name(value) -> str:
    """规范并校验预设名，未知值附带可用选项报错。"""
    name = str(value or "safe").strip().lower()
    if name not in PRESETS:
        choices = ", ".join(sorted(PRESETS))
        raise RuntimeError(f"[tool.appbuild.trim].preset must be one of: {choices}")
    return name


@dataclass
class TrimReport:
    """What one trim pass removed, in the units the build prints."""

    removed_files: int = 0
    removed_dirs: int = 0
    removed_bytes: int = 0

    def add(self, other: "TrimReport") -> None:
        """把另一轮裁剪计数累加到当前报告。"""
        self.removed_files += other.removed_files
        self.removed_dirs += other.removed_dirs
        self.removed_bytes += other.removed_bytes

    def __bool__(self) -> bool:
        """报告是否实际删除了文件或目录。"""
        return bool(self.removed_files or self.removed_dirs)

    def describe(self, scope: str = "payload") -> str:
        """格式化指定裁剪范围的删除数量和体积，供构建日志使用。"""
        return (
            f"Trim ({scope}): removed {self.removed_files} file(s) and "
            f"{self.removed_dirs} director(ies), freed {self.removed_bytes / 1_000_000:.1f} MB"
        )

    def as_dict(self) -> dict[str, int]:
        """导出文件、目录和字节计数，供结构化结果记录。"""
        return {
            "removed_files": self.removed_files,
            "removed_dirs": self.removed_dirs,
            "removed_bytes": self.removed_bytes,
        }


@dataclass(frozen=True)
class TrimPolicy:
    """The two trees' remove/keep pattern sets, as one build-wide value."""

    site_packages: tuple[str, ...] = ()
    site_packages_keep: tuple[str, ...] = ()
    runtime: tuple[str, ...] = ()
    runtime_keep: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """冻结策略前统一校验并规范四组删除及保留模式。"""
        for name in ("site_packages", "site_packages_keep", "runtime", "runtime_keep"):
            value = getattr(self, name)
            object.__setattr__(self, name, _pattern_tuple(value, f"[tool.appbuild.trim].{name}"))

    @classmethod
    def from_config(cls, config=None, preset=None) -> "TrimPolicy":
        """合并全局规则、命名预设和项目规则；CLI 预设可覆盖配置预设。

        Build the policy, expanding the named preset plus explicit patterns.

        ``preset`` overrides ``[tool.appbuild.trim].preset`` for one build, which
        is how ``--trim-preset`` selects the aggressive bundle without editing
        the configuration.
        """
        cfg = config if isinstance(config, dict) else {}
        selected = _preset_name(cfg.get("preset", "safe") if preset is None else preset)
        bundle = PRESETS[selected]
        site = _pattern_tuple(cfg.get("site_packages", ()), "[tool.appbuild.trim].site_packages")
        site_keep = _pattern_tuple(cfg.get("site_packages_keep", ()), "[tool.appbuild.trim].site_packages_keep")
        runtime = _pattern_tuple(cfg.get("runtime", ()), "[tool.appbuild.trim].runtime")
        runtime_keep = _pattern_tuple(cfg.get("runtime_keep", ()), "[tool.appbuild.trim].runtime_keep")
        preset_site = _pattern_tuple(bundle["site_packages"], "[tool.appbuild.trim].preset")
        preset_runtime = _pattern_tuple(bundle["runtime"], "[tool.appbuild.trim].preset")
        global_site = _pattern_tuple(GLOBAL_SITE_PACKAGES, "[tool.appbuild.trim].global")
        global_runtime = _pattern_tuple(GLOBAL_RUNTIME, "[tool.appbuild.trim].global")
        return cls(
            site_packages=_dedupe(global_site + site + preset_site),
            site_packages_keep=site_keep,
            runtime=_dedupe(global_runtime + runtime + preset_runtime),
            runtime_keep=runtime_keep,
        )

    def merge(
        self,
        *,
        site_packages: Sequence[str] = (),
        site_packages_keep: Sequence[str] = (),
        runtime: Sequence[str] = (),
        runtime_keep: Sequence[str] = (),
    ) -> "TrimPolicy":
        """追加本次命令行模式，返回新的不可变裁剪策略。

        Add one build's command-line patterns to the configured policy."""
        return TrimPolicy(
            site_packages=self.site_packages + _pattern_tuple(site_packages, "[tool.appbuild.trim].site_packages"),
            site_packages_keep=self.site_packages_keep + _pattern_tuple(
                site_packages_keep, "[tool.appbuild.trim].site_packages_keep"
            ),
            runtime=self.runtime + _pattern_tuple(runtime, "[tool.appbuild.trim].runtime"),
            runtime_keep=self.runtime_keep + _pattern_tuple(runtime_keep, "[tool.appbuild.trim].runtime_keep"),
        )

    @property
    def has_site_rules(self) -> bool:
        """判断是否存在 site-packages 删除规则。"""
        return bool(self.site_packages)

    @property
    def has_runtime_rules(self) -> bool:
        """判断是否存在 runtime 删除规则。"""
        return bool(self.runtime)


def _matches(relative: str, patterns: Sequence) -> bool:
    """判断相对路径是否匹配任一已编译模式。"""
    return any(pattern.match(relative) for pattern in patterns)


def _is_kept(relative: str, keep: Sequence) -> bool:
    """路径自身或任意祖先匹配 keep 时返回真。

    True when the path or any ancestor matches a keep pattern."""
    candidate = relative
    while True:
        if _matches(candidate, keep):
            return True
        if "/" not in candidate:
            return False
        candidate = candidate.rsplit("/", 1)[0]



def _size(path: Path) -> int:
    """读取文件字节数；无法读取时以零计入统计。"""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _remove(path: Path) -> TrimReport:
    """删除文件或整个子树并返回统计；符号链接仅删除链接本身。

    Delete a file or directory subtree and report what it held."""
    report = TrimReport()
    if path.is_symlink() or path.is_file():
        size = _size(path)
        try:
            path.unlink()
        except FileNotFoundError:
            return report
        report.removed_files = 1
        report.removed_bytes = size
        return report
    if not path.is_dir():
        return report

    files = directories = size = 0
    for current, _subdirectories, names in os.walk(path):
        current_path = Path(current)
        directories += 1
        for name in names:
            files += 1
            size += _size(current_path / name)
    shutil.rmtree(path)
    report.removed_files = files
    report.removed_dirs = directories
    report.removed_bytes = size
    return report


def trim_tree(root: Path, patterns: Sequence[str], keep: Sequence[str] = ()) -> TrimReport:
    """执行 keep 优先的相对路径裁剪；预计算受保护祖先，避免重复扫描子树。

    Remove every path under ``root`` matched by ``patterns``.

    ``root`` is the bundled tree (``site-packages`` or the copied runtime), and
    patterns are relative to it. A matched directory is removed with its whole
    subtree; a ``keep`` match protects the path even when a parent directory was
    named for removal.
    """
    root_path = Path(root)
    if not root_path.is_dir():
        raise RuntimeError(f"Trim root is not a directory: {root_path}")
    removals = tuple(_translate(pattern) for pattern in patterns)
    keeps = tuple(_translate(pattern) for pattern in keep)
    report = TrimReport()
    if not removals:
        return report

    # A directory named for removal whose subtree contains a kept path cannot be
    # deleted wholesale. It is walked instead, and every descendant that is not
    # protected has to be removed even though it does not match the original
    # pattern itself: the removal applies to the subtree, not just the directory
    # entry. ``forced`` carries that decision down the walk.
    forced: set[str] = set()
    protected_ancestors = set()
    if keeps:
        for current, directories, files in os.walk(root_path):
            for name in directories + files:
                relative = (Path(current) / name).relative_to(root_path).as_posix()
                if _matches(relative, keeps):
                    while "/" in relative:
                        relative = relative.rsplit("/", 1)[0]
                        protected_ancestors.add(relative)

    for current, directories, files in os.walk(root_path, topdown=True):
        current_path = Path(current)
        relative_dir = "" if current_path == root_path else current_path.relative_to(root_path).as_posix()
        forced_here = relative_dir in forced

        kept_directories: list[str] = []
        for name in sorted(directories):
            path = current_path / name
            relative = f"{relative_dir}/{name}" if relative_dir else name
            if _is_kept(relative, keeps):
                # The whole subtree is protected, so it is not worth walking.
                continue
            if forced_here or _matches(relative, removals):
                if relative in protected_ancestors:
                    # Descend instead of deleting: an inner path is kept, so
                    # this directory stays but its subtree is still trimmed.
                    forced.add(relative)
                    kept_directories.append(name)
                else:
                    report.add(_remove(path))
                continue
            kept_directories.append(name)
        directories[:] = kept_directories

        for name in sorted(files):
            relative = f"{relative_dir}/{name}" if relative_dir else name
            if _is_kept(relative, keeps):
                continue
            if forced_here or _matches(relative, removals):
                report.add(_remove(current_path / name))

    return report
