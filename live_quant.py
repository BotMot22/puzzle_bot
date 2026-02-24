#!/usr/bin/env python3
"""
QUANT-GRADE LIVE SIGNAL RUNNER
================================
What a top quant fund actually does, applied to Polymarket 5-min crypto.

THE PIPELINE (every 5 minutes):
  1. DISCOVER  — Find the current Polymarket 5-min market (slug + token IDs)
  2. PRICE     — Get real bid/ask/mid from Polymarket CLOB orderbook
  3. SIGNAL    — Pull Binance data, compute 46 features, run ML model → P(UP)
  4. EDGE      — Compare our P(UP) against Polymarket's actual price
  5. SIZE      — Kelly criterion position sizing based on real edge
  6. RESOLVE   — When the 5-min window closes, check if we were right
  7. LOG       — Every decision logged to CSV with full audit trail

KEY DIFFERENCES FROM AMATEUR:
  - Real Polymarket prices, not simulated
  - Edge = our_prob - actual_market_bid (what you'd actually pay)
  - Spread-aware: we check bid/ask, not just midpoint
  - Multiple signal sources: Binance spot + taker flow + volatility regime
  - Walk-forward model: retrained on rolling window, no lookahead
"""

import time
import csv
import os
import sys
import json
import traceback
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (PREDICTION_WINDOW, KELLY_FRACTION, MAX_POSITION_PCT,
                    MIN_EDGE, BANKROLL)
from data.fetcher import fetch_klines
from signals.features import compute_features, get_feature_columns
from signals.model import QuantModel, HeuristicScorer

import warnings
warnings.filterwarnings("ignore")

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
LOG_FILE = "data/trade_log.csv"
STATE_FILE = "data/live_state.json"
ASSETS = ["BTC", "ETH", "SOL", "XRP"]
BINANCE_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

LOG_FIELDS = [
    "id", "timestamp", "asset", "window_ts",
    "poly_slug", "direction",
    # Our model output
    "prob_up_logistic", "prob_up_heuristic", "prob_up_ensemble",
    # Real Polymarket prices
    "poly_up_bid", "poly_up_ask", "poly_up_mid",
    "poly_down_bid", "poly_down_ask", "poly_down_mid",
    "poly_spread",
    # Edge calculation
    "edge", "edge_vs_mid",
    # Position sizing
    "kelly_fraction", "bet_size_usd",
    # Context
    "binance_price", "rsi", "macd_hist", "bb_zscore",
    "net_taker_flow", "vol_ratio", "ret_5",
    # Action
    "action",
    # Resolution (filled later)
    "resolved", "exit_price", "fwd_return_bps", "outcome", "pnl",
    "bankroll_after",
]


# ═══════════════════════════════════════════════════════════════
# POLYMARKET DATA LAYER
# ═══════════════════════════════════════════════════════════════

def get_current_window_ts() -> int:
    """Get the current 5-minute aligned Unix timestamp."""
    return int(time.time() // 300) * 300


def get_next_window_ts() -> int:
    """Get the next 5-minute window start."""
    return get_current_window_ts() + 300


def discover_market(asset: str, window_ts: int) -> dict:
    """
    STEP 1: Find the Polymarket market for this asset and time window.
    Returns token IDs and condition ID.
    """
    slug = f"{asset.lower()}-updown-5m-{window_ts}"
    try:
        resp = requests.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
        data = resp.json()
        if not data:
            return None
        m = data[0]
        tokens = json.loads(m["clobTokenIds"])
        outcomes = json.loads(m["outcomes"])
        return {
            "slug": slug,
            "condition_id": m["conditionId"],
            "up_token": tokens[0] if outcomes[0] == "Up" else tokens[1],
            "down_token": tokens[1] if outcomes[0] == "Up" else tokens[0],
            "window_ts": window_ts,
        }
    except Exception as e:
        print(f"  [WARN] Market discovery failed for {slug}: {e}")
        return None


def get_polymarket_prices(token_id: str) -> dict:
    """
    STEP 2: Get real bid/ask/mid/spread from Polymarket CLOB.
    This is what you'd ACTUALLY pay to buy shares.
    """
    try:
        mid_r = requests.get(f"{CLOB_API}/midpoint?token_id={token_id}", timeout=5)
        bid_r = requests.get(f"{CLOB_API}/price?token_id={token_id}&side=BUY", timeout=5)
        ask_r = requests.get(f"{CLOB_API}/price?token_id={token_id}&side=SELL", timeout=5)
        spread_r = requests.get(f"{CLOB_API}/spread?token_id={token_id}", timeout=5)

        mid = float(mid_r.json().get("mid", 0))
        bid = float(bid_r.json().get("price", 0))
        ask = float(ask_r.json().get("price", 0))
        spread = float(spread_r.json().get("spread", 0))

        return {"mid": mid, "bid": bid, "ask": ask, "spread": spread}
    except Exception as e:
        print(f"  [WARN] Price fetch failed: {e}")
        return {"mid": 0.5, "bid": 0.49, "ask": 0.51, "spread": 0.02}


def get_binance_price(symbol: str) -> float:
    """Get current spot price from Binance."""
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}",
                         timeout=5)
        return float(r.json()["price"])
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════
# SIGNAL ENGINE
# ═══════════════════════════════════════════════════════════════

def fetch_features(binance_symbol: str) -> tuple:
    """
    STEP 3: Pull latest Binance data and compute all features.
    Returns (feature_row, feature_cols, full_df).
    """
    df = fetch_klines(binance_symbol, lookback_days=8)
    df = compute_features(df)
    feature_cols = get_feature_columns(df)

    valid = df.dropna(subset=feature_cols)
    if len(valid) < 100:
        return None, feature_cols, df

    return valid, feature_cols, df


def compute_edge(our_prob: float, direction: str, up_prices: dict,
                 down_prices: dict) -> tuple:
    """
    STEP 4: Calculate real edge against Polymarket's actual market.

    CRITICAL QUANT CONCEPT:
      If we think P(UP) = 0.55 and Polymarket's ask for UP = 0.50,
      then our edge = 0.55 - 0.50 = 0.05 (5 cents per dollar risked).

      But we buy at the ASK (not midpoint) — the ask is what you pay.
      A real quant always calculates edge vs execution price.
    """
    if direction == "UP":
        # We'd buy UP shares at the ask price
        exec_price = up_prices["ask"]
        our_directional_prob = our_prob
    else:
        # We'd buy DOWN shares at the ask price
        exec_price = down_prices["ask"]
        our_directional_prob = 1 - our_prob

    if exec_price <= 0 or exec_price >= 1:
        return 0.0, exec_price

    edge = our_directional_prob - exec_price
    return edge, exec_price


def kelly_size(our_prob: float, exec_price: float, bankroll: float) -> float:
    """
    STEP 5: Kelly criterion for binary payoff.

    For buying at price p with payoff $1:
      Win: +$(1-p)  with probability our_prob
      Lose: -$p     with probability (1-our_prob)

    Kelly: f* = (p_win * b - p_lose) / b
           where b = (1-exec_price)/exec_price (decimal odds)
    """
    if our_prob <= exec_price or exec_price <= 0 or exec_price >= 1:
        return 0.0

    edge = our_prob - exec_price
    if edge < MIN_EDGE:
        return 0.0

    b = (1 - exec_price) / exec_price
    q = 1 - our_prob
    kelly_f = (our_prob * b - q) / b
    kelly_f *= KELLY_FRACTION  # fractional Kelly
    kelly_f = max(kelly_f, 0)

    bet = min(kelly_f * bankroll, MAX_POSITION_PCT * bankroll)
    return bet if bet >= 0.50 else 0.0  # minimum $0.50 bet


# ═══════════════════════════════════════════════════════════════
# STATE & LOGGING
# ═══════════════════════════════════════════════════════════════

def init_log():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writeheader()


def log_trade(trade: dict):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writerow({k: trade.get(k, "") for k in LOG_FIELDS})


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "signal_count": 0,
        "bankroll": {a: BANKROLL for a in ASSETS},
        "pnl": {a: 0.0 for a in ASSETS},
        "wins": {a: 0 for a in ASSETS},
        "losses": {a: 0 for a in ASSETS},
        "skips": {a: 0 for a in ASSETS},
        "pending": [],
    }


def save_state(state):
    """Atomic write: write to tmp file, then rename (POSIX atomic)."""
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(STATE_FILE) or ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ═══════════════════════════════════════════════════════════════
# RESOLUTION
# ═══════════════════════════════════════════════════════════════

def resolve_trades(state: dict):
    """
    STEP 6: Check if pending trades have resolved.
    A 5-min window trade resolves once the window closes.
    """
    still_pending = []
    now = time.time()

    for t in state["pending"]:
        window_end = t["window_ts"] + 300  # window_ts + 5 minutes
        if now < window_end + 15:  # wait 15s buffer for price to settle
            still_pending.append(t)
            continue

        asset = t["asset"]
        binance_sym = BINANCE_MAP[asset]
        exit_price = get_binance_price(binance_sym)

        if exit_price <= 0:
            still_pending.append(t)
            continue

        entry_price = t["binance_price"]
        fwd_ret = (exit_price - entry_price) / entry_price
        fwd_ret_bps = fwd_ret * 10000

        if t["direction"] == "UP":
            correct = fwd_ret > 0
        else:
            correct = fwd_ret <= 0

        bet_size = t["bet_size_usd"]
        exec_price = t["poly_up_ask"] if t["direction"] == "UP" else t["poly_down_ask"]

        if correct:
            pnl = (bet_size / exec_price) * (1 - exec_price)
        else:
            pnl = -bet_size

        state["bankroll"][asset] += pnl
        state["pnl"][asset] += pnl
        if correct:
            state["wins"][asset] += 1
        else:
            state["losses"][asset] += 1

        outcome = "WIN" if correct else "LOSS"
        total = state["wins"][asset] + state["losses"][asset]
        wr = state["wins"][asset] / max(total, 1)

        # Log resolution
        resolved = {**t,
                    "resolved": True,
                    "exit_price": exit_price,
                    "fwd_return_bps": round(fwd_ret_bps, 2),
                    "outcome": outcome,
                    "pnl": round(pnl, 2),
                    "bankroll_after": round(state["bankroll"][asset], 2)}
        log_trade(resolved)

        mark = ">>>" if correct else "XXX"
        print(f"\n  {mark} RESOLVED: {asset} {t['direction']}")
        print(f"      Entry: ${entry_price:,.2f} -> Exit: ${exit_price:,.2f} "
              f"({fwd_ret_bps:+.1f} bps)")
        print(f"      {outcome}: ${pnl:+.2f} | Bankroll: ${state['bankroll'][asset]:,.2f} "
              f"| {state['wins'][asset]}W-{state['losses'][asset]}L ({wr:.1%})")

    state["pending"] = still_pending


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run():
    init_log()
    state = load_state()

    print("=" * 80)
    print("  POLYMARKET QUANT ENGINE — LIVE")
    print("  Real prices. Real edge. Real risk management.")
    print("=" * 80)
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Assets: {', '.join(ASSETS)}")
    print(f"  Bankroll: ${BANKROLL:,.2f} / asset  |  "
          f"Min Edge: {MIN_EDGE:.0%}  |  Kelly: {KELLY_FRACTION:.0%}")
    print(f"  Log: {LOG_FILE}")
    print("=" * 80)

    # ── Train initial models ──
    models = {}
    heuristic = HeuristicScorer()

    for asset in ASSETS:
        sym = BINANCE_MAP[asset]
        print(f"\n[INIT] Training model for {asset} ({sym})...")
        df = fetch_klines(sym, lookback_days=8)
        df = compute_features(df)
        fc = get_feature_columns(df)
        clean = df.dropna(subset=fc + ["target"])
        if len(clean) < 500:
            print(f"[WARN] Only {len(clean)} clean bars for {asset}")
            continue
        model = QuantModel()
        model.train(clean[fc], clean["target"])
        models[asset] = {"model": model, "feature_cols": fc, "trained_at": 0}
        print(f"[INIT] {asset} model ready ({len(clean)} bars)")
        for feat, imp in model.get_top_features(5).items():
            print(f"  {feat:25s} {imp:.4f}")

    print(f"\n{'━' * 80}")
    print(f"  LIVE. Waiting for next 5-minute window boundary...")
    print(f"{'━' * 80}\n")

    # ── Align to next 5-minute boundary ──
    now_ts = time.time()
    next_boundary = (int(now_ts // 300) + 1) * 300
    wait = next_boundary - now_ts
    if wait > 5:
        print(f"  Sleeping {wait:.0f}s until next 5-min boundary "
              f"({datetime.fromtimestamp(next_boundary, tz=timezone.utc).strftime('%H:%M:%S')})...\n")
        time.sleep(wait)

    # ── Main signal loop ──
    while True:
        try:
            state["signal_count"] += 1
            sig_id = state["signal_count"]
            now = datetime.now(timezone.utc)
            window_ts = get_current_window_ts()

            print(f"\n{'━' * 80}")
            print(f"  SIGNAL #{sig_id} @ {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print(f"  Window: {window_ts}")
            print(f"{'━' * 80}")

            # Resolve any completed trades
            resolve_trades(state)

            for asset in ASSETS:
                if asset not in models:
                    continue

                sym = BINANCE_MAP[asset]

                # ── 1. Discover Polymarket market ──
                market = discover_market(asset, window_ts)
                if not market:
                    # Try next window (current might have expired)
                    market = discover_market(asset, get_next_window_ts())
                if not market:
                    print(f"  [{asset}] No active market found, skipping")
                    state["skips"][asset] += 1
                    continue

                # ── 2. Get REAL Polymarket prices ──
                up_prices = get_polymarket_prices(market["up_token"])
                down_prices = get_polymarket_prices(market["down_token"])

                # ── 3. Generate our probability estimate ──
                df = fetch_klines(sym, lookback_days=2)
                df = compute_features(df)
                fc = models[asset]["feature_cols"]
                valid = df.dropna(subset=fc)
                if valid.empty:
                    print(f"  [{asset}] No valid feature data")
                    continue

                latest = valid.iloc[-1:]
                X = latest[fc]

                # Logistic regression probability
                lg_prob = float(models[asset]["model"].predict_proba(X)[0, 1])
                # Heuristic probability
                h_prob = float(heuristic.predict_proba(latest[fc])[0, 1])
                # Ensemble: weight toward ML model
                ens_prob = 0.7 * lg_prob + 0.3 * h_prob

                binance_price = get_binance_price(sym)

                # ── 4. Determine direction and edge ──
                if ens_prob >= 0.5:
                    direction = "UP"
                    our_prob = ens_prob
                else:
                    direction = "DOWN"
                    our_prob = 1 - ens_prob

                edge, exec_price = compute_edge(ens_prob, direction,
                                                up_prices, down_prices)
                edge_vs_mid = our_prob - (up_prices["mid"] if direction == "UP"
                                          else down_prices["mid"])

                # ── 5. Position sizing ──
                bankroll = state["bankroll"][asset]
                bet_size = kelly_size(our_prob, exec_price, bankroll)

                # Extract key signal values for logging
                row = latest.iloc[0]
                signals = {
                    "rsi": row.get("rsi", np.nan),
                    "macd_hist": row.get("macd_hist", np.nan),
                    "bb_zscore": row.get("bb_zscore", np.nan),
                    "net_taker_flow": row.get("net_taker_flow", np.nan),
                    "vol_ratio": row.get("vol_ratio", np.nan),
                    "ret_5": row.get("ret_5", np.nan),
                }

                # ── Build trade record ──
                trade = {
                    "id": sig_id,
                    "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "asset": asset,
                    "window_ts": window_ts,
                    "poly_slug": market["slug"],
                    "direction": direction,
                    "prob_up_logistic": round(lg_prob, 4),
                    "prob_up_heuristic": round(h_prob, 4),
                    "prob_up_ensemble": round(ens_prob, 4),
                    "poly_up_bid": up_prices["bid"],
                    "poly_up_ask": up_prices["ask"],
                    "poly_up_mid": up_prices["mid"],
                    "poly_down_bid": down_prices["bid"],
                    "poly_down_ask": down_prices["ask"],
                    "poly_down_mid": down_prices["mid"],
                    "poly_spread": up_prices["spread"],
                    "edge": round(edge, 4),
                    "edge_vs_mid": round(edge_vs_mid, 4),
                    "kelly_fraction": round(kelly_size(our_prob, exec_price, 1.0), 4),
                    "bet_size_usd": round(bet_size, 2),
                    "binance_price": binance_price,
                    **{k: round(float(v), 4) if not np.isnan(float(v)) else ""
                       for k, v in signals.items()},
                }

                # ── 6. Execute or skip ──
                if bet_size > 0 and edge >= MIN_EDGE:
                    trade["action"] = "TRADE"
                    state["pending"].append({**trade, "placed_at": time.time()})
                    log_trade({**trade, "resolved": False})

                    pct = (bet_size / bankroll) * 100
                    print(f"\n  {'='*60}")
                    print(f"  >>> TRADE: {asset} {direction}")
                    print(f"  {'='*60}")
                    print(f"  Polymarket:  UP bid/ask = ${up_prices['bid']:.2f}/${up_prices['ask']:.2f}"
                          f"  |  DOWN bid/ask = ${down_prices['bid']:.2f}/${down_prices['ask']:.2f}")
                    print(f"  Our P(UP):   {lg_prob:.3f} (ML) | {h_prob:.3f} (rules) "
                          f"| {ens_prob:.3f} (ensemble)")
                    print(f"  EDGE:        {edge:.3f} ({edge*100:.1f} cents/dollar) "
                          f"vs ask ${exec_price:.2f}")
                    print(f"  Bet Size:    ${bet_size:.2f} ({pct:.1f}% of ${bankroll:,.2f})")
                    print(f"  Binance:     ${binance_price:,.2f}")
                    print(f"  Signals:     RSI={signals['rsi']:.1f}  "
                          f"MACD={signals['macd_hist']:+.2f}  "
                          f"BB={signals['bb_zscore']:+.2f}  "
                          f"Flow={signals['net_taker_flow']:+.3f}")
                    print(f"  Slug:        {market['slug']}")
                else:
                    trade["action"] = "SKIP"
                    state["skips"][asset] += 1
                    log_trade(trade)

                    reason = "no edge" if edge < MIN_EDGE else "bet too small"
                    print(f"\n  --- {asset}: SKIP ({reason}) | "
                          f"P(UP)={ens_prob:.3f} | "
                          f"Edge={edge:.3f} | "
                          f"UP={up_prices['mid']:.2f} DOWN={down_prices['mid']:.2f}")

            # ── Portfolio status ──
            print(f"\n  {'─'*60}")
            print(f"  PORTFOLIO @ Signal #{sig_id}")
            print(f"  {'─'*60}")
            print(f"  {'Asset':<6} {'Bankroll':>12} {'PnL':>10} {'W':>4} {'L':>4} "
                  f"{'WR':>7} {'Skip':>5}")
            total_bank = 0
            total_pnl = 0
            for a in ASSETS:
                w = state["wins"][a]
                l = state["losses"][a]
                wr = w / max(w + l, 1)
                print(f"  {a:<6} ${state['bankroll'][a]:>11,.2f} "
                      f"${state['pnl'][a]:>+9,.2f} {w:>4} {l:>4} "
                      f"{wr:>6.1%} {state['skips'][a]:>5}")
                total_bank += state["bankroll"][a]
                total_pnl += state["pnl"][a]
            print(f"  {'TOTAL':<6} ${total_bank:>11,.2f} ${total_pnl:>+9,.2f}")
            print(f"  Pending: {len(state['pending'])} trades awaiting resolution")
            print(f"  {'─'*60}")

            # Retrain every 50 signals (~4 hours)
            for asset in ASSETS:
                if asset not in models:
                    continue
                if sig_id - models[asset]["trained_at"] >= 50:
                    print(f"\n  [RETRAIN] {asset}...")
                    sym = BINANCE_MAP[asset]
                    df = fetch_klines(sym, lookback_days=8)
                    df = compute_features(df)
                    fc = models[asset]["feature_cols"]
                    clean = df.dropna(subset=fc + ["target"])
                    if len(clean) >= 500:
                        m = QuantModel()
                        m.train(clean[fc], clean["target"])
                        models[asset]["model"] = m
                        models[asset]["trained_at"] = sig_id
                        print(f"  [RETRAIN] {asset} done ({len(clean)} bars)")

            save_state(state)

            # Wait for next 5-min window
            now_ts = time.time()
            next_b = (int(now_ts // 300) + 1) * 300
            sleep = max(next_b - time.time() + 2, 10)  # +2s buffer
            next_str = datetime.fromtimestamp(next_b, tz=timezone.utc).strftime('%H:%M:%S')
            print(f"\n  Next signal at {next_str} UTC ({sleep:.0f}s)  |  Ctrl+C to stop\n")
            time.sleep(sleep)

        except KeyboardInterrupt:
            print("\n\n[SHUTDOWN] Saving state...")
            save_state(state)
            print(f"[SHUTDOWN] {LOG_FILE} has full trade log")
            break
        except Exception as e:
            print(f"\n[ERROR] {e}")
            traceback.print_exc()
            save_state(state)
            time.sleep(30)


if __name__ == "__main__":
    run()
