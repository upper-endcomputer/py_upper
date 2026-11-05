from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from ..config import BUILD, app_name, native_exclude_patterns, optimize_config
from ..fs import copy_file_contents, sha256
from .deps import (
    Dependency, DependencyResolver, _mac_install_name, _system_dependency,
    dependency_names, elf_dynamic_entries, linux_host_libraries,
)
from .inspect import inspect, is_debug_artifact, native_files as _native_files, verify_arch


def prune_excluded_native_files(site: Path) -> list[Path]:
    """Remove configured native payloads before the dependency closure runs."""
    patterns = native_exclude_patterns()
    if not patterns:
        return []
    import fnmatch

    removed: list[Path] = []
    emptied: set[Path] = set()
    for path in sorted(site.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(site).as_posix()
        if not any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
            continue
        path.unlink()
        removed.append(path)
        parent = path.parent
        while parent != site and site in parent.parents:
            emptied.add(parent)
            parent = parent.parent
    # Only remove directories that the exclusions emptied. An empty directory
    # elsewhere can be a PEP 420 namespace package.
    for directory in sorted(emptied, key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return removed


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ad_hoc_sign(path: Path) -> None:
    """Give a Mach-O image a fresh ad-hoc signature.

    Any byte-level rewrite invalidates the existing signature, and dyld then
    refuses to load the image. Copying a framework binary out of its framework
    counts as such a rewrite: the original Developer ID signature is bound to
    the framework's Info.plist, so the copy fails to load even though its bytes
    are intact.
    """
    codesign = shutil.which("codesign")
    if codesign:
        result = subprocess.run([codesign, "--force", "--sign", "-", "--timestamp=none", str(path)], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"codesign failed for {path}: {result.stderr.strip()}")


def _thin_mach_o(path: Path, arch: str) -> None:
    """Drop the slices the target architecture cannot load.

    PySide6 and pyobjc publish universal2 wheels, and the bundle declares a
    single architecture, so the foreign slice is dead weight that roughly
    doubles the size of every bundled image.
    """
    if inspect(path).arch != "universal":
        return
    lipo = shutil.which("lipo")
    if not lipo:
        raise RuntimeError(f"thinning {path.name} to {arch} requires lipo")
    thinned = path.with_name(path.name + ".thin")
    subprocess.run([lipo, "-thin", arch, "-output", str(thinned), str(path)], check=True)
    # lipo writes a fresh file, so an executable slice would lose its bits.
    shutil.copymode(path, thinned)
    thinned.replace(path)


def _mac_aliases(roots: list[Path]) -> dict[str, Path]:
    aliases: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or is_debug_artifact(path):
                continue
            try:
                info = inspect(path)
            except (OSError, ValueError):
                continue
            if not info.loadable or not info.format.startswith("Mach-O"):
                continue
            install_name = _mac_install_name(path)
            if not install_name:
                continue
            aliases.setdefault(os.path.basename(install_name).lower(), path.resolve())
    return aliases




def _strip_native(path: Path, target) -> None:
    if not bool(optimize_config().get("strip_native", False)):
        return
    if target.os == "windows":
        tool = shutil.which("llvm-strip") or shutil.which("strip")
        if not tool:
            raise RuntimeError("strip_native=true requires llvm-strip or strip on PATH for Windows targets")
        cmd = [tool, str(path)]
    else:
        tool = shutil.which("strip")
        if not tool:
            raise RuntimeError("strip_native=true requires strip on PATH")
        cmd = [tool, "--strip-unneeded", str(path)] if target.os == "linux" else [tool, "-x", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"strip failed for {path}: {(result.stderr or result.stdout).strip()}")


def _relocate_linux_dependencies(dependencies: list[Dependency], copied: set[Path]) -> None:
    """Wire newly bundled libraries into the existing ELF search paths."""
    affected = copied | {dep.owner for dep in dependencies if dep.resolved in copied}
    if not affected:
        return
    patchelf = shutil.which("patchelf")
    if not patchelf:
        raise RuntimeError("Bundling external Linux libraries requires patchelf")
    for owner in sorted(affected):
        entries = elf_dynamic_entries(owner)
        has_runpath = any(kind == "RUNPATH" for kind, _ in entries)
        tag = "RUNPATH" if has_runpath else "RPATH"
        existing = [part for kind, value in entries if kind == tag for part in value.split(":")]
        required = []
        for dep in dependencies:
            if dep.owner != owner or dep.resolved is None or dep.external:
                continue
            if "/" in dep.name:
                subprocess.run([patchelf, "--replace-needed", dep.name, dep.resolved.name, str(owner)], check=True)
            relative = Path(os.path.relpath(dep.resolved.parent, owner.parent)).as_posix()
            required.append("$ORIGIN" if relative == "." else "$ORIGIN/" + relative)
        combined = list(dict.fromkeys(existing + required))
        if combined == existing:
            continue
        # Keep RPATH inheritance for libraries that already use it. RUNPATH is
        # the default for new paths; no wheel-provided entries are discarded.
        force = ["--force-rpath"] if not has_runpath and any(kind == "RPATH" for kind, _ in entries) else []
        subprocess.run([patchelf, *force, "--set-rpath", ":".join(combined), str(owner)], check=True)


def bundle_native_dependencies(root: Path, target, env: dict[str, str] | None = None) -> list[Dependency]:
    """Resolve and embed the native dependency closure of the application payload."""
    root = root.resolve()
    name = app_name()
    package_site = root / "Contents" / "Resources" / "site-packages" if target.os == "macos" else root / "site-packages"
    runtime_root = package_site.parent / "runtime"
    runtime_lib = runtime_root if target.os == "windows" else runtime_root / "lib"
    search_roots = [package_site, runtime_lib, root]
    queue = _native_files(package_site, target)
    # libpython and stdlib extensions are loaded even when no third-party
    # image imports them. They need the same complete closure as the app.
    # Windows runtimes also contain foreign-architecture venv/tool .exe stubs;
    # those are standalone templates, not libraries loaded by the launcher.
    queue.extend(path for path in _native_files(runtime_lib, target)
                 if target.os != "windows" or path.suffix.lower() != ".exe")
    launcher = (root / "Contents" / "MacOS" / name) if target.os == "macos" else root / (f"{name}.exe" if target.os == "windows" else name)
    if launcher.is_file():
        queue.append(launcher)
    queue = list(dict.fromkeys(queue))
    aliases = _mac_aliases(search_roots) if target.os == "macos" else {}
    # One basename index and one rpath cache for the whole closure: Qt-sized
    # dependency graphs otherwise re-walk the tree for every load name.
    resolver = DependencyResolver(search_roots, aliases)

    seen: set[Path] = set()
    copied: set[Path] = set()
    host_libraries: dict[str, Path] | None = None
    dependencies: list[Dependency] = []
    while queue:
        source = queue.pop(0).resolve()
        if source in seen:
            continue
        seen.add(source)
        verify_arch(source, target)
        for dep_name in dependency_names(source, env):
            if _system_dependency(dep_name, target):
                continue
            resolved = resolver.resolve(dep_name, source)
            if resolved is None and target.os == "linux" and "/" not in dep_name:
                if host_libraries is None:
                    host_libraries = linux_host_libraries(target)
                resolved = host_libraries.get(dep_name.lower())
            if resolved is None:
                dependencies.append(Dependency(source, dep_name, None, True))
                continue
            if not _inside(resolved, root):
                verify_arch(resolved, target)
                # ELF keeps the load name (often libdemo.so.1) even when its
                # source symlink resolves to libdemo.so.1.2. macOS install
                # names are rewritten below and can use the physical name.
                filename = Path(dep_name).name if target.os == "linux" else resolved.name
                destination = source.parent / filename
                if destination.exists():
                    # Several owners can import the same absolute library.
                    # Its first copy is a different path by definition; only
                    # different bytes under that basename are a collision.
                    if sha256(destination) != sha256(resolved):
                        raise RuntimeError(f"Native dependency basename collision: {destination} != {resolved}")
                else:
                    copy_file_contents(resolved, destination)
                    resolver.index.add(destination)
                    print(f"Native dependency: bundled {resolved} as {destination.relative_to(root)}")
                resolved = destination.resolve()
                copied.add(resolved)
            # A library already inside runtime/resources still has its own
            # dependencies. Stopping at "found in bundle" hides broken chains
            # until a clean machine first tries to load them.
            if resolved not in seen:
                queue.append(resolved)
            dependencies.append(Dependency(source, dep_name, resolved, False))

    report_dir = BUILD / "native-deps" / target.key
    report_dir.mkdir(parents=True, exist_ok=True)
    report = [
        {
            "owner": str(dep.owner.relative_to(root)),
            "name": dep.name,
            "resolved": str(dep.resolved.relative_to(root)) if dep.resolved and _inside(dep.resolved, root) else None,
            "unresolved": dep.external,
        }
        for dep in dependencies
    ]
    (report_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    unresolved = [dep for dep in dependencies if dep.external]
    if unresolved:
        lines = sorted(set(f"{dep.owner.relative_to(root)}: {dep.name}" for dep in unresolved))
        raise RuntimeError(
            "Unresolved native dependencies:\n  " + "\n  ".join(lines)
            + "\nOptional native payloads can be dropped with [tool.py_upper.native].exclude "
              "in app/pyproject.toml."
        )

    # Full RPATH replacement remains opt-in. The mandatory relocation below
    # only adds paths needed by newly bundled libraries and keeps existing ones.
    if target.os == "linux" and shutil.which("patchelf") and bool(optimize_config().get("rewrite_rpath", False)):
        for binary in seen:
            if binary.suffix.lower() == ".so":
                subprocess.run(["patchelf", "--set-rpath", "$ORIGIN", str(binary)], check=True)

    if target.os == "linux":
        _relocate_linux_dependencies(dependencies, copied)

    if target.os == "macos" and shutil.which("install_name_tool"):
        native_images = [path for path in seen if inspect(path).format.startswith("Mach-O")]
        for binary in native_images:
            _thin_mach_o(binary, target.arch)
        # Nothing removes the existing signature first. install_name_tool and
        # strip invalidate it in place, and ad_hoc_sign re-signs with --force
        # afterwards. `codesign --remove-signature` shrinks __LINKEDIT without
        # updating its vmsize, and ld64 then rejects the file with "link edit
        # information does not fill the __LINKEDIT segment".
        for dep in dependencies:
            if dep.external or dep.resolved is None:
                continue
            owner = dep.owner
            if not inspect(owner).format.startswith("Mach-O"):
                continue
            relative = os.path.relpath(dep.resolved, owner.parent).replace(os.sep, "/")
            new_name = "@loader_path/" + relative
            if dep.name == new_name:
                continue
            subprocess.run(["install_name_tool", "-change", dep.name, new_name, str(owner)], check=True)
        for binary in native_images:
            _strip_native(binary, target)
        # Ad-hoc signing must run inside-out. Nested code such as
        # QtWebEngineCore.framework/Helpers/QtWebEngineProcess.app is rejected
        # ("code object is not signed at all") unless the code it contains is
        # signed first, and signing the launcher seals the whole .app bundle,
        # so it has to happen last. Deepest path first satisfies both, and
        # matches the order build.py's release path uses.
        for binary in sorted(native_images, key=lambda path: (-len(path.parts), path == launcher)):
            ad_hoc_sign(binary)
    else:
        for binary in seen:
            _strip_native(binary, target)
    return dependencies
