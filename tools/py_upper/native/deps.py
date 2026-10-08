from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .inspect import inspect as inspect_binary, verify_arch
from ..config import python_version


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


def _mac_install_name(path: Path) -> str | None:
    """Return a dylib's LC_ID_DYLIB install name, when present."""
    otool = shutil.which("otool")
    if not otool:
        return None
    p = subprocess.run([otool, "-D", str(path)], capture_output=True, text=True, check=False)
    if p.returncode != 0:
        return None
    lines = [line.strip() for line in p.stdout.splitlines() if line.strip()]
    return lines[1] if len(lines) >= 2 else None


def _mac_dependencies(path: Path) -> list[str]:
    otool = shutil.which("otool")
    if not otool:
        return []
    p = subprocess.run([otool, "-L", str(path)], capture_output=True, text=True, check=True)
    result = [line.strip().split(" ", 1)[0] for line in p.stdout.splitlines()[1:] if line.strip()]
    # `otool -L` prints a dylib's own LC_ID_DYLIB as the first load command
    # entry. It is an identity, not a dependency and must not be resolved or
    # rewritten as if another library were being loaded.
    install_name = _mac_install_name(path)
    if install_name in result:
        result.remove(install_name)
    return result


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
    try:
        binary_format = inspect_binary(path).format
    except (OSError, ValueError):
        binary_format = ""

    if binary_format == "PE" or path.suffix.lower() in {".pyd", ".dll", ".exe"}:
        return _dumpbin_dependencies(path, env) or _pe_imports(path)
    if binary_format.startswith("Mach-O"):
        return _mac_dependencies(path)
    if binary_format == "ELF":
        return _elf_dependencies(path)

    suffix = path.suffix.lower()
    if suffix == ".dylib":
        return _mac_dependencies(path)
    if suffix == ".so":
        return _elf_dependencies(path)
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


class DependencyIndex:
    """Basename index over the bundle search roots.

    Qt ships hundreds of libraries that reference each other, so resolving each
    load name with its own recursive tree walk makes the dependency closure
    O(files x dependencies). The index is built once and extended as the
    closure copies new libraries into the bundle.
    """

    def __init__(self, roots: list[Path]):
        self._by_name: dict[str, list[Path]] = {}
        self._seen: set[Path] = set()
        for root in roots:
            try:
                root = root.resolve()
            except OSError:
                continue
            if root in self._seen or not root.exists():
                continue
            self._seen.add(root)
            for path in root.rglob("*"):
                if path.is_file():
                    self.add(path)

    def add(self, path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        key = resolved.name.lower()
        bucket = self._by_name.setdefault(key, [])
        if resolved not in bucket:
            bucket.append(resolved)

    def matches(self, key: str) -> list[Path]:
        return list(self._by_name.get(key, ()))


class DependencyResolver:
    """Resolve native load names against a bundle using platform semantics.

    In particular, macOS ``@rpath`` is resolved from the owner's ``LC_RPATH``
    entries, then from the bundle search roots. PBS places Tcl/Tk dylibs in
    nested directories under ``runtime/lib``, so basename lookup must recurse
    rather than checking only the top-level runtime directory.
    """

    def __init__(self, search_roots: list[Path], aliases: dict[str, Path] | None = None):
        self.search_roots = [Path(root) for root in search_roots]
        self.aliases = aliases or {}
        self.index = DependencyIndex(self.search_roots)
        self._rpaths: dict[Path, list[str]] = {}

    def rpaths(self, owner: Path) -> list[str]:
        if owner not in self._rpaths:
            self._rpaths[owner] = _mac_rpaths(owner)
        return self._rpaths[owner]

    def resolve(self, name: str, owner: Path | None = None) -> Path | None:
        key = _name_key(name)
        candidates: list[Path] = []

        alias = self.aliases.get(key)
        if alias is not None:
            candidates.append(alias)

        if owner is not None:
            if name.startswith(("$ORIGIN/", "${ORIGIN}/", "@loader_path/")):
                prefix = name.split("/", 1)[1]
                candidates.append(owner.parent / prefix)
            elif name == "@loader_path":
                candidates.append(owner.parent)
            elif name.startswith("@rpath/"):
                rel = name.split("/", 1)[1]
                for rpath in self.rpaths(owner):
                    for base in _expand_mac_path(rpath, owner, self.search_roots):
                        candidates.append(base / rel)
                candidates.extend(r / rel for r in self.search_roots)

        if Path(name).is_absolute():
            candidates.append(Path(name))

        candidates.extend(self.index.matches(key))
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            # An alias can intentionally map a load name to a differently named
            # file (for example PCBUSB.dylib -> @rpath/libPCBUSB.0.12.1.dylib).
            # In that case the alias target is authoritative and must not be
            # rejected merely because its basename differs from the load name.
            if alias is not None and resolved == Path(alias).resolve():
                return resolved
            if resolved.name.lower() == key:
                return resolved
        return None



# SONAMEs a Linux desktop is expected to provide. The X11, OpenGL, and Wayland
# stacks are bound to the host's display server and graphics driver, so a
# bundled copy would be both redundant and unusable, and the font and image
# codecs Qt's plugins load are equally part of the base system. Qt links all of
# them unconditionally, which is why every one of them reaches the closure.
LINUX_HOST_LIBRARIES = frozenset({
    # Loader, C runtime and compiler support libraries.
    "linux-vdso.so.1", "ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1",
    "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
    "libutil.so.1", "libresolv.so.2", "libgcc_s.so.1", "libstdc++.so.6",
    # Display server and graphics driver stack.
    "libx11.so.6", "libx11-xcb.so.1",
    "libxcb.so.1", "libxcb-cursor.so.0", "libxcb-icccm.so.4", "libxcb-image.so.0",
    "libxcb-keysyms.so.1", "libxcb-randr.so.0", "libxcb-render.so.0",
    "libxcb-render-util.so.0", "libxcb-shape.so.0", "libxcb-shm.so.0",
    "libxcb-sync.so.1", "libxcb-util.so.1", "libxcb-xfixes.so.0", "libxcb-xkb.so.1",
    "libxkbcommon.so.0", "libxkbcommon-x11.so.0",
    "libgl.so.1", "libegl.so.1", "libdrm.so.2",
    "libwayland-client.so.0", "libwayland-cursor.so.0",
    # Font and image codecs Qt's plugins link; the versions differ per wheel
    # build, so both the 5 and 6 generations of libtiff are listed.
    "libfreetype.so.6", "libtiff.so.5", "libtiff.so.6",
    "libwebp.so.7", "libwebpdemux.so.2", "libwebpmux.so.3",
})


# DLLs a Windows desktop provides, the counterpart of LINUX_HOST_LIBRARIES.
# They belong to the host OS: kernel and CRT, shell and windowing, the DirectX
# and Media Foundation stack, the security and networking APIs, ODBC, and the
# ICU library Windows has exposed since the Creators Update (Qt 6 links ICU,
# which is why the PySide6 wheels ship none). Qt links all of them
# unconditionally, so every one of them reaches the closure; bundling a copy
# would ship an unpatched binary next to the copy the OS keeps updated.
WINDOWS_HOST_LIBRARIES = frozenset({
    # Kernel, CRT and device management.
    "kernel32.dll", "ntdll.dll", "msvcrt.dll", "ucrtbase.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "advapi32.dll",
    "bcrypt.dll", "ncrypt.dll", "cfgmgr32.dll", "setupapi.dll",
    "winusb.dll", "hid.dll", "winscard.dll",
    # Shell, windowing and user interface.
    "user32.dll", "gdi32.dll", "shell32.dll", "shlwapi.dll", "comctl32.dll",
    "comdlg32.dll", "ole32.dll", "oleaut32.dll", "imm32.dll", "uxtheme.dll",
    "dwmapi.dll", "dcomp.dll", "propsys.dll", "uiautomationcore.dll",
    "wtsapi32.dll", "authz.dll", "userenv.dll", "mpr.dll", "netapi32.dll",
    "version.dll", "urlmon.dll",
    # Graphics, audio and media.
    "d2d1.dll", "d3d9.dll", "d3d11.dll", "d3d12.dll", "d3dcompiler_47.dll",
    "dxgi.dll", "dxva2.dll", "evr.dll", "mf.dll", "mfplat.dll",
    "mfreadwrite.dll", "mmdevapi.dll", "avrt.dll", "winmm.dll",
    "fontsub.dll", "dwrite.dll",
    # Networking, diagnostics, security and data access.
    "ws2_32.dll", "winhttp.dll", "iphlpapi.dll", "dnsapi.dll", "dhcpcsvc.dll",
    "secur32.dll", "crypt32.dll", "dbghelp.dll", "imagehlp.dll", "pdh.dll",
    "odbc32.dll", "icuuc.dll",
})


def _system_dependency(name: str, target) -> bool:
    base = _name_key(name)
    if target.os == "windows":
        # The target runtime's own CPython DLLs, which the launcher loads out of
        # the runtime directory: the version-specific library, and the stable
        # ABI forwarder that pulls it in for every abi3 extension (PySide6's
        # QtCore.pyd among them). Neither is ever a wheel payload, and the
        # version cannot be a constant of the set above.
        python_dlls = {"python3.dll", "python" + "".join(python_version().split(".")[:2]) + ".dll"}
        return (
            base in WINDOWS_HOST_LIBRARIES
            or base in python_dlls
            or base.startswith(("api-ms-win-", "ext-ms-win-"))
        )
    if target.os == "linux":
        return base in LINUX_HOST_LIBRARIES
    return name.startswith(("/System/Library/", "/usr/lib/")) or base.startswith(("libsystem.", "libc++", "libobjc."))


def scan_tree(root: Path, target, env: dict[str, str] | None = None) -> list[Dependency]:
    suffixes = {".pyd", ".dll", ".exe"} if target.os == "windows" else ({".so", ".dylib"} if target.os == "macos" else {".so"})
    binaries = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in suffixes]
    result: list[Dependency] = []
    search_roots = [root]
    resolver = DependencyResolver(search_roots)
    for binary in binaries:
        try:
            verify_arch(binary, target)
        except Exception as exc:
            result.append(Dependency(binary, f"<architecture: {exc}>", None, True))
            continue
        for name in dependency_names(binary, env):
            if _system_dependency(name, target):
                continue
            resolved = resolver.resolve(name, binary)
            result.append(Dependency(binary, name, resolved, resolved is None))
    return result
