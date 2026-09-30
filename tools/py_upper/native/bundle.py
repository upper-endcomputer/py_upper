from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from ..config import BUILD, app_name
from .deps import Dependency, _system_dependency, dependency_names, resolve_dependency
from .inspect import verify_arch


def bundle_native_dependencies(root: Path, target, env: dict[str, str] | None = None) -> list[Dependency]:
    """Validate and close the native dependency graph inside the final bundle."""
    site = root / "site-packages"
    runtime = root / "runtime"
    search_roots = [site, runtime, root]
    suffixes = {".pyd", ".dll", ".exe"} if target.os == "windows" else ({".so", ".dylib"} if target.os == "macos" else {".so"})
    queue = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in suffixes]
    # Unix launchers have no extension; the fixed application entrypoint is
    # nevertheless part of the native dependency graph.
    name = app_name()
    launcher = root / (f"{name}.exe" if target.os == "windows" else name)
    if launcher.is_file():
        queue.append(launcher)
    seen: set[Path] = set()
    deps: list[Dependency] = []
    while queue:
        source = queue.pop(0).resolve()
        if source in seen:
            continue
        seen.add(source)
        verify_arch(source, target)
        for name in dependency_names(source, env):
            if _system_dependency(name, target):
                continue
            resolved = resolve_dependency(name, search_roots, source)
            if resolved is None:
                deps.append(Dependency(source, name, None, True))
                continue
            # Dependencies already inside the final bundle are fine. If a tool
            # resolves a library outside it, copy that library next to its owner.
            try:
                resolved.relative_to(root.resolve())
                inside = True
            except ValueError:
                inside = False
            if not inside:
                verify_arch(resolved, target)
                destination = source.parent / resolved.name
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(resolved, destination)
                    queue.append(destination)
                resolved = destination.resolve()
            deps.append(Dependency(source, name, resolved, False))

    report_dir = BUILD / "native-deps" / target.key
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(
        json.dumps([
            {
                "owner": str(d.owner.relative_to(root)),
                "name": d.name,
                "resolved": str(d.resolved.relative_to(root)) if d.resolved else None,
                "unresolved": d.external,
            } for d in deps
        ], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    unresolved = [d for d in deps if d.external]
    if unresolved:
        lines = [f"{d.owner.name}: {d.name}" for d in unresolved]
        raise RuntimeError("Unresolved native dependencies:\n  " + "\n  ".join(lines))

    if target.os == "linux" and shutil.which("patchelf"):
        for binary in seen:
            if binary.suffix.lower() == ".so":
                subprocess.run(["patchelf", "--set-rpath", "$ORIGIN", str(binary)], check=True)
    if target.os == "macos" and shutil.which("install_name_tool"):
        for dependency in deps:
            if dependency.external or dependency.resolved is None:
                continue
            binary = dependency.owner
            if binary.suffix.lower() not in {".so", ".dylib"} and binary.name != app_name():
                continue
            try:
                rel = os.path.relpath(dependency.resolved, binary.parent)
            except ValueError:
                continue
            new_name = "@loader_path/" + rel.replace(os.sep, "/")
            if dependency.name == new_name:
                continue
            subprocess.run(
                ["install_name_tool", "-change", dependency.name, new_name, str(binary)],
                check=True,
            )
    return deps
