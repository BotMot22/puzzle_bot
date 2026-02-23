"""
Configuration for the Polymarket 5-Minute Crypto Prediction Backtester.

This is the control center — every tunable parameter lives here.
A quant fund keeps config separate so you can run parameter sweeps
without touching strategy logic.
"""

# ── Data Source ──────────────────────────────────────────────────
# We pull free 1-minute candles from Binance (highest liquidity).
# 1-min granularity lets us reconstruct any 5-min window exactly.
BINANCE_BASE = "https://api.binance.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
KLINE_INTERVAL = "1m"           # base candle size
PREDICTION_WINDOW = 5           # minutes — the Polymarket contract window
LOOKBACK_DAYS = 30              # how much history to pull for backtesting

# ── Feature / Signal Parameters ─────────────────────────────────
# Short-term momentum
MOMENTUM_WINDOWS = [1, 2, 3, 5, 10, 15, 20]  # in minutes

# RSI
RSI_PERIOD = 14

# Bollinger Bands
BB_PERIOD = 20
BB_STD = 2.0

# MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# VWAP lookback (rolling minutes)
VWAP_PERIOD = 60

# Volatility
VOL_WINDOWS = [5, 15, 30, 60]  # realized vol lookback windows

# Volume profile
VOLUME_MA_PERIOD = 20

# ── Model / Signal Combination ──────────────────────────────────
# Walk-forward validation
TRAIN_WINDOW = 1440 * 7        # 7 days of 1-min bars for training
TEST_WINDOW  = 1440            # 1 day for testing (walk-forward step)

# ── Risk Management ─────────────────────────────────────────────
KELLY_FRACTION = 0.25          # quarter-Kelly (conservative)
MAX_POSITION_PCT = 0.05        # never risk more than 5% of bankroll on one bet
MIN_EDGE = 0.02                # don't trade unless estimated edge > 2%
BANKROLL = 1000.0              # starting bankroll in USD

# ── Execution ────────────────────────────────────────────────────
# Polymarket binary payoff: you pay price p, receive $1 if correct, $0 if wrong
# The "implied probability" = price. Our edge = (our_prob - price).
# We model slippage as a fixed spread cost per trade.
SLIPPAGE_CENTS = 0.01          # 1 cent per share slippage estimate

# ── Output ──────────────────────────────────────────────────────
RESULTS_DIR = "data/results"
