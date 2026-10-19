#pragma once

// Minimal SHAKE-256 keystream, matching `hashlib.shake_256(key).digest(n)`.
//
// The egg format is MAGIC + XOR(zip bytes, SHAKE-256(key) stream), so the
// launcher needs the same stream before CPython exists. This is a compact
// Keccak-f[1600] implementation; it assumes a little-endian host, which every
// target this launcher ships for is.

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace pyupper {

inline std::uint64_t rotl64(std::uint64_t value, int bits) {
    return (value << bits) | (value >> (64 - bits));
}

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
    Shake256() { std::memset(state_, 0, sizeof(state_)); }

    void absorb(const std::uint8_t* data, std::size_t length) {
        for (std::size_t i = 0; i < length; ++i) {
            state_[position_ / 8] ^= static_cast<std::uint64_t>(data[i]) << (8 * (position_ % 8));
            if (++position_ == kRate) {
                keccak_f1600(state_);
                position_ = 0;
            }
        }
    }

    void finalize() {
        state_[position_ / 8] ^= static_cast<std::uint64_t>(0x1f) << (8 * (position_ % 8));
        state_[(kRate - 1) / 8] ^= static_cast<std::uint64_t>(0x80) << (8 * ((kRate - 1) % 8));
        keccak_f1600(state_);
        position_ = 0;
    }

    // XORs the squeezed stream into `output`, which is what decryption wants.
    void squeeze_xor(std::uint8_t* output, std::size_t length) {
        for (std::size_t i = 0; i < length; ++i) {
            if (position_ == kRate) {
                keccak_f1600(state_);
                position_ = 0;
            }
            output[i] ^= static_cast<std::uint8_t>((state_[position_ / 8] >> (8 * (position_ % 8))) & 0xff);
            ++position_;
        }
    }

private:
    static constexpr std::size_t kRate = 136;  // SHAKE-256 rate: 200 - 2 * 32
    std::uint64_t state_[25];
    std::size_t position_ = 0;
};

inline void shake256_xor(std::uint8_t* data, std::size_t length,
                         const std::uint8_t* key, std::size_t key_length) {
    Shake256 shake;
    shake.absorb(key, key_length);
    shake.finalize();
    shake.squeeze_xor(data, length);
}

}  // namespace pyupper
