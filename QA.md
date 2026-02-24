# QA Report — polymarket_quant

## How to Run
```bash
cd /root/polymarket_quant
source venv/bin/activate
python3 scalp_bot.py        # Main 5-min scalp bot (tmux: scalp_bot)
python3 expiry_scalp_bot.py # Expiry scanner (separate)
python3 live_quant.py       # Quant signal runner (standalone)
python3 run_backtest.py     # Backtester
```

## Validation Commands
```bash
python3 -c "import py_compile; py_compile.compile('scalp_bot.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('expiry_scalp_bot.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('live_quant.py', doraise=True)"
tmux capture-pane -t scalp_bot -p -S -20   # Check bot output
cat data/scalp_state.json                   # Check state
tail -5 data/scalp_trades.csv               # Recent trades
```

---

## Bug Tracker

| ID | Title | Severity | File:Line | Root Cause | Fix | Status |
|----|-------|----------|-----------|------------|-----|--------|
| B1 | conditionId 0x prefix assumption | HIGH | scalp_bot.py:97, expiry_scalp_bot.py:84 | `bytes.fromhex(cid[2:])` assumes `0x` prefix. Without it, strips first 2 hex chars → wrong condition ID → silent redeem failure | Check for `0x` prefix before stripping | FIXED |
| B2 | No timeout on redeem API call | HIGH | scalp_bot.py:83-85 | `requests.get()` without timeout → bot hangs if data-api is down | Added `timeout=15` | FIXED |
| B3 | Silent redeem failures | MED | scalp_bot.py:111-114 | Bare `except: pass` hides all errors including wrong condition IDs, gas issues, nonce conflicts | Log error message on failure | FIXED |
| B4 | Feature distribution mismatch | MED | scalp_bot.py:216 | `refresh_features()` fetches 1 day but model scaler was fit on 2 days → feature distribution shift → miscalibrated probabilities | Changed to 2-day lookback (matches init/retrain) | FIXED |
| B5 | Silent ML predict failures | LOW | scalp_bot.py:244-257 | `except Exception: ml_prob = 0.5` silently falls back without logging → hard to debug why model returns no edge | Added warning logs on prediction failure | FIXED |
| B6 | `p['size']` format crash in redeem | LOW | scalp_bot.py:109 | API may return size as string → `:.2f` on string crashes (caught by outer try) | Cast to `float()` explicitly | FIXED |
| B7 | Flat price resolution ambiguity | KNOWN | scalp_bot.py:511 | `exit_px == open_px` resolves as DOWN. Matches Polymarket behavior but could mismatch if Binance price ≠ Polymarket oracle by a few cents | Not fixable without querying actual Polymarket resolution oracle | ACCEPTED |

---

## Manual Regression Checklist

### After any code change:
- [ ] `python3 -c "import py_compile; py_compile.compile('scalp_bot.py', doraise=True)"`
- [ ] Restart bot: `tmux send-keys -t scalp_bot C-c` then `python3 scalp_bot.py`
- [ ] Verify all 4 models train: look for `[MODEL] BTC/ETH/SOL/XRP ready`
- [ ] Verify market discovery: look for `Markets: ['BTC', 'ETH', 'SOL', 'XRP']`
- [ ] Wait 1 window — confirm dashboard prints with no errors
- [ ] Check `data/scalp_state.json` is valid JSON with version=4

### After strategy parameter changes:
- [ ] Bump `STATE_VERSION` to force state reset
- [ ] Verify state reset message: `[STATE] Version mismatch`
- [ ] Confirm bankrolls reset to `STARTING_BANKROLL`

### After redemption changes:
- [ ] Wait for a resolved trade
- [ ] Check for `[REDEEM]` messages in output (not `[REDEEM FAIL]`)
- [ ] Verify wallet USDC balance increases after redeem

### Periodic health checks:
- [ ] `tmux capture-pane -t scalp_bot -p -S -5` — bot is printing poll lines
- [ ] No `[ERROR]` lines in recent output
- [ ] `data/scalp_state.json` updated within last 10 minutes
- [ ] `cat data/scalp_trades.csv | wc -l` — trade count growing if active

---

## Known Limitations

1. **Resolution uses Binance price, not Polymarket oracle** — Our win/loss tracking may differ from actual Polymarket resolution by a few cents
2. **Feature refresh adds latency** — 4 API calls (1 per asset) at each window open; if Binance is slow, we may miss early scalp zone polls
3. **Single-threaded** — All HTTP calls are synchronous; a slow API response blocks everything
4. **No order fill confirmation** — FOK either fills or doesn't; we trust the CLOB response but don't verify on-chain
5. **Gas price hardcoded** — `maxPriorityFeePerGas` at 50 gwei; during Polygon congestion, redemptions may fail

## Common Failure Modes & Debug

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Bot prints `[ERROR]` and sleeps 5s | Uncaught exception in main loop | Check traceback, usually API timeout |
| `[NOFILL]` on every trade | Insufficient CLOB liquidity at our price | Normal for SOL/XRP, check BTC/ETH fills |
| No trades for many windows | ML not finding 3% edge at $0.85-$0.95 | Expected — strategy is selective. Consider lowering MIN_EDGE or widening ask range |
| `[MODEL] training failed` | Binance API down or rate limited | Will retry at next retrain cycle (50 windows) |
| State file corrupt | Power loss during write | Delete `data/scalp_state.json` — bot resets to defaults |
