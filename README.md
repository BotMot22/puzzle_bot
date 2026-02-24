# puzzle_bot

24/7 multi-strategy scanner for [Bitcoin Puzzle #71](https://btcpuzzle.info/puzzle/71) — 7.1 BTC bounty.

## Target

| Parameter | Value |
|---|---|
| Address | `1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU` |
| h160 | `f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8` |
| Key Range | `0x400000000000000000` to `0x7FFFFFFFFFFFFFFFFF` (2^70 to 2^71-1) |
| Keyspace | 1,180,591,620,717,411,303,424 keys |
| Reward | 7.1 BTC |

## Architecture

Three concurrent scanners + blockchain monitor:

| Bot | Engine | Speed (this box) | Strategy |
|---|---|---|---|
| `puzzle_bot` | Python + iceland secp256k1 | ~1.1M keys/s | Hybrid random-jump + sequential batch |
| `bitcrack` | BitCrack (OpenCL) | ~0.7M keys/s (CPU) / ~2.5B keys/s (GPU) | Sequential with checkpoint resume |
| `pubkey_monitor` | Python + public APIs | Polls every 60s | Watches for public key on-chain |

If the public key is ever revealed (someone spends from the address), `kangaroo_launcher.py` can solve it in minutes on a GPU.

## Quick Start

```bash
# Setup
python3 -m venv btc_puzzle_env
source btc_puzzle_env/bin/activate
pip install bit bitcoin ecdsa pycryptodome coincurve base58 requests numba numpy

# Clone iceland secp256k1 (fast C-backed crypto)
git clone https://github.com/iceland2k14/secp256k1.git iceland_secp256k1

# Run Python scanner (adjust -w to your CPU count)
python3 turbo_scanner.py -w 4

# Run BitCrack (requires OpenCL or CUDA)
# See gpu_deploy.sh for GPU cloud setup
bash gpu_deploy.sh

# Run public key monitor
python3 pubkey_monitor.py

# Or launch everything in tmux
bash launch.sh
```

## Files

| File | Purpose |
|---|---|
| `turbo_scanner.py` | Primary Python scanner (24/7 bot) |
| `scanner.py` | Multi-strategy scanner (sequential, random, hybrid) |
| `pubkey_monitor.py` | Watches blockchain for public key revelation |
| `kangaroo_launcher.py` | Pollard's Kangaroo solver (if pubkey found) |
| `gpu_deploy.sh` | GPU cloud instance setup (vast.ai, Lambda, RunPod) |
| `gpu_vast_ai.sh` | vast.ai specific launcher |
| `multi_gpu_launch.sh` | Multi-GPU BitCrack orchestrator |
| `launch.sh` | tmux launcher for all bots |
| `start_monitors.sh` | tmux launcher for pubkey monitor |
| `QA.md` | QA report, bug tracker, regression checklist |
| `AGENTS.md` | Agent orchestration architecture |

## GPU Deployment

For serious cracking, you need GPUs. The deployment scripts handle:
- Detecting GPU model and CUDA version
- Building BitCrack from source
- Splitting keyspace across multiple GPUs
- Checkpoint/resume support
- vast.ai instance management

```bash
# On a GPU machine:
bash gpu_deploy.sh

# Multi-GPU:
bash multi_gpu_launch.sh

# vast.ai cloud:
bash gpu_vast_ai.sh --create
```

## The Math

| Setup | Keys/sec | Full Keyspace Time |
|---|---|---|
| 4 CPU cores | ~1.8M | ~20M years |
| 1x RTX 4090 | ~2.5B | ~15,000 years |
| 10x RTX 4090 | ~25B | ~1,500 years |
| 100x GPU cluster | ~250B | ~150 years |
| Kangaroo (with pubkey) | N/A | **Minutes** |

Every key checked has a 1-in-1.18-sextillion chance. It's a lottery at industrial scale.

## License

MIT
