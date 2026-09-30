from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .inspect import verify_arch


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
            value = line.strip()
            if value.startswith("Summary"):
                break
            if re.fullmatch(r"[A-Za-z0-9_.-]+\.dll", value, re.I):
                result.append(value)
    return result


def _pe_imports(path: Path) -> list[str]:
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return []
    pe = int.from_bytes(data[0x3C:0x40], "little")
    if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        return []
    coff = pe + 4
    sections = int.from_bytes(data[coff + 2:coff + 4], "little")
    opt_size = int.from_bytes(data[coff + 16:coff + 18], "little")
    opt = coff + 20
    magic = int.from_bytes(data[opt:opt + 2], "little")
    if magic not in (0x10B, 0x20B) or opt + opt_size > len(data):
        return []
    dd = opt + (96 if magic == 0x10B else 112)
    if dd + 12 > len(data):
        return []
    import_rva = int.from_bytes(data[dd + 8:dd + 12], "little")
    if not import_rva:
        return []
    sec_start = opt + opt_size
    sections_info = []
    for i in range(sections):
        off = sec_start + i * 40
        if off + 40 > len(data):
            return []
        va = int.from_bytes(data[off + 12:off + 16], "little")
        raw_size = int.from_bytes(data[off + 16:off + 20], "little")
        raw_ptr = int.from_bytes(data[off + 20:off + 24], "little")
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


def _elf_dependencies(path: Path) -> list[str]:
    readelf = shutil.which("readelf")
    if readelf:
        p = subprocess.run([readelf, "-d", str(path)], capture_output=True, text=True, check=True)
        result: list[str] = []
        for line in p.stdout.splitlines():
            m = re.search(r"Shared library: \[([^]]+)\]", line)
            if m:
                result.append(m.group(1))
        return result
    objdump = shutil.which("objdump")
    if objdump:
        p = subprocess.run([objdump, "-p", str(path)], capture_output=True, text=True, check=True)
        return [m.group(1) for line in p.stdout.splitlines() if (m := re.search(r"NEEDED\s+(.+)", line))]
    return []


def dependency_names(path: Path, env: dict[str, str] | None = None) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in {".pyd", ".dll", ".exe"}:
        return _dumpbin_dependencies(path, env) or _pe_imports(path)
    if suffix == ".dylib":
        return _mac_dependencies(path)
    if suffix == ".so":
        return _elf_dependencies(path)

    # The launcher itself has no extension on Unix. Detect native executables
    # by their file signature so its DT_NEEDED/load commands are also checked.
    try:
        head = path.read_bytes()[:4]
    except OSError:
        return []
    if head == b"\x7fELF":
        return _elf_dependencies(path)
    if head in {b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}:
        return _mac_dependencies(path)
    return []


def _name_key(value: str) -> str:
    return os.path.basename(value).lower()


def _mac_rpaths(path: Path) -> list[str]:
    """Return LC_RPATH entries from a Mach-O image."""
    otool = shutil.which("otool")
    if not otool:
        return []
    p = subprocess.run([otool, "-l", str(path)], capture_output=True, text=True, check=True)
    lines = p.stdout.splitlines()
    result: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for candidate in lines[index + 1 : index + 5]:
            value = candidate.strip()
            if value.startswith("path "):
                result.append(value[5:].split(" ", 1)[0])
                break
    return result


def _expand_mac_path(value: str, owner: Path, search_roots: list[Path]) -> list[Path]:
    if not value:
        return []
    if value.startswith("@loader_path/"):
        return [owner.parent / value.split("/", 1)[1]]
    if value == "@loader_path":
        return [owner.parent]
    executable_dirs: list[Path] = []
    for root in search_roots:
        candidate = root / "Contents" / "MacOS"
        if candidate.is_dir():
            executable_dirs.append(candidate)
            break
    if value.startswith("@executable_path/"):
        rel = value.split("/", 1)[1]
        return [base / rel for base in executable_dirs]
    if value == "@executable_path":
        return executable_dirs
    if value.startswith("$ORIGIN/") or value.startswith("${ORIGIN}/"):
        return [owner.parent / value.split("/", 1)[1]]
    return [Path(value)]


def _recursive_name_matches(search_roots: list[Path], key: str) -> list[Path]:
    matches: list[Path] = []
    seen: set[Path] = set()
    for root in search_roots:
        root = root.resolve()
        if root in seen or not root.exists():
            continue
        seen.add(root)
        direct = root / key
        candidates = [direct] if direct.is_file() else []
        candidates.extend(
            candidate for candidate in root.rglob("*")
            if candidate.is_file() and candidate.name.lower() == key and candidate != direct
        )
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                matches.append(resolved)
    return matches


def resolve_dependency(name: str, search_roots: list[Path], owner: Path | None = None) -> Path | None:
    """Resolve a native dependency using its platform-aware load path semantics.

    In particular, macOS ``@rpath`` is resolved from the owner's ``LC_RPATH``
    entries, then from the bundle search roots.  PBS places Tcl/Tk dylibs in
    nested directories under ``runtime/lib``, so basename lookup must recurse
    rather than checking only the top-level runtime directory.
    """
    key = _name_key(name)
    candidates: list[Path] = []

    if owner is not None:
        if name.startswith(("$ORIGIN/", "${ORIGIN}/", "@loader_path/")):
            prefix = name.split("/", 1)[1]
            candidates.append(owner.parent / prefix)
        elif name == "@loader_path":
            candidates.append(owner.parent)
        elif name.startswith("@rpath/"):
            rel = name.split("/", 1)[1]
            for rpath in _mac_rpaths(owner):
                for base in _expand_mac_path(rpath, owner, search_roots):
                    candidates.append(base / rel)
            candidates.extend(r / rel for r in search_roots)

    if Path(name).is_absolute():
        candidates.append(Path(name))

    candidates.extend(_recursive_name_matches(search_roots, key))
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file() and resolved.name.lower() == key:
            return resolved
    return None


def _system_dependency(name: str, target) -> bool:
    base = _name_key(name)
    if target.os == "windows":
        return base in {
            "kernel32.dll", "user32.dll", "advapi32.dll", "ws2_32.dll", "ole32.dll",
            "oleaut32.dll", "shell32.dll", "gdi32.dll", "bcrypt.dll", "crypt32.dll",
            "ntdll.dll", "msvcrt.dll", "ucrtbase.dll", "vcruntime140.dll", "vcruntime140_1.dll",
            "python313.dll",
        } or base.startswith(("api-ms-win-", "ext-ms-win-"))
    if target.os == "linux":
        return base in {
            "linux-vdso.so.1", "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0",
            "librt.so.1", "libutil.so.1", "libresolv.so.2", "libgcc_s.so.1", "libstdc++.so.6",
            "ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1",
        }
    return name.startswith(("/System/Library/", "/usr/lib/")) or base.startswith(("libsystem.", "libc++", "libobjc."))


def scan_tree(root: Path, target, env: dict[str, str] | None = None) -> list[Dependency]:
    suffixes = {".pyd", ".dll", ".exe"} if target.os == "windows" else ({".so", ".dylib"} if target.os == "macos" else {".so"})
    binaries = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in suffixes]
    result: list[Dependency] = []
    search_roots = [root]
    for binary in binaries:
        try:
            verify_arch(binary, target)
        except Exception as exc:
            result.append(Dependency(binary, f"<architecture: {exc}>", None, True))
            continue
        for name in dependency_names(binary, env):
            if _system_dependency(name, target):
                continue
            resolved = resolve_dependency(name, search_roots, binary)
            result.append(Dependency(binary, name, resolved, resolved is None))
    return result
