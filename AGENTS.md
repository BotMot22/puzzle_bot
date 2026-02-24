# PUZZLE_BOT — Agent Orchestration Architecture

## A) System Map

**What this is:** A 24/7 brute-force scanner for Bitcoin Puzzle #71 (7.1 BTC bounty). Generates private keys in the range 2^70 to 2^71-1, hashes them to Bitcoin addresses, and checks for a match against target address `1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU`.

**Tech stack:**
- Language: Python 3.12
- Key library: iceland `secp256k1` (C-backed, `/root/iceland_secp256k1/`)
- Concurrency: `multiprocessing` (process-per-core)
- Runtime: tmux session `puzzle_bot`, venv at `/root/btc_puzzle_env/`
- No database. Stats persisted to JSON. Logs to flat files.

**Entrypoints:**
- `turbo_scanner.py` — primary 24/7 bot (hybrid random-jump strategy)
- `scanner.py` — multi-strategy scanner (sequential, random, hybrid workers)
- `launch.sh` — tmux launcher

**Key modules:**
| Module | Purpose | Owner candidate |
|---|---|---|
| `turbo_scanner.py` | Core scanning engine | Scanner Agent |
| `scanner.py` | Alternative multi-strategy engine | Scanner Agent |
| `launch.sh` | Deployment/process management | Ops Agent |
| `iceland_secp256k1/secp256k1.py` | Crypto primitives (external) | Crypto Agent |
| `data/turbo_stats.json` | Runtime metrics | Ops Agent |

**External integrations:** None currently. Candidates: webhook alerts, Telegram bot, GPU offload.

**Risk hotspots:**
1. iceland secp256k1 API — undocumented, arg order is footgun (BUG-001 proved this)
2. No cumulative persistence — restart loses all progress
3. No distributed coordination — multiple machines would duplicate work
4. CPU-only — 1000x slower than GPU equivalent

---

## B) Proposed Agent Roster

| # | Agent Name | Mission | Expertise |
|---|---|---|---|
| 1 | **Scanner Agent** | Own the core scanning loop: correctness, throughput, strategy | Python multiprocessing, secp256k1, bit manipulation |
| 2 | **Crypto Agent** | Own all cryptographic operations and library integration | ECDSA, secp256k1, Bitcoin address derivation, hash functions |
| 3 | **Ops Agent** | Own deployment, monitoring, alerting, log management | tmux, bash, systemd, process management, JSON stats |
| 4 | **Perf Agent** | Own benchmarking, profiling, and throughput optimization | Python profiling, C extensions, batch tuning, memory |

### Detailed Agent Profiles

#### 1. Scanner Agent
- **Scope:** `turbo_scanner.py`, `scanner.py` — scanning strategies, worker logic, shared state
- **Does NOT touch:** Crypto library internals, deployment scripts, monitoring
- **Primary files:** `/root/puzzle71/turbo_scanner.py`, `/root/puzzle71/scanner.py`
- **Task types:** New scanning strategies, worker allocation, batch/chunk tuning, correctness fixes
- **Output contract:** Returns `{file, line_range, change_description, repro_command, regression_check}`
- **Guardrails:** Must run RC-1 and RC-2 regression checks before any change to scanning loop. Must not change crypto API call signatures without Crypto Agent review.
- **Context strategy:** Keep turbo_scanner.py fully in memory. Re-read scanner.py on demand.

#### 2. Crypto Agent
- **Scope:** All `secp256k1` / `ice.*` calls, h160 comparison logic, key serialization (WIF, hex)
- **Does NOT touch:** Worker orchestration, process management, stats
- **Primary files:** `/root/iceland_secp256k1/secp256k1.py`, `/root/iceland_secp256k1/README.md`
- **Task types:** API correctness validation, new crypto approaches (BSGS, kangaroo), library upgrades
- **Output contract:** Returns `{function_name, correct_signature, test_snippet, performance_notes}`
- **Guardrails:** Every API call change must include a 10-key individual-vs-batch verification.
- **Context strategy:** Keep secp256k1.py function signatures + README in memory. Deep-read on demand.

#### 3. Ops Agent
- **Scope:** `launch.sh`, tmux sessions, log management, stats monitoring, alerting
- **Does NOT touch:** Scanning logic, crypto operations
- **Primary files:** `/root/puzzle71/launch.sh`, `/root/puzzle71/QA.md`, `/root/puzzle71/data/`
- **Task types:** Add alerting (webhook/Telegram), log rotation, systemd service, health checks
- **Output contract:** Returns `{script_path, install_command, validation_command, rollback_command}`
- **Guardrails:** Must not restart scanner without confirming current session stats are saved.
- **Context strategy:** Keep launch.sh + QA.md in memory. Stats JSON on demand.

#### 4. Perf Agent
- **Scope:** Benchmarking, profiling, batch size tuning, memory usage
- **Does NOT touch:** Business logic, crypto correctness, deployment
- **Primary files:** `/root/puzzle71/turbo_scanner.py` (read-only), `/root/iceland_secp256k1/benchmark.py`
- **Task types:** Profile hot loops, optimize batch sizes, reduce lock contention, memory analysis
- **Output contract:** Returns `{benchmark_results, recommended_params, before_after_comparison}`
- **Guardrails:** Must not change scanning logic. Recommends params only; Scanner Agent applies.
- **Context strategy:** Keep turbo_worker function in memory. Re-read benchmark.py on demand.

---

## C) Orchestration Protocol

### Delegation Template
```
@{AGENT_NAME}
TASK: {one-line description}
FILES: {exact paths}
CONTEXT: {relevant prior findings or constraints}
ACCEPTANCE: {what "done" looks like}
DEADLINE: {priority — P0/P1/P2}
OUTPUT FORMAT: {expected return format}
```

### Agent Response Template
```
AGENT: {name}
TASK: {task reference}
STATUS: {done | blocked | needs-decision}
CHANGES: {list of file:line changes}
VERIFICATION: {exact command that proves correctness}
RISKS: {side effects or unknowns}
FOLLOW-UP: {any tasks spawned}
```

### Conflict Resolution
1. **Crypto Agent wins** on any dispute about API usage or key derivation correctness
2. **Scanner Agent wins** on strategy and worker allocation decisions
3. **Perf Agent** recommendations are advisory — Scanner Agent decides whether to apply
4. **Ops Agent wins** on deployment and process management decisions
5. If two agents edit the same file, the domain owner reviews the other's diff before merge

### Merge Policy
- Each agent works on its owned files only
- Cross-cutting changes require explicit Orchestrator approval
- All changes must include a regression check command
- Commits are per-agent, per-task (small and atomic)

### Handoff Checklist
- [ ] Current file state described (path + relevant line numbers)
- [ ] What was changed and why (1-2 sentences)
- [ ] Regression check command included
- [ ] Any new assumptions or unknowns flagged
- [ ] Stats JSON or log snippet if relevant

---

## D) Parallel Backlog

### Scanner Agent (P0)
| # | Task | Files | Outcome | Acceptance |
|---|---|---|---|---|
| S1 | Add cumulative key counter persistence across restarts | `turbo_scanner.py` | Counter loads from disk on start, saves on shutdown | Counter survives restart |
| S2 | Add range deduplication (bloom filter or set) to avoid rescanning | `turbo_scanner.py` | Track scanned CHUNK ranges, skip duplicates | No duplicate chunks in 1hr run |
| S3 | Implement Kangaroo / BSGS strategy as alternative worker type | new: `bsgs_scanner.py` | Advanced math-based search alongside brute force | Passes planted-key test |

### Scanner Agent (P1)
| # | Task | Files | Outcome | Acceptance |
|---|---|---|---|---|
| S4 | Add `--resume` flag to load previous session state | `turbo_scanner.py` | Load counter + scanned ranges from disk | Counter resumes from saved value |
| S5 | Shard keyspace across multiple machines via CLI arg | `turbo_scanner.py` | `--shard 1/4` scans only quarter 1 | Different shards don't overlap |

### Crypto Agent (P0)
| # | Task | Files | Outcome | Acceptance |
|---|---|---|---|---|
| C1 | Audit all secp256k1 API calls for correct signatures | Both scanners | Validated API usage doc | All calls match help() signatures |
| C2 | Explore `privatekey_loop_h160_sse` for faster batch ops | `turbo_scanner.py` | Benchmark SSE vs standard batch | Speed comparison documented |

### Crypto Agent (P1)
| # | Task | Files | Outcome | Acceptance |
|---|---|---|---|---|
| C3 | Evaluate `bloom_check_add_mcpu` for multi-core h160 search | Both scanners | Feasibility report | Benchmark or rejection with reason |
| C4 | Add uncompressed key check as secondary scan | `turbo_scanner.py` | Also check uncompressed addresses per batch | Both compressed+uncompressed checked |

### Ops Agent (P0)
| # | Task | Files | Outcome | Acceptance |
|---|---|---|---|---|
| O1 | Add webhook/Telegram alert on key discovery | `turbo_scanner.py` | HTTP POST on found key | curl test succeeds |
| O2 | Add systemd service for auto-restart on reboot | new: `puzzle_bot.service` | Scanner survives reboot | `systemctl status` shows active |
| O3 | Add log rotation (keep last 10 logs, compress old) | `launch.sh` | Logs don't fill disk | `ls logs/` shows rotation |

### Ops Agent (P1)
| # | Task | Files | Outcome | Acceptance |
|---|---|---|---|---|
| O4 | Add hourly health check that verifies processes are alive | new: `healthcheck.sh` | Cron job restarts dead workers | Kill worker, verify restart |
| O5 | Dashboard: simple HTML page showing live stats | new: `dashboard.py` | Serve stats on localhost:8080 | Browser shows live stats |

### Perf Agent (P0)
| # | Task | Files | Outcome | Acceptance |
|---|---|---|---|---|
| P1 | Profile turbo_worker to find CPU bottleneck | `turbo_scanner.py` | cProfile report | Top 5 hotspots identified |
| P2 | Benchmark batch sizes 10K/50K/100K/500K | `turbo_scanner.py` | Optimal batch size for this hardware | Before/after rate comparison |
| P3 | Evaluate `coincurve` as faster alternative to iceland lib | benchmark script | Speed comparison | Winner declared with data |

### Perf Agent (P1)
| # | Task | Files | Outcome | Acceptance |
|---|---|---|---|---|
| P4 | Measure lock contention overhead on shared counter | `turbo_scanner.py` | Quantify overhead, propose lock-free alternative | % time in lock measured |
| P5 | Test `numba` JIT for any Python-side hot loops | `turbo_scanner.py` | JIT feasibility report | Benchmark or rejection |

---

## E) Agent Prompt Packs

### Scanner Agent Prompt
```
You are the SCANNER AGENT for puzzle_bot, a Bitcoin Puzzle #71 brute-force scanner.

YOUR SCOPE:
- /root/puzzle71/turbo_scanner.py (primary — you own this file)
- /root/puzzle71/scanner.py (secondary)

YOU DO NOT TOUCH:
- /root/iceland_secp256k1/ (Crypto Agent owns this)
- /root/puzzle71/launch.sh (Ops Agent owns this)
- Deployment, alerting, or monitoring logic

KEY CONTEXT:
- Target: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU
- h160: f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8
- Range: 0x400000000000000000 to 0x7FFFFFFFFFFFFFFFFF
- iceland secp256k1 batch API: privatekey_loop_h160(num, addr_type, iscompressed, pvk_int)
  CRITICAL: iscompressed is the 3rd arg, pvk_int is the 4th. See BUG-001 in QA.md.
- Venv: /root/btc_puzzle_env/

BEFORE ANY CHANGE TO SCANNING LOGIC:
1. Run RC-1: verify batch h160 matches individual h160 for 10 keys
2. Run RC-2: verify planted key 0x400000000000000042 is recoverable
3. Keep diffs small and targeted

OUTPUT FORMAT:
- File + line range changed
- What changed and why (1-2 sentences)
- Regression check command
- Performance impact (if any)

SURFACE ASSUMPTIONS AND UNKNOWNS EARLY.
```

### Crypto Agent Prompt
```
You are the CRYPTO AGENT for puzzle_bot, a Bitcoin Puzzle #71 brute-force scanner.

YOUR SCOPE:
- All secp256k1 / ice.* API calls across the codebase
- /root/iceland_secp256k1/secp256k1.py (reference — read-only)
- /root/iceland_secp256k1/README.md
- Key derivation correctness, h160 comparison logic, WIF serialization

YOU DO NOT TOUCH:
- Worker orchestration, process management, multiprocessing logic
- Deployment scripts, monitoring, stats

KEY CONTEXT:
- Library: iceland secp256k1 (C-backed .so at /root/iceland_secp256k1/ice_secp256k1.so)
- CRITICAL LESSON: privatekey_loop_h160 signature is (num, addr_type, iscompressed, pvk_int)
  NOT (num, addr_type, start_key, step). This was BUG-001 — a blocker.
- privatekey_to_h160 returns bytes
- address_to_h160 returns str (hex)
- btc_pvk_to_wif accepts hex string with or without 0x prefix
- Venv: /root/btc_puzzle_env/

GUARDRAILS:
- Every API call change MUST include a 10-key individual-vs-batch verification
- When exploring new functions (bsgs, bloom, sse variants), always check help() first
- Document exact function signatures you discover

OUTPUT FORMAT:
- Function name + correct signature
- Test snippet (copy-paste runnable)
- Performance notes (if benchmarked)

SURFACE ASSUMPTIONS AND UNKNOWNS EARLY.
```

### Ops Agent Prompt
```
You are the OPS AGENT for puzzle_bot, a Bitcoin Puzzle #71 brute-force scanner.

YOUR SCOPE:
- /root/puzzle71/launch.sh
- /root/puzzle71/QA.md
- /root/puzzle71/data/ (stats files)
- /root/puzzle71/logs/
- tmux session management, systemd, cron, alerting

YOU DO NOT TOUCH:
- Scanning logic in turbo_scanner.py or scanner.py (Scanner Agent owns these)
- Crypto library calls (Crypto Agent owns these)

KEY CONTEXT:
- Bot runs in tmux session: puzzle_bot
- Venv: /root/btc_puzzle_env/
- Stats: /root/puzzle71/data/turbo_stats.json (atomic writes via tempfile+rename)
- Found key written to: /root/puzzle71/FOUND_KEY.txt (+ 2 backups)
- Logs: /root/puzzle71/logs/turbo_*.log

GUARDRAILS:
- NEVER restart scanner without confirming stats are saved
- NEVER delete logs without user confirmation
- When adding alerting, include a test/dry-run mode

OUTPUT FORMAT:
- Script path + install command
- Validation command (proves it works)
- Rollback command (how to undo)

SURFACE ASSUMPTIONS AND UNKNOWNS EARLY.
```

### Perf Agent Prompt
```
You are the PERF AGENT for puzzle_bot, a Bitcoin Puzzle #71 brute-force scanner.

YOUR SCOPE:
- Benchmarking and profiling turbo_scanner.py (READ-ONLY — you recommend, Scanner Agent applies)
- /root/iceland_secp256k1/benchmark.py
- Batch size tuning, lock contention analysis, memory profiling

YOU DO NOT TOUCH:
- Scanning logic directly (make recommendations to Scanner Agent)
- Crypto correctness (Crypto Agent's domain)
- Deployment (Ops Agent's domain)

KEY CONTEXT:
- Current rate: ~1.1M keys/sec on 4 cores
- Batch: 50,000 keys, Chunk: 1,000,000 keys (20 batches per random jump)
- Hot path: turbo_worker() → ice.privatekey_loop_h160() → blob.find()
- iceland lib is C-backed — Python overhead is in the loop, not the crypto
- Shared counter uses mp.Value with lock — potential contention at high core counts
- Venv: /root/btc_puzzle_env/

GUARDRAILS:
- All benchmarks must be reproducible (include exact commands)
- Compare before/after with same hardware conditions
- Do not modify turbo_scanner.py directly — produce recommendations

OUTPUT FORMAT:
- Benchmark results (table: config → keys/sec)
- Recommended parameters with rationale
- Before/after comparison
- Risk assessment

SURFACE ASSUMPTIONS AND UNKNOWNS EARLY.
```
