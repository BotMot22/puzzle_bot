/*
 * Optimized SHA256 and RIPEMD160 for Bitcoin public key hashing.
 *
 * SHA256 of exactly 33 bytes (compressed pubkey):
 *   - Input is always 33 bytes -> 1 SHA256 block (64 bytes with padding)
 *   - Pre-compute the padding once
 *   - Use SHA-NI intrinsics if available
 *
 * RIPEMD160 of exactly 32 bytes (SHA256 output):
 *   - Input is always 32 bytes -> 1 RIPEMD160 block (64 bytes with padding)
 */

#ifndef SHA256_RMD160_FAST_H
#define SHA256_RMD160_FAST_H

#include <stdint.h>
#include <string.h>

/* ========== SHA256 for exactly 33 bytes ========== */

/* SHA256 initial hash values */
static const uint32_t sha256_H0[8] = {
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19
};

/* SHA256 round constants */
static const uint32_t sha256_K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
};

#define ROR32(x, n) (((x) >> (n)) | ((x) << (32 - (n))))
#define CH(x, y, z) (((x) & (y)) ^ (~(x) & (z)))
#define MAJ(x, y, z) (((x) & (y)) ^ ((x) & (z)) ^ ((y) & (z)))
#define EP0(x) (ROR32(x, 2) ^ ROR32(x, 13) ^ ROR32(x, 22))
#define EP1(x) (ROR32(x, 6) ^ ROR32(x, 11) ^ ROR32(x, 25))
#define SIG0(x) (ROR32(x, 7) ^ ROR32(x, 18) ^ ((x) >> 3))
#define SIG1(x) (ROR32(x, 17) ^ ROR32(x, 19) ^ ((x) >> 10))

static inline uint32_t be32(const unsigned char *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8)  | (uint32_t)p[3];
}

static inline void put_be32(unsigned char *p, uint32_t v) {
    p[0] = (v >> 24) & 0xFF;
    p[1] = (v >> 16) & 0xFF;
    p[2] = (v >> 8) & 0xFF;
    p[3] = v & 0xFF;
}

/* SHA256 of exactly 33 bytes -> 32-byte hash */
static inline void sha256_33(const unsigned char input[33], unsigned char output[32]) {
    /* Prepare the single 64-byte block:
     * bytes 0-32: input data (33 bytes)
     * byte 33: 0x80 (padding start)
     * bytes 34-61: zeros
     * bytes 62-63: length in bits = 33*8 = 264 = 0x0108 (big endian)
     */
    unsigned char block[64];
    memcpy(block, input, 33);
    block[33] = 0x80;
    memset(block + 34, 0, 24);
    /* Length in bits: 33 * 8 = 264 = 0x00000108 */
    block[56] = 0; block[57] = 0; block[58] = 0; block[59] = 0;
    block[60] = 0; block[61] = 0; block[62] = 0x01; block[63] = 0x08;

    /* Parse block into 16 words */
    uint32_t W[64];
    for (int i = 0; i < 16; i++) {
        W[i] = be32(block + i * 4);
    }
    /* Extend to 64 words */
    for (int i = 16; i < 64; i++) {
        W[i] = SIG1(W[i-2]) + W[i-7] + SIG0(W[i-15]) + W[i-16];
    }

    /* Initialize working variables */
    uint32_t a = sha256_H0[0], b = sha256_H0[1], c = sha256_H0[2], d = sha256_H0[3];
    uint32_t e = sha256_H0[4], f = sha256_H0[5], g = sha256_H0[6], h = sha256_H0[7];

    /* 64 rounds */
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + EP1(e) + CH(e, f, g) + sha256_K[i] + W[i];
        uint32_t t2 = EP0(a) + MAJ(a, b, c);
        h = g; g = f; f = e; e = d + t1;
        d = c; c = b; b = a; a = t1 + t2;
    }

    /* Output */
    put_be32(output,      a + sha256_H0[0]);
    put_be32(output + 4,  b + sha256_H0[1]);
    put_be32(output + 8,  c + sha256_H0[2]);
    put_be32(output + 12, d + sha256_H0[3]);
    put_be32(output + 16, e + sha256_H0[4]);
    put_be32(output + 20, f + sha256_H0[5]);
    put_be32(output + 24, g + sha256_H0[6]);
    put_be32(output + 28, h + sha256_H0[7]);
}

/* ========== RIPEMD160 for exactly 32 bytes ========== */

static const uint32_t rmd160_H0[5] = {
    0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0
};

#define F(x, y, z) ((x) ^ (y) ^ (z))
#define G(x, y, z) (((x) & (y)) | (~(x) & (z)))
#define H(x, y, z) (((x) | ~(y)) ^ (z))
#define I(x, y, z) (((x) & (z)) | ((y) & ~(z)))
#define J(x, y, z) ((x) ^ ((y) | ~(z)))

#define ROL32(x, n) (((x) << (n)) | ((x) >> (32 - (n))))

#define RMD_ROUND(a, b, c, d, e, f, x, k, s) { \
    a += f + x + k; \
    a = ROL32(a, s) + e; \
    c = ROL32(c, 10); \
}

/* RIPEMD160 of exactly 32 bytes -> 20-byte hash */
static inline void rmd160_32(const unsigned char input[32], unsigned char output[20]) {
    /* Prepare single 64-byte block:
     * bytes 0-31: input (32 bytes)
     * byte 32: 0x80
     * bytes 33-55: zeros
     * bytes 56-63: length in bits = 256 = 0x0100 (little-endian)
     */
    uint32_t X[16];
    /* Input in little-endian uint32 */
    for (int i = 0; i < 8; i++) {
        X[i] = (uint32_t)input[i*4] | ((uint32_t)input[i*4+1] << 8) |
               ((uint32_t)input[i*4+2] << 16) | ((uint32_t)input[i*4+3] << 24);
    }
    X[8]  = 0x00000080;  /* 0x80 after 32 bytes */
    X[9]  = 0; X[10] = 0; X[11] = 0;
    X[12] = 0; X[13] = 0;
    X[14] = 256; /* length in bits, little-endian */
    X[15] = 0;

    uint32_t al = rmd160_H0[0], bl = rmd160_H0[1], cl = rmd160_H0[2], dl = rmd160_H0[3], el = rmd160_H0[4];
    uint32_t ar = rmd160_H0[0], br = rmd160_H0[1], cr = rmd160_H0[2], dr = rmd160_H0[3], er = rmd160_H0[4];
    uint32_t t;

    /* Left rounds */
    /* Round 1: F, K=0x00000000 */
    RMD_ROUND(al, bl, cl, dl, el, F(bl,cl,dl), X[ 0], 0x00000000, 11);
    RMD_ROUND(el, al, bl, cl, dl, F(al,bl,cl), X[ 1], 0x00000000, 14);
    RMD_ROUND(dl, el, al, bl, cl, F(el,al,bl), X[ 2], 0x00000000, 15);
    RMD_ROUND(cl, dl, el, al, bl, F(dl,el,al), X[ 3], 0x00000000, 12);
    RMD_ROUND(bl, cl, dl, el, al, F(cl,dl,el), X[ 4], 0x00000000,  5);
    RMD_ROUND(al, bl, cl, dl, el, F(bl,cl,dl), X[ 5], 0x00000000,  8);
    RMD_ROUND(el, al, bl, cl, dl, F(al,bl,cl), X[ 6], 0x00000000,  7);
    RMD_ROUND(dl, el, al, bl, cl, F(el,al,bl), X[ 7], 0x00000000,  9);
    RMD_ROUND(cl, dl, el, al, bl, F(dl,el,al), X[ 8], 0x00000000, 11);
    RMD_ROUND(bl, cl, dl, el, al, F(cl,dl,el), X[ 9], 0x00000000, 13);
    RMD_ROUND(al, bl, cl, dl, el, F(bl,cl,dl), X[10], 0x00000000, 14);
    RMD_ROUND(el, al, bl, cl, dl, F(al,bl,cl), X[11], 0x00000000, 15);
    RMD_ROUND(dl, el, al, bl, cl, F(el,al,bl), X[12], 0x00000000,  6);
    RMD_ROUND(cl, dl, el, al, bl, F(dl,el,al), X[13], 0x00000000,  7);
    RMD_ROUND(bl, cl, dl, el, al, F(cl,dl,el), X[14], 0x00000000,  9);
    RMD_ROUND(al, bl, cl, dl, el, F(bl,cl,dl), X[15], 0x00000000,  8);

    /* Round 2: G, K=0x5A827999 */
    RMD_ROUND(el, al, bl, cl, dl, G(al,bl,cl), X[ 7], 0x5A827999,  7);
    RMD_ROUND(dl, el, al, bl, cl, G(el,al,bl), X[ 4], 0x5A827999,  6);
    RMD_ROUND(cl, dl, el, al, bl, G(dl,el,al), X[13], 0x5A827999,  8);
    RMD_ROUND(bl, cl, dl, el, al, G(cl,dl,el), X[ 1], 0x5A827999, 13);
    RMD_ROUND(al, bl, cl, dl, el, G(bl,cl,dl), X[10], 0x5A827999, 11);
    RMD_ROUND(el, al, bl, cl, dl, G(al,bl,cl), X[ 6], 0x5A827999,  9);
    RMD_ROUND(dl, el, al, bl, cl, G(el,al,bl), X[15], 0x5A827999,  7);
    RMD_ROUND(cl, dl, el, al, bl, G(dl,el,al), X[ 3], 0x5A827999, 15);
    RMD_ROUND(bl, cl, dl, el, al, G(cl,dl,el), X[12], 0x5A827999,  7);
    RMD_ROUND(al, bl, cl, dl, el, G(bl,cl,dl), X[ 0], 0x5A827999, 12);
    RMD_ROUND(el, al, bl, cl, dl, G(al,bl,cl), X[ 9], 0x5A827999, 15);
    RMD_ROUND(dl, el, al, bl, cl, G(el,al,bl), X[ 5], 0x5A827999,  9);
    RMD_ROUND(cl, dl, el, al, bl, G(dl,el,al), X[ 2], 0x5A827999, 11);
    RMD_ROUND(bl, cl, dl, el, al, G(cl,dl,el), X[14], 0x5A827999,  7);
    RMD_ROUND(al, bl, cl, dl, el, G(bl,cl,dl), X[11], 0x5A827999, 13);
    RMD_ROUND(el, al, bl, cl, dl, G(al,bl,cl), X[ 8], 0x5A827999, 12);

    /* Round 3: H, K=0x6ED9EBA1 */
    RMD_ROUND(dl, el, al, bl, cl, H(el,al,bl), X[ 3], 0x6ED9EBA1, 11);
    RMD_ROUND(cl, dl, el, al, bl, H(dl,el,al), X[10], 0x6ED9EBA1, 13);
    RMD_ROUND(bl, cl, dl, el, al, H(cl,dl,el), X[14], 0x6ED9EBA1,  6);
    RMD_ROUND(al, bl, cl, dl, el, H(bl,cl,dl), X[ 4], 0x6ED9EBA1,  7);
    RMD_ROUND(el, al, bl, cl, dl, H(al,bl,cl), X[ 9], 0x6ED9EBA1, 14);
    RMD_ROUND(dl, el, al, bl, cl, H(el,al,bl), X[15], 0x6ED9EBA1,  9);
    RMD_ROUND(cl, dl, el, al, bl, H(dl,el,al), X[ 8], 0x6ED9EBA1, 13);
    RMD_ROUND(bl, cl, dl, el, al, H(cl,dl,el), X[ 1], 0x6ED9EBA1, 15);
    RMD_ROUND(al, bl, cl, dl, el, H(bl,cl,dl), X[ 2], 0x6ED9EBA1, 14);
    RMD_ROUND(el, al, bl, cl, dl, H(al,bl,cl), X[ 7], 0x6ED9EBA1,  8);
    RMD_ROUND(dl, el, al, bl, cl, H(el,al,bl), X[ 0], 0x6ED9EBA1, 13);
    RMD_ROUND(cl, dl, el, al, bl, H(dl,el,al), X[ 6], 0x6ED9EBA1,  6);
    RMD_ROUND(bl, cl, dl, el, al, H(cl,dl,el), X[13], 0x6ED9EBA1,  5);
    RMD_ROUND(al, bl, cl, dl, el, H(bl,cl,dl), X[11], 0x6ED9EBA1, 12);
    RMD_ROUND(el, al, bl, cl, dl, H(al,bl,cl), X[ 5], 0x6ED9EBA1,  7);
    RMD_ROUND(dl, el, al, bl, cl, H(el,al,bl), X[12], 0x6ED9EBA1,  5);

    /* Round 4: I, K=0x8F1BBCDC */
    RMD_ROUND(cl, dl, el, al, bl, I(dl,el,al), X[ 1], 0x8F1BBCDC, 11);
    RMD_ROUND(bl, cl, dl, el, al, I(cl,dl,el), X[ 9], 0x8F1BBCDC, 12);
    RMD_ROUND(al, bl, cl, dl, el, I(bl,cl,dl), X[11], 0x8F1BBCDC, 14);
    RMD_ROUND(el, al, bl, cl, dl, I(al,bl,cl), X[10], 0x8F1BBCDC, 15);
    RMD_ROUND(dl, el, al, bl, cl, I(el,al,bl), X[ 0], 0x8F1BBCDC, 14);
    RMD_ROUND(cl, dl, el, al, bl, I(dl,el,al), X[ 8], 0x8F1BBCDC, 15);
    RMD_ROUND(bl, cl, dl, el, al, I(cl,dl,el), X[12], 0x8F1BBCDC,  9);
    RMD_ROUND(al, bl, cl, dl, el, I(bl,cl,dl), X[ 4], 0x8F1BBCDC,  8);
    RMD_ROUND(el, al, bl, cl, dl, I(al,bl,cl), X[13], 0x8F1BBCDC,  9);
    RMD_ROUND(dl, el, al, bl, cl, I(el,al,bl), X[ 3], 0x8F1BBCDC, 14);
    RMD_ROUND(cl, dl, el, al, bl, I(dl,el,al), X[ 7], 0x8F1BBCDC,  5);
    RMD_ROUND(bl, cl, dl, el, al, I(cl,dl,el), X[15], 0x8F1BBCDC,  6);
    RMD_ROUND(al, bl, cl, dl, el, I(bl,cl,dl), X[14], 0x8F1BBCDC,  8);
    RMD_ROUND(el, al, bl, cl, dl, I(al,bl,cl), X[ 5], 0x8F1BBCDC,  6);
    RMD_ROUND(dl, el, al, bl, cl, I(el,al,bl), X[ 6], 0x8F1BBCDC,  5);
    RMD_ROUND(cl, dl, el, al, bl, I(dl,el,al), X[ 2], 0x8F1BBCDC, 12);

    /* Round 5: J, K=0xA953FD4E */
    RMD_ROUND(bl, cl, dl, el, al, J(cl,dl,el), X[ 4], 0xA953FD4E,  9);
    RMD_ROUND(al, bl, cl, dl, el, J(bl,cl,dl), X[ 0], 0xA953FD4E, 15);
    RMD_ROUND(el, al, bl, cl, dl, J(al,bl,cl), X[ 5], 0xA953FD4E,  5);
    RMD_ROUND(dl, el, al, bl, cl, J(el,al,bl), X[ 9], 0xA953FD4E, 11);
    RMD_ROUND(cl, dl, el, al, bl, J(dl,el,al), X[ 7], 0xA953FD4E,  6);
    RMD_ROUND(bl, cl, dl, el, al, J(cl,dl,el), X[12], 0xA953FD4E,  8);
    RMD_ROUND(al, bl, cl, dl, el, J(bl,cl,dl), X[ 2], 0xA953FD4E, 13);
    RMD_ROUND(el, al, bl, cl, dl, J(al,bl,cl), X[10], 0xA953FD4E, 12);
    RMD_ROUND(dl, el, al, bl, cl, J(el,al,bl), X[14], 0xA953FD4E,  5);
    RMD_ROUND(cl, dl, el, al, bl, J(dl,el,al), X[ 1], 0xA953FD4E, 12);
    RMD_ROUND(bl, cl, dl, el, al, J(cl,dl,el), X[ 3], 0xA953FD4E, 13);
    RMD_ROUND(al, bl, cl, dl, el, J(bl,cl,dl), X[ 8], 0xA953FD4E, 14);
    RMD_ROUND(el, al, bl, cl, dl, J(al,bl,cl), X[11], 0xA953FD4E, 11);
    RMD_ROUND(dl, el, al, bl, cl, J(el,al,bl), X[ 6], 0xA953FD4E,  8);
    RMD_ROUND(cl, dl, el, al, bl, J(dl,el,al), X[15], 0xA953FD4E,  5);
    RMD_ROUND(bl, cl, dl, el, al, J(cl,dl,el), X[13], 0xA953FD4E,  6);

    /* Right rounds */
    /* Round 1: J, K=0x50A28BE6 */
    RMD_ROUND(ar, br, cr, dr, er, J(br,cr,dr), X[ 5], 0x50A28BE6,  8);
    RMD_ROUND(er, ar, br, cr, dr, J(ar,br,cr), X[14], 0x50A28BE6,  9);
    RMD_ROUND(dr, er, ar, br, cr, J(er,ar,br), X[ 7], 0x50A28BE6,  9);
    RMD_ROUND(cr, dr, er, ar, br, J(dr,er,ar), X[ 0], 0x50A28BE6, 11);
    RMD_ROUND(br, cr, dr, er, ar, J(cr,dr,er), X[ 9], 0x50A28BE6, 13);
    RMD_ROUND(ar, br, cr, dr, er, J(br,cr,dr), X[ 2], 0x50A28BE6, 15);
    RMD_ROUND(er, ar, br, cr, dr, J(ar,br,cr), X[11], 0x50A28BE6, 15);
    RMD_ROUND(dr, er, ar, br, cr, J(er,ar,br), X[ 4], 0x50A28BE6,  5);
    RMD_ROUND(cr, dr, er, ar, br, J(dr,er,ar), X[13], 0x50A28BE6,  7);
    RMD_ROUND(br, cr, dr, er, ar, J(cr,dr,er), X[ 6], 0x50A28BE6,  7);
    RMD_ROUND(ar, br, cr, dr, er, J(br,cr,dr), X[15], 0x50A28BE6,  8);
    RMD_ROUND(er, ar, br, cr, dr, J(ar,br,cr), X[ 8], 0x50A28BE6, 11);
    RMD_ROUND(dr, er, ar, br, cr, J(er,ar,br), X[ 1], 0x50A28BE6, 14);
    RMD_ROUND(cr, dr, er, ar, br, J(dr,er,ar), X[10], 0x50A28BE6, 14);
    RMD_ROUND(br, cr, dr, er, ar, J(cr,dr,er), X[ 3], 0x50A28BE6, 12);
    RMD_ROUND(ar, br, cr, dr, er, J(br,cr,dr), X[12], 0x50A28BE6,  6);

    /* Round 2: I, K=0x5C4DD124 */
    RMD_ROUND(er, ar, br, cr, dr, I(ar,br,cr), X[ 6], 0x5C4DD124,  9);
    RMD_ROUND(dr, er, ar, br, cr, I(er,ar,br), X[11], 0x5C4DD124, 13);
    RMD_ROUND(cr, dr, er, ar, br, I(dr,er,ar), X[ 3], 0x5C4DD124, 15);
    RMD_ROUND(br, cr, dr, er, ar, I(cr,dr,er), X[ 7], 0x5C4DD124,  7);
    RMD_ROUND(ar, br, cr, dr, er, I(br,cr,dr), X[ 0], 0x5C4DD124, 12);
    RMD_ROUND(er, ar, br, cr, dr, I(ar,br,cr), X[13], 0x5C4DD124,  8);
    RMD_ROUND(dr, er, ar, br, cr, I(er,ar,br), X[ 5], 0x5C4DD124,  9);
    RMD_ROUND(cr, dr, er, ar, br, I(dr,er,ar), X[10], 0x5C4DD124, 11);
    RMD_ROUND(br, cr, dr, er, ar, I(cr,dr,er), X[14], 0x5C4DD124,  7);
    RMD_ROUND(ar, br, cr, dr, er, I(br,cr,dr), X[15], 0x5C4DD124,  7);
    RMD_ROUND(er, ar, br, cr, dr, I(ar,br,cr), X[ 8], 0x5C4DD124, 12);
    RMD_ROUND(dr, er, ar, br, cr, I(er,ar,br), X[12], 0x5C4DD124,  7);
    RMD_ROUND(cr, dr, er, ar, br, I(dr,er,ar), X[ 4], 0x5C4DD124,  6);
    RMD_ROUND(br, cr, dr, er, ar, I(cr,dr,er), X[ 9], 0x5C4DD124, 15);
    RMD_ROUND(ar, br, cr, dr, er, I(br,cr,dr), X[ 1], 0x5C4DD124, 13);
    RMD_ROUND(er, ar, br, cr, dr, I(ar,br,cr), X[ 2], 0x5C4DD124, 11);

    /* Round 3: H, K=0x6D703EF3 */
    RMD_ROUND(dr, er, ar, br, cr, H(er,ar,br), X[15], 0x6D703EF3,  9);
    RMD_ROUND(cr, dr, er, ar, br, H(dr,er,ar), X[ 5], 0x6D703EF3,  7);
    RMD_ROUND(br, cr, dr, er, ar, H(cr,dr,er), X[ 1], 0x6D703EF3, 15);
    RMD_ROUND(ar, br, cr, dr, er, H(br,cr,dr), X[ 3], 0x6D703EF3, 11);
    RMD_ROUND(er, ar, br, cr, dr, H(ar,br,cr), X[ 7], 0x6D703EF3,  8);
    RMD_ROUND(dr, er, ar, br, cr, H(er,ar,br), X[14], 0x6D703EF3,  6);
    RMD_ROUND(cr, dr, er, ar, br, H(dr,er,ar), X[ 6], 0x6D703EF3,  6);
    RMD_ROUND(br, cr, dr, er, ar, H(cr,dr,er), X[ 9], 0x6D703EF3, 14);
    RMD_ROUND(ar, br, cr, dr, er, H(br,cr,dr), X[11], 0x6D703EF3, 12);
    RMD_ROUND(er, ar, br, cr, dr, H(ar,br,cr), X[ 8], 0x6D703EF3, 13);
    RMD_ROUND(dr, er, ar, br, cr, H(er,ar,br), X[12], 0x6D703EF3,  5);
    RMD_ROUND(cr, dr, er, ar, br, H(dr,er,ar), X[ 2], 0x6D703EF3, 14);
    RMD_ROUND(br, cr, dr, er, ar, H(cr,dr,er), X[10], 0x6D703EF3, 13);
    RMD_ROUND(ar, br, cr, dr, er, H(br,cr,dr), X[ 0], 0x6D703EF3, 13);
    RMD_ROUND(er, ar, br, cr, dr, H(ar,br,cr), X[ 4], 0x6D703EF3,  7);
    RMD_ROUND(dr, er, ar, br, cr, H(er,ar,br), X[13], 0x6D703EF3,  5);

    /* Round 4: G, K=0x7A6D76E9 */
    RMD_ROUND(cr, dr, er, ar, br, G(dr,er,ar), X[ 8], 0x7A6D76E9, 15);
    RMD_ROUND(br, cr, dr, er, ar, G(cr,dr,er), X[ 6], 0x7A6D76E9,  5);
    RMD_ROUND(ar, br, cr, dr, er, G(br,cr,dr), X[ 4], 0x7A6D76E9,  8);
    RMD_ROUND(er, ar, br, cr, dr, G(ar,br,cr), X[ 1], 0x7A6D76E9, 11);
    RMD_ROUND(dr, er, ar, br, cr, G(er,ar,br), X[ 3], 0x7A6D76E9, 14);
    RMD_ROUND(cr, dr, er, ar, br, G(dr,er,ar), X[11], 0x7A6D76E9, 14);
    RMD_ROUND(br, cr, dr, er, ar, G(cr,dr,er), X[15], 0x7A6D76E9,  6);
    RMD_ROUND(ar, br, cr, dr, er, G(br,cr,dr), X[ 0], 0x7A6D76E9, 14);
    RMD_ROUND(er, ar, br, cr, dr, G(ar,br,cr), X[ 5], 0x7A6D76E9,  6);
    RMD_ROUND(dr, er, ar, br, cr, G(er,ar,br), X[12], 0x7A6D76E9,  9);
    RMD_ROUND(cr, dr, er, ar, br, G(dr,er,ar), X[ 2], 0x7A6D76E9, 12);
    RMD_ROUND(br, cr, dr, er, ar, G(cr,dr,er), X[13], 0x7A6D76E9,  9);
    RMD_ROUND(ar, br, cr, dr, er, G(br,cr,dr), X[ 9], 0x7A6D76E9, 12);
    RMD_ROUND(er, ar, br, cr, dr, G(ar,br,cr), X[ 7], 0x7A6D76E9,  5);
    RMD_ROUND(dr, er, ar, br, cr, G(er,ar,br), X[10], 0x7A6D76E9, 15);
    RMD_ROUND(cr, dr, er, ar, br, G(dr,er,ar), X[14], 0x7A6D76E9,  8);

    /* Round 5: F, K=0x00000000 */
    RMD_ROUND(br, cr, dr, er, ar, F(cr,dr,er), X[12], 0x00000000,  8);
    RMD_ROUND(ar, br, cr, dr, er, F(br,cr,dr), X[15], 0x00000000,  5);
    RMD_ROUND(er, ar, br, cr, dr, F(ar,br,cr), X[10], 0x00000000, 12);
    RMD_ROUND(dr, er, ar, br, cr, F(er,ar,br), X[ 4], 0x00000000,  9);
    RMD_ROUND(cr, dr, er, ar, br, F(dr,er,ar), X[ 1], 0x00000000, 12);
    RMD_ROUND(br, cr, dr, er, ar, F(cr,dr,er), X[ 5], 0x00000000,  5);
    RMD_ROUND(ar, br, cr, dr, er, F(br,cr,dr), X[ 8], 0x00000000, 14);
    RMD_ROUND(er, ar, br, cr, dr, F(ar,br,cr), X[ 7], 0x00000000,  6);
    RMD_ROUND(dr, er, ar, br, cr, F(er,ar,br), X[ 6], 0x00000000,  8);
    RMD_ROUND(cr, dr, er, ar, br, F(dr,er,ar), X[ 2], 0x00000000, 13);
    RMD_ROUND(br, cr, dr, er, ar, F(cr,dr,er), X[13], 0x00000000,  6);
    RMD_ROUND(ar, br, cr, dr, er, F(br,cr,dr), X[14], 0x00000000,  5);
    RMD_ROUND(er, ar, br, cr, dr, F(ar,br,cr), X[ 0], 0x00000000, 15);
    RMD_ROUND(dr, er, ar, br, cr, F(er,ar,br), X[ 3], 0x00000000, 13);
    RMD_ROUND(cr, dr, er, ar, br, F(dr,er,ar), X[ 9], 0x00000000, 11);
    RMD_ROUND(br, cr, dr, er, ar, F(cr,dr,er), X[11], 0x00000000, 11);

    /* Finalize: standard RIPEMD-160 finalization */
    t = rmd160_H0[1] + cl + dr;
    uint32_t h0 = rmd160_H0[0];
    uint32_t h1 = rmd160_H0[1];
    uint32_t h2 = rmd160_H0[2];
    uint32_t h3 = rmd160_H0[3];
    uint32_t h4 = rmd160_H0[4];
    h0 = t;
    h1 = h2 + dl + er;
    h2 = h3 + el + ar;
    h3 = h4 + al + br;
    h4 = rmd160_H0[0] + bl + cr;

    /* Output in little-endian */
    output[ 0] = h0; output[ 1] = h0 >> 8; output[ 2] = h0 >> 16; output[ 3] = h0 >> 24;
    output[ 4] = h1; output[ 5] = h1 >> 8; output[ 6] = h1 >> 16; output[ 7] = h1 >> 24;
    output[ 8] = h2; output[ 9] = h2 >> 8; output[10] = h2 >> 16; output[11] = h2 >> 24;
    output[12] = h3; output[13] = h3 >> 8; output[14] = h3 >> 16; output[15] = h3 >> 24;
    output[16] = h4; output[17] = h4 >> 8; output[18] = h4 >> 16; output[19] = h4 >> 24;
}

/* Combined hash160: RIPEMD160(SHA256(33-byte input)) */
static inline void hash160_fast(const unsigned char input[33], unsigned char output[20]) {
    unsigned char sha[32];
    sha256_33(input, sha);
    rmd160_32(sha, output);
}

#endif /* SHA256_RMD160_FAST_H */
