/**
 * @file sha3.h
 * @brief 实现与 Python SHAKE-256 对齐的 Keccak 运算；当前目标为小端平台，修改时必须对照协议测试向量。
 * @note 中文说明为本项目补充；原版权、许可和上游注释保留。
 */
#pragma once

// Minimal SHAKE-256 keystream and authentication, matching
// `hashlib.shake_256(...)`.
//
// The egg format is
//   MAGIC2 + SALT + TAG + XOR(zip bytes, SHAKE-256(key + SALT) stream)
// with TAG = SHAKE-256(key + MAGIC2 + SALT + ciphertext)[:32], so the launcher
// needs both the stream and a plain digest before CPython exists. This is a
// compact Keccak-f[1600] implementation.
//
// Absorb and squeeze work a 64-bit lane at a time through memcpy rather than
// byte by byte: the byte loop was ~150 MB/s, which made the authentication pass
// over a 1.4 MB egg cost more than everything else the launcher does before
// Py_Initialize. Lane-wise access is ~8x faster and keeps the authenticated
// format inside the cost the unauthenticated one already paid.

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace pyupper {

/** @brief 对 64 位值循环左移；调用点保证 bits 在 1 到 63 之间。 */
inline std::uint64_t rotl64(std::uint64_t value, int bits) {
    return (value << bits) | (value >> (64 - bits));
}

/** @brief 从至少 8 字节缓冲区读取一条 lane；当前实现要求小端宿主，不要求地址对齐。 */
inline std::uint64_t load_le64(const std::uint8_t* data) {
    std::uint64_t value = 0;
    std::memcpy(&value, data, sizeof(value));
    return value;
}

/** @brief 向至少 8 字节缓冲区写一条 lane；当前实现要求小端宿主。 */
inline void store_le64(std::uint8_t* data, std::uint64_t value) {
    std::memcpy(data, &value, sizeof(value));
}

/** @brief 原地执行 24 轮 Keccak-f 置换；state 必须提供 25 条 64 位 lane。 */
inline void keccak_f1600(std::uint64_t state[25]) {
    static const std::uint64_t round_constants[24] = {
        0x0000000000000001ULL, 0x0000000000008082ULL, 0x800000000000808aULL,
        0x8000000080008000ULL, 0x000000000000808bULL, 0x0000000080000001ULL,
        0x8000000080008081ULL, 0x8000000000008009ULL, 0x000000000000008aULL,
        0x0000000000000088ULL, 0x0000000080008009ULL, 0x000000008000000aULL,
        0x000000008000808bULL, 0x800000000000008bULL, 0x8000000000008089ULL,
        0x8000000000008003ULL, 0x8000000000008002ULL, 0x8000000000000080ULL,
        0x000000000000800aULL, 0x800000008000000aULL, 0x8000000080008081ULL,
        0x8000000000008080ULL, 0x0000000080000001ULL, 0x8000000080008008ULL,
    };
    static const int rotation[24] = {
        1, 3, 6, 10, 15, 21, 28, 36, 45, 55, 2, 14,
        27, 41, 56, 8, 25, 43, 62, 18, 39, 61, 20, 44,
    };
    static const int lanes[24] = {
        10, 7, 11, 17, 18, 3, 5, 16, 8, 21, 24, 4,
        15, 23, 19, 13, 12, 2, 20, 14, 22, 9, 6, 1,
    };

    for (int round = 0; round < 24; ++round) {
        std::uint64_t columns[5];
        for (int i = 0; i < 5; ++i) {
            columns[i] = state[i] ^ state[i + 5] ^ state[i + 10] ^ state[i + 15] ^ state[i + 20];
        }
        for (int i = 0; i < 5; ++i) {
            const std::uint64_t delta = columns[(i + 4) % 5] ^ rotl64(columns[(i + 1) % 5], 1);
            for (int j = 0; j < 25; j += 5) state[j + i] ^= delta;
        }

        std::uint64_t carry = state[1];
        for (int i = 0; i < 24; ++i) {
            const int lane = lanes[i];
            const std::uint64_t saved = state[lane];
            state[lane] = rotl64(carry, rotation[i]);
            carry = saved;
        }

        for (int j = 0; j < 25; j += 5) {
            for (int i = 0; i < 5; ++i) columns[i] = state[j + i];
            for (int i = 0; i < 5; ++i) {
                state[j + i] = columns[i] ^ ((~columns[(i + 1) % 5]) & columns[(i + 2) % 5]);
            }
        }
        state[0] ^= round_constants[round];
    }
}

class Shake256 {
public:
    /** @brief 初始化全零海绵状态，进入吸收阶段。 */
    Shake256() { std::memset(state_, 0, sizeof(state_)); }

    /** @brief 吸收 length 字节；仅在 finalize 前调用，data 在调用期间有效。 */
    void absorb(const std::uint8_t* data, std::size_t length) {
        // Fill the current partial lane first, so the bulk of the input moves
        // eight bytes per iteration.
        while (length > 0 && (position_ % 8) != 0) {
            state_[position_ / 8] ^= static_cast<std::uint64_t>(*data) << (8 * (position_ % 8));
            ++data;
            --length;
            if (++position_ == kRate) {
                keccak_f1600(state_);
                position_ = 0;
            }
        }
        while (length >= 8) {
            state_[position_ / 8] ^= load_le64(data);
            data += 8;
            length -= 8;
            position_ += 8;
            if (position_ == kRate) {
                keccak_f1600(state_);
                position_ = 0;
            }
        }
        while (length > 0) {
            state_[position_ / 8] ^= static_cast<std::uint64_t>(*data) << (8 * (position_ % 8));
            ++data;
            --length;
            if (++position_ == kRate) {
                keccak_f1600(state_);
                position_ = 0;
            }
        }
    }

    /** @brief 写入 SHAKE 域分离及填充并切换到挤出阶段；每个实例调用一次。 */
    void finalize() {
        state_[position_ / 8] ^= static_cast<std::uint64_t>(0x1f) << (8 * (position_ % 8));
        state_[(kRate - 1) / 8] ^= static_cast<std::uint64_t>(0x80) << (8 * ((kRate - 1) % 8));
        keccak_f1600(state_);
        position_ = 0;
    }

    // XORs the squeezed stream into `output`, which is what decryption wants.
    /** @brief 将 length 字节输出流异或到可写缓冲区；必须已 finalize。 */
    void squeeze_xor(std::uint8_t* output, std::size_t length) {
        while (length > 0 && (position_ % 8) != 0) {
            if (position_ == kRate) {
                keccak_f1600(state_);
                position_ = 0;
            }
            output[0] ^= static_cast<std::uint8_t>((state_[position_ / 8] >> (8 * (position_ % 8))) & 0xff);
            ++output;
            --length;
            ++position_;
        }
        while (length >= 8) {
            if (position_ == kRate) {
                keccak_f1600(state_);
                position_ = 0;
            }
            store_le64(output, load_le64(output) ^ state_[position_ / 8]);
            output += 8;
            length -= 8;
            position_ += 8;
        }
        while (length > 0) {
            if (position_ == kRate) {
                keccak_f1600(state_);
                position_ = 0;
            }
            output[0] ^= static_cast<std::uint8_t>((state_[position_ / 8] >> (8 * (position_ % 8))) & 0xff);
            ++output;
            --length;
            ++position_;
        }
    }

    // Writes `length` bytes of the squeezed stream, for a plain digest.
    /** @brief 写出 length 字节摘要，覆盖目标缓冲区；必须已 finalize。 */
    void squeeze(std::uint8_t* output, std::size_t length) {
        std::memset(output, 0, length);
        squeeze_xor(output, length);
    }

private:
    static constexpr std::size_t kRate = 136;  // SHAKE-256 rate: 200 - 2 * 32
    std::uint64_t state_[25];
    std::size_t position_ = 0;
};

/** @brief 以给定 key 为输入产生字节流并原地异或 data；所有长度单位为字节。 */
inline void shake256_xor(std::uint8_t* data, std::size_t length,
                         const std::uint8_t* key, std::size_t key_length) {
    Shake256 shake;
    shake.absorb(key, key_length);
    shake.finalize();
    shake.squeeze_xor(data, length);
}

/** @brief 依次吸收两段输入并写出指定字节数摘要；输入输出缓冲区由调用者持有。 */
inline void shake256_digest(const std::uint8_t* first, std::size_t first_length,
                            const std::uint8_t* second, std::size_t second_length,
                            std::uint8_t* output, std::size_t output_length) {
    Shake256 shake;
    shake.absorb(first, first_length);
    shake.absorb(second, second_length);
    shake.finalize();
    shake.squeeze(output, output_length);
}

}  // namespace pyupper
