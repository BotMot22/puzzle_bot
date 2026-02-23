"""
REPORTING & ANALYSIS — Understanding Your Results
===================================================

QUANT FUNDAMENTAL #5: "A strategy is only as good as its worst day."

What separates amateur traders from quant funds:

METRICS THAT MATTER:
  1. Sharpe Ratio — risk-adjusted return (>1 is decent, >2 is excellent)
  2. Max Drawdown — your worst nightmare scenario
  3. Win Rate × Avg Win/Loss — the expectancy equation
  4. Profit Factor — gross wins / gross losses (>1.5 is good)
  5. Brier Score — are your probabilities calibrated? (lower = better)
  6. Edge Consistency — does your edge persist across time periods?

THINGS QUANT FUNDS OBSESS OVER:
  - Is the edge decaying? (alpha decay)
  - What's the capacity? (how much can you bet before moving the market)
  - What's the correlation to simple strategies? (is this just momentum?)
  - Would this survive a regime change? (bull vs bear vs sideways)
"""

import numpy as np
import pandas as pd
from sklearn.metrics import brier_score_loss
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BANKROLL


def generate_report(result, predictions: pd.DataFrame) -> str:
    """Generate comprehensive backtest report."""

    lines = []
    lines.append("=" * 70)
    lines.append(f"  BACKTEST REPORT: {result.symbol} — {result.model_type} model")
    lines.append("=" * 70)

    # ── Overview ──
    lines.append(f"\n{'─' * 40}")
    lines.append("  OVERVIEW")
    lines.append(f"{'─' * 40}")
    lines.append(f"  Starting Bankroll:   ${BANKROLL:,.2f}")
    lines.append(f"  Ending Bankroll:     ${result.equity_curve[-1]:,.2f}")
    lines.append(f"  Total Return:        {result.return_pct:+.2f}%")
    lines.append(f"  Total PnL:           ${result.total_pnl:+,.2f}")
    lines.append(f"  Total Trades:        {result.total_trades}")

    if result.total_trades == 0:
        lines.append("\n  No trades executed. Edge threshold may be too high.")
        return "\n".join(lines)

    # ── Performance Metrics ──
    lines.append(f"\n{'─' * 40}")
    lines.append("  PERFORMANCE METRICS")
    lines.append(f"{'─' * 40}")
    lines.append(f"  Win Rate:            {result.win_rate:.1%}")
    lines.append(f"  Avg Win:             ${result.avg_win:+.2f}")
    lines.append(f"  Avg Loss:            ${result.avg_loss:+.2f}")
    lines.append(f"  Profit Factor:       {result.profit_factor:.2f}")
    lines.append(f"  Sharpe Ratio:        {result.sharpe_ratio:.2f}")
    lines.append(f"  Max Drawdown:        {result.max_drawdown:.1%}")
    lines.append(f"  Avg Edge:            {result.avg_edge:.3f}")

    # ── Probability Calibration ──
    if len(predictions) > 0:
        valid = predictions.dropna(subset=["prob_up", "target"])
        if len(valid) > 0:
            brier = brier_score_loss(valid["target"], valid["prob_up"])
            # Baseline brier (always predict 0.5)
            baseline_brier = brier_score_loss(valid["target"],
                                              np.full(len(valid), 0.5))
            brier_skill = 1 - brier / baseline_brier

            lines.append(f"\n{'─' * 40}")
            lines.append("  PROBABILITY CALIBRATION")
            lines.append(f"{'─' * 40}")
            lines.append(f"  Brier Score:         {brier:.4f}")
            lines.append(f"  Baseline Brier:      {baseline_brier:.4f}")
            lines.append(f"  Brier Skill Score:   {brier_skill:.4f}")
            lines.append(f"    (>0 = better than coin flip, <0 = worse)")

            # Calibration buckets
            lines.append(f"\n  Calibration Table (predicted vs actual):")
            lines.append(f"  {'Bucket':>12} {'Count':>8} {'Pred':>8} {'Actual':>8} {'Gap':>8}")
            valid_sorted = valid.copy()
            valid_sorted["bucket"] = pd.cut(valid_sorted["prob_up"],
                                            bins=[0, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 1.0])
            for bucket, group in valid_sorted.groupby("bucket", observed=True):
                if len(group) > 0:
                    pred = group["prob_up"].mean()
                    actual = group["target"].mean()
                    gap = actual - pred
                    lines.append(f"  {str(bucket):>12} {len(group):>8} "
                                 f"{pred:>8.3f} {actual:>8.3f} {gap:>+8.3f}")

    # ── Edge Analysis ──
    if result.total_trades >= 20:
        lines.append(f"\n{'─' * 40}")
        lines.append("  EDGE ANALYSIS")
        lines.append(f"{'─' * 40}")

        # Split trades into quartiles by time
        n = len(result.trades)
        quarters = [result.trades[i:i + n//4] for i in range(0, n, max(n//4, 1))][:4]
        for q_idx, q_trades in enumerate(quarters):
            if q_trades:
                q_wr = sum(1 for t in q_trades if t.outcome == 1) / len(q_trades)
                q_pnl = sum(t.pnl for t in q_trades)
                q_edge = np.mean([t.edge for t in q_trades])
                lines.append(f"  Q{q_idx+1}: WR={q_wr:.1%}  PnL=${q_pnl:+.2f}  "
                             f"Avg Edge={q_edge:.3f}  Trades={len(q_trades)}")

        # Edge decay detection
        if len(quarters) >= 2 and quarters[0] and quarters[-1]:
            early_wr = sum(1 for t in quarters[0] if t.outcome == 1) / len(quarters[0])
            late_wr = sum(1 for t in quarters[-1] if t.outcome == 1) / len(quarters[-1])
            if late_wr < early_wr - 0.05:
                lines.append(f"\n  ⚠ WARNING: Possible edge decay detected")
                lines.append(f"    Early WR: {early_wr:.1%} → Late WR: {late_wr:.1%}")

    # ── Trade Distribution ──
    lines.append(f"\n{'─' * 40}")
    lines.append("  TRADE DISTRIBUTION")
    lines.append(f"{'─' * 40}")
    up_trades = [t for t in result.trades if t.direction == "UP"]
    down_trades = [t for t in result.trades if t.direction == "DOWN"]
    lines.append(f"  UP bets:   {len(up_trades)} "
                 f"(WR: {sum(1 for t in up_trades if t.outcome==1)/max(len(up_trades),1):.1%})")
    lines.append(f"  DOWN bets: {len(down_trades)} "
                 f"(WR: {sum(1 for t in down_trades if t.outcome==1)/max(len(down_trades),1):.1%})")

    pnls = [t.pnl for t in result.trades]
    lines.append(f"\n  PnL Distribution:")
    lines.append(f"    Min:     ${min(pnls):+.2f}")
    lines.append(f"    P25:     ${np.percentile(pnls, 25):+.2f}")
    lines.append(f"    Median:  ${np.percentile(pnls, 50):+.2f}")
    lines.append(f"    P75:     ${np.percentile(pnls, 75):+.2f}")
    lines.append(f"    Max:     ${max(pnls):+.2f}")

    lines.append(f"\n{'─' * 40}")
    lines.append("  KEY TAKEAWAYS")
    lines.append(f"{'─' * 40}")

    if result.sharpe_ratio > 2:
        lines.append("  [+] Excellent Sharpe ratio — strong risk-adjusted returns")
    elif result.sharpe_ratio > 1:
        lines.append("  [+] Good Sharpe ratio — decent risk-adjusted returns")
    elif result.sharpe_ratio > 0:
        lines.append("  [~] Positive but modest Sharpe — might not survive costs")
    else:
        lines.append("  [-] Negative Sharpe — strategy is losing money risk-adjusted")

    if result.max_drawdown > 0.3:
        lines.append("  [-] Drawdown >30% — too risky for most allocators")
    elif result.max_drawdown > 0.15:
        lines.append("  [~] Drawdown 15-30% — acceptable but monitor closely")
    else:
        lines.append("  [+] Drawdown <15% — well controlled risk")

    if result.win_rate > 0.55:
        lines.append("  [+] Win rate >55% — meaningful predictive edge")
    elif result.win_rate > 0.50:
        lines.append("  [~] Win rate 50-55% — edge is thin, execution matters")
    else:
        lines.append("  [-] Win rate <50% — need to review signal quality")

    if result.profit_factor > 1.5:
        lines.append("  [+] Profit factor >1.5 — wins meaningfully exceed losses")
    elif result.profit_factor > 1.0:
        lines.append("  [~] Profit factor 1-1.5 — profitable but slim margin")
    else:
        lines.append("  [-] Profit factor <1 — losing money on average")

    lines.append("\n" + "=" * 70)
    return "\n".join(lines)


def print_equity_curve_ascii(result, width: int = 60):
    """Simple ASCII equity curve visualization."""
    if not result.equity_curve:
        return

    curve = result.equity_curve
    min_val = min(curve)
    max_val = max(curve)
    val_range = max_val - min_val if max_val != min_val else 1

    height = 20
    n = len(curve)
    step = max(n // width, 1)
    sampled = [curve[i] for i in range(0, n, step)][:width]

    print(f"\n  Equity Curve ({result.symbol} - {result.model_type})")
    print(f"  ${max_val:,.0f} ┐")

    for row in range(height, -1, -1):
        threshold = min_val + (row / height) * val_range
        line = "  " + " " * 8 + "│"
        for val in sampled:
            if val >= threshold:
                line += "█"
            else:
                line += " "
        if row == height // 2:
            mid = min_val + 0.5 * val_range
            print(f"  ${mid:,.0f}" + " " * (8 - len(f"${mid:,.0f}")) + "│" +
                  line[11:])
        else:
            print(line)

    print(f"  ${min_val:,.0f} ┘" + "─" * (len(sampled) + 1))
    print(f"  " + " " * 9 + "Start" + " " * (len(sampled) - 8) + "End")
