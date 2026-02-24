#!/usr/bin/env python3
"""
PUZZLE_BOT — Bitcoin Puzzle #71 Scanner
Target: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU
h160:   f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8
Range:  0x400000000000000000 → 0x7FFFFFFFFFFFFFFFFF (2^70 to 2^71-1)

Strategy: Hybrid random-jump + sequential-batch scanning.
  - Each worker picks a random offset, scans CHUNK consecutive keys
  - Uses iceland secp256k1 batch h160 for max throughput
  - blob.find() with alignment-safe loop for correctness
"""

import sys
import os
import time
import random
import signal
import json
import tempfile
import argparse
import multiprocessing as mp
from datetime import datetime, timedelta

sys.path.insert(0, "/root/iceland_secp256k1")
import secp256k1 as ice

# ─── CONFIG ───────────────────────────────────────────────────────────
TARGET_H160 = bytes.fromhex("f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8")
TARGET_ADDR = "1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU"
START = 0x400000000000000000
END   = 0x7FFFFFFFFFFFFFFFFF
KEYSPACE = END - START + 1

BATCH = 50000                   # keys per batch
CHUNK = BATCH * 20              # keys per random jump (1M per chunk)
LOG_INTERVAL = 15               # seconds between status prints
STATS_FILE = "/root/puzzle71/data/turbo_stats.json"
FOUND_PATHS = [
    "/root/puzzle71/FOUND_KEY.txt",
    "/root/FOUND_KEY_PUZZLE71.txt",
    "/tmp/FOUND_KEY.txt",
]

# ─── SHARED STATE ─────────────────────────────────────────────────────
shutdown_flag = mp.Event()
found_flag = mp.Event()
counter = mp.Value('q', 0)


def save_key(pk_int):
    """Save cracked key to multiple locations with redundancy."""
    pk_hex = hex(pk_int)
    try:
        wif = ice.btc_pvk_to_wif(pk_hex)
    except Exception:
        wif = "ERROR_GENERATING_WIF"
    try:
        addr = ice.privatekey_to_address(0, True, pk_int)
    except Exception:
        addr = "ERROR_GENERATING_ADDR"

    msg = (
        f"\n{'='*60}\n"
        f"  PUZZLE #71 CRACKED\n"
        f"  Private Key (hex): {pk_hex}\n"
        f"  Private Key (int): {pk_int}\n"
        f"  WIF: {wif}\n"
        f"  Address: {addr}\n"
        f"  Time: {datetime.now().isoformat()}\n"
        f"{'='*60}\n"
    )
    for p in FOUND_PATHS:
        try:
            with open(p, 'w') as f:
                f.write(msg)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
    print(msg, flush=True)


def scan_blob_for_target(blob, target, batch_size):
    """Search blob for target h160, handling alignment correctly.
    Returns the aligned index (key offset) or -1 if not found."""
    search_start = 0
    while True:
        idx = blob.find(target, search_start)
        if idx == -1:
            return -1
        if idx % 20 == 0:
            return idx // 20
        # Unaligned match — advance past it and keep searching
        search_start = idx + 1


def turbo_worker(wid):
    """Hybrid turbo worker: sequential batches at random offsets."""
    random.seed(int.from_bytes(os.urandom(8), 'big') ^ (wid * 31337))
    local = 0

    while not shutdown_flag.is_set() and not found_flag.is_set():
        # Pick random start within range, ensuring batch won't exceed END
        max_base = END - CHUNK + 1
        if max_base < START:
            max_base = START
        base = random.randint(START, max_base)

        for offset in range(0, CHUNK, BATCH):
            if shutdown_flag.is_set() or found_flag.is_set():
                return

            key_start = base + offset
            # SSE variant is ~30% faster than non-SSE
            # Correct arg order: (num, addr_type, iscompressed, pvk_int)
            blob = ice.privatekey_loop_h160_sse(BATCH, 0, True, key_start)

            # Alignment-safe search
            key_offset = scan_blob_for_target(blob, TARGET_H160, BATCH)
            if key_offset >= 0:
                found_pk = key_start + key_offset
                # Double-verify with individual address generation
                verify_addr = ice.privatekey_to_address(0, True, found_pk)
                if verify_addr == TARGET_ADDR:
                    save_key(found_pk)
                    found_flag.set()
                    shutdown_flag.set()
                    return
                else:
                    # False positive from h160 collision (astronomically unlikely)
                    print(f"[W-{wid}] False positive at 0x{found_pk:x}: {verify_addr}", flush=True)

            local += BATCH

        # Bulk update shared counter (less lock contention)
        with counter.get_lock():
            counter.value += local
        local = 0


def write_stats_atomic(data):
    """Write stats JSON atomically via temp file + rename."""
    try:
        dir_path = os.path.dirname(STATS_FILE)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
        os.replace(tmp_path, STATS_FILE)
    except Exception:
        # Fallback: direct write
        try:
            with open(STATS_FILE, 'w') as f:
                json.dump(data, f)
        except Exception:
            pass


def monitor(nworkers):
    """Stats printer."""
    t0 = time.time()
    last_c = 0
    last_t = t0
    peak = 0

    print(f"\n{'='*70}")
    print(f"  PUZZLE_BOT — Bitcoin Puzzle #71")
    print(f"  Target h160: {TARGET_H160.hex()}")
    print(f"  Keyspace: {KEYSPACE:.4e} ({KEYSPACE:,})")
    print(f"  Batch: {BATCH:,} | Chunk: {CHUNK:,}")
    print(f"  Workers: {nworkers} (CPUs detected: {mp.cpu_count()})")
    print(f"  Started: {datetime.now()}")
    print(f"{'='*70}\n", flush=True)

    while not shutdown_flag.is_set() and not found_flag.is_set():
        time.sleep(LOG_INTERVAL)
        now = time.time()
        c = counter.value
        dt = now - last_t
        dc = c - last_c

        rate = dc / dt if dt > 0 else 0
        avg = c / (now - t0) if (now - t0) > 0 else 0
        peak = max(peak, rate)
        prob = c / KEYSPACE
        elapsed = timedelta(seconds=int(now - t0))
        keys_per_day = avg * 86400

        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"Keys: {c:>15,} | "
            f"Rate: {rate:>12,.0f}/s | "
            f"Peak: {peak:>12,.0f}/s | "
            f"P(found): {prob:.3e} | "
            f"Up: {elapsed}",
            flush=True
        )

        last_c = c
        last_t = now

        write_stats_atomic({
            "checked": c, "rate": rate, "avg": avg, "peak": peak,
            "prob": prob, "uptime_s": int(now - t0),
            "workers": nworkers, "keys_per_day": keys_per_day,
            "updated": datetime.now().isoformat()
        })


def main():
    parser = argparse.ArgumentParser(description="PUZZLE_BOT — Bitcoin Puzzle #71 Scanner")
    parser.add_argument('-w', '--workers', type=int, default=4,
                        help='Number of worker processes (default: 4)')
    parser.add_argument('-b', '--batch', type=int, default=0,
                        help='Batch size override')
    args = parser.parse_args()

    nworkers = args.workers

    if args.batch > 0:
        global BATCH, CHUNK
        BATCH = args.batch
        CHUNK = BATCH * 20

    # Sanity checks
    assert START < END, "Invalid key range"
    assert BATCH > 0, "Batch size must be positive"
    assert CHUNK > 0, "Chunk size must be positive"
    assert CHUNK <= KEYSPACE, "Chunk larger than keyspace"

    print(f"PUZZLE_BOT starting with {nworkers} workers...", flush=True)

    procs = []

    # Monitor (daemon so it dies if main dies)
    m = mp.Process(target=monitor, args=(nworkers,), daemon=True)
    m.start()
    procs.append(m)

    # Workers
    for i in range(nworkers):
        p = mp.Process(target=turbo_worker, args=(i,))
        p.start()
        procs.append(p)

    print(f"All {nworkers} workers launched.", flush=True)

    def handle_signal(sig, frame):
        print("\nShutting down...", flush=True)
        shutdown_flag.set()
        for p in procs:
            p.join(timeout=10)
        c = counter.value
        print(f"\nSession total: {c:,} keys checked", flush=True)
        print(f"P(found): {c/KEYSPACE:.3e}", flush=True)

        write_stats_atomic({
            "checked": c, "status": "stopped",
            "prob": c / KEYSPACE,
            "updated": datetime.now().isoformat()
        })
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not found_flag.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        handle_signal(None, None)

    if found_flag.is_set():
        time.sleep(3)
    for p in procs:
        p.join(timeout=5)


if __name__ == "__main__":
    main()
