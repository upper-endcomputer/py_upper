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


def inspect(path: Path) -> BinaryInfo:
    data = path.read_bytes()[:4096]
    if data[:2] == b"MZ" and len(data) >= 0x40:
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if pe + 6 <= len(data) and data[pe:pe + 4] == b"PE\0\0":
            machine = struct.unpack_from("<H", data, pe + 4)[0]
            return BinaryInfo(path, "PE", PE_MACHINE.get(machine, f"0x{machine:04x}"))
    if len(data) >= 4:
        magic = struct.unpack_from(">I", data, 0)[0]
        if magic in (0xFEEDFACF, 0xCFFAEDFE, 0xFEEDFACE, 0xCEFAEDFE):
            little = magic in (0xFEEDFACF, 0xFEEDFACE)
            endian = "<" if little else ">"
            cputype = struct.unpack_from(endian + "I", data, 4)[0]
            return BinaryInfo(path, "Mach-O", MACH_CPU.get(cputype, f"0x{cputype:08x}"))
        if magic in (0xCAFEBABE, 0xBEBAFECA, 0xCAFEBABF, 0xBFBAFECA):
            # Fat binaries are accepted only when they contain the requested slice.
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


def expected_arch(target) -> str:
    return target.arch


def verify_arch(path: Path, target) -> BinaryInfo:
    info = inspect(path)
    if info.arch != expected_arch(target) and not (info.arch == "universal" and target.arch in {"x86_64", "arm64"}):
        raise RuntimeError(f"Architecture mismatch: {path.name}: {info.arch}, expected {target.arch}")
    return info
