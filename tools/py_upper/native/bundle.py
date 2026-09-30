from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from ..config import BUILD, app_name, optimize_config
from ..fs import copy_file_contents
from .deps import Dependency, _mac_install_name, _system_dependency, dependency_names, resolve_dependency
from .inspect import BinaryInfo, inspect, verify_arch


def _native_files(root: Path, target) -> list[Path]:
    """Return native images, including framework binaries without .dylib suffixes."""
    result: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if target.os == "windows" and suffix not in {".pyd", ".dll", ".exe"}:
            continue
        if target.os == "linux" and suffix != ".so":
            continue
        try:
            info = inspect(path)
        except (OSError, ValueError):
            continue
        if (target.os == "macos" and info.format.startswith("Mach-O")) or info.format in {"ELF", "PE"}:
            result.append(path)
    return sorted(set(result))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _maybe_remove_signature(path: Path) -> None:
    codesign = shutil.which("codesign")
    if codesign:
        subprocess.run([codesign, "--remove-signature", str(path)], capture_output=True, check=False)


def _ad_hoc_sign(path: Path) -> None:
    codesign = shutil.which("codesign")
    if codesign:
        result = subprocess.run([codesign, "--force", "--sign", "-", "--timestamp=none", str(path)], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"codesign failed for {path}: {result.stderr.strip()}")


def _mac_aliases(roots: list[Path]) -> dict[str, Path]:
    aliases: dict[str, Path] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                info = inspect(path)
            except (OSError, ValueError):
                continue
            if not info.format.startswith("Mach-O"):
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


def bundle_native_dependencies(root: Path, target, env: dict[str, str] | None = None) -> list[Dependency]:
    """Resolve and embed the native dependency closure of the application payload."""
    name = app_name()
    package_site = root / "Contents" / "Resources" / "site-packages" if target.os == "macos" else root / "site-packages"
    runtime_lib = root / "Contents" / "Resources" / "runtime" / "lib" if target.os == "macos" else root / "runtime" / "lib"
    search_roots = [package_site, runtime_lib, root]
    queue = _native_files(package_site, target)
    launcher = (root / "Contents" / "MacOS" / name) if target.os == "macos" else root / (f"{name}.exe" if target.os == "windows" else name)
    if launcher.is_file():
        queue.append(launcher)
    queue = list(dict.fromkeys(queue))
    aliases = _mac_aliases(search_roots) if target.os == "macos" else {}

    seen: set[Path] = set()
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
            resolved = resolve_dependency(dep_name, search_roots, source, aliases=aliases)
            if resolved is None:
                dependencies.append(Dependency(source, dep_name, None, True))
                continue
            if not _inside(resolved, root):
                verify_arch(resolved, target)
                destination = source.parent / resolved.name
                if destination.exists():
                    if destination.resolve() != resolved.resolve():
                        raise RuntimeError(f"Native dependency basename collision: {destination} != {resolved}")
                else:
                    copy_file_contents(resolved, destination)
                resolved = destination.resolve()
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
        raise RuntimeError("Unresolved native dependencies:\n  " + "\n  ".join(lines))

    # Linux RPATH rewriting is opt-in. Many wheels intentionally carry multiple
    # RPATH entries; replacing them unconditionally can break otherwise valid
    # third-party packages. The bundle copier already preserves existing RPATHs.
    if target.os == "linux" and shutil.which("patchelf") and bool(optimize_config().get("rewrite_rpath", False)):
        for binary in seen:
            if binary.suffix.lower() == ".so":
                subprocess.run(["patchelf", "--set-rpath", "$ORIGIN", str(binary)], check=True)

    if target.os == "macos" and shutil.which("install_name_tool"):
        native_images = [path for path in seen if inspect(path).format.startswith("Mach-O")]
        changed: set[Path] = set()
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
            if owner not in changed:
                _maybe_remove_signature(owner)
                changed.add(owner)
            subprocess.run(["install_name_tool", "-change", dep.name, new_name, str(owner)], check=True)
        for binary in native_images:
            if bool(optimize_config().get("strip_native", False)):
                _maybe_remove_signature(binary)
            _strip_native(binary, target)
        for binary in native_images:
            _ad_hoc_sign(binary)
    else:
        for binary in seen:
            _strip_native(binary, target)
    return dependencies
