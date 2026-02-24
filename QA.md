# QA Report — polymarket_quant

## How to Run
```bash
cd /root/polymarket_quant
source venv/bin/activate
python3 scalp_bot.py        # Main 5-min scalp bot (tmux: scalp_bot)
python3 expiry_scalp_bot.py # Expiry scanner (tmux: expiry_bot)
python3 redeem_monitor.py   # Redemption monitor (tmux: redeem_monitor)
python3 live_quant.py       # Quant signal runner (standalone)
python3 run_backtest.py     # Backtester
```

## Validation Commands
```bash
# Syntax check all files
python3 -c "import py_compile; py_compile.compile('scalp_bot.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('expiry_scalp_bot.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('live_quant.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('redeem_monitor.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('signals/model.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('signals/features.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('data/fetcher.py', doraise=True)"

# Check bot output
tmux capture-pane -t scalp_bot -p -S -20
tmux capture-pane -t expiry_bot -p -S -20

# Check state
python3 -c "import json; d=json.load(open('data/scalp_state.json')); print(json.dumps(d, indent=2))"
tail -5 data/scalp_trades.csv
```

---

## Bug Tracker

| ID | Title | Severity | File:Line | Root Cause | Fix | Verified | Status |
|----|-------|----------|-----------|------------|-----|----------|--------|
| B1 | conditionId 0x prefix assumption | HIGH | scalp_bot.py:97, expiry_scalp_bot.py:82 | `bytes.fromhex(cid[2:])` assumes `0x` prefix. Without it, strips first 2 hex chars → wrong condition ID | Check for `0x` prefix before stripping | Code review | FIXED (prev) |
| B2 | No timeout on redeem API call | HIGH | scalp_bot.py:83-85 | `requests.get()` without timeout → bot hangs | Added `timeout=15` | Code review | FIXED (prev) |
| B3 | Silent redeem failures | MED | scalp_bot.py:115-116 | Bare `except: pass` hides errors | Log error message on failure | Code review | FIXED (prev) |
| B4 | Feature distribution mismatch | MED | scalp_bot.py:216 | `refresh_features()` fetched 1 day but scaler fit on 2 days | Changed to 2-day lookback | Verified same lookback_days | FIXED (prev) |
| B5 | Silent ML predict failures | LOW | scalp_bot.py:254-255 | Falls back to 0.5 without logging | Added warning logs | Code review | FIXED (prev) |
| B6 | `p['size']` format crash in redeem | LOW | scalp_bot.py:112 | API may return size as string | Cast to `float()` | Code review | FIXED (prev) |
| B7 | Flat price resolution ambiguity | KNOWN | scalp_bot.py:519 | `exit_px == open_px` resolves as DOWN | Matches Polymarket behavior | N/A | ACCEPTED |
| **B8** | **Non-atomic state writes in scalp_bot** | **HIGH** | scalp_bot.py:335-337 | Direct `open('w')` + `json.dump()` — if process killed mid-write, state file is corrupt/empty. Loses bankroll/PnL/trade history. | Replaced with atomic tmpfile+rename (same pattern as expiry_scalp_bot) | `python3 -c "import json; json.load(open('data/scalp_state.json'))"` after restart | **FIXED** |
| **B9** | **Nonce race between scalp_bot and redeem_monitor** | **HIGH** | scalp_bot.py:627, redeem_monitor.py:63-105 | Both processes call on-chain `redeemPositions()` independently → can fetch same nonce → one tx fails with "nonce too low" → wastes gas, delays redemptions | Removed `redeem_positions()` from scalp_bot main loop; redeem_monitor handles all redemptions | Verified only redeem_monitor tmux session does on-chain txs | **FIXED** |
| **B10** | **Banner says "v4" but code is v5** | **LOW** | scalp_bot.py:563 | `print_banner()` says "v4" but `STATE_VERSION=5`, docstring says "v5", ask range is 0.75-0.95 not 0.85-0.95 | Updated banner to "v5", corrected ask range display | Visual check on restart | **FIXED** |
| **B11** | **Dashboard labels say "Last 15s" / "30s+BTC" but actual timings are 45s/60s** | **MED** | scalp_bot.py:379,583 | `S1_LEAD=45` but strat label says "Last15s"; `S2_LEAD=60` but label says "30s" | Updated all labels to match actual timing (45s/60s) | Visual check on dashboard print | **FIXED** |
| **B12** | **check_s1 docstring says "3% edge" but MIN_EDGE is 2%** | **LOW** | scalp_bot.py:438 | Stale docstring from previous version | Updated to "2% edge" | Code review | **FIXED** |
| **B13** | **Non-atomic state writes in live_quant** | **MED** | live_quant.py:260-262 | Same as B8 — direct `open('w')` can corrupt state | Replaced with atomic tmpfile+rename | Same verification as B8 | **FIXED** |
| **B14** | **Bare `except:` catches KeyboardInterrupt/SystemExit** | **LOW** | scalp_bot.py:284, live_quant.py:148 | Bare `except:` in get_ask() and get_binance_price() catches KeyboardInterrupt, making it harder to stop bot | Changed to `except Exception:` | Code review | **FIXED** |
| **B15** | **7 redundant logret_ features add noise to ML model** | **MED** | signals/features.py:73,230 | `logret_1..logret_20` are linearly correlated with `ret_1..ret_20` (log(1+x) ≈ x for small x). Doubles feature dimensionality without adding signal → dilutes regularized model. | Excluded `logret_*` from `get_feature_columns()` — 46→39 features | Verified `len(get_feature_columns(df)) == 39` | **FIXED** |
| **B16** | **QA.md references version=4 but STATE_VERSION is 5** | **LOW** | QA.md:47 | Stale doc | Updated to version=5 | This file | **FIXED** |

---

## Manual Regression Checklist

### After any code change:
- [ ] Syntax check: `python3 -c "import py_compile; py_compile.compile('scalp_bot.py', doraise=True)"`
- [ ] Restart bot: `tmux send-keys -t scalp_bot C-c` then re-run `python3 scalp_bot.py`
- [ ] Verify banner says "v5" and "ML EDGE at $0.75-$0.95"
- [ ] Verify all 4 models train: look for `[MODEL] BTC/ETH/SOL/XRP ready`
- [ ] Verify 39 feature columns (not 46): check for absence of `logret_*` in model training output
- [ ] Verify market discovery: look for `Markets: ['BTC', 'ETH', 'SOL', 'XRP']`
- [ ] Wait 1 window — confirm dashboard prints with no errors
- [ ] Dashboard labels show "S1: ML Edge 45s" and "S2: 60s+BTC Confirm"
- [ ] Check `data/scalp_state.json` is valid JSON with version=5
- [ ] No `[ERROR]` lines in output

### After strategy parameter changes:
- [ ] Bump `STATE_VERSION` to force state reset
- [ ] Verify state reset message: `[STATE] Version mismatch`
- [ ] Confirm bankrolls reset to `STARTING_BANKROLL`

### After redemption changes:
- [ ] Verify only `redeem_monitor` tmux session does on-chain transactions
- [ ] `scalp_bot` output should NOT contain `[REDEEM]` messages
- [ ] Check `tmux capture-pane -t redeem_monitor -p -S -5` for redemption activity
- [ ] Verify wallet USDC balance increases after redeem

### After feature/model changes:
- [ ] `python3 -c "from data.fetcher import fetch_klines; from signals.features import compute_features, get_feature_columns; df=fetch_klines('BTCUSDT',lookback_days=1); df=compute_features(df); fc=get_feature_columns(df); print(f'{len(fc)} features'); assert not any('logret' in c for c in fc)"`
- [ ] Restart scalp_bot to trigger full retrain with new features
- [ ] Verify model trains without errors

### State file integrity (after B8/B13 atomic write fix):
- [ ] While bot is running: `python3 -c "import json; json.load(open('data/scalp_state.json'))"`
- [ ] Kill bot with `kill -9` and verify state file is valid JSON (not empty/corrupt)
- [ ] Verify `os.replace()` path exists: `ls -la data/*.tmp` should be empty (no leftover temps)

### Periodic health checks:
- [ ] `tmux capture-pane -t scalp_bot -p -S -5` — bot is printing poll lines
- [ ] `tmux capture-pane -t expiry_bot -p -S -5` — expiry bot scanning
- [ ] `tmux capture-pane -t redeem_monitor -p -S -5` — monitor active
- [ ] No `[ERROR]` lines in recent output
- [ ] `data/scalp_state.json` updated within last 10 minutes
- [ ] Trade CSV growing: `wc -l data/scalp_trades.csv`

---

## Known Limitations

1. **Resolution uses Binance price, not Polymarket oracle** — Win/loss tracking may differ from actual Polymarket resolution by a few cents
2. **Feature refresh adds latency** — 4 API calls (1 per asset) at each window open; if Binance is slow, may miss early scalp zone polls
3. **Single-threaded** — All HTTP calls are synchronous; a slow API response blocks everything
4. **No order fill confirmation** — FOK either fills or doesn't; we trust the CLOB response but don't verify on-chain
5. **Gas price hardcoded** — `maxPriorityFeePerGas` at 50 gwei; during Polygon congestion, redemptions may fail
6. **Expiry bot traded_tokens grows unbounded** — Currently bounded at 500 but state file can get large with many pending positions (~17KB observed)
7. **No distributed locking** — If multiple instances of the same bot run, they will conflict on state files and nonces

## Common Failure Modes & Debug

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Bot prints `[ERROR]` and sleeps 5s | Uncaught exception in main loop | Check traceback, usually API timeout |
| `[NOFILL]` on every trade | Insufficient CLOB liquidity at our price | Normal for SOL/XRP, check BTC/ETH fills |
| No trades for many windows | ML not finding 2% edge at $0.75-$0.95 | Expected — strategy is selective. Consider lowering MIN_EDGE or widening ask range |
| `[MODEL] training failed` | Binance API down or rate limited | Will retry at next retrain cycle (50 windows) |
| State file corrupt/empty | Power loss during write (pre-B8 fix) | Delete `data/scalp_state.json` — bot resets to defaults |
| `nonce too low` errors in redeem_monitor | Another process sent a tx first | Normal if both scalp_bot and redeem_monitor were running pre-B9 fix. After fix, only redeem_monitor does on-chain txs |
| scalp_bot shows `[REDEEM]` messages | Old code before B9 fix | Restart scalp_bot to pick up fix |
| Feature count != 39 | Old features.py | Restart bot to retrain model with correct features |
