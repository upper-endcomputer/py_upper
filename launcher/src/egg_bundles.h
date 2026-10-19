#pragma once

// Reads the merged application bundles out of an encrypted egg without a
// CPython interpreter. The egg is MAGIC + XOR(zip, SHAKE-256(key)); bundle
// members are stored uncompressed by tools/py_upper/egg.py so this parser
// needs no zlib or lzma. Only `py_upper_bundles/...` entries are returned.
//
// The launcher calls this before Py_Initialize: PyImport_ExtendInittab only
// accepts modules while the interpreter is unborn, so the bundles cannot wait
// for the Python-side egg loader.

#include "sha3.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <set>
#include <string>
#include <vector>

namespace pyupper {

struct EggBundleFile {
    std::string name;                  // Zip member name, always UTF-8/ASCII here.
    std::vector<std::uint8_t> data;    // Exact file bytes.
};

namespace egg_detail {

constexpr std::uint32_t kEndOfCentralDirectory = 0x06054b50;
constexpr std::uint32_t kCentralDirectoryEntry = 0x02014b50;
constexpr std::uint32_t kLocalFileHeader = 0x04034b50;

inline int hex_value(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

inline bool parse_key(const std::string& text, std::vector<std::uint8_t>& key, std::string& error) {
    if (text.empty()) {
        error = "egg key is empty";
        return false;
    }
    if (text.size() % 2 != 0) {
        error = "egg key is not an even-length hexadecimal string";
        return false;
    }
    key.clear();
    key.reserve(text.size() / 2);
    for (std::size_t i = 0; i < text.size(); i += 2) {
        const int high = hex_value(text[i]);
        const int low = hex_value(text[i + 1]);
        if (high < 0 || low < 0) {
            error = "egg key is not hexadecimal";
            return false;
        }
        key.push_back(static_cast<std::uint8_t>((high << 4) | low));
    }
    if (key.size() < 16) {
        error = "egg key is shorter than 16 bytes";
        return false;
    }
    return true;
}

inline bool read_u16(const std::vector<std::uint8_t>& data, std::size_t offset, std::uint16_t& value) {
    if (offset + 2 > data.size()) return false;
    value = static_cast<std::uint16_t>(data[offset]) |
            static_cast<std::uint16_t>(data[offset + 1] << 8);
    return true;
}

inline bool read_u32(const std::vector<std::uint8_t>& data, std::size_t offset, std::uint32_t& value) {
    if (offset + 4 > data.size()) return false;
    value = static_cast<std::uint32_t>(data[offset]) |
            (static_cast<std::uint32_t>(data[offset + 1]) << 8) |
            (static_cast<std::uint32_t>(data[offset + 2]) << 16) |
            (static_cast<std::uint32_t>(data[offset + 3]) << 24);
    return true;
}

inline bool safe_member_name(const std::string& name, const std::string& prefix, std::string& error) {
    if (name.compare(0, prefix.size(), prefix) != 0) return false;
    if (name.size() <= prefix.size() || name.back() == '/') {
        error = "bundle member has no file name: " + name;
        return false;
    }
    if (name.find('\\') != std::string::npos || name.find('\0') != std::string::npos ||
        name.front() == '/') {
        error = "unsafe bundle member: " + name;
        return false;
    }
    std::size_t start = 0;
    while (start <= name.size()) {
        const std::size_t slash = name.find('/', start);
        const std::string part = name.substr(start, slash == std::string::npos ? std::string::npos : slash - start);
        if (part.empty() || part == "." || part == "..") {
            error = "unsafe bundle member: " + name;
            return false;
        }
        if (slash == std::string::npos) break;
        start = slash + 1;
    }
    return true;
}

inline std::string hex16(const std::vector<std::uint8_t>& bytes) {
    static const char* digits = "0123456789abcdef";
    std::string text;
    text.reserve(bytes.size() * 2);
    for (std::uint8_t byte : bytes) {
        text.push_back(digits[byte >> 4]);
        text.push_back(digits[byte & 0x0f]);
    }
    return text;
}

}  // namespace egg_detail

inline bool read_egg_bundles(const std::filesystem::path& egg_path,
                             const std::string& key_hex,
                             const std::string& prefix,
                             std::vector<EggBundleFile>& files,
                             std::string& cache_key,
                             std::string& error) {
    namespace detail = egg_detail;

    files.clear();
    cache_key.clear();
    error.clear();

    std::ifstream stream(egg_path, std::ios::binary);
    if (!stream) {
        error = "cannot read egg: " + egg_path.string();
        return false;
    }
    std::vector<std::uint8_t> raw((std::istreambuf_iterator<char>(stream)),
                                  std::istreambuf_iterator<char>());
    if (raw.empty()) {
        error = "egg is empty";
        return false;
    }

    static const std::uint8_t magic[] = {'W', 'B', 'E', 'G', 'G', '0', '1', '!'};
    std::vector<std::uint8_t> payload;
    if (raw.size() >= sizeof(magic) && std::equal(std::begin(magic), std::end(magic), raw.begin())) {
        std::vector<std::uint8_t> key;
        if (!detail::parse_key(key_hex, key, error)) return false;
        payload.assign(raw.begin() + static_cast<std::ptrdiff_t>(sizeof(magic)), raw.end());
        shake256_xor(payload.data(), payload.size(), key.data(), key.size());
    } else {
        // A plain zip is accepted, mirroring the Python loader.
        payload = std::move(raw);
    }

    std::vector<std::uint8_t> digest(16, 0);
    shake256_xor(digest.data(), digest.size(), payload.data(), payload.size());
    cache_key = detail::hex16(digest);

    std::size_t end = std::string::npos;
    if (payload.size() >= 22) {
        const std::size_t last = payload.size() - 22;
        const std::size_t first = last > 0xffff ? last - 0xffff : 0;
        for (std::size_t offset = last + 1; offset-- > first;) {
            std::uint32_t signature = 0;
            if (detail::read_u32(payload, offset, signature) && signature == detail::kEndOfCentralDirectory) {
                end = offset;
                break;
            }
        }
    }
    if (end == std::string::npos) {
        error = "egg payload is not a zip archive";
        return false;
    }

    std::uint16_t count = 0;
    std::uint32_t directory_size = 0;
    std::uint32_t directory_offset = 0;
    if (!detail::read_u16(payload, end + 10, count) ||
        !detail::read_u32(payload, end + 12, directory_size) ||
        !detail::read_u32(payload, end + 16, directory_offset)) {
        error = "truncated zip end record";
        return false;
    }
    if (count == 0xffff || directory_offset == 0xffffffff || directory_size == 0xffffffff) {
        error = "ZIP64 eggs are not supported";
        return false;
    }
    if (static_cast<std::size_t>(directory_offset) + directory_size > payload.size()) {
        error = "zip central directory is outside the egg";
        return false;
    }

    std::set<std::string> seen;
    std::size_t cursor = directory_offset;
    for (std::uint16_t index = 0; index < count; ++index) {
        std::uint32_t signature = 0;
        if (!detail::read_u32(payload, cursor, signature) || signature != detail::kCentralDirectoryEntry) {
            error = "bad zip central directory entry";
            return false;
        }
        std::uint16_t flags = 0;
        std::uint16_t method = 0;
        std::uint32_t compressed_size = 0;
        std::uint32_t uncompressed_size = 0;
        std::uint16_t name_length = 0;
        std::uint16_t extra_length = 0;
        std::uint16_t comment_length = 0;
        std::uint32_t local_offset = 0;
        if (!detail::read_u16(payload, cursor + 8, flags) ||
            !detail::read_u16(payload, cursor + 10, method) ||
            !detail::read_u32(payload, cursor + 20, compressed_size) ||
            !detail::read_u32(payload, cursor + 24, uncompressed_size) ||
            !detail::read_u16(payload, cursor + 28, name_length) ||
            !detail::read_u16(payload, cursor + 30, extra_length) ||
            !detail::read_u16(payload, cursor + 32, comment_length) ||
            !detail::read_u32(payload, cursor + 42, local_offset)) {
            error = "truncated zip central directory entry";
            return false;
        }
        const std::size_t next = cursor + 46 + name_length + extra_length + comment_length;
        if (cursor + 46 + name_length > payload.size() || next > payload.size()) {
            error = "zip entry name is outside the egg";
            return false;
        }
        const std::string name(reinterpret_cast<const char*>(payload.data() + cursor + 46), name_length);
        cursor = next;

        if (name.compare(0, prefix.size(), prefix) != 0) continue;
        if (flags & 0x0001) {
            error = "zip member is encrypted: " + name;
            return false;
        }
        if (method != 0 || compressed_size != uncompressed_size) {
            error = "bundle member must be stored uncompressed: " + name;
            return false;
        }
        if (!detail::safe_member_name(name, prefix, error)) return false;
        if (!seen.insert(name).second) {
            error = "duplicate bundle member: " + name;
            return false;
        }
        if (local_offset + 30 > payload.size()) {
            error = "zip local header is outside the egg: " + name;
            return false;
        }
        std::uint32_t local_signature = 0;
        std::uint16_t local_name_length = 0;
        std::uint16_t local_extra_length = 0;
        if (!detail::read_u32(payload, local_offset, local_signature) ||
            local_signature != detail::kLocalFileHeader ||
            !detail::read_u16(payload, local_offset + 26, local_name_length) ||
            !detail::read_u16(payload, local_offset + 28, local_extra_length)) {
            error = "bad zip local header: " + name;
            return false;
        }
        if (local_name_length != name_length ||
            local_offset + 30 + local_name_length + local_extra_length > payload.size()) {
            error = "zip local header does not match the central directory: " + name;
            return false;
        }
        if (!std::equal(name.begin(), name.end(),
                        reinterpret_cast<const char*>(payload.data() + local_offset + 30))) {
            error = "zip member name mismatch: " + name;
            return false;
        }
        const std::size_t data_offset = local_offset + 30 + local_name_length + local_extra_length;
        if (data_offset + compressed_size > payload.size()) {
            error = "zip member data is outside the egg: " + name;
            return false;
        }
        EggBundleFile file;
        file.name = name;
        file.data.assign(payload.begin() + static_cast<std::ptrdiff_t>(data_offset),
                         payload.begin() + static_cast<std::ptrdiff_t>(data_offset + compressed_size));
        files.push_back(std::move(file));
    }
    return true;
}

}  // namespace pyupper
