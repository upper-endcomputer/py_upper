"""Declarative payload trimming for the bundled site-packages and runtime.

Qt pruning removes what the application's imports prove is dead. Every project
also ships payload no import can describe: C++ headers, example trees, type
stubs, and a runtime's development-only stdlib modules. ``[tool.py_upper.trim]``
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
# * distribution metadata (pip / importlib.metadata bookkeeping);
# * type stubs (the interpreter never loads them);
# * Qt binding examples and C++ headers (development-only in every layout).
#
# A project that really queries importlib.metadata/pkg_resources can protect a
# distribution with ``site_packages_keep``; keep wins over these rules.
GLOBAL_SITE_PACKAGES = (
    "**/*.dist-info",
    "**/*.egg-info",
    "**/*.pyi",
    "PySide*/**/examples",
    "PySide*/**/include",
    "PyQt*/**/examples",
    "PyQt*/**/include",
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

# Named rule bundles add to the global rules above. ``safe`` adds nothing.
# ``release`` is kept as an alias for existing configurations: distribution
# metadata became a global rule, so it no longer has anything of its own.
# ``minimal`` adds payload that is optional at runtime and can break a machine
# or an application that actually uses it, so it is never enabled by default.
PRESETS = {
    "safe": {"site_packages": (), "runtime": ()},
    "release": {"site_packages": (), "runtime": ()},
    "minimal": {
        "site_packages": (
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
    """A TOML/CLI value as a tuple of glob patterns, rejecting the wrong shape."""
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
    """Normalize one pattern and refuse anything that could escape the tree.

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
    """Compile a relative glob into a regex over POSIX-style relative paths."""
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
    """Preserve order while removing a preset pattern the config already lists."""
    return tuple(dict.fromkeys(values))


def _preset_name(value) -> str:
    name = str(value or "safe").strip().lower()
    if name not in PRESETS:
        choices = ", ".join(sorted(PRESETS))
        raise RuntimeError(f"[tool.py_upper.trim].preset must be one of: {choices}")
    return name


@dataclass
class TrimReport:
    """What one trim pass removed, in the units the build prints."""

    removed_files: int = 0
    removed_dirs: int = 0
    removed_bytes: int = 0

    def add(self, other: "TrimReport") -> None:
        self.removed_files += other.removed_files
        self.removed_dirs += other.removed_dirs
        self.removed_bytes += other.removed_bytes

    def __bool__(self) -> bool:
        return bool(self.removed_files or self.removed_dirs)

    def describe(self, scope: str = "payload") -> str:
        return (
            f"Trim ({scope}): removed {self.removed_files} file(s) and "
            f"{self.removed_dirs} director(ies), freed {self.removed_bytes / 1_000_000:.1f} MB"
        )

    def as_dict(self) -> dict[str, int]:
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
        for name in ("site_packages", "site_packages_keep", "runtime", "runtime_keep"):
            value = getattr(self, name)
            object.__setattr__(self, name, _pattern_tuple(value, f"[tool.py_upper.trim].{name}"))

    @classmethod
    def from_config(cls, config=None, preset=None) -> "TrimPolicy":
        """Build the policy, expanding the named preset plus explicit patterns.

        ``preset`` overrides ``[tool.py_upper.trim].preset`` for one build, which
        is how ``--trim-preset`` selects the aggressive bundle without editing
        the configuration.
        """
        cfg = config if isinstance(config, dict) else {}
        selected = _preset_name(cfg.get("preset", "safe") if preset is None else preset)
        bundle = PRESETS[selected]
        site = _pattern_tuple(cfg.get("site_packages", ()), "[tool.py_upper.trim].site_packages")
        site_keep = _pattern_tuple(cfg.get("site_packages_keep", ()), "[tool.py_upper.trim].site_packages_keep")
        runtime = _pattern_tuple(cfg.get("runtime", ()), "[tool.py_upper.trim].runtime")
        runtime_keep = _pattern_tuple(cfg.get("runtime_keep", ()), "[tool.py_upper.trim].runtime_keep")
        preset_site = _pattern_tuple(bundle["site_packages"], "[tool.py_upper.trim].preset")
        preset_runtime = _pattern_tuple(bundle["runtime"], "[tool.py_upper.trim].preset")
        global_site = _pattern_tuple(GLOBAL_SITE_PACKAGES, "[tool.py_upper.trim].global")
        global_runtime = _pattern_tuple(GLOBAL_RUNTIME, "[tool.py_upper.trim].global")
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
        """Add one build's command-line patterns to the configured policy."""
        return TrimPolicy(
            site_packages=self.site_packages + _pattern_tuple(site_packages, "[tool.py_upper.trim].site_packages"),
            site_packages_keep=self.site_packages_keep + _pattern_tuple(
                site_packages_keep, "[tool.py_upper.trim].site_packages_keep"
            ),
            runtime=self.runtime + _pattern_tuple(runtime, "[tool.py_upper.trim].runtime"),
            runtime_keep=self.runtime_keep + _pattern_tuple(runtime_keep, "[tool.py_upper.trim].runtime_keep"),
        )

    @property
    def has_site_rules(self) -> bool:
        return bool(self.site_packages)

    @property
    def has_runtime_rules(self) -> bool:
        return bool(self.runtime)


def _matches(relative: str, patterns: Sequence) -> bool:
    return any(pattern.match(relative) for pattern in patterns)


def _is_kept(relative: str, keep: Sequence) -> bool:
    """True when the path or any ancestor matches a keep pattern."""
    candidate = relative
    while True:
        if _matches(candidate, keep):
            return True
        if "/" not in candidate:
            return False
        candidate = candidate.rsplit("/", 1)[0]


def _subtree_has_keep(directory: Path, root: Path, keep: Sequence) -> bool:
    """Whether anything below ``directory`` is protected by a keep pattern."""
    for current, directories, files in os.walk(directory):
        current_path = Path(current)
        relative_dir = current_path.relative_to(root).as_posix()
        for name in list(directories) + files:
            if _is_kept(f"{relative_dir}/{name}", keep):
                return True
    return False


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _remove(path: Path) -> TrimReport:
    """Delete a file or directory subtree and report what it held."""
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
    """Remove every path under ``root`` matched by ``patterns``.

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
                if keeps and _subtree_has_keep(path, root_path, keeps):
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