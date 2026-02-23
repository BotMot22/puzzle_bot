#!/usr/bin/env python3
"""
POLYMARKET 5-MINUTE CRYPTO PREDICTION BACKTESTER
==================================================

This is the main entry point. Run this to execute the full pipeline:

  1. FETCH DATA      — Pull 1-min candles from Binance
  2. ENGINEER FEATURES — Compute 50+ quantitative signals
  3. GENERATE SIGNALS — Combine features into P(UP) probability
  4. BACKTEST        — Simulate trading with Kelly sizing
  5. REPORT          — Analyze results like a quant fund

QUANT FUND WORKFLOW (what this simulates):
  ┌─────────┐    ┌──────────┐    ┌─────────┐    ┌──────────┐    ┌────────┐
  │  DATA   │───▸│ FEATURES │───▸│  MODEL  │───▸│ RISK MGT │───▸│ REPORT │
  │ Binance │    │ 50+ sigs │    │ P(UP)   │    │ Kelly    │    │ Sharpe │
  └─────────┘    └──────────┘    └─────────┘    └──────────┘    └────────┘

Usage:
  python run_backtest.py                          # default: both BTC & ETH, both models
  python run_backtest.py --symbol BTCUSDT         # BTC only
  python run_backtest.py --model heuristic        # rule-based only
  python run_backtest.py --model logistic         # ML model only
  python run_backtest.py --days 14                # 14 days of data
  python run_backtest.py --refresh                # force re-download data
"""

import argparse
import sys
import os
import time

# Ensure imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import SYMBOLS, LOOKBACK_DAYS, BANKROLL
from data.fetcher import load_or_fetch
from signals.features import compute_features, get_feature_columns
from signals.model import walk_forward_predict
from backtest.engine import run_backtest
from backtest.report import generate_report, print_equity_curve_ascii


def main():
    parser = argparse.ArgumentParser(description="Polymarket 5-Min Crypto Backtester")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Symbol to test (BTCUSDT or ETHUSDT). Default: both")
    parser.add_argument("--model", type=str, default="both",
                        choices=["heuristic", "logistic", "both"],
                        help="Model type to use")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS,
                        help=f"Days of history (default: {LOOKBACK_DAYS})")
    parser.add_argument("--refresh", action="store_true",
                        help="Force re-download data")
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else SYMBOLS
    models = ["heuristic", "logistic"] if args.model == "both" else [args.model]

    # Override lookback if specified
    if args.days != LOOKBACK_DAYS:
        import config
        config.LOOKBACK_DAYS = args.days

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║   POLYMARKET 5-MIN CRYPTO PREDICTION BACKTESTER            ║")
    print("║   Quantitative Trading Framework v1.0                      ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"\n  Symbols:     {', '.join(symbols)}")
    print(f"  Models:      {', '.join(models)}")
    print(f"  Lookback:    {args.days} days")
    print(f"  Bankroll:    ${BANKROLL:,.2f}")
    print()

    all_results = []

    for symbol in symbols:
        print(f"\n{'━' * 60}")
        print(f"  Processing {symbol}")
        print(f"{'━' * 60}")

        # ── Step 1: Data ──
        t0 = time.time()
        df = load_or_fetch(symbol, force_refresh=args.refresh)
        print(f"  Data loaded in {time.time()-t0:.1f}s")

        # ── Step 2: Features ──
        t0 = time.time()
        df = compute_features(df)
        feature_cols = get_feature_columns(df)
        print(f"  {len(feature_cols)} features computed in {time.time()-t0:.1f}s")

        # ── Step 3 & 4: Model → Predict → Backtest ──
        for model_type in models:
            print(f"\n  ── {model_type.upper()} MODEL ──")
            t0 = time.time()

            use_heuristic = (model_type == "heuristic")
            predictions = walk_forward_predict(
                df, feature_cols, use_heuristic=use_heuristic
            )

            if predictions.empty:
                print(f"  No predictions generated for {symbol}/{model_type}")
                continue

            print(f"  {len(predictions)} predictions in {time.time()-t0:.1f}s")

            # Backtest
            result = run_backtest(predictions, symbol, model_type)
            all_results.append((result, predictions))

            # Report
            report = generate_report(result, predictions)
            print(report)
            print_equity_curve_ascii(result)

    # ── Summary Comparison ──
    if len(all_results) > 1:
        print(f"\n\n{'═' * 70}")
        print(f"  STRATEGY COMPARISON")
        print(f"{'═' * 70}")
        print(f"  {'Strategy':<25} {'Return':>8} {'Sharpe':>8} {'WinRate':>8} "
              f"{'MaxDD':>8} {'Trades':>8} {'PF':>8}")
        print(f"  {'─'*25} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*8}")

        for result, _ in all_results:
            name = f"{result.symbol[:3]}/{result.model_type[:4]}"
            print(f"  {name:<25} {result.return_pct:>+7.1f}% "
                  f"{result.sharpe_ratio:>8.2f} {result.win_rate:>7.1%} "
                  f"{result.max_drawdown:>7.1%} {result.total_trades:>8} "
                  f"{result.profit_factor:>8.2f}")

    print(f"\n\n{'━' * 60}")
    print("  QUANT FUNDAMENTALS RECAP")
    print(f"{'━' * 60}")
    print("""
  What this backtester teaches you about quant trading:

  1. DATA IS KING — Clean, validated data is the foundation.
     We used 1-min Binance candles with gap detection.

  2. FEATURES > MODELS — 50+ signals across 5 categories:
     momentum, mean reversion, volatility, volume, microstructure.

  3. WALK-FORWARD VALIDATION — Never train on future data.
     We retrain the model as we slide through time.

  4. KELLY SIZING — Bet proportional to your edge, not your ego.
     Quarter-Kelly keeps you alive through drawdowns.

  5. MEASURE EVERYTHING — Sharpe, drawdown, Brier score,
     calibration, edge decay, profit factor.

  NEXT STEPS (what a real fund would do):
  • Add more data sources (funding rates, open interest, sentiment)
  • Test across different market regimes (bull/bear/sideways)
  • Add transaction cost sensitivity analysis
  • Implement live paper trading before real capital
  • Monitor for alpha decay in production
""")


if __name__ == "__main__":
    main()
