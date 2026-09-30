from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from ..config import BUILD
from .deps import Dependency, scan_tree


def bundle_native_dependencies(root: Path, target, env: dict[str, str] | None = None) -> list[Dependency]:
    """Validate native dependency closure without copying host OS binaries."""
    deps = scan_tree(root, target, env)
    report_dir = BUILD / "native-deps" / target.key
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(
        json.dumps(
            [
                {
                    "owner": str(d.owner.relative_to(root)),
                    "name": d.name,
                    "resolved": str(d.resolved.relative_to(root)) if d.resolved else None,
                    "unresolved": d.external,
                }
                for d in deps
            ],
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    unresolved = [d for d in deps if d.external]
    if unresolved:
        lines = [f"{d.owner.name}: {d.name}" for d in unresolved]
        raise RuntimeError("Unresolved native dependencies:\n  " + "\n  ".join(lines))
    return deps


def _system_dependency(dep: str, target) -> bool:
    name = Path(dep).name.lower()
    if target.os == "windows":
        return name in {
            "kernel32.dll","user32.dll","advapi32.dll","ole32.dll","oleaut32.dll",
            "shell32.dll","gdi32.dll","ws2_32.dll","ucrtbase.dll","vcruntime140.dll",
            "vcruntime140_1.dll","msvcp140.dll","ntdll.dll"
        } or name.startswith(("api-ms-win-", "ext-ms-win-"))
    if target.os == "linux":
        return name in {
            "linux-vdso.so.1","libc.so.6","libm.so.6","libdl.so.2","libpthread.so.0",
            "librt.so.1","libutil.so.1","libresolv.so.2","libgcc_s.so.1","libstdc++.so.6",
            "ld-linux-x86-64.so.2","ld-linux-aarch64.so.1"
        }
    return dep.startswith(("/System/Library/","/usr/lib/")) or name.startswith(("libsystem.","libc++"))

def _resolve_dep(source: Path, dep: str, root: Path) -> Path | None:
    name=Path(dep).name
    candidates=[]
    if dep.startswith("@loader_path/"):
        candidates.append(source.parent / dep.split("/",1)[1])
    elif dep.startswith("$ORIGIN/"):
        candidates.append(source.parent / dep.split("/",1)[1])
    elif dep.startswith("@rpath/"):
        candidates.extend([root / dep.split("/",1)[1], source.parent / dep.split("/",1)[1]])
    elif Path(dep).is_absolute():
        candidates.append(Path(dep))
    candidates.extend([source.parent/name, root/name])
    return next((p.resolve() for p in candidates if p.exists() and p.is_file()), None)

def bundle_native_dependencies(stage: Path, target, runtime_root: Path | None = None) -> list[Path]:
    site=stage/"site-packages"
    suffixes={".pyd",".dll"} if target.os=="windows" else ({".so",".dylib"} if target.os=="macos" else {".so"})
    copied=[]
    for source in [p for p in site.rglob("*") if p.is_file() and p.suffix.lower() in suffixes]:
        for dep in dependencies(source):
            if _system_dependency(dep,target): continue
            resolved=_resolve_dep(source,dep,site)
            if not resolved: continue
            try: resolved.relative_to(stage.resolve())
            except ValueError:
                verify_arch(resolved,target)
                dst=source.parent/resolved.name
                if not dst.exists(): shutil.copy2(resolved,dst); copied.append(dst)
    if target.os=="linux" and shutil.which("patchelf"):
        for p in site.rglob("*.so"):
            if p.is_file(): subprocess.run(["patchelf","--set-rpath","$ORIGIN",str(p)],check=True)
    if target.os=="macos" and shutil.which("install_name_tool") and shutil.which("otool"):
        for p in [x for x in site.rglob("*") if x.is_file() and x.suffix in {".so",".dylib"}]:
            q=subprocess.run(["otool","-L",str(p)],capture_output=True,text=True,check=True)
            for line in q.stdout.splitlines()[1:]:
                dep=line.strip().split(" ",1)[0] if line.strip() else ""
                if dep and not _system_dependency(dep,target) and not dep.startswith("@"):
                    subprocess.run(["install_name_tool","-change",dep,"@loader_path/"+Path(dep).name,str(p)],check=True)
    return copied
