"""
FEATURE ENGINEERING — The Alpha Factory
=========================================

QUANT FUNDAMENTAL #2: "Alpha comes from features, not models."

The best quant funds spend 80% of their effort on feature engineering
and only 20% on model selection. A simple model with great features
will crush a complex model with bad features.

CATEGORIES OF FEATURES (what a real quant desk computes):

1. MOMENTUM / TREND
   - Price returns over multiple horizons
   - Rate of change, acceleration
   - MACD, moving average crossovers

2. MEAN REVERSION
   - RSI (overbought/oversold)
   - Bollinger Band position (z-score)
   - Distance from VWAP

3. VOLATILITY REGIME
   - Realized volatility at multiple horizons
   - Volatility ratio (short/long) — regime detection
   - Garman-Klass volatility estimator (uses OHLC, more efficient)

4. VOLUME / LIQUIDITY
   - Volume relative to moving average
   - Taker buy ratio (order flow imbalance)
   - Quote volume momentum

5. MICROSTRUCTURE
   - Candle body ratio (close-open vs high-low)
   - Upper/lower wick ratios
   - Number of trades per candle
"""

import pandas as pd
import numpy as np
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (MOMENTUM_WINDOWS, RSI_PERIOD, BB_PERIOD, BB_STD,
                    MACD_FAST, MACD_SLOW, MACD_SIGNAL, VWAP_PERIOD,
                    VOL_WINDOWS, VOLUME_MA_PERIOD, PREDICTION_WINDOW)


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Take raw OHLCV data and produce a feature matrix.
    Each row = one 1-minute bar, columns = all computed features.

    IMPORTANT: All features use ONLY past data (no lookahead bias).
    This is the #1 mistake amateur quants make.
    """
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    open_ = df["open"]

    # ════════════════════════════════════════════════════════════════
    # 1. MOMENTUM FEATURES
    #    "Is price trending? How fast? Accelerating or decelerating?"
    # ════════════════════════════════════════════════════════════════
    for w in MOMENTUM_WINDOWS:
        # Simple return: (price_now / price_w_bars_ago) - 1
        df[f"ret_{w}"] = close.pct_change(w)

        # Log return (better statistical properties, additive over time)
        df[f"logret_{w}"] = np.log(close / close.shift(w))

    # Return acceleration: is momentum increasing or decreasing?
    df["ret_accel"] = df["ret_1"] - df["ret_1"].shift(1)

    # EMA crossover signals (fast EMA above slow = bullish)
    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    df["macd_hist_accel"] = df["macd_hist"] - df["macd_hist"].shift(1)

    # ════════════════════════════════════════════════════════════════
    # 2. MEAN REVERSION FEATURES
    #    "Is price overextended? Due for a snapback?"
    # ════════════════════════════════════════════════════════════════

    # RSI — Relative Strength Index
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # Normalized RSI: map to [-1, 1] centered at 50
    df["rsi_norm"] = (df["rsi"] - 50) / 50

    # Bollinger Bands — price z-score
    bb_ma = close.rolling(BB_PERIOD).mean()
    bb_std = close.rolling(BB_PERIOD).std()
    df["bb_zscore"] = (close - bb_ma) / bb_std.replace(0, np.nan)

    # %B: where in the band is price? 0=lower, 1=upper
    df["bb_pctb"] = (close - (bb_ma - BB_STD * bb_std)) / (2 * BB_STD * bb_std).replace(0, np.nan)

    # VWAP deviation — institutional benchmark
    typical_price = (high + low + close) / 3
    cum_tp_vol = (typical_price * volume).rolling(VWAP_PERIOD).sum()
    cum_vol = volume.rolling(VWAP_PERIOD).sum()
    vwap = cum_tp_vol / cum_vol.replace(0, np.nan)
    df["vwap_dev"] = (close - vwap) / vwap.replace(0, np.nan)

    # ════════════════════════════════════════════════════════════════
    # 3. VOLATILITY FEATURES
    #    "What regime are we in? High vol = mean reversion.
    #     Low vol = trend following."
    # ════════════════════════════════════════════════════════════════

    for w in VOL_WINDOWS:
        # Realized volatility (annualized std of log returns)
        log_ret = np.log(close / close.shift(1))
        df[f"rvol_{w}"] = log_ret.rolling(w).std() * np.sqrt(525600)  # annualize

        # Garman-Klass volatility (more efficient estimator using OHLC)
        log_hl = np.log(high / low) ** 2
        log_co = np.log(close / open_) ** 2
        gk = 0.5 * log_hl - (2 * np.log(2) - 1) * log_co
        df[f"gk_vol_{w}"] = gk.rolling(w).mean().apply(lambda x: np.sqrt(max(x, 0)) * np.sqrt(525600))

    # Volatility ratio: short vol / long vol
    # > 1 means volatility expanding (breakout), < 1 means contracting (range)
    if len(VOL_WINDOWS) >= 2:
        df["vol_ratio"] = df[f"rvol_{VOL_WINDOWS[0]}"] / df[f"rvol_{VOL_WINDOWS[-1]}"].replace(0, np.nan)

    # ATR (Average True Range) — captures gap risk
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(14).mean()
    df["atr_pct"] = df["atr_14"] / close  # normalize by price

    # ════════════════════════════════════════════════════════════════
    # 4. VOLUME / ORDER FLOW FEATURES
    #    "Where is the smart money? Follow the volume."
    # ════════════════════════════════════════════════════════════════

    # Volume relative to its own moving average
    vol_ma = volume.rolling(VOLUME_MA_PERIOD).mean()
    df["vol_ratio_ma"] = volume / vol_ma.replace(0, np.nan)

    # Taker buy ratio — order flow imbalance
    # High = aggressive buyers dominating = bullish pressure
    df["taker_buy_ratio"] = df["taker_buy_volume"] / volume.replace(0, np.nan)

    # Net taker flow (deviation from 0.5 neutral)
    df["net_taker_flow"] = df["taker_buy_ratio"] - 0.5

    # Volume-weighted momentum (returns weighted by relative volume)
    df["vol_weighted_ret_5"] = df["ret_5"] * df["vol_ratio_ma"]

    # OBV slope (On-Balance Volume trend)
    obv = (np.sign(close.diff()) * volume).cumsum()
    df["obv_slope_10"] = obv.rolling(10).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) == 10 else np.nan,
        raw=False
    )

    # ════════════════════════════════════════════════════════════════
    # 5. MICROSTRUCTURE / CANDLE FEATURES
    #    "What does the shape of each candle tell us?"
    # ════════════════════════════════════════════════════════════════

    candle_range = (high - low).replace(0, np.nan)

    # Body ratio: how much of the candle is body vs wicks
    df["body_ratio"] = (close - open_).abs() / candle_range

    # Candle direction encoded: 1 = bullish, -1 = bearish
    df["candle_dir"] = np.sign(close - open_)

    # Upper wick ratio (selling pressure)
    df["upper_wick"] = (high - pd.concat([close, open_], axis=1).max(axis=1)) / candle_range

    # Lower wick ratio (buying pressure)
    df["lower_wick"] = (pd.concat([close, open_], axis=1).min(axis=1) - low) / candle_range

    # Consecutive candle direction (streak)
    dirs = df["candle_dir"].values
    streak = np.zeros(len(dirs))
    for i in range(1, len(dirs)):
        if dirs[i] == dirs[i-1] and dirs[i] != 0:
            streak[i] = streak[i-1] + dirs[i]
        else:
            streak[i] = dirs[i]
    df["candle_streak"] = streak

    # Trades per candle relative to average
    trades_ma = df["num_trades"].rolling(VOLUME_MA_PERIOD).mean()
    df["trades_ratio"] = df["num_trades"] / trades_ma.replace(0, np.nan)

    # ════════════════════════════════════════════════════════════════
    # 6. TARGET VARIABLE
    #    "Did price go UP or DOWN over the next 5 minutes?"
    # ════════════════════════════════════════════════════════════════

    # Forward return over prediction window (THIS IS WHAT WE'RE PREDICTING)
    df["fwd_ret"] = close.shift(-PREDICTION_WINDOW) / close - 1
    df["target"] = (df["fwd_ret"] > 0).astype(int)  # 1 = UP, 0 = DOWN

    # Forward volatility (useful for confidence calibration)
    fwd_log_rets = np.log(close.shift(-1) / close)
    df["fwd_vol"] = fwd_log_rets.rolling(PREDICTION_WINDOW).std().shift(-PREDICTION_WINDOW)

    return df


def get_feature_columns(df: pd.DataFrame) -> list:
    """Return the list of feature column names (exclude target/meta)."""
    exclude = {"timestamp", "open", "high", "low", "close", "volume",
               "close_time", "quote_volume", "num_trades",
               "taker_buy_volume", "taker_buy_quote_volume",
               "fwd_ret", "target", "fwd_vol", "ignore"}
    # Exclude logret_ columns (redundant with ret_ and add noise to model)
    return [c for c in df.columns
            if c not in exclude and not c.startswith("logret_")]
