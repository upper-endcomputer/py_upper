from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .inspect import inspect, verify_arch


@dataclass(frozen=True)
class Dependency:
    owner: Path
    name: str
    resolved: Path | None
    external: bool


def _dumpbin_dependencies(path: Path, env: dict[str, str] | None = None) -> list[str]:
    dumpbin = shutil.which("dumpbin", path=env.get("PATH") if env else None)
    if not dumpbin:
        return []
    p = subprocess.run([dumpbin, "/DEPENDENTS", str(path)], capture_output=True, text=True, env=env, check=True)
    result: list[str] = []
    active = False
    for line in p.stdout.splitlines():
        if "Image has the following dependencies" in line:
            active = True
            continue
        if active:
            s = line.strip()
            if s.startswith("Summary"):
                break
            if re.fullmatch(r"[A-Za-z0-9_.-]+\.dll", s, re.I):
                result.append(s)
    return result


def _pe_imports(path: Path) -> list[str]:
    """Small PE import-table reader; avoids making pefile a build dependency."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return []
    pe = int.from_bytes(data[0x3C:0x40], "little")
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    coff = pe + 4
    sections = int.from_bytes(data[coff + 2:coff + 4], "little")
    opt_size = int.from_bytes(data[coff + 16:coff + 18], "little")
    opt = coff + 20
    magic = int.from_bytes(data[opt:opt + 2], "little")
    if magic not in (0x10B, 0x20B):
        return []
    dd = opt + (96 if magic == 0x10B else 112)
    import_rva = int.from_bytes(data[dd + 8:dd + 12], "little")
    if not import_rva:
        return []
    sec_start = opt + opt_size
    sections_info = []
    for i in range(sections):
        s = sec_start + i * 40
        va = int.from_bytes(data[s + 12:s + 16], "little")
        raw_size = int.from_bytes(data[s + 16:s + 20], "little")
        raw_ptr = int.from_bytes(data[s + 20:s + 24], "little")
        sections_info.append((va, max(raw_size, 1), raw_ptr))

    def rva_to_offset(rva: int) -> int | None:
        for va, size, raw in sections_info:
            if va <= rva < va + size:
                return raw + (rva - va)
        return None

    off = rva_to_offset(import_rva)
    if off is None:
        return []
    names: list[str] = []
    while off + 20 <= len(data):
        oft = int.from_bytes(data[off:off + 4], "little")
        _time = int.from_bytes(data[off + 4:off + 8], "little")
        _fwd = int.from_bytes(data[off + 8:off + 12], "little")
        name_rva = int.from_bytes(data[off + 12:off + 16], "little")
        ft = int.from_bytes(data[off + 16:off + 20], "little")
        if not any((oft, name_rva, ft)):
            break
        noff = rva_to_offset(name_rva)
        if noff is None:
            break
        end = data.find(b"\0", noff)
        if end < 0:
            break
        name = data[noff:end].decode("ascii", errors="replace")
        if name.lower().endswith(".dll"):
            names.append(name)
        off += 20
    return names


def _mac_dependencies(path: Path) -> list[str]:
    otool = shutil.which("otool")
    if not otool:
        return []
    p = subprocess.run([otool, "-L", str(path)], capture_output=True, text=True, check=True)
    return [line.strip().split(" ", 1)[0] for line in p.stdout.splitlines()[1:] if line.strip()]


def dependency_names(path: Path, env: dict[str, str] | None = None) -> list[str]:
    if path.suffix.lower() in {".pyd", ".dll", ".exe"}:
        result = _dumpbin_dependencies(path, env)
        return result or _pe_imports(path)
    if path.suffix.lower() in {".so", ".dylib"}:
        return _mac_dependencies(path)
    return []


def _name_key(value: str) -> str:
    return os.path.basename(value).lower()


def resolve_dependency(name: str, search_roots: list[Path]) -> Path | None:
    key = _name_key(name)
    for root in search_roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.name.lower() == key:
                return p
    return None


def scan_tree(root: Path, target, env: dict[str, str] | None = None) -> list[Dependency]:
    binaries = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".pyd", ".dll", ".so", ".dylib"}]
    search_roots = [root]
    result: list[Dependency] = []
    for binary in binaries:
        try:
            verify_arch(binary, target)
        except Exception as exc:
            result.append(Dependency(binary, f"<architecture: {exc}>", None, True))
            continue
        for name in dependency_names(binary, env):
            base = _name_key(name)
            # These are supplied by the OS or bundled Python runtime and must not
            # be copied from the host OS into the application.
            system = {
                "kernel32.dll", "user32.dll", "advapi32.dll", "ws2_32.dll",
                "ole32.dll", "oleaut32.dll", "shell32.dll", "gdi32.dll",
                "bcrypt.dll", "crypt32.dll", "ntdll.dll", "msvcrt.dll",
                "ucrtbase.dll", "vcruntime140.dll", "vcruntime140_1.dll",
                "python313.dll", "libsystem.b.dylib", "libc++.1.dylib",
                "libc++abi.dylib", "libsystem.dylib",
            }
            if base in system or name.startswith("/usr/lib/") or name.startswith("/System/"):
                continue
            resolved = resolve_dependency(name, search_roots)
            result.append(Dependency(binary, name, resolved, resolved is None))
    return result


def dependencies(path: Path) -> list[str]:
    if path.suffix.lower() in {".pyd", ".dll", ".exe"}:
        dumpbin = shutil.which("dumpbin")
        if not dumpbin:
            return []
        p = subprocess.run([dumpbin, "/DEPENDENTS", str(path)], capture_output=True, text=True, check=True)
        return [line.strip() for line in p.stdout.splitlines()
                if re.fullmatch(r"[A-Za-z0-9_.-]+\.dll", line.strip(), re.I)]
    if path.suffix.lower() in {".so", ".dylib"}:
        if shutil.which("otool") and path.suffix.lower() == ".dylib":
            p = subprocess.run(["otool", "-L", str(path)], capture_output=True, text=True, check=True)
            return [line.strip().split(" ", 1)[0] for line in p.stdout.splitlines()[1:] if line.strip()]
        if shutil.which("ldd"):
            p = subprocess.run(["ldd", str(path)], capture_output=True, text=True, check=True)
            result=[]
            for line in p.stdout.splitlines():
                line=line.strip()
                if "=>" in line:
                    result.append(line.split("=>",1)[0].strip())
                elif line.startswith("/") or line.startswith("linux-vdso"):
                    result.append(line.split(" ",1)[0])
            return result
    return []
