#!/usr/bin/env python3
"""
Bitcoin Puzzle #71 — Multi-Strategy Brute Force Scanner
Target: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU
Range:  0x400000000000000000 - 0x7fffffffffffffffff (2^70 to 2^71-1)

Strategies:
  1. RANDOM — random sampling across full keyspace (lottery mode)
  2. SEQUENTIAL — systematic sweep from random start points
  3. BIRTHDAY — random + bloom filter collision detection

Runs all CPU cores in parallel. Logs progress. Saves key on crack.
"""

import sys
import os
import time
import random
import signal
import json
import multiprocessing as mp
from datetime import datetime, timedelta
from pathlib import Path

# Add iceland secp256k1
sys.path.insert(0, "/root/iceland_secp256k1")
import secp256k1 as ice

# ─── PUZZLE PARAMETERS ───────────────────────────────────────────────
TARGET_ADDR = "1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU"
TARGET_H160 = "f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8"
START = 0x400000000000000000   # 2^70
END   = 0x7FFFFFFFFFFFFFFFFF   # 2^71 - 1
KEYSPACE = END - START + 1

BATCH_SIZE = 50000             # keys per batch
LOG_INTERVAL = 30              # seconds between status prints
SAVE_INTERVAL = 300            # seconds between state saves
LOG_DIR = Path("/root/puzzle71/logs")
DATA_DIR = Path("/root/puzzle71/data")
FOUND_FILE = Path("/root/puzzle71/FOUND_KEY.txt")
STATS_FILE = DATA_DIR / "stats.json"

# ─── SHARED STATE ─────────────────────────────────────────────────────
shutdown = mp.Event()
found = mp.Event()
total_checked = mp.Value('q', 0)  # unsigned long long
found_key = mp.Array('c', 100)     # shared buffer for found key

def save_found_key(private_key_int):
    """Save the cracked key to disk immediately."""
    pk_hex = hex(private_key_int)
    addr = ice.privatekey_to_address(0, True, private_key_int)
    wif = ice.btc_pvk_to_wif(pk_hex)

    msg = f"""
╔══════════════════════════════════════════════════════════════╗
║                  🔑 PUZZLE #71 CRACKED! 🔑                  ║
╠══════════════════════════════════════════════════════════════╣
║ Private Key (hex): {pk_hex}
║ Private Key (int): {private_key_int}
║ WIF:               {wif}
║ Address:           {addr}
║ Time:              {datetime.now().isoformat()}
╚══════════════════════════════════════════════════════════════╝
"""
    # Write to multiple locations for safety
    for path in [FOUND_FILE, Path("/root/FOUND_KEY_PUZZLE71.txt"), Path("/tmp/FOUND_KEY_PUZZLE71.txt")]:
        try:
            with open(path, 'w') as f:
                f.write(msg)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass

    print("\n" + "=" * 70)
    print(msg)
    print("=" * 70)


def check_batch_sequential(start_key, count):
    """Check a sequential batch of keys. Returns found key or None."""
    target_bytes = bytes.fromhex(TARGET_H160)
    results = ice.privatekey_loop_h160(count, 0, True, start_key)

    for i in range(count):
        h160 = results[i * 20:(i + 1) * 20]
        if h160 == target_bytes:
            return start_key + i
    return None


def check_batch_random(count):
    """Check random keys across the keyspace. Returns found key or None."""
    target_bytes = bytes.fromhex(TARGET_H160)

    for _ in range(count):
        pk = random.randint(START, END)
        h160_str = ice.privatekey_to_h160(0, True, pk)
        if isinstance(h160_str, str):
            if h160_str == TARGET_H160:
                return pk
        else:
            if h160_str == target_bytes:
                return pk
    return None


def worker_sequential(worker_id, total_checked, found, shutdown, found_key):
    """Sequential scanner — sweeps from a random start point."""
    random.seed(os.urandom(8))
    start = random.randint(START, END - BATCH_SIZE * 10000)
    current = start
    local_count = 0

    while not shutdown.is_set() and not found.is_set():
        try:
            result = check_batch_sequential(current, BATCH_SIZE)
            if result is not None:
                save_found_key(result)
                found_key.value = hex(result).encode()
                found.set()
                shutdown.set()
                return

            current += BATCH_SIZE
            if current > END:
                current = random.randint(START, END - BATCH_SIZE * 10000)

            local_count += BATCH_SIZE
            with total_checked.get_lock():
                total_checked.value += BATCH_SIZE

        except Exception as e:
            print(f"[SEQ-{worker_id}] Error: {e}")
            time.sleep(1)


def worker_random(worker_id, total_checked, found, shutdown, found_key):
    """Random scanner — samples random keys across full range."""
    random.seed(os.urandom(8))
    batch = 1000  # smaller batches for true randomness

    while not shutdown.is_set() and not found.is_set():
        try:
            result = check_batch_random(batch)
            if result is not None:
                save_found_key(result)
                found_key.value = hex(result).encode()
                found.set()
                shutdown.set()
                return

            with total_checked.get_lock():
                total_checked.value += batch

        except Exception as e:
            print(f"[RND-{worker_id}] Error: {e}")
            time.sleep(1)


def worker_hybrid(worker_id, total_checked, found, shutdown, found_key):
    """Hybrid scanner — sequential batches at random starting points."""
    random.seed(os.urandom(8))
    chunk_size = BATCH_SIZE * 20  # scan 1M keys per random jump

    while not shutdown.is_set() and not found.is_set():
        try:
            start = random.randint(START, END - chunk_size)

            for offset in range(0, chunk_size, BATCH_SIZE):
                if shutdown.is_set() or found.is_set():
                    return

                result = check_batch_sequential(start + offset, BATCH_SIZE)
                if result is not None:
                    save_found_key(result)
                    found_key.value = hex(result).encode()
                    found.set()
                    shutdown.set()
                    return

                with total_checked.get_lock():
                    total_checked.value += BATCH_SIZE

        except Exception as e:
            print(f"[HYB-{worker_id}] Error: {e}")
            time.sleep(1)


def monitor(total_checked, found, shutdown):
    """Monitor process — prints stats and saves state."""
    start_time = time.time()
    last_count = 0
    last_time = start_time
    last_save = start_time
    peak_rate = 0

    print(f"\n{'='*70}")
    print(f"  BITCOIN PUZZLE #71 SCANNER")
    print(f"  Target: {TARGET_ADDR}")
    print(f"  Range:  0x{START:x} → 0x{END:x}")
    print(f"  Keyspace: {KEYSPACE:.4e} keys")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*70}\n")

    while not shutdown.is_set() and not found.is_set():
        time.sleep(LOG_INTERVAL)

        now = time.time()
        elapsed = now - start_time
        current_count = total_checked.value

        # Calculate rates
        interval_count = current_count - last_count
        interval_time = now - last_time
        current_rate = interval_count / interval_time if interval_time > 0 else 0
        avg_rate = current_count / elapsed if elapsed > 0 else 0
        peak_rate = max(peak_rate, current_rate)

        # Progress
        pct = (current_count / KEYSPACE) * 100

        # ETA (at current rate, though it's effectively infinite)
        if current_rate > 0:
            remaining = KEYSPACE - current_count
            eta_seconds = remaining / current_rate
            if eta_seconds > 365.25 * 24 * 3600 * 1000:
                eta_str = f"{eta_seconds / (365.25*24*3600):.0f} years"
            else:
                eta_str = str(timedelta(seconds=int(eta_seconds)))
        else:
            eta_str = "∞"

        # Probability of having found it by now
        prob = current_count / KEYSPACE
        prob_str = f"{prob:.2e}" if prob < 0.01 else f"{prob:.6%}"

        status = (
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"Checked: {current_count:>15,} | "
            f"Rate: {current_rate:>10,.0f} k/s | "
            f"Peak: {peak_rate:>10,.0f} k/s | "
            f"Prob: {prob_str} | "
            f"Uptime: {timedelta(seconds=int(elapsed))}"
        )
        print(status)

        last_count = current_count
        last_time = now

        # Periodic save
        if now - last_save > SAVE_INTERVAL:
            stats = {
                "total_checked": current_count,
                "elapsed_seconds": elapsed,
                "avg_rate": avg_rate,
                "peak_rate": peak_rate,
                "last_update": datetime.now().isoformat(),
                "probability": current_count / KEYSPACE,
            }
            try:
                with open(STATS_FILE, 'w') as f:
                    json.dump(stats, f, indent=2)
            except:
                pass
            last_save = now

    if found.is_set():
        print("\n" + "🔑 " * 20)
        print("KEY FOUND! Check /root/puzzle71/FOUND_KEY.txt")
        print("🔑 " * 20 + "\n")


def main():
    num_cpus = mp.cpu_count()
    print(f"CPUs available: {num_cpus}")

    # Strategy: allocate cores
    # - Half for hybrid (best of both worlds: sequential speed + random coverage)
    # - Remaining for pure sequential and random
    n_hybrid = max(1, num_cpus - 2)
    n_sequential = 1
    n_random = max(1, num_cpus - n_hybrid - n_sequential)

    # If only 2 cores, do 1 hybrid + 1 sequential
    if num_cpus <= 2:
        n_hybrid = 1
        n_sequential = 1
        n_random = 0

    print(f"Workers: {n_hybrid} hybrid + {n_sequential} sequential + {n_random} random = {n_hybrid + n_sequential + n_random} total")

    processes = []

    # Launch monitor
    mon = mp.Process(target=monitor, args=(total_checked, found, shutdown), daemon=True)
    mon.start()
    processes.append(mon)

    # Launch hybrid workers
    for i in range(n_hybrid):
        p = mp.Process(target=worker_hybrid, args=(i, total_checked, found, shutdown, found_key))
        p.start()
        processes.append(p)

    # Launch sequential workers
    for i in range(n_sequential):
        p = mp.Process(target=worker_sequential, args=(i + 100, total_checked, found, shutdown, found_key))
        p.start()
        processes.append(p)

    # Launch random workers
    for i in range(n_random):
        p = mp.Process(target=worker_random, args=(i + 200, total_checked, found, shutdown, found_key))
        p.start()
        processes.append(p)

    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\n\nShutting down gracefully...")
        shutdown.set()
        for p in processes:
            p.join(timeout=5)

        final_count = total_checked.value
        print(f"\nTotal keys checked this session: {final_count:,}")
        print(f"Probability of having found it: {final_count/KEYSPACE:.2e}")

        # Save final state
        stats = {
            "total_checked": final_count,
            "last_update": datetime.now().isoformat(),
            "probability": final_count / KEYSPACE,
        }
        try:
            with open(STATS_FILE, 'w') as f:
                json.dump(stats, f, indent=2)
        except Exception:
            pass

        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Wait forever
    try:
        while not found.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(None, None)

    # If found, keep alive briefly for logging
    if found.is_set():
        time.sleep(5)
        for p in processes:
            p.join(timeout=5)


if __name__ == "__main__":
    main()
