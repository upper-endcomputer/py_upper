from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BinaryInfo:
    path: Path
    format: str
    arch: str


PE_MACHINE = {0x8664: "x86_64", 0xAA64: "arm64", 0x14C: "x86"}
MACH_CPU = {0x01000007: "x86_64", 0x0100000C: "arm64", 7: "x86"}

# ARM64EC images declare IMAGE_FILE_MACHINE_AMD64 (0x8664) so that x64 code can
# link against them, which makes the machine field alone indistinguishable from
# a real x64 image.  The .hexpthk section holding the ARM64EC hybrid export
# thunks is what tells them apart; a plain x64 image never carries one.  Windows
# on Arm64 loads ARM64EC images in Arm64 and x64 processes alike, and the CPython
# runtime ships one (vcruntime140_1.dll), so an arm64 target has to accept them.
_ARM64EC_MACHINE = 0x8664
_ARM64EC_SECTION = b".hexpthk"

# Image arch -> target arches that can load it under a different name.  ARM64EC
# binaries are not usable on x64-only hardware, so windows-x86_64 still rejects
# them.
_COMPATIBLE_TARGETS = {"arm64ec": frozenset({"arm64"})}

# Mach-O magic values are byte-order dependent.  The previous implementation
# treated FEEDFACF as little-endian, which reversed 0x0100000c (arm64) into
# 0x0c000001 and caused valid Apple Silicon dylibs to be rejected.
_MACH_LITTLE_MAGICS = {0xCFFAEDFE, 0xCEFAEDFE}
_MACH_BIG_MAGICS = {0xFEEDFACF, 0xFEEDFACE}
_FAT_BIG_MAGIC = 0xCAFEBABE
_FAT_LITTLE_MAGIC = 0xBEBAFECA
_FAT64_BIG_MAGIC = 0xCAFEBABF
_FAT64_LITTLE_MAGIC = 0xBFBAFECA


def _pe_has_arm64ec_section(data: bytes, pe: int) -> bool:
    """Whether the PE carries the ARM64EC metadata section.

    The section table follows the optional header and can sit past the first
    bytes read for the machine field, so it is looked up in the whole image.
    """
    if pe + 24 > len(data):
        return False
    section_count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    table = pe + 24 + optional_size
    entries = data[table:table + section_count * 40]
    return any(entries[offset:offset + 8].startswith(_ARM64EC_SECTION) for offset in range(0, len(entries) - 7, 40))


def _mach_arches(data: bytes, magic: int) -> set[str]:
    little = magic in _MACH_LITTLE_MAGICS
    endian = "<" if little else ">"
    cputype = struct.unpack_from(endian + "I", data, 4)[0]
    return {MACH_CPU.get(cputype, f"0x{cputype:08x}")}


def _fat_arches(data: bytes, magic: int) -> set[str]:
    little = magic in {_FAT_LITTLE_MAGIC, _FAT64_LITTLE_MAGIC}
    endian = "<" if little else ">"
    nfat_arch = struct.unpack_from(endian + "I", data, 4)[0]
    is_64 = magic in {_FAT64_BIG_MAGIC, _FAT64_LITTLE_MAGIC}
    entry_size = 32 if is_64 else 20
    if len(data) < 8 + nfat_arch * entry_size:
        raise ValueError(f"Truncated Mach-O universal header: {len(data)} bytes")
    arches: set[str] = set()
    for index in range(nfat_arch):
        offset = 8 + index * entry_size
        cputype = struct.unpack_from(endian + "I", data, offset)[0]
        arches.add(MACH_CPU.get(cputype, f"0x{cputype:08x}"))
    return arches


def inspect(path: Path) -> BinaryInfo:
    raw = path.read_bytes()
    data = raw[:4096]
    if data[:2] == b"MZ" and len(data) >= 0x40:
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if pe + 6 <= len(data) and data[pe:pe + 4] == b"PE\0\0":
            machine = struct.unpack_from("<H", data, pe + 4)[0]
            arch = PE_MACHINE.get(machine, f"0x{machine:04x}")
            if machine == _ARM64EC_MACHINE and _pe_has_arm64ec_section(raw, pe):
                arch = "arm64ec"
            return BinaryInfo(path, "PE", arch)
    if len(data) >= 4:
        magic = struct.unpack_from(">I", data, 0)[0]
        if magic in _MACH_LITTLE_MAGICS | _MACH_BIG_MAGICS:
            return BinaryInfo(path, "Mach-O", next(iter(_mach_arches(data, magic))))
        if magic in {_FAT_BIG_MAGIC, _FAT_LITTLE_MAGIC, _FAT64_BIG_MAGIC, _FAT64_LITTLE_MAGIC}:
            return BinaryInfo(path, "Mach-O universal", "universal")
    if len(data) >= 20 and data[:4] == b"\x7fELF":
        # e_ident[5] selects the ELF byte order. e_machine is at offset 18.
        if data[5] == 1:
            endian = "<"
        elif data[5] == 2:
            endian = ">"
        else:
            raise ValueError(f"Invalid ELF byte order: {path}")
        machine = struct.unpack_from(endian + "H", data, 18)[0]
        elf = {62: "x86_64", 183: "arm64"}
        return BinaryInfo(path, "ELF", elf.get(machine, f"0x{machine:04x}"))
    raise ValueError(f"Unsupported or invalid native binary: {path}")


_TARGET_FORMATS = {"windows": "PE", "macos": "Mach-O", "linux": "ELF"}


def target_format(target) -> str:
    """Return the native container format the target OS can actually load."""
    try:
        return _TARGET_FORMATS[target.os]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported target OS: {target.os}") from exc


def expected_arch(target) -> str:
    return target.arch


def compatible_with(arch: str, target_arch: str) -> bool:
    """Whether a target of ``target_arch`` can load an image of ``arch``."""
    return arch == target_arch or target_arch in _COMPATIBLE_TARGETS.get(arch, ())


def verify_arch(path: Path, target) -> BinaryInfo:
    info = inspect(path)
    if info.arch == "universal":
        data = path.read_bytes()[:4096]
        magic = struct.unpack_from(">I", data, 0)[0]
        arches = _fat_arches(data, magic)
        if expected_arch(target) not in arches:
            raise RuntimeError(
                f"Architecture mismatch: {path.name}: universal ({', '.join(sorted(arches))}), "
                f"expected {target.arch}"
            )
        return info
    if not compatible_with(info.arch, expected_arch(target)):
        raise RuntimeError(f"Architecture mismatch: {path.name}: {info.arch}, expected {target.arch}")
    return info
