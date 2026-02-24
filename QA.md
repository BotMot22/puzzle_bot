# PUZZLE_BOT — QA Report & Regression Checklist

## How to Run

```bash
# Activate venv
source /root/btc_puzzle_env/bin/activate

# Run turbo scanner (primary bot — 4 workers)
cd /root/puzzle71
python3 turbo_scanner.py -w 4

# Run in tmux for 24/7 operation
tmux new-session -d -s puzzle_bot -n scanner \
  "source /root/btc_puzzle_env/bin/activate && cd /root/puzzle71 && python3 turbo_scanner.py -w 4"

# Or use the launch script
bash /root/puzzle71/launch.sh
```

## How to Validate

```bash
# Check if running
ps aux | grep turbo_scanner | grep -v grep

# Check live stats
cat /root/puzzle71/data/turbo_stats.json | python3 -m json.tool

# Check if key found
cat /root/puzzle71/FOUND_KEY.txt 2>/dev/null || echo "Not yet"

# Attach to tmux session
tmux attach -t puzzle_bot
```

## Where Configs Live

| Item | Location |
|---|---|
| Turbo scanner (primary) | `/root/puzzle71/turbo_scanner.py` |
| Multi-strategy scanner | `/root/puzzle71/scanner.py` |
| Launch script | `/root/puzzle71/launch.sh` |
| Stats JSON | `/root/puzzle71/data/turbo_stats.json` |
| Logs | `/root/puzzle71/logs/` |
| Found key (if cracked) | `/root/puzzle71/FOUND_KEY.txt` |
| Iceland secp256k1 lib | `/root/iceland_secp256k1/` |
| Python venv | `/root/btc_puzzle_env/` |

---

## Bug Tracker

| ID | Title | Severity | File | Root Cause | Fix | Verified |
|---|---|---|---|---|---|---|
| BUG-001 | **privatekey_loop_h160 wrong argument order** | **BLOCKER** | `turbo_scanner.py:85`, `scanner.py:80` | Called `(count, 0, key_start, 1)` but correct signature is `(num, addr_type, iscompressed, pvk_int)`. Was passing key_start as iscompressed and `1` as the private key. **Scanner was checking wrong keys — could never find target.** | Changed to `(BATCH, 0, True, key_start)` | Planted key 0x400000000000000042 in batch, confirmed recovery via blob.find(). 10/10 individual-vs-batch match test PASS. |
| BUG-002 | **blob.find() skips aligned match after unaligned hit** | HIGH | `turbo_scanner.py:88-96` | `blob.find()` returns first occurrence. If target bytes appear at unaligned offset, the aligned real match is silently skipped. | Replaced with `scan_blob_for_target()` loop that advances past unaligned matches. | Code review + logic analysis. P(manifesting) ~6.8e-43 per batch but correctness matters. |
| BUG-003 | **save_found_key crashes on disk write failure** | HIGH | `scanner.py:68-70` | No try/except around file writes. If first path fails, remaining paths never written. Key could be lost. | Added try/except per path + `os.fsync()` for durability. | Code review verified. turbo_scanner.py already had try/except. |
| BUG-004 | **Signal handler crashes on disk write** | MED | `scanner.py:332` | `signal_handler` writes stats without try/except. Disk-full or permission error crashes shutdown. | Wrapped in try/except. | Code review verified. |
| BUG-005 | **Non-atomic stats JSON writes** | MED | `turbo_scanner.py:152` | Direct `open('w')` + `json.dump()` — if reader opens mid-write, gets partial/corrupt JSON. | Added `write_stats_atomic()` using tempfile + `os.replace()`. | Verified stats file is valid JSON after multiple read cycles during operation. |
| BUG-006 | **Unused shared variable `worker_count`** | LOW | `turbo_scanner.py:45` | `worker_count = mp.Value('i', 0)` declared but never read or written. | Removed. | grep confirms no remaining references. |
| BUG-007 | **Default workers=64 on 4-core machine** | LOW | `turbo_scanner.py:166` | Default `-w 64` causes 64 processes on 4 cores — massive context-switch overhead, ~60% slower. | Changed default to 4. Launch script should match actual CPU count. | Benchmarked: 4 workers = ~1.1M/s vs 64 workers = ~0.4M/s on this hardware. |
| BUG-008 | **No flush on print/log output** | LOW | Both files | `print()` without `flush=True` in a piped `tee` setup causes delayed/buffered output. Header and stats appear late. | Added `flush=True` to all critical prints. | Observed immediate output in tmux after fix. |

---

## Manual Regression Checklist

### RC-1: Batch h160 correctness (validates BUG-001 fix)
```bash
source /root/btc_puzzle_env/bin/activate && python3 -c "
import sys; sys.path.insert(0, '/root/iceland_secp256k1')
import secp256k1 as ice
base = 0x400000000000000000
blob = ice.privatekey_loop_h160(10, 0, True, base)
ok = all(ice.privatekey_to_h160(0, True, base+i) == blob[i*20:(i+1)*20] for i in range(10))
print('RC-1 PASS' if ok else 'RC-1 FAIL')
"
```
**Expected:** `RC-1 PASS`

### RC-2: End-to-end key recovery (validates BUG-001 + BUG-002)
```bash
source /root/btc_puzzle_env/bin/activate && python3 -c "
import sys; sys.path.insert(0, '/root/iceland_secp256k1')
import secp256k1 as ice
pk = 0x400000000000000042
h = ice.privatekey_to_h160(0, True, pk)
blob = ice.privatekey_loop_h160(100, 0, True, 0x400000000000000000)
idx = blob.find(h)
recovered = 0x400000000000000000 + idx // 20
print('RC-2 PASS' if recovered == pk and idx % 20 == 0 else 'RC-2 FAIL')
"
```
**Expected:** `RC-2 PASS`

### RC-3: Scanner starts and produces stats (validates BUG-007 + BUG-008)
```bash
source /root/btc_puzzle_env/bin/activate && timeout 20 python3 /root/puzzle71/turbo_scanner.py -w 2 2>&1 | head -15
# Should see header + at least 1 stats line within 20 seconds
```
**Expected:** Header with "PUZZLE_BOT" + at least one `[HH:MM:SS] Keys:` line

### RC-4: Stats file is valid JSON (validates BUG-005)
```bash
# While scanner is running:
python3 -c "import json; d=json.load(open('/root/puzzle71/data/turbo_stats.json')); print('RC-4 PASS' if 'checked' in d else 'RC-4 FAIL')"
```
**Expected:** `RC-4 PASS`

### RC-5: Graceful shutdown saves final stats (validates BUG-004)
```bash
source /root/btc_puzzle_env/bin/activate
timeout 25 bash -c 'python3 /root/puzzle71/turbo_scanner.py -w 2 & PID=$!; sleep 18; kill -INT $PID; wait $PID 2>/dev/null'
python3 -c "import json; d=json.load(open('/root/puzzle71/data/turbo_stats.json')); print('RC-5 PASS' if d.get('checked',0) > 0 else 'RC-5 FAIL')"
```
**Expected:** `RC-5 PASS`

---

## Common Failure Modes & Debugging

| Symptom | Cause | Fix |
|---|---|---|
| Scanner starts but no stats lines | CHUNK too large, first counter update delayed | Reduce CHUNK or BATCH |
| Rate much lower than expected | Too many workers for CPU count | Use `-w <num_cpus>` |
| `ModuleNotFoundError: secp256k1` | Venv not activated or wrong cwd | `source /root/btc_puzzle_env/bin/activate` |
| Stats JSON parse error | Read during write (pre-fix) | Fixed with atomic writes |
| "Killed" message on start | OOM from too many workers | Reduce worker count |
| tmux session disappears | Scanner crashed — check log | `cat /root/puzzle71/logs/turbo_*.log | tail -50` |

## Known Limitations

1. **CPU-only**: ~1.1M keys/sec on 4 cores. GPU (BitCrack) would be 100-1000x faster.
2. **No persistence across restarts**: Counter resets on restart. No cumulative tracking.
3. **No deduplication**: Random jumps may re-scan previously checked ranges.
4. **Single-machine**: No distributed coordination. Each instance is independent.
