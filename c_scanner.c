/*
 * Bitcoin Puzzle #71 Scanner v4 - MAXIMUM PERFORMANCE
 *
 * Target: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU
 * h160:   f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8
 * Range:  0x400000000000000000 to 0x7FFFFFFFFFFFFFFFFF (71-bit keyspace)
 *
 * KEY OPTIMIZATIONS:
 *   1. Uses secp256k1 INTERNAL Jacobian point operations directly
 *      (bypasses the public API overhead entirely)
 *   2. BATCH INVERSION via secp256k1_ge_set_all_gej_var():
 *      Converts N Jacobian points to Affine with a SINGLE field inversion
 *      using Montgomery's trick (1 inversion + O(N) multiplications)
 *   3. secp256k1_gej_add_ge_var for Jacobian + Affine point addition
 *      (fastest form of EC point addition, no inversions)
 *   4. Direct secp256k1_eckey_pubkey_serialize33 (no public API overhead)
 *   5. Lock-free atomic counters, fast xorshift PRNG
 *
 * Compile (from secp256k1_src directory):
 *   gcc -O3 -march=native -I/root/secp256k1_src/include -I/root/secp256k1_src/src \
 *       -I/root/secp256k1_src -Wno-deprecated-declarations -Wno-unused-function \
 *       -o /root/puzzle71/c_scanner /root/puzzle71/c_scanner.c -lcrypto -lpthread
 */

#define _GNU_SOURCE

/* secp256k1 build config */
#define SECP256K1_BUILD

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdatomic.h>
#include <pthread.h>
#include <time.h>
#include <unistd.h>
#include <signal.h>
#include <fcntl.h>
#include <sys/time.h>

/* Include secp256k1 source as single compilation unit */
#include "include/secp256k1.h"
#include "include/secp256k1_preallocated.h"
#include "src/assumptions.h"
#include "src/checkmem.h"
#include "src/util.h"
#include "src/field_impl.h"
#include "src/scalar_impl.h"
#include "src/group_impl.h"
#include "src/ecmult_impl.h"
#include "src/ecmult_const_impl.h"
#include "src/ecmult_gen_impl.h"
#include "src/ecdsa_impl.h"
#include "src/eckey_impl.h"
#include "src/hash_impl.h"
#include "src/int128_impl.h"
#include "src/scratch_impl.h"
#include "src/selftest.h"
#include "src/hsort_impl.h"

/* Include precomputed tables for ecmult_gen */
#include "src/precomputed_ecmult.c"
#include "src/precomputed_ecmult_gen.c"

/* Custom optimized SHA256 and RIPEMD160 (specialized for 33 and 32 byte inputs) */
#include "/root/puzzle71/sha256_rmd160_fast.h"

/* ======================== Configuration ======================== */

static const unsigned char TARGET_H160[20] = {
    0xf6, 0xf5, 0x43, 0x1d, 0x25, 0xbb, 0xf7, 0xb1,
    0x2e, 0x8a, 0xdd, 0x9a, 0xf5, 0xe3, 0x47, 0x5c,
    0x44, 0xa0, 0xa5, 0xb8
};

static uint32_t TARGET_PREFIX;

/* Batch size for batch inversion */
#define BATCH_SIZE     2048

/* How many batches per random start */
#define NUM_BATCHES    2048
#define CHUNK_SIZE     ((uint64_t)BATCH_SIZE * NUM_BATCHES)

static int NUM_THREADS = 4;
#define STATS_INTERVAL 10

/* ======================== Global State ======================== */

static atomic_ullong g_total_keys = 0;
static atomic_int    g_found = 0;
static volatile sig_atomic_t g_interrupted = 0;
static double        g_start_time_d;

/* Generator point G in affine coordinates */
static secp256k1_ge g_gen_affine;

/* secp256k1 context for ecmult_gen (initial scalar multiplication) */
static secp256k1_ecmult_gen_context g_ecmult_gen_ctx;

/* ======================== Utility Functions ======================== */

/* hash160 wrapper using optimized implementations */
static inline void hash160(const unsigned char data[33], unsigned char out[20]) {
    hash160_fast(data, out);
}

static uint64_t read_urandom_u64(void) {
    uint64_t val;
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd >= 0) {
        if (read(fd, &val, sizeof(val)) != sizeof(val))
            val = (uint64_t)time(NULL) ^ ((uint64_t)clock() << 20);
        close(fd);
    } else {
        val = (uint64_t)time(NULL) ^ ((uint64_t)clock() << 20);
    }
    return val;
}

typedef struct { uint64_t s; } xorshift64_t;

static inline uint64_t xorshift64_next(xorshift64_t *rng) {
    uint64_t x = rng->s;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    rng->s = x;
    return x * 0x2545F4914F6CDD1DULL;
}

static void random_start(xorshift64_t *rng, uint64_t *hi, uint64_t *lo) {
    uint64_t r = xorshift64_next(rng);
    *hi = 0x4ULL + (r & 0x3ULL);
    *lo = xorshift64_next(rng);
}

static void format_privkey(char *buf, uint64_t hi, uint64_t lo) {
    sprintf(buf, "0x%llX%016llX",
            (unsigned long long)hi, (unsigned long long)lo);
}

/* Convert (hi, lo) private key to a secp256k1_scalar */
static void make_scalar(secp256k1_scalar *s, uint64_t hi, uint64_t lo) {
    unsigned char b32[32] = {0};
    b32[23] = (unsigned char)(hi & 0xFF);
    b32[24] = (unsigned char)((lo >> 56) & 0xFF);
    b32[25] = (unsigned char)((lo >> 48) & 0xFF);
    b32[26] = (unsigned char)((lo >> 40) & 0xFF);
    b32[27] = (unsigned char)((lo >> 32) & 0xFF);
    b32[28] = (unsigned char)((lo >> 24) & 0xFF);
    b32[29] = (unsigned char)((lo >> 16) & 0xFF);
    b32[30] = (unsigned char)((lo >> 8) & 0xFF);
    b32[31] = (unsigned char)(lo & 0xFF);
    int overflow;
    secp256k1_scalar_set_b32(s, b32, &overflow);
}

static double get_time_sec(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec + tv.tv_usec / 1e6;
}

static void report_found(uint64_t hi, uint64_t lo) {
    char keystr[64];
    format_privkey(keystr, hi, lo);

    printf("\n");
    printf("============================================================\n");
    printf("  PUZZLE #71 KEY FOUND!\n");
    printf("  Private Key: %s\n", keystr);
    printf("============================================================\n");
    fflush(stdout);

    FILE *f = fopen("/root/puzzle71/FOUND_KEY.txt", "w");
    if (f) {
        fprintf(f, "PUZZLE #71 SOLUTION\n");
        fprintf(f, "Private Key: %s\n", keystr);
        fprintf(f, "Target: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU\n");
        fprintf(f, "Hash160: f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8\n");
        time_t now = time(NULL);
        fprintf(f, "Found: %s", ctime(&now));
        unsigned long long total = atomic_load(&g_total_keys);
        fprintf(f, "Total keys checked: %llu\n", total);
        fclose(f);
    }

    atomic_store(&g_found, 1);
}

/* ======================== Worker Thread ======================== */

typedef struct {
    int thread_id;
} thread_arg_t;

static void *scanner_thread(void *arg) {
    thread_arg_t *ta = (thread_arg_t *)arg;
    int tid = ta->thread_id;

    /* Allocate batch buffers */
    secp256k1_gej *jac_batch = (secp256k1_gej *)malloc(sizeof(secp256k1_gej) * BATCH_SIZE);
    secp256k1_ge  *aff_batch = (secp256k1_ge *)malloc(sizeof(secp256k1_ge) * BATCH_SIZE);
    if (!jac_batch || !aff_batch) {
        fprintf(stderr, "Thread %d: malloc failed\n", tid);
        return NULL;
    }

    unsigned char pub33[33];
    unsigned char h160_buf[20];

    xorshift64_t rng;
    rng.s = read_urandom_u64() ^ ((uint64_t)(tid + 1) * 6364136223846793005ULL);
    if (rng.s == 0) rng.s = 1;

    uint64_t local_count = 0;

    while (!atomic_load(&g_found)) {
        uint64_t hi, lo;
        random_start(&rng, &hi, &lo);

        /* Full scalar multiplication for the starting point: P = privkey * G */
        secp256k1_scalar privkey_scalar;
        make_scalar(&privkey_scalar, hi, lo);

        secp256k1_gej current_jac;
        secp256k1_ecmult_gen(&g_ecmult_gen_ctx, &current_jac, &privkey_scalar);
        secp256k1_scalar_clear(&privkey_scalar);

        /* Process NUM_BATCHES batches */
        for (int batch_num = 0; batch_num < NUM_BATCHES && !atomic_load(&g_found); batch_num++) {

            /* Step 1: Generate BATCH_SIZE sequential Jacobian points */
            jac_batch[0] = current_jac;
            for (int i = 1; i < BATCH_SIZE; i++) {
                secp256k1_gej_add_ge_var(&jac_batch[i], &jac_batch[i-1], &g_gen_affine, NULL);
            }

            /* Step 2: Batch convert Jacobian -> Affine (1 field inversion for all!) */
            secp256k1_ge_set_all_gej_var(aff_batch, jac_batch, BATCH_SIZE);

            /* Step 3: Serialize, hash, and check each point */
            for (int i = 0; i < BATCH_SIZE; i++) {
                /* Direct serialization to 33-byte compressed pubkey */
                secp256k1_eckey_pubkey_serialize33(&aff_batch[i], pub33);

                /* Hash160 = RIPEMD160(SHA256(pub33)) */
                hash160(pub33, h160_buf);

                /* Fast 4-byte prefix check before full compare */
                if (__builtin_expect(*(uint32_t*)h160_buf == TARGET_PREFIX, 0)) {
                    if (memcmp(h160_buf, TARGET_H160, 20) == 0) {
                        uint64_t offset = (uint64_t)batch_num * BATCH_SIZE + i;
                        uint64_t found_lo = lo + offset;
                        uint64_t found_hi = hi + (found_lo < lo ? 1 : 0);
                        report_found(found_hi, found_lo);
                        goto done;
                    }
                }
            }

            /* Advance current_jac past this batch */
            secp256k1_gej_add_ge_var(&current_jac, &jac_batch[BATCH_SIZE-1], &g_gen_affine, NULL);

            local_count += BATCH_SIZE;

            if (__builtin_expect(local_count >= 500000, 0)) {
                atomic_fetch_add(&g_total_keys, local_count);
                local_count = 0;
            }
        }

        if (local_count > 0) {
            atomic_fetch_add(&g_total_keys, local_count);
            local_count = 0;
        }
    }

done:
    if (local_count > 0)
        atomic_fetch_add(&g_total_keys, local_count);

    free(jac_batch);
    free(aff_batch);
    return NULL;
}

/* ======================== Stats Thread ======================== */

static void *stats_thread(void *arg) {
    (void)arg;
    unsigned long long prev_total = 0;
    double prev_time = get_time_sec();

    while (!atomic_load(&g_found)) {
        sleep(STATS_INTERVAL);
        if (atomic_load(&g_found)) break;

        double now = get_time_sec();
        double elapsed = now - g_start_time_d;
        double dt = now - prev_time;
        unsigned long long total = atomic_load(&g_total_keys);
        double avg_rate = (elapsed > 0) ? (double)total / elapsed : 0;
        double inst_rate = (dt > 0) ? (double)(total - prev_total) / dt : 0;

        printf("[%7.1fs] Checked: %14llu | Avg: %8.2f Mk/s | Now: %8.2f Mk/s\n",
               elapsed, total, avg_rate / 1e6, inst_rate / 1e6);
        fflush(stdout);

        prev_total = total;
        prev_time = now;
    }
    return NULL;
}

/* ======================== Signal Handler ======================== */

static void signal_handler(int sig) {
    (void)sig;
    g_interrupted = 1;
    atomic_store(&g_found, 1);
}

/* ======================== Initialization ======================== */

static int init_secp256k1(void) {
    /* Initialize ecmult_gen context (precomputed tables for k*G) */
    secp256k1_ecmult_gen_context_build(&g_ecmult_gen_ctx);

    /* Compute generator point G in affine: G = 1*G */
    secp256k1_scalar one;
    secp256k1_scalar_set_int(&one, 1);
    secp256k1_gej gj;
    secp256k1_ecmult_gen(&g_ecmult_gen_ctx, &gj, &one);
    secp256k1_ge_set_gej_var(&g_gen_affine, &gj);
    secp256k1_scalar_clear(&one);

    return 1;
}

static void cleanup_secp256k1(void) {
    secp256k1_ecmult_gen_context_clear(&g_ecmult_gen_ctx);
}

/* ======================== Main ======================== */

int main(int argc, char *argv[]) {
    memcpy(&TARGET_PREFIX, TARGET_H160, 4);

    printf("============================================================\n");
    printf("  Bitcoin Puzzle #71 Scanner v4 - BATCH INVERSION MODE\n");
    printf("  Target: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU\n");
    printf("  Hash160: f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8\n");
    printf("  Range: 0x400000000000000000 - 0x7FFFFFFFFFFFFFFFFF\n");
    printf("  Batch: %d pts | %d batches/chunk | %llu keys/chunk\n",
           BATCH_SIZE, NUM_BATCHES, (unsigned long long)CHUNK_SIZE);
    printf("============================================================\n");

    if (argc > 1) {
        NUM_THREADS = atoi(argv[1]);
        if (NUM_THREADS < 1) NUM_THREADS = 1;
        if (NUM_THREADS > 256) NUM_THREADS = 256;
    }
    printf("  Threads: %d\n", NUM_THREADS);

    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    printf("  Initializing secp256k1 internals...\n");
    if (!init_secp256k1()) {
        fprintf(stderr, "FATAL: Failed to initialize secp256k1\n");
        return 1;
    }

    /* Verify setup */
    {
        /* Check G point */
        unsigned char gs[33];
        secp256k1_ge g_test = g_gen_affine;
        secp256k1_eckey_pubkey_serialize33(&g_test, gs);
        printf("  G: %02x%02x%02x...%02x%02x\n", gs[0], gs[1], gs[2], gs[31], gs[32]);

        /* Verify hash160 of G matches known value */
        unsigned char gh[20];
        hash160(gs, gh);
        unsigned char expected[20] = {
            0x75,0x1e,0x76,0xe8,0x19,0x91,0x96,0xd4,0x54,0x94,
            0x1c,0x45,0xd1,0xb3,0xa3,0x23,0xf1,0x43,0x3b,0xd6
        };
        printf("  Hash160(G) test: %s\n",
               memcmp(gh, expected, 20) == 0 ? "PASSED" : "FAILED");
        if (memcmp(gh, expected, 20) != 0) return 1;

        /* Verify EC addition: 2G == G+G */
        secp256k1_scalar two_s;
        secp256k1_scalar_set_int(&two_s, 2);
        secp256k1_gej twog_j;
        secp256k1_ecmult_gen(&g_ecmult_gen_ctx, &twog_j, &two_s);
        secp256k1_ge twog_a;
        secp256k1_ge_set_gej_var(&twog_a, &twog_j);

        secp256k1_gej gj;
        secp256k1_ecmult_gen(&g_ecmult_gen_ctx, &gj, &secp256k1_scalar_one);
        secp256k1_gej sum_j;
        secp256k1_gej_add_ge_var(&sum_j, &gj, &g_gen_affine, NULL);
        secp256k1_ge sum_a;
        secp256k1_ge_set_gej_var(&sum_a, &sum_j);

        unsigned char s1[33], s2[33];
        secp256k1_eckey_pubkey_serialize33(&twog_a, s1);
        secp256k1_eckey_pubkey_serialize33(&sum_a, s2);
        printf("  EC add test: %s\n",
               memcmp(s1, s2, 33) == 0 ? "PASSED" : "FAILED");
        if (memcmp(s1, s2, 33) != 0) return 1;

        /* Verify batch inversion */
        secp256k1_gej batch_j[4];
        secp256k1_ge batch_a[4];
        secp256k1_ecmult_gen(&g_ecmult_gen_ctx, &batch_j[0], &secp256k1_scalar_one);
        for (int i = 1; i < 4; i++) {
            secp256k1_gej_add_ge_var(&batch_j[i], &batch_j[i-1], &g_gen_affine, NULL);
        }
        secp256k1_ge_set_all_gej_var(batch_a, batch_j, 4);

        int batch_ok = 1;
        for (int i = 0; i < 4; i++) {
            secp256k1_scalar si;
            secp256k1_scalar_set_int(&si, i + 1);
            secp256k1_gej dj;
            secp256k1_ecmult_gen(&g_ecmult_gen_ctx, &dj, &si);
            secp256k1_ge da;
            secp256k1_ge_set_gej_var(&da, &dj);
            unsigned char a1[33], a2[33];
            secp256k1_eckey_pubkey_serialize33(&batch_a[i], a1);
            secp256k1_eckey_pubkey_serialize33(&da, a2);
            if (memcmp(a1, a2, 33) != 0) { batch_ok = 0; break; }
        }
        printf("  Batch inversion test: %s\n", batch_ok ? "PASSED" : "FAILED");
        if (!batch_ok) return 1;
    }

    printf("============================================================\n");
    printf("  Starting scan...\n");
    printf("============================================================\n\n");
    fflush(stdout);

    g_start_time_d = get_time_sec();

    pthread_t stats_tid;
    pthread_create(&stats_tid, NULL, stats_thread, NULL);

    pthread_t *workers = malloc(sizeof(pthread_t) * NUM_THREADS);
    thread_arg_t *args = malloc(sizeof(thread_arg_t) * NUM_THREADS);

    for (int i = 0; i < NUM_THREADS; i++) {
        args[i].thread_id = i;
        pthread_create(&workers[i], NULL, scanner_thread, &args[i]);
    }

    for (int i = 0; i < NUM_THREADS; i++) {
        pthread_join(workers[i], NULL);
    }

    pthread_cancel(stats_tid);
    pthread_join(stats_tid, NULL);

    double end_time = get_time_sec();
    double elapsed = end_time - g_start_time_d;
    unsigned long long total = atomic_load(&g_total_keys);
    double rate = (elapsed > 0) ? (double)total / elapsed : 0;

    printf("\n============================================================\n");
    if (g_interrupted) {
        printf("  Scan interrupted by user.\n");
    } else if (atomic_load(&g_found)) {
        printf("  KEY FOUND! Check /root/puzzle71/FOUND_KEY.txt\n");
    }
    printf("  Total keys checked: %llu\n", total);
    printf("  Elapsed: %.1f seconds\n", elapsed);
    printf("  Average rate: %.0f keys/sec (%.2f Mkeys/sec)\n", rate, rate / 1e6);
    printf("============================================================\n");

    cleanup_secp256k1();
    free(workers);
    free(args);
    return 0;
}
