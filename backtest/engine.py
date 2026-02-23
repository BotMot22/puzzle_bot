"""
BACKTESTING ENGINE — Simulating Real Trading
==============================================

QUANT FUNDAMENTAL #4: "If you can't backtest it, you can't trade it."

This engine simulates placing bets on Polymarket's 5-minute
BTC/ETH up/down markets. Key concepts:

POLYMARKET BINARY MECHANICS:
  - You buy shares at price p (e.g., $0.55 for "YES BTC UP")
  - If correct: you receive $1.00 → profit = $1.00 - p
  - If wrong: you receive $0.00 → loss = -p
  - The price IS the market's implied probability

OUR EDGE:
  - We compute our own P(UP) using quantitative signals
  - If our P(UP) > market price + slippage → buy YES (bet UP)
  - If our P(UP) < (1 - market price) - slippage → buy NO (bet DOWN)
  - The difference (our_prob - market_prob) is our EDGE

POSITION SIZING — Kelly Criterion:
  - f* = (p * b - q) / b
  - where p = our probability, q = 1-p, b = odds
  - For binary at price c: f* = p - c  (simplified)
  - We use fractional Kelly (25%) because:
    1. Our probability estimates have error
    2. Kelly is optimal but VERY aggressive
    3. Quarter-Kelly ≈ 75% of Kelly's growth at 50% of the variance

WHAT WE TRACK:
  - PnL curve (equity over time)
  - Win rate, avg win, avg loss
  - Sharpe ratio (risk-adjusted returns)
  - Max drawdown (worst peak-to-trough decline)
  - Brier score (calibration of probabilities)
  - Edge decay (does our edge persist or is it transient?)
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (KELLY_FRACTION, MAX_POSITION_PCT, MIN_EDGE,
                    BANKROLL, SLIPPAGE_CENTS, PREDICTION_WINDOW)


@dataclass
class Trade:
    timestamp: object
    direction: str        # "UP" or "DOWN"
    our_prob: float       # our estimated probability of direction
    market_price: float   # what we'd pay on Polymarket (implied prob)
    edge: float           # our_prob - market_price
    bet_size: float       # $ risked
    shares: float         # shares bought (bet_size / market_price)
    outcome: int          # 1 = correct, 0 = wrong
    pnl: float            # profit/loss on this trade
    bankroll_after: float  # bankroll after this trade


@dataclass
class BacktestResult:
    symbol: str
    model_type: str
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    timestamps: List[object] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winning_trades(self) -> int:
        return sum(1 for t in self.trades if t.outcome == 1)

    @property
    def win_rate(self) -> float:
        return self.winning_trades / max(self.total_trades, 1)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def avg_edge(self) -> float:
        return np.mean([t.edge for t in self.trades]) if self.trades else 0

    @property
    def sharpe_ratio(self) -> float:
        """Annualized Sharpe ratio of trade returns."""
        if not self.trades:
            return 0
        returns = [t.pnl / max(t.bet_size, 0.01) for t in self.trades]
        if np.std(returns) == 0:
            return 0
        # ~288 five-minute periods per day, 365 days/year
        trades_per_year = 288 * 365
        return (np.mean(returns) / np.std(returns)) * np.sqrt(trades_per_year)

    @property
    def max_drawdown(self) -> float:
        """Maximum peak-to-trough decline in equity."""
        if not self.equity_curve:
            return 0
        peak = self.equity_curve[0]
        max_dd = 0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def profit_factor(self) -> float:
        """Gross profits / gross losses."""
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        return gross_profit / max(gross_loss, 0.01)

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trades if t.outcome == 1]
        return np.mean(wins) if wins else 0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.trades if t.outcome == 0]
        return np.mean(losses) if losses else 0

    @property
    def return_pct(self) -> float:
        return (self.total_pnl / BANKROLL) * 100


def simulate_market_price(prob_up: float) -> float:
    """
    In a real system, you'd fetch live Polymarket order book prices.
    For backtesting, we simulate the market price as being close to
    the "true" probability with some noise.

    KEY INSIGHT: Markets are efficient but not perfectly efficient.
    The market price reflects aggregate opinion, but we model it as
    the base rate (~50% for 5-min crypto) plus some noise.
    """
    # 5-minute crypto markets hover around 50/50
    # Add small random deviation to simulate market mispricing
    base = 0.50
    noise = np.random.normal(0, 0.03)  # market has some noise
    return np.clip(base + noise, 0.05, 0.95)


def kelly_size(our_prob: float, market_price: float,
               bankroll: float) -> float:
    """
    KELLY CRITERION — Optimal bet sizing.

    For a binary bet where you pay `market_price` to win $1:
      - If correct: profit = 1 - market_price
      - If wrong: loss = market_price
      - Odds b = (1 - market_price) / market_price
      - Kelly fraction f* = (p*b - q) / b = p - market_price / (1 - market_price) * (1-p)
      - Simplified for binary: f* ≈ (our_prob - market_price)

    We use fractional Kelly to be conservative.
    """
    if our_prob <= market_price:
        return 0  # no edge, don't bet

    edge = our_prob - market_price
    if edge < MIN_EDGE:
        return 0  # edge too small to overcome transaction costs

    # Kelly fraction for binary payoff
    b = (1 - market_price) / market_price  # decimal odds
    q = 1 - our_prob
    kelly_f = (our_prob * b - q) / b

    # Apply fractional Kelly
    kelly_f *= KELLY_FRACTION

    # Cap at max position size
    bet_size = min(kelly_f * bankroll, MAX_POSITION_PCT * bankroll)

    # Don't bet less than $0.10
    return max(bet_size, 0) if bet_size >= 0.10 else 0


def run_backtest(predictions: pd.DataFrame, symbol: str,
                 model_type: str = "logistic") -> BacktestResult:
    """
    Run the full backtesting simulation.

    For each prediction point:
    1. Get our probability estimate
    2. Compare to simulated market price
    3. Size position using Kelly
    4. Record outcome
    5. Update bankroll
    """
    result = BacktestResult(symbol=symbol, model_type=model_type)
    bankroll = BANKROLL
    result.equity_curve.append(bankroll)

    # We only bet every PREDICTION_WINDOW bars to avoid overlapping bets
    step = PREDICTION_WINDOW
    indices = range(0, len(predictions) - step, step)

    trades_taken = 0
    trades_skipped = 0

    for i in indices:
        row = predictions.iloc[i]
        prob_up = row["prob_up"]
        target = row["target"]
        fwd_ret = row["fwd_ret"]
        ts = row["timestamp"]

        if pd.isna(prob_up) or pd.isna(target):
            continue

        # Determine our bet direction and probability
        if prob_up >= 0.5:
            direction = "UP"
            our_prob = prob_up
        else:
            direction = "DOWN"
            our_prob = 1 - prob_up

        # Simulate market price
        market_price = simulate_market_price(prob_up)
        slippage_adjusted = market_price + SLIPPAGE_CENTS

        # Calculate edge
        edge = our_prob - slippage_adjusted

        # Size the bet
        bet_size = kelly_size(our_prob, slippage_adjusted, bankroll)

        if bet_size == 0:
            trades_skipped += 1
            result.equity_curve.append(bankroll)
            result.timestamps.append(ts)
            continue

        # Determine outcome
        if direction == "UP":
            correct = int(fwd_ret > 0)
        else:
            correct = int(fwd_ret <= 0)

        # Calculate PnL
        shares = bet_size / slippage_adjusted
        if correct:
            pnl = shares * (1 - slippage_adjusted)  # win: receive $1 per share
        else:
            pnl = -bet_size  # lose: lose entire bet

        bankroll += pnl
        bankroll = max(bankroll, 0)  # can't go below zero

        trade = Trade(
            timestamp=ts,
            direction=direction,
            our_prob=our_prob,
            market_price=slippage_adjusted,
            edge=edge,
            bet_size=bet_size,
            shares=shares,
            outcome=correct,
            pnl=pnl,
            bankroll_after=bankroll,
        )
        result.trades.append(trade)
        result.equity_curve.append(bankroll)
        result.timestamps.append(ts)
        trades_taken += 1

    print(f"\n[BACKTEST] {symbol} ({model_type})")
    print(f"  Trades taken: {trades_taken}, skipped: {trades_skipped}")
    print(f"  Bankroll: ${BANKROLL:.2f} → ${bankroll:.2f}")

    return result
