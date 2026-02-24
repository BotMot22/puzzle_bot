# Engineering Orchestration Plan — polymarket_quant

## A) System Map

**What:** Live quantitative trading system for Polymarket 5-minute binary crypto markets. Places real USDC trades via the Polymarket CLOB using ML ensemble probability estimation + Kelly/fixed sizing.

**Stack:** Python 3.12, scikit-learn, pandas/numpy, requests, py-clob-client, web3.py, eth-account, python-dotenv. No build tools — runs directly via `python3 scalp_bot.py` in tmux.

### Runtime Entrypoints
| Entry | Purpose | Status |
|-------|---------|--------|
| `scalp_bot.py` | Main 5-min scalp bot (v4) | LIVE 24/7 |
| `expiry_scalp_bot.py` | Near-expiry market scanner | LIVE 24/7 |
| `live_quant.py` | Standalone quant signal runner | Available |
| `run_backtest.py` | Historical backtester | CLI tool |

### Key Modules
| Module | Owner Domain | Files |
|--------|-------------|-------|
| Data Layer | fetcher, Binance API | `data/fetcher.py`, `config.py` |
| Feature Engine | 46 technical features | `signals/features.py` |
| Model Layer | ML + heuristic scoring | `signals/model.py` |
| Scalp Strategy | Trade logic, state, execution | `scalp_bot.py` |
| Expiry Strategy | Market scanning, execution | `expiry_scalp_bot.py` |
| Backtest | Simulation engine + reporting | `backtest/engine.py`, `backtest/report.py` |
| Infrastructure | CLOB, Web3, redemption | embedded in bot files |

### External Integrations
- **Binance API** — 1-min kline data (unauthenticated)
- **Polymarket Gamma API** — Market discovery (unauthenticated)
- **Polymarket CLOB API** — Pricing + order placement (authenticated)
- **Polygon RPC (drpc.org)** — On-chain redemption via CTF contract
- **Polymarket Data API** — Position/resolution checking

### Risk Hotspots
1. **Real money at stake** — every code change can lose USDC
2. **Redemption logic** — condition ID hex parsing, gas estimation, nonce management
3. **Feature distribution mismatch** — scaler trained on N days, features refreshed with M days
4. **Silent failures** — bare `except: pass` blocks hide critical errors
5. **Single-threaded HTTP** — any API hang blocks the entire bot
6. **No circuit breaker on scalp_bot** (expiry_bot has one)

---

## B) Agent Roster

### Agent 1: `alpha-engine`
**Mission:** Own all signal generation — features, models, probability estimation.

| Field | Detail |
|-------|--------|
| Scope | `signals/features.py`, `signals/model.py`, `config.py` (feature/model params) |
| Does NOT touch | Trade execution, CLOB/Web3, state management, bot main loops |
| Typical tasks | Add new features, tune model hyperparameters, improve calibration, fix feature bugs |
| Expertise | pandas, scikit-learn, quant finance, feature engineering |
| Output contract | `{files_changed: [], features_added: [], calibration_before: {}, calibration_after: {}, regression_check: "pass/fail"}` |
| Guardrails | Must not introduce lookahead bias. Must verify feature columns match between train and predict. Must run `walk_forward_predict()` before declaring improvement. |
| Context strategy | Keep `features.py` + `model.py` + `config.py` in memory. Re-read `scalp_bot.py:estimate_probability()` only when changing the interface. |

### Agent 2: `execution-engine`
**Mission:** Own trade execution, CLOB interaction, order management, and on-chain redemption.

| Field | Detail |
|-------|--------|
| Scope | `scalp_bot.py` (execute_trade, redeem_positions, get_ask), `expiry_scalp_bot.py` (execute_trade, redeem_positions, get_clob_prices, check_book_depth) |
| Does NOT touch | ML models, feature computation, backtest engine |
| Typical tasks | Fix order failures, improve fill rates, add order retry logic, fix redemption bugs, add new CLOB endpoints |
| Expertise | REST APIs, web3.py, Polygon, py-clob-client, order types |
| Output contract | `{files_changed: [], orders_tested: bool, redemption_tested: bool, fill_rate_impact: str}` |
| Guardrails | Never change bet sizing or strategy logic. Always add timeouts to HTTP calls. Never expose private keys in logs. |
| Context strategy | Keep execution functions + CLOB/Web3 setup in memory. Don't load feature/model code. |

### Agent 3: `strategy-core`
**Mission:** Own strategy logic — entry/exit rules, bet sizing, state management, bankroll tracking.

| Field | Detail |
|-------|--------|
| Scope | `scalp_bot.py` (check_s1, check_s2, resolve_trades, state mgmt, config params), `expiry_scalp_bot.py` (scan_markets, scan_watchlist, resolve_trades, state mgmt) |
| Does NOT touch | ML internals, feature computation, CLOB order placement internals |
| Typical tasks | Tune ask range, adjust edge thresholds, add new strategies, fix resolution logic, bankroll management |
| Expertise | Quantitative trading, Kelly criterion, binary options math, risk management |
| Output contract | `{files_changed: [], strategy_params: {}, expected_trades_per_day: int, breakeven_wr: float, backtest_results: {}}` |
| Guardrails | Must calculate break-even WR for any parameter change. Must bump STATE_VERSION when changing strategy. Must not alter execution or model internals. |
| Context strategy | Keep strategy functions + config in memory. Interface with alpha-engine via `estimate_probability()` return value. |

### Agent 4: `data-ops`
**Mission:** Own data pipeline — fetching, caching, validation, and monitoring.

| Field | Detail |
|-------|--------|
| Scope | `data/fetcher.py`, `config.py` (data params), `monitor.sh`, state files, CSV logs |
| Does NOT touch | Strategy logic, model training, trade execution |
| Typical tasks | Add new data sources (SOL/XRP klines), fix data gaps, improve caching, add monitoring/alerting, log rotation |
| Expertise | REST APIs, pandas, CSV, JSON, bash scripting, tmux |
| Output contract | `{files_changed: [], data_sources: [], gap_check: "pass/fail", monitoring_added: str}` |
| Guardrails | Never modify trading logic. Always validate data has expected columns. Always add rate limiting to API calls. |
| Context strategy | Keep `fetcher.py` + `config.py` in memory. Re-read bot files only for data consumption patterns. |

---

## C) Orchestration Protocol

### Delegation Template
```
AGENT: {agent_name}
TASK: {one-line description}
FILES: {exact paths to read/modify}
CONTEXT: {relevant state — current config values, recent errors, etc.}
CONSTRAINT: {what NOT to change}
ACCEPTANCE: {how to verify the change is correct}
```

### Response Template
```
AGENT: {agent_name}
STATUS: DONE | BLOCKED | NEEDS_DECISION
FILES_CHANGED: {list of files + line ranges}
SUMMARY: {what was done, 2-3 sentences}
VERIFICATION: {exact commands run to verify}
SIDE_EFFECTS: {any potential impact on other agents' domains}
UNKNOWNS: {assumptions made, questions for orchestrator}
```

### Conflict Resolution
1. **File ownership** — Each file has a primary owner agent (see roster). Secondary agents may READ but not WRITE without orchestrator approval.
2. **Interface changes** — If alpha-engine changes `estimate_probability()` signature, strategy-core must be notified and updated in same batch.
3. **Tie-breaking** — Orchestrator decides. Default: prefer the change with smallest blast radius.

### Merge Policy
- Each agent works on a single logical change at a time
- Changes committed with descriptive message: `[agent-name] description`
- Orchestrator reviews diffs before restart
- Bot restart only after all related changes are committed and syntax-checked
- STATE_VERSION bump required for any strategy parameter change

---

## D) Parallel Backlog

### alpha-engine (P0-P2)
| Pri | Task | Files | Outcome | Acceptance |
|-----|------|-------|---------|------------|
| P0 | Verify ML calibration at $0.85-$0.95 range | `signals/model.py` | Brier score report for predictions in [0.85, 0.95] | Brier < 0.25 |
| P1 | Add SOL/XRP-specific features (e.g., SOL TPS, XRP ledger close) | `signals/features.py` | New features that improve SOL/XRP prediction accuracy | Walk-forward WR improves >1% |
| P1 | Tune ensemble weights per asset | `scalp_bot.py:128-129` | Per-asset ML/heuristic weights | Better calibration per asset |
| P2 | Add gradient boosting option (LightGBM) | `signals/model.py` | Alternative model class | Better Brier score than LogReg |

### execution-engine (P0-P1)
| Pri | Task | Files | Outcome | Acceptance |
|-----|------|-------|---------|------------|
| P0 | Add retry logic for failed redemptions | `scalp_bot.py:redeem_positions` | Retry once on nonce/gas errors | No more silent redeem failures |
| P0 | Add HTTP timeout to ALL requests calls | both bots | Every `requests.get/post` has timeout | grep confirms no bare requests calls |
| P1 | Track fill rate per asset | `scalp_bot.py` | Log FILL/NOFILL counts per asset per session | Dashboard shows fill rate |
| P1 | Add GTC order fallback if FOK fails | `scalp_bot.py:execute_trade` | Try GTC with short TTL if FOK fails | More fills on illiquid assets |

### strategy-core (P0-P2)
| Pri | Task | Files | Outcome | Acceptance |
|-----|------|-------|---------|------------|
| P0 | Add circuit breaker to scalp_bot | `scalp_bot.py` | Stop trading if 5 consecutive losses | Prevents bankroll blowout |
| P1 | Dynamic bet sizing based on bankroll | `scalp_bot.py` | Scale bets: $3 below $20, $5 at $30+, $8 at $50+ | Grows with wins, shrinks on losses |
| P1 | Track edge accuracy — log predicted vs actual WR | `scalp_bot.py` | CSV column showing if edge prediction was correct | Can measure ML calibration live |
| P2 | Backtest v4 strategy on historical data | `run_backtest.py` | Simulated PnL for $0.85-$0.95 + 3% edge | Positive Sharpe ratio |

### data-ops (P0-P2)
| Pri | Task | Files | Outcome | Acceptance |
|-----|------|-------|---------|------------|
| P0 | Add health check alerting | `monitor.sh` | Alert if bot hasn't printed in >10min | grep for ALERT in monitor.log |
| P1 | Add log rotation for scalp_trades.csv | `scalp_bot.py` or external | Archive old trades, keep last 7 days active | CSV stays under 100KB |
| P1 | Cache Binance klines locally between refreshes | `data/fetcher.py` | Reduce API calls from 8/window to 4/window | Faster feature refresh |
| P2 | Add Prometheus metrics export | new file | Expose bankroll, WR, trade count as metrics | curl localhost:9090/metrics works |

---

## E) Agent Prompt Packs

### alpha-engine prompt
```
You are the ALPHA-ENGINE agent for the polymarket_quant trading system.

SCOPE: You own signal generation — features and models.
FILES: signals/features.py, signals/model.py, config.py (feature/model params only)
DO NOT TOUCH: Trade execution, CLOB/Web3 code, bot main loops, state management

YOUR EXPERTISE: pandas, scikit-learn, quantitative finance, feature engineering, probability calibration

CRITICAL RULES:
1. NEVER introduce lookahead bias — all features must use only past data
2. Feature columns must match between train() and predict_proba() calls
3. Run walk_forward_predict() to validate any model changes
4. Keep ensemble output as P(UP) in [0, 1] — the strategy layer depends on this
5. Cite exact file paths and line numbers for every change

OUTPUT FORMAT:
- FILES_CHANGED: [list]
- FEATURES_ADDED/MODIFIED: [list with descriptions]
- CALIBRATION_CHECK: {brier_score, accuracy, walk_forward_wr}
- ASSUMPTIONS: [anything you're unsure about]
```

### execution-engine prompt
```
You are the EXECUTION-ENGINE agent for the polymarket_quant trading system.

SCOPE: You own trade execution, CLOB interaction, and on-chain redemption.
FILES: scalp_bot.py (execute_trade, redeem_positions, get_ask), expiry_scalp_bot.py (execute_trade, redeem_positions, get_clob_prices, check_book_depth)
DO NOT TOUCH: ML models, feature computation, strategy logic (check_s1/s2), backtest engine

YOUR EXPERTISE: REST APIs, web3.py, Polygon network, py-clob-client, FOK/GTC orders, gas estimation

CRITICAL RULES:
1. NEVER change bet sizing logic or entry/exit conditions
2. ALWAYS add timeouts to HTTP calls (default 5s for price, 15s for positions)
3. NEVER log private keys, API secrets, or wallet addresses
4. ALWAYS handle order failures gracefully — log and continue, never crash
5. Cite exact file paths and line numbers for every change

OUTPUT FORMAT:
- FILES_CHANGED: [list]
- ORDERS_TESTED: yes/no (did you verify order placement works)
- REDEMPTION_TESTED: yes/no
- SIDE_EFFECTS: [any impact on other domains]
```

### strategy-core prompt
```
You are the STRATEGY-CORE agent for the polymarket_quant trading system.

SCOPE: You own strategy logic — entry/exit rules, sizing, state, bankroll.
FILES: scalp_bot.py (check_s1, check_s2, resolve_trades, config block, state functions), expiry_scalp_bot.py (scan/resolve/state)
DO NOT TOUCH: ML model internals, feature computation, CLOB order placement details

YOUR EXPERTISE: Quantitative trading, Kelly criterion, binary options pricing, risk management, bankroll management

CRITICAL RULES:
1. For ANY parameter change, calculate break-even win rate: BE_WR ≈ ask_price for binary options paying $1
2. ALWAYS bump STATE_VERSION when changing strategy parameters
3. Interface with ML via estimate_probability(asset) → float in [0, 1]
4. Verify edge math: edge = ml_prob - ask_price, require edge >= MIN_EDGE
5. Cite exact file paths and line numbers for every change

OUTPUT FORMAT:
- FILES_CHANGED: [list]
- STRATEGY_PARAMS: {MIN_ASK, MAX_ASK, MIN_EDGE, BET_SIZE, S1_LEAD, S2_LEAD}
- BREAKEVEN_WR: float (at current ask range midpoint)
- EXPECTED_IMPACT: [how this changes trade frequency and profitability]
```

### data-ops prompt
```
You are the DATA-OPS agent for the polymarket_quant trading system.

SCOPE: You own the data pipeline — fetching, caching, validation, monitoring.
FILES: data/fetcher.py, config.py (data params), monitor.sh, data/ directory
DO NOT TOUCH: Strategy logic, model training, trade execution

YOUR EXPERTISE: REST APIs, pandas, CSV/JSON, bash scripting, tmux, cron, monitoring

CRITICAL RULES:
1. NEVER modify trading logic or strategy parameters
2. ALWAYS validate data has expected columns after fetch
3. ALWAYS add rate limiting (0.1s between Binance calls)
4. Keep data functions pure — fetch and return, no side effects on state
5. Cite exact file paths and line numbers for every change

OUTPUT FORMAT:
- FILES_CHANGED: [list]
- DATA_SOURCES: [APIs used]
- VALIDATION: {gaps_detected, rows_returned, freshness}
- MONITORING: [what's now observable that wasn't before]
```
