#!/usr/bin/env python3
"""
Comprehensive Benchmark of Iceland secp256k1 Library
=====================================================
Target: Bitcoin Puzzle #71
Address: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU
h160:    f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8
Range:   0x400000000000000000 .. 0x7FFFFFFFFFFFFFFFFF

Tests every relevant function for puzzle scanning performance.
"""

import sys
sys.path.insert(0, '/root/iceland_secp256k1')
import secp256k1 as ice
import time
import os

# ─── Configuration ───────────────────────────────────────────────────────────
TARGET_H160 = bytes.fromhex("f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8")
RANGE_START = 0x400000000000000000
RANGE_END   = 0x7FFFFFFFFFFFFFFFFF
NUM_CPUS    = os.cpu_count() or 4

# Test private key in the puzzle range
TEST_PK = RANGE_START + 123456789

# Precompute a base point for increment tests
BASE_POINT = ice.scalar_multiplication(TEST_PK)
G_POINT = ice.scalar_multiplication(1)

# ─── Helpers ─────────────────────────────────────────────────────────────────
def fmt(n):
    """Format number with commas."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}K"
    else:
        return f"{n:.0f}"

def benchmark(name, func, iterations, keys_per_call=1):
    """Run a benchmark and report results."""
    # Warmup
    try:
        for _ in range(min(10, iterations)):
            func()
    except Exception as e:
        print(f"  [{name}] FAILED during warmup: {e}")
        return None

    # Timed run
    start = time.perf_counter()
    for _ in range(iterations):
        func()
    elapsed = time.perf_counter() - start

    total_keys = iterations * keys_per_call
    keys_sec = total_keys / elapsed
    print(f"  [{name}]")
    print(f"    Iterations: {iterations:,} x {keys_per_call} keys/call = {total_keys:,} total keys")
    print(f"    Time: {elapsed:.4f}s  |  Keys/sec: {fmt(keys_sec)}/s  ({keys_sec:,.0f})")
    print()
    return keys_sec

def divider(title):
    print()
    print(f"{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
#  START BENCHMARKS
# ═══════════════════════════════════════════════════════════════════════════════

print(f"""
╔══════════════════════════════════════════════════════════════════════╗
║         Iceland secp256k1 — Comprehensive Benchmark Suite          ║
║                     Bitcoin Puzzle #71                              ║
╠══════════════════════════════════════════════════════════════════════╣
║  CPUs: {NUM_CPUS:<4}                                                     ║
║  Range: 0x{RANGE_START:X} .. 0x{RANGE_END:X}          ║
║  Range size: {RANGE_END - RANGE_START + 1:,} keys ({(RANGE_END-RANGE_START+1).bit_length()}-bit)               ║
║  Target h160: {TARGET_H160.hex()}           ║
╚══════════════════════════════════════════════════════════════════════╝
""")

results = {}

# ─────────────────────────────────────────────────────────────────────────────
divider("1. SCALAR MULTIPLICATION (Full EC point multiply)")
# ─────────────────────────────────────────────────────────────────────────────

# 1a. scalar_multiplication (single key -> 65-byte uncompressed point)
N = 100_000
r = benchmark("scalar_multiplication(pk)",
    lambda: ice.scalar_multiplication(TEST_PK), N)
if r: results['scalar_multiplication'] = r

# 1b. scalar_multiplications (batch list of keys -> concatenated points)
BATCH = 1000
pk_list = list(range(TEST_PK, TEST_PK + BATCH))
N2 = 200
r = benchmark(f"scalar_multiplications({BATCH} keys)",
    lambda: ice.scalar_multiplications(pk_list), N2, BATCH)
if r: results['scalar_multiplications_batch'] = r

# 1c. point_multiplication(P, k) — same as scalar_mult but explicit base point
N = 100_000
r = benchmark("point_multiplication(G, pk)",
    lambda: ice.point_multiplication(G_POINT, TEST_PK), N)
if r: results['point_multiplication'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("2. PRIVATE KEY -> HASH160 (Full pipeline: mult + compress + hash)")
# ─────────────────────────────────────────────────────────────────────────────

# 2a. privatekey_to_h160 (single)
N = 100_000
r = benchmark("privatekey_to_h160(0, True, pk)",
    lambda: ice.privatekey_to_h160(0, True, TEST_PK), N)
if r: results['privatekey_to_h160'] = r

# 2b. privatekey_loop_h160 (batch sequential)
for batch_sz in [1000, 10_000, 100_000]:
    iters = max(10, 500_000 // batch_sz)
    r = benchmark(f"privatekey_loop_h160(count={batch_sz})",
        lambda bs=batch_sz: ice.privatekey_loop_h160(bs, 0, True, TEST_PK), iters, batch_sz)
    if r: results[f'privatekey_loop_h160_{batch_sz}'] = r

# 2c. privatekey_loop_h160_sse (SSE variant)
for batch_sz in [1000, 10_000, 100_000]:
    iters = max(10, 500_000 // batch_sz)
    r = benchmark(f"privatekey_loop_h160_sse(count={batch_sz})",
        lambda bs=batch_sz: ice.privatekey_loop_h160_sse(bs, 0, True, TEST_PK), iters, batch_sz)
    if r: results[f'privatekey_loop_h160_sse_{batch_sz}'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("3. PRIVATE KEY -> ADDRESS STRING")
# ─────────────────────────────────────────────────────────────────────────────

N = 100_000
r = benchmark("privatekey_to_address(0, True, pk)",
    lambda: ice.privatekey_to_address(0, True, TEST_PK), N)
if r: results['privatekey_to_address'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("4. POINT SEQUENTIAL INCREMENT (EC addition only — no full multiply)")
# ─────────────────────────────────────────────────────────────────────────────
print("  NOTE: This is the KEY optimization — each subsequent key requires only")
print("  one EC point addition instead of a full scalar multiplication!")
print()

# 4a. point_increment (single: pt + G)
N = 200_000
r = benchmark("point_increment(pt) [single add G]",
    lambda: ice.point_increment(BASE_POINT), N)
if r: results['point_increment'] = r

# 4b. point_sequential_increment (batch: pt+G, pt+2G, ..., pt+nG)
for batch_sz in [1000, 10_000, 100_000, 500_000]:
    iters = max(5, 1_000_000 // batch_sz)
    r = benchmark(f"point_sequential_increment(count={batch_sz})",
        lambda bs=batch_sz: ice.point_sequential_increment(bs, BASE_POINT), iters, batch_sz)
    if r: results[f'point_seq_inc_{batch_sz}'] = r

# 4c. point_sequential_increment_P2 (with custom P2 group, initialized to G)
ice.init_P2_Group(G_POINT)
for batch_sz in [1000, 10_000, 100_000, 500_000]:
    iters = max(5, 1_000_000 // batch_sz)
    r = benchmark(f"point_sequential_increment_P2(count={batch_sz})",
        lambda bs=batch_sz: ice.point_sequential_increment_P2(bs, BASE_POINT), iters, batch_sz)
    if r: results[f'point_seq_inc_P2_{batch_sz}'] = r

# 4d. point_sequential_increment_P2_mcpu (multi-core full points)
for batch_sz in [10_000, 100_000, 500_000, 1_000_000]:
    iters = max(3, 1_000_000 // batch_sz)
    r = benchmark(f"point_sequential_increment_P2_mcpu(count={batch_sz}, mcpu={NUM_CPUS})",
        lambda bs=batch_sz: ice.point_sequential_increment_P2_mcpu(bs, BASE_POINT, NUM_CPUS), iters, batch_sz)
    if r: results[f'point_seq_inc_P2_mcpu_{batch_sz}'] = r

# 4e. point_sequential_increment_P2X_mcpu (multi-core X-coordinate only — 32 bytes each)
print("  NOTE: P2X variant returns only X-coordinates (32 bytes each vs 65).")
print("  Less data but may need extra work for h160 computation.")
print()
for batch_sz in [10_000, 100_000, 500_000, 1_000_000]:
    iters = max(3, 1_000_000 // batch_sz)
    r = benchmark(f"point_sequential_increment_P2X_mcpu(count={batch_sz}, mcpu={NUM_CPUS})",
        lambda bs=batch_sz: ice.point_sequential_increment_P2X_mcpu(bs, BASE_POINT, NUM_CPUS), iters, batch_sz)
    if r: results[f'point_seq_inc_P2X_mcpu_{batch_sz}'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("5. POINT ADDITION / LOOP ADDITION")
# ─────────────────────────────────────────────────────────────────────────────

# 5a. point_addition (single: P1 + P2)
N = 200_000
P2 = ice.scalar_multiplication(TEST_PK + 999)
r = benchmark("point_addition(P1, P2)",
    lambda: ice.point_addition(BASE_POINT, P2), N)
if r: results['point_addition'] = r

# 5b. point_loop_addition (P1 + P2, P1 + 2*P2, ...)
for batch_sz in [1000, 10_000, 100_000]:
    iters = max(5, 500_000 // batch_sz)
    r = benchmark(f"point_loop_addition(count={batch_sz}, P1, G)",
        lambda bs=batch_sz: ice.point_loop_addition(bs, BASE_POINT, G_POINT), iters, batch_sz)
    if r: results[f'point_loop_addition_{batch_sz}'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("6. HASHING FUNCTIONS (SHA256, RIPEMD160, Hash160)")
# ─────────────────────────────────────────────────────────────────────────────

# Compressed pubkey for hashing tests
cpub_str = ice.point_to_cpub(BASE_POINT)
cpub_bytes = bytes.fromhex(cpub_str)

# 6a. hash160 (SHA256 + RIPEMD160)
N = 500_000
r = benchmark("hash160(33-byte compressed pubkey)",
    lambda: ice.hash160(cpub_bytes), N)
if r: results['hash160'] = r

# 6b. pubkey_to_h160 (from 65-byte uncompressed, compress internally + hash)
N = 200_000
r = benchmark("pubkey_to_h160(0, True, 65-byte point)",
    lambda: ice.pubkey_to_h160(0, True, BASE_POINT), N)
if r: results['pubkey_to_h160'] = r

# 6c. get_sha256
N = 500_000
r = benchmark("get_sha256(33 bytes)",
    lambda: ice.get_sha256(cpub_bytes), N)
if r: results['get_sha256'] = r

# 6d. rmd160
sha_out = ice.get_sha256(cpub_bytes)
sha_bytes = bytes.fromhex(sha_out) if isinstance(sha_out, str) else sha_out
N = 500_000
r = benchmark("rmd160(32 bytes)",
    lambda: ice.rmd160(sha_bytes), N)
if r: results['rmd160'] = r

# 6e. point_to_cpub (compress a 65-byte point to 33-byte)
N = 500_000
r = benchmark("point_to_cpub(65-byte point)",
    lambda: ice.point_to_cpub(BASE_POINT), N)
if r: results['point_to_cpub'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("7. ETH GROUP ADDRESS (sequential keys, C-level pipeline)")
# ─────────────────────────────────────────────────────────────────────────────

for batch_sz in [1000, 10_000, 100_000]:
    iters = max(5, 200_000 // batch_sz)
    r = benchmark(f"privatekey_group_to_ETH_address(pk, {batch_sz})",
        lambda bs=batch_sz: ice.privatekey_group_to_ETH_address(TEST_PK, bs), iters, batch_sz)
    if r: results[f'pvk_group_ETH_{batch_sz}'] = r

# Bytes variant
for batch_sz in [1000, 10_000, 100_000]:
    iters = max(5, 200_000 // batch_sz)
    r = benchmark(f"privatekey_group_to_ETH_address_bytes(pk, {batch_sz})",
        lambda bs=batch_sz: ice.privatekey_group_to_ETH_address_bytes(TEST_PK, bs), iters, batch_sz)
    if r: results[f'pvk_group_ETH_bytes_{batch_sz}'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("8. BSGS BABY TABLE CREATION")
# ─────────────────────────────────────────────────────────────────────────────

# create_baby_table returns 32-byte X coordinates for baby step table
for table_sz in [1000, 10_000, 100_000]:
    iters = max(3, 100_000 // table_sz)
    r = benchmark(f"create_baby_table(1, {table_sz})",
        lambda ts=table_sz: ice.create_baby_table(1, ts), iters, table_sz)
    if r: results[f'create_baby_table_{table_sz}'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("9. BLOOM FILTER OPERATIONS")
# ─────────────────────────────────────────────────────────────────────────────

# Create a small bloom filter with some h160 entries
print("  Setting up bloom filter with 10,000 entries...")
num_entries = 10_000
# Generate some h160 values
h160_list = []
test_h160s = ice.privatekey_loop_h160(num_entries, 0, True, TEST_PK)
for i in range(num_entries):
    h160_list.append(test_h160s[i*20:(i+1)*20])

# Fill_in_bloom returns (bits, hashes, filter_bytes, fp, count)
bloom_bits, bloom_hashes, bloom_filter, _fp, _cnt = ice.Fill_in_bloom(h160_list, 1e-8)
print(f"  Bloom: bits={bloom_bits}, hashes={bloom_hashes}, filter_size={len(bloom_filter)} bytes")
print(f"  Bloom filled with {num_entries} entries")
print()

# check_in_bloom (single)
N = 500_000
test_h = h160_list[0]
r = benchmark("check_in_bloom(h160, bits, hashes, bf)",
    lambda: ice.check_in_bloom(test_h, bloom_bits, bloom_hashes, bloom_filter), N)
if r: results['check_in_bloom'] = r

# bloom_check_add_mcpu — batch check
# Signature: bloom_check_add_mcpu(bigbuff, num_items, sz, mcpu, check_add, bloom_bits, bloom_hashes, bloom_filter)
# check_add: 0=check, 1=add
for batch_sz in [1000, 10_000]:
    bigbuff = b''.join(h160_list[:batch_sz])
    iters = max(10, 200_000 // batch_sz)
    r = benchmark(f"bloom_check_add_mcpu(check, {batch_sz} items, mcpu={NUM_CPUS})",
        lambda bb=bigbuff, bs=batch_sz: ice.bloom_check_add_mcpu(bb, bs, 20, NUM_CPUS, 0, bloom_bits, bloom_hashes, bloom_filter),
        iters, batch_sz)
    if r: results[f'bloom_check_mcpu_{batch_sz}'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("10. COLLISION CHECKING (requires bin file loaded in RAM)")
# ─────────────────────────────────────────────────────────────────────────────

# We need to create a bin file with sorted h160 data, load it, then check
print("  Creating sorted binary h160 file for collision testing...")
import struct

# Sort the h160 list and write as binary
sorted_h160 = sorted(h160_list)
bin_file = '/tmp/test_h160.bin'
with open(bin_file, 'wb') as f:
    for h in sorted_h160:
        f.write(h)

try:
    ice.Load_data_to_memory(bin_file, False)
    print(f"  Loaded {num_entries} h160 entries into memory")
    print()

    # check_collision (single)
    N = 500_000
    r = benchmark("check_collision(h160) [binary search in RAM]",
        lambda: ice.check_collision(test_h), N)
    if r: results['check_collision'] = r

    # check_collision_mcpu (batch)
    for batch_sz in [1000, 10_000]:
        bigbuff = b''.join(h160_list[:batch_sz])
        iters = max(10, 200_000 // batch_sz)
        r = benchmark(f"check_collision_mcpu({batch_sz} items, mcpu={NUM_CPUS})",
            lambda bb=bigbuff, bs=batch_sz: ice.check_collision_mcpu(bb, bs, NUM_CPUS),
            iters, batch_sz)
        if r: results[f'check_collision_mcpu_{batch_sz}'] = r
except Exception as e:
    print(f"  Collision check setup failed: {e}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
divider("11. COMBINED PIPELINE BENCHMARKS (Real-world scanning)")
# ─────────────────────────────────────────────────────────────────────────────

print("  These test the actual throughput of complete scanning strategies.")
print()

# Strategy A: privatekey_loop_h160 (all-in-one C function)
batch_sz = 100_000
iters = 20
def strategy_a():
    return ice.privatekey_loop_h160(batch_sz, 0, True, TEST_PK)
r = benchmark(f"Strategy A: privatekey_loop_h160({batch_sz}) [all-in-one]",
    strategy_a, iters, batch_sz)
if r: results['strategy_A_loop_h160'] = r

# Strategy A-SSE: privatekey_loop_h160_sse
def strategy_a_sse():
    return ice.privatekey_loop_h160_sse(batch_sz, 0, True, TEST_PK)
r = benchmark(f"Strategy A-SSE: privatekey_loop_h160_sse({batch_sz})",
    strategy_a_sse, iters, batch_sz)
if r: results['strategy_A_SSE_loop_h160'] = r

# Strategy B: point_sequential_increment + pubkey_to_h160 loop
def strategy_b():
    pts = ice.point_sequential_increment(batch_sz, BASE_POINT)
    # Now hash each point — this is the bottleneck
    for i in range(batch_sz):
        ice.pubkey_to_h160(0, True, pts[i*65:(i+1)*65])
r = benchmark(f"Strategy B: seq_increment({batch_sz}) + pubkey_to_h160 loop",
    strategy_b, 3, batch_sz)
if r: results['strategy_B_seq_plus_hash'] = r

# Strategy C: point_sequential_increment only (if we had C-level hash pipeline)
def strategy_c():
    return ice.point_sequential_increment(batch_sz, BASE_POINT)
r = benchmark(f"Strategy C: point_sequential_increment({batch_sz}) [points only, no hash]",
    strategy_c, 20, batch_sz)
if r: results['strategy_C_points_only'] = r

# Strategy D: P2_mcpu points only
def strategy_d():
    return ice.point_sequential_increment_P2_mcpu(batch_sz, BASE_POINT, NUM_CPUS)
r = benchmark(f"Strategy D: point_seq_inc_P2_mcpu({batch_sz}, mcpu={NUM_CPUS}) [points only]",
    strategy_d, 20, batch_sz)
if r: results['strategy_D_P2_mcpu_points'] = r

# Strategy E: P2X_mcpu (X-coord only) — minimal data
def strategy_e():
    return ice.point_sequential_increment_P2X_mcpu(batch_sz, BASE_POINT, NUM_CPUS)
r = benchmark(f"Strategy E: point_seq_inc_P2X_mcpu({batch_sz}, mcpu={NUM_CPUS}) [X-only]",
    strategy_e, 20, batch_sz)
if r: results['strategy_E_P2X_mcpu'] = r

# Strategy F: privatekey_loop_h160 + bloom check
def strategy_f():
    h160s = ice.privatekey_loop_h160(batch_sz, 0, True, TEST_PK)
    # batch bloom check
    ice.bloom_check_add_mcpu(h160s, batch_sz, 20, NUM_CPUS, 0, bloom_bits, bloom_hashes, bloom_filter)
r = benchmark(f"Strategy F: loop_h160({batch_sz}) + bloom_check_mcpu [full pipeline]",
    strategy_f, 10, batch_sz)
if r: results['strategy_F_h160_bloom'] = r

# Strategy G: privatekey_loop_h160 + simple byte comparison (target scan)
def strategy_g():
    h160s = ice.privatekey_loop_h160(batch_sz, 0, True, TEST_PK)
    # Just scan for target h160 in the buffer
    target = TARGET_H160
    for i in range(batch_sz):
        if h160s[i*20:(i+1)*20] == target:
            return True
    return False
r = benchmark(f"Strategy G: loop_h160({batch_sz}) + Python byte scan [single target]",
    strategy_g, 10, batch_sz)
if r: results['strategy_G_h160_pyscan'] = r

# Strategy H: privatekey_loop_h160 + 'in' operator (fastest Python check)
def strategy_h():
    h160s = ice.privatekey_loop_h160(batch_sz, 0, True, TEST_PK)
    return TARGET_H160 in h160s  # Python 'in' on bytes is C-optimized
r = benchmark(f"Strategy H: loop_h160({batch_sz}) + Python 'in' bytes search",
    strategy_h, 20, batch_sz)
if r: results['strategy_H_h160_in'] = r

# Strategy I: privatekey_loop_h160_sse + 'in' search
def strategy_i():
    h160s = ice.privatekey_loop_h160_sse(batch_sz, 0, True, TEST_PK)
    return TARGET_H160 in h160s
r = benchmark(f"Strategy I: loop_h160_sse({batch_sz}) + Python 'in' bytes search",
    strategy_i, 20, batch_sz)
if r: results['strategy_I_sse_in'] = r


# ─────────────────────────────────────────────────────────────────────────────
divider("12. LARGE BATCH TESTS (finding optimal batch size)")
# ─────────────────────────────────────────────────────────────────────────────

print("  Testing privatekey_loop_h160 and _sse at various batch sizes")
print("  to find the performance sweet spot.")
print()

for batch_sz in [10_000, 50_000, 100_000, 500_000, 1_000_000, 2_000_000]:
    iters = max(3, 2_000_000 // batch_sz)
    try:
        r = benchmark(f"privatekey_loop_h160(count={batch_sz:,})",
            lambda bs=batch_sz: ice.privatekey_loop_h160(bs, 0, True, TEST_PK), iters, batch_sz)
        if r: results[f'loop_h160_batch_{batch_sz}'] = r
    except Exception as e:
        print(f"  FAILED at batch_sz={batch_sz}: {e}")
        print()

print("  --- SSE variant ---")
print()
for batch_sz in [10_000, 50_000, 100_000, 500_000, 1_000_000, 2_000_000]:
    iters = max(3, 2_000_000 // batch_sz)
    try:
        r = benchmark(f"privatekey_loop_h160_sse(count={batch_sz:,})",
            lambda bs=batch_sz: ice.privatekey_loop_h160_sse(bs, 0, True, TEST_PK), iters, batch_sz)
        if r: results[f'loop_h160_sse_batch_{batch_sz}'] = r
    except Exception as e:
        print(f"  FAILED at batch_sz={batch_sz}: {e}")
        print()


# ═══════════════════════════════════════════════════════════════════════════════
#  RESULTS SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════

divider("RESULTS SUMMARY — Sorted by Keys/sec (fastest first)")

sorted_results = sorted(results.items(), key=lambda x: x[1], reverse=True)

print(f"  {'Function':<60} {'Keys/sec':>15}")
print(f"  {'─'*60} {'─'*15}")
for name, rate in sorted_results:
    print(f"  {name:<60} {fmt(rate):>15}/s")

print()
divider("KEY FINDINGS & RECOMMENDATIONS")

# Find the best strategy
strategies = {k: v for k, v in results.items() if k.startswith('strategy_')}
if strategies:
    best_strat = max(strategies.items(), key=lambda x: x[1])
    print(f"  FASTEST FULL PIPELINE: {best_strat[0]}")
    print(f"    -> {fmt(best_strat[1])}/s ({best_strat[1]:,.0f} keys/sec)")
    print()

# Best loop_h160 batch size
loop_results = {k: v for k, v in results.items() if k.startswith('loop_h160_batch_')}
if loop_results:
    best_loop = max(loop_results.items(), key=lambda x: x[1])
    print(f"  BEST loop_h160 BATCH SIZE: {best_loop[0]}")
    print(f"    -> {fmt(best_loop[1])}/s")
    print()

sse_results = {k: v for k, v in results.items() if k.startswith('loop_h160_sse_batch_')}
if sse_results:
    best_sse = max(sse_results.items(), key=lambda x: x[1])
    print(f"  BEST loop_h160_sse BATCH SIZE: {best_sse[0]}")
    print(f"    -> {fmt(best_sse[1])}/s")
    print()

# Point generation rate
pt_results = {k: v for k, v in results.items() if 'point_seq' in k or 'P2' in k}
if pt_results:
    best_pt = max(pt_results.items(), key=lambda x: x[1])
    print(f"  FASTEST POINT GENERATION: {best_pt[0]}")
    print(f"    -> {fmt(best_pt[1])}/s")
    print()

# Bottleneck analysis
if 'strategy_C_points_only' in results and 'strategy_A_loop_h160' in results:
    pt_rate = results['strategy_C_points_only']
    h160_rate = results['strategy_A_loop_h160']
    print(f"  BOTTLENECK ANALYSIS:")
    print(f"    Point generation (no hash): {fmt(pt_rate)}/s")
    print(f"    Full h160 pipeline:         {fmt(h160_rate)}/s")
    print(f"    Hashing overhead:           {(1 - h160_rate/pt_rate)*100:.1f}% of time spent on SHA256+RIPEMD160")
    print()

# Time to scan full range
if strategies:
    best_rate = best_strat[1]
    range_size = RANGE_END - RANGE_START + 1
    seconds = range_size / best_rate
    hours = seconds / 3600
    days = hours / 24
    years = days / 365.25
    print(f"  TIME TO SCAN FULL RANGE ({range_size:.2e} keys):")
    print(f"    At {fmt(best_rate)}/s: {years:.1f} years ({days:,.0f} days)")
    print(f"    Need ~{range_size / (365.25*24*3600) / 1e6:.0f}M keys/sec for 1-year scan")
    print()

print(f"  RECOMMENDED APPROACH:")
print(f"    1. Use privatekey_loop_h160 / _sse for all-in-one h160 generation")
print(f"    2. Use Python 'in' operator on result bytes for single-target scanning")
print(f"    3. Use bloom_check_add_mcpu for multi-target scanning")
print(f"    4. Optimal batch size: test above to find your sweet spot")
print(f"    5. For BSGS approach: use create_baby_table + bloom for baby-giant steps")
print()

# Cleanup
try:
    os.remove(bin_file)
except:
    pass

print("Benchmark complete.")
