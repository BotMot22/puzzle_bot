#!/usr/bin/env python3
"""
LIVE SIGNAL RUNNER — Real-Time Trade Signals for Polymarket 5-Min Markets
==========================================================================

Pulls fresh Binance data every 5 minutes, computes features, generates
a probability estimate, and logs the trade decision.

Outputs:
  - Console: live trade signals as they fire
  - CSV log: data/trade_log.csv (append-mode, persistent)
  - Summary: prints running PnL and stats after each signal
"""

import time
import csv
import os
import sys
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (SYMBOLS, PREDICTION_WINDOW, KELLY_FRACTION,
                    MAX_POSITION_PCT, MIN_EDGE, BANKROLL, SLIPPAGE_CENTS,
                    TRAIN_WINDOW)
from data.fetcher import fetch_klines
from signals.features import compute_features, get_feature_columns
from signals.model import QuantModel, HeuristicScorer
from backtest.engine import kelly_size

import warnings
warnings.filterwarnings("ignore")

# ── Config ──
LOG_FILE = "data/trade_log.csv"
STATE_FILE = "data/live_state.json"
RETRAIN_INTERVAL = 60  # retrain model every 60 signals (~5 hours)
LOOKBACK_BARS = 15000  # ~10 days of 1-min bars for training context

LOG_FIELDS = [
    "signal_id", "timestamp", "symbol", "model",
    "direction", "prob_up", "prob_down", "confidence",
    "edge", "market_price_sim", "kelly_bet_size",
    "action", "entry_price", "bankroll_before",
    # filled after resolution
    "resolved", "exit_price", "fwd_return", "outcome",
    "pnl", "bankroll_after",
]


def init_log():
    """Create trade log CSV with headers if it doesn't exist."""
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()
        print(f"[LOG] Created {LOG_FILE}")


def load_state() -> dict:
    """Load persistent state (bankrolls, signal count, pending trades)."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "signal_count": 0,
        "bankroll": {s: BANKROLL for s in SYMBOLS},
        "total_pnl": {s: 0.0 for s in SYMBOLS},
        "wins": {s: 0 for s in SYMBOLS},
        "losses": {s: 0 for s in SYMBOLS},
        "skipped": {s: 0 for s in SYMBOLS},
        "pending": [],  # trades waiting for 5-min resolution
        "models_trained_at": {s: 0 for s in SYMBOLS},
    }


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log_trade(trade: dict):
    """Append a trade to the CSV log."""
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writerow({k: trade.get(k, "") for k in LOG_FIELDS})


def fetch_recent(symbol: str, bars: int = LOOKBACK_BARS) -> pd.DataFrame:
    """Fetch recent 1-min candles for live analysis."""
    days = max(bars // 1440 + 1, 2)
    df = fetch_klines(symbol, lookback_days=days)
    return df.tail(bars).reset_index(drop=True)


def resolve_pending(state: dict, current_prices: dict):
    """
    Check pending trades from 5 minutes ago and resolve them.
    This is the moment of truth — did price go UP or DOWN?
    """
    still_pending = []
    for trade in state["pending"]:
        elapsed = time.time() - trade["placed_at"]
        if elapsed < PREDICTION_WINDOW * 60:
            still_pending.append(trade)
            continue

        symbol = trade["symbol"]
        entry = trade["entry_price"]
        current = current_prices.get(symbol)

        if current is None:
            still_pending.append(trade)
            continue

        fwd_ret = (current - entry) / entry
        if trade["direction"] == "UP":
            correct = fwd_ret > 0
        else:
            correct = fwd_ret <= 0

        bet_size = trade["kelly_bet_size"]
        market_price = trade["market_price_sim"]

        if correct:
            pnl = (bet_size / market_price) * (1 - market_price)
        else:
            pnl = -bet_size

        state["bankroll"][symbol] += pnl
        state["total_pnl"][symbol] += pnl
        if correct:
            state["wins"][symbol] += 1
        else:
            state["losses"][symbol] += 1

        outcome_str = "WIN" if correct else "LOSS"
        emoji = "+" if correct else "-"

        # Update the CSV log with resolution
        trade["resolved"] = True
        trade["exit_price"] = current
        trade["fwd_return"] = fwd_ret
        trade["outcome"] = outcome_str
        trade["pnl"] = round(pnl, 2)
        trade["bankroll_after"] = round(state["bankroll"][symbol], 2)
        log_trade(trade)

        total_trades = state["wins"][symbol] + state["losses"][symbol]
        wr = state["wins"][symbol] / max(total_trades, 1)

        print(f"  [{outcome_str}] {symbol} {trade['direction']} | "
              f"Entry: ${entry:,.2f} -> Exit: ${current:,.2f} | "
              f"Ret: {fwd_ret:+.4%} | PnL: ${pnl:+.2f} | "
              f"Bankroll: ${state['bankroll'][symbol]:,.2f} | "
              f"Record: {state['wins'][symbol]}W-{state['losses'][symbol]}L ({wr:.1%})")

    state["pending"] = still_pending


def print_header():
    print("\n" + "=" * 80)
    print("  POLYMARKET LIVE SIGNAL RUNNER")
    print("  5-Minute BTC & ETH Up/Down Predictions")
    print("  Using Logistic Regression + Walk-Forward Training")
    print("=" * 80)
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Bankroll: ${BANKROLL:,.2f} per asset")
    print(f"  Min Edge: {MIN_EDGE:.0%} | Kelly Fraction: {KELLY_FRACTION:.0%}")
    print(f"  Signal Interval: {PREDICTION_WINDOW} minutes")
    print(f"  Log File: {LOG_FILE}")
    print("=" * 80 + "\n")


def print_status(state: dict):
    print(f"\n{'─' * 80}")
    print(f"  PORTFOLIO STATUS @ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
          f"  |  Signal #{state['signal_count']}")
    print(f"{'─' * 80}")
    print(f"  {'Symbol':<10} {'Bankroll':>12} {'PnL':>12} {'Wins':>6} {'Losses':>6} "
          f"{'WinRate':>8} {'Skipped':>8}")
    for s in SYMBOLS:
        total = state["wins"][s] + state["losses"][s]
        wr = state["wins"][s] / max(total, 1)
        print(f"  {s:<10} ${state['bankroll'][s]:>11,.2f} "
              f"${state['total_pnl'][s]:>+11,.2f} "
              f"{state['wins'][s]:>6} {state['losses'][s]:>6} "
              f"{wr:>7.1%} {state['skipped'][s]:>8}")
    total_bankroll = sum(state["bankroll"][s] for s in SYMBOLS)
    total_pnl = sum(state["total_pnl"][s] for s in SYMBOLS)
    print(f"  {'TOTAL':<10} ${total_bankroll:>11,.2f} ${total_pnl:>+11,.2f}")
    print(f"  Pending resolutions: {len(state['pending'])}")
    print(f"{'─' * 80}\n")


def run():
    init_log()
    state = load_state()
    print_header()

    # Train models for each symbol
    models = {}
    heuristic = HeuristicScorer()

    for symbol in SYMBOLS:
        print(f"[INIT] Fetching data and training model for {symbol}...")
        df = fetch_recent(symbol)
        df = compute_features(df)
        feature_cols = get_feature_columns(df)

        clean = df.dropna(subset=feature_cols + ["target"])
        if len(clean) < 500:
            print(f"[WARN] Not enough clean data for {symbol}, need 500+, got {len(clean)}")
            continue

        X = clean[feature_cols]
        y = clean["target"]

        model = QuantModel()
        model.train(X, y)
        models[symbol] = {
            "model": model,
            "feature_cols": feature_cols,
            "last_train": state["signal_count"],
        }

        print(f"[INIT] {symbol} model trained on {len(clean)} bars")
        top = model.get_top_features(5)
        for feat, imp in top.items():
            print(f"  {feat:30s} {imp:.4f}")

    if not models:
        print("[ERROR] No models trained. Exiting.")
        return

    print(f"\n[LIVE] Starting signal loop. Checking every {PREDICTION_WINDOW} minutes...\n")

    while True:
        try:
            state["signal_count"] += 1
            now = datetime.now(timezone.utc)
            print(f"\n{'━' * 80}")
            print(f"  SIGNAL #{state['signal_count']} @ "
                  f"{now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            print(f"{'━' * 80}")

            current_prices = {}

            for symbol in SYMBOLS:
                if symbol not in models:
                    continue

                # Fetch latest data
                df = fetch_recent(symbol, bars=500)
                df = compute_features(df)
                feature_cols = models[symbol]["feature_cols"]

                # Get latest complete bar
                latest = df.dropna(subset=feature_cols).iloc[-1:]
                if latest.empty:
                    print(f"  [{symbol}] No valid data, skipping")
                    continue

                current_price = float(latest["close"].iloc[0])
                current_prices[symbol] = current_price

                # ── Generate predictions from both models ──
                X = latest[feature_cols]

                # Logistic model
                lg_probs = models[symbol]["model"].predict_proba(X)
                lg_prob_up = float(lg_probs[0, 1])

                # Heuristic model
                h_probs = heuristic.predict_proba(latest[feature_cols])
                h_prob_up = float(h_probs[0, 1])

                # Ensemble: 70% logistic, 30% heuristic
                ensemble_prob_up = 0.7 * lg_prob_up + 0.3 * h_prob_up

                # Direction & confidence
                if ensemble_prob_up >= 0.5:
                    direction = "UP"
                    our_prob = ensemble_prob_up
                else:
                    direction = "DOWN"
                    our_prob = 1 - ensemble_prob_up

                confidence = abs(ensemble_prob_up - 0.5) * 2  # 0-1 scale

                # Simulate market price (in live: fetch from Polymarket API)
                market_price = 0.50 + np.random.normal(0, 0.02)
                market_price = np.clip(market_price, 0.35, 0.65)
                slippage_adj = market_price + SLIPPAGE_CENTS

                edge = our_prob - slippage_adj
                bankroll = state["bankroll"][symbol]
                bet_size = kelly_size(our_prob, slippage_adj, bankroll)

                # Build trade record
                trade = {
                    "signal_id": state["signal_count"],
                    "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": symbol,
                    "model": "ensemble",
                    "direction": direction,
                    "prob_up": round(lg_prob_up, 4),
                    "prob_down": round(1 - lg_prob_up, 4),
                    "confidence": round(confidence, 4),
                    "edge": round(edge, 4),
                    "market_price_sim": round(slippage_adj, 4),
                    "kelly_bet_size": round(bet_size, 2),
                    "entry_price": current_price,
                    "bankroll_before": round(bankroll, 2),
                    "placed_at": time.time(),
                }

                if bet_size > 0:
                    action = "TRADE"
                    trade["action"] = action
                    state["pending"].append(trade)

                    # Log the entry (resolution comes later)
                    trade_log_entry = trade.copy()
                    trade_log_entry["resolved"] = False
                    log_trade(trade_log_entry)

                    pct_of_bank = (bet_size / bankroll) * 100

                    print(f"\n  >>> {symbol} @ ${current_price:,.2f}")
                    print(f"      Direction:   {direction}")
                    print(f"      P(UP):       {lg_prob_up:.3f} (logistic) | "
                          f"{h_prob_up:.3f} (heuristic) | "
                          f"{ensemble_prob_up:.3f} (ensemble)")
                    print(f"      Confidence:  {confidence:.1%}")
                    print(f"      Edge:        {edge:.3f} "
                          f"(need >{MIN_EDGE})")
                    print(f"      Bet Size:    ${bet_size:.2f} "
                          f"({pct_of_bank:.1f}% of bankroll)")
                    print(f"      Mkt Price:   ${slippage_adj:.4f}")

                    # Key features for this prediction
                    print(f"      Key signals:")
                    for feat in ["ret_1", "ret_5", "rsi", "macd_hist",
                                 "bb_zscore", "net_taker_flow", "vol_ratio"]:
                        if feat in latest.columns:
                            val = float(latest[feat].iloc[0])
                            print(f"        {feat:20s} = {val:+.4f}")
                else:
                    action = "SKIP"
                    trade["action"] = action
                    state["skipped"][symbol] += 1
                    log_trade(trade)

                    print(f"\n  --- {symbol} @ ${current_price:,.2f} | "
                          f"SKIP (edge={edge:.3f} < {MIN_EDGE})")
                    print(f"      P(UP): {ensemble_prob_up:.3f} | "
                          f"Confidence: {confidence:.1%}")

            # Resolve any pending trades from 5+ minutes ago
            if current_prices:
                resolve_pending(state, current_prices)

            # Retrain models periodically
            for symbol in SYMBOLS:
                if symbol not in models:
                    continue
                signals_since_train = state["signal_count"] - models[symbol]["last_train"]
                if signals_since_train >= RETRAIN_INTERVAL:
                    print(f"\n[RETRAIN] Retraining {symbol} model "
                          f"(every {RETRAIN_INTERVAL} signals)...")
                    df = fetch_recent(symbol)
                    df = compute_features(df)
                    feature_cols = models[symbol]["feature_cols"]
                    clean = df.dropna(subset=feature_cols + ["target"])
                    if len(clean) >= 500:
                        model = QuantModel()
                        model.train(clean[feature_cols], clean["target"])
                        models[symbol]["model"] = model
                        models[symbol]["last_train"] = state["signal_count"]
                        print(f"[RETRAIN] {symbol} retrained on {len(clean)} bars")

            # Print status
            print_status(state)
            save_state(state)

            # Wait for next 5-minute window
            sleep_secs = PREDICTION_WINDOW * 60
            next_time = now.strftime('%H:%M')
            print(f"  Next signal in {PREDICTION_WINDOW} minutes... "
                  f"(Ctrl+C to stop)\n")
            time.sleep(sleep_secs)

        except KeyboardInterrupt:
            print("\n\n[STOP] Shutting down gracefully...")
            save_state(state)
            print_status(state)
            print(f"[STOP] Trade log saved to {LOG_FILE}")
            print(f"[STOP] State saved to {STATE_FILE}")
            break
        except Exception as e:
            print(f"\n[ERROR] {e}")
            import traceback
            traceback.print_exc()
            save_state(state)
            print("[ERROR] Retrying in 30 seconds...")
            time.sleep(30)


if __name__ == "__main__":
    run()
