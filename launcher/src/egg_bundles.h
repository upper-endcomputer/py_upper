/**
 * @file egg_bundles.h
 * @brief 在 CPython 初始化前认证并提取合并 bundle；校验成员范围和归档结构后才交给加载器。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
#pragma once

// Reads the merged application bundles out of an authenticated egg without a
// CPython interpreter.
//
// Egg layout v2:
//   MAGIC2 (8) + SALT (16) + TAG (32) + XOR(zip bytes, SHAKE-256(key + SALT))
//   TAG = SHAKE-256(key + MAGIC2 + SALT + ciphertext)[:32]
// Untagged and legacy archives are not accepted.
//
// The launcher calls this before Py_Initialize: PyImport_ExtendInittab only
// accepts modules while the interpreter is unborn, so the bundles cannot wait
// for the Python-side egg loader.
//
// Cost note: verifying the tag costs one absorb pass over the ciphertext, and
// decrypting costs one squeeze pass. That is exactly the two passes this
// function already paid before authentication existed (decrypt, then digest the
// plaintext for the cache key), because the tag replaces that plaintext digest
// as the cache key.
// 两次遍历的结构相近不代表认证耗时为零；实际成本需在目标机器上测量。

#include "sha3.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <limits>
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

/** @brief 将单个十六进制字符转为半字节；无效字符返回 -1。 */
inline int hex_value(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/** @brief 解析至少 16 字节的十六进制密钥；失败返回 false 并设置 error。 */
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

/** @brief 从 ZIP 字节数组按小端读取 16 位数；越界返回 false。 */
inline bool read_u16(const std::vector<std::uint8_t>& data, std::size_t offset, std::uint16_t& value) {
    if (offset + 2 > data.size()) return false;
    value = static_cast<std::uint16_t>(data[offset]) |
            static_cast<std::uint16_t>(data[offset + 1] << 8);
    return true;
}

/** @brief 从 ZIP 字节数组按小端读取 32 位数；越界返回 false。 */
inline bool read_u32(const std::vector<std::uint8_t>& data, std::size_t offset, std::uint32_t& value) {
    if (offset + 4 > data.size()) return false;
    value = static_cast<std::uint32_t>(data[offset]) |
            (static_cast<std::uint32_t>(data[offset + 1]) << 8) |
            (static_cast<std::uint32_t>(data[offset + 2]) << 16) |
            (static_cast<std::uint32_t>(data[offset + 3]) << 24);
    return true;
}

/** @brief 检查合并库成员前缀及相对路径段；不匹配或不安全时返回 false。 */
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

/** @brief 把 length 字节编码为小写十六进制字符串，输入缓冲区保持借用。 */
inline std::string hex(const std::uint8_t* bytes, std::size_t length) {
    static const char* digits = "0123456789abcdef";
    std::string text;
    text.reserve(length * 2);
    for (std::size_t i = 0; i < length; ++i) {
        text.push_back(digits[bytes[i] >> 4]);
        text.push_back(digits[bytes[i] & 0x0f]);
    }
    return text;
}

// Constant-time compare: the tag check must not leak how many leading bytes
// matched, even though an attacker reaching it already controls the archive.
/** @brief 遍历完整指定长度比较标签，避免按首个差异提前返回。 */
inline bool equal_constant_time(const std::uint8_t* left, const std::uint8_t* right, std::size_t length) {
    unsigned difference = 0;
    for (std::size_t i = 0; i < length; ++i) difference |= static_cast<unsigned>(left[i] ^ right[i]);
    return difference == 0;
}

}  // namespace egg_detail

// Authenticate the current egg format before decrypting it.
//
// `cache_key` is the current format authentication tag: it identifies
// the exact archive a cache directory was produced from, so a rebuilt egg never
// reuses a stale cache.
/** @brief 仅接受当前认证格式，先校验标签再解密；payload 和 cache_key 接收成功结果。 */
inline bool open_egg(const std::vector<std::uint8_t>& raw,
                     const std::string& key_hex,
                     std::vector<std::uint8_t>& payload,
                     std::string& cache_key,
                     std::string& error) {
    namespace detail = egg_detail;

    static const std::uint8_t magic_v2[] = {'W', 'B', 'E', 'G', 'G', '0', '2', '!'};
    constexpr std::size_t kSaltSize = 16;
    constexpr std::size_t kTagSize = 32;
    constexpr std::size_t kHeaderV2 = sizeof(magic_v2) + kSaltSize;

    if (raw.empty()) {
        error = "egg is empty";
        return false;
    }

    std::vector<std::uint8_t> key;
    if (!detail::parse_key(key_hex, key, error)) return false;

    if (raw.size() >= sizeof(magic_v2) &&
        std::equal(std::begin(magic_v2), std::end(magic_v2), raw.begin())) {
        if (raw.size() < kHeaderV2 + kTagSize) {
            error = "egg is truncated";
            return false;
        }
        const std::uint8_t* header = raw.data();
        const std::uint8_t* tag = raw.data() + kHeaderV2;
        const std::uint8_t* ciphertext = raw.data() + kHeaderV2 + kTagSize;
        const std::size_t ciphertext_size = raw.size() - kHeaderV2 - kTagSize;

        // TAG = SHAKE-256(key + header + ciphertext)[:32]
        Shake256 shake;
        shake.absorb(key.data(), key.size());
        shake.absorb(header, kHeaderV2);
        shake.absorb(ciphertext, ciphertext_size);
        shake.finalize();
        std::uint8_t expected[kTagSize];
        shake.squeeze(expected, kTagSize);
        if (!detail::equal_constant_time(tag, expected, kTagSize)) {
            error = "egg authentication failed: archive was rebuilt or edited";
            return false;
        }

        // The key stream is SHAKE-256(key + SALT), so the same plaintext encrypts
        // to different bytes per build.
        std::vector<std::uint8_t> stream_key(key);
        stream_key.insert(stream_key.end(), header + sizeof(magic_v2), header + kHeaderV2);
        payload.assign(ciphertext, ciphertext + ciphertext_size);
        shake256_xor(payload.data(), payload.size(), stream_key.data(), stream_key.size());
        cache_key = detail::hex(tag, kTagSize);
        return true;
    }

    error = "egg is not authenticated: rebuild it with this version of appbuild";
    return false;
}

// One in-memory result feeds both the pre-init bundle parser and the Python
// loader. Nothing writes the plaintext zip to disk.
struct EggContents {
    std::vector<std::uint8_t> payload;   // Decrypted zip bytes.
    std::string cache_key;               // Authentication tag.
    std::string error;
    bool ok = false;
};

/** @brief 完整读取归档并认证解密，返回拥有 payload 内存的结果对象。 */
inline EggContents read_egg(const std::filesystem::path& egg_path,
                            const std::string& key_hex) {
    EggContents result;
    std::ifstream stream(egg_path, std::ios::binary | std::ios::ate);
    if (!stream) {
        result.error = "cannot open egg";
        return result;
    }
    const std::streamoff size = stream.tellg();
    std::vector<std::uint8_t> raw;
    if (size < 0 || size > (std::numeric_limits<std::streamsize>::max)() ||
        static_cast<std::uintmax_t>(size) > raw.max_size()) {
        result.error = "invalid egg file size";
        return result;
    }
    raw.resize(static_cast<std::size_t>(size));
    stream.seekg(0);
    if (!stream || (!raw.empty() && !stream.read(reinterpret_cast<char*>(raw.data()),
                                                 static_cast<std::streamsize>(size)))) {
        result.error = "cannot read egg";
        return result;
    }
    result.ok = open_egg(raw, key_hex, result.payload, result.cache_key, result.error);
    return result;
}

// Parses bundle members out of an already-decrypted payload.
/** @brief 从已认证 ZIP 中提取指定前缀的未压缩合并库；拒绝 ZIP64、重复和越界条目。 */
inline bool read_bundles_from_payload(const std::vector<std::uint8_t>& payload,
                                      const std::string& prefix,
                                      std::vector<EggBundleFile>& files,
                                      std::string& error) {
    namespace detail = egg_detail;

    files.clear();
    error.clear();

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
