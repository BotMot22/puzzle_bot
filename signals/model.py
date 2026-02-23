"""
SIGNAL GENERATION — Combining Features into a Probability
==========================================================

QUANT FUNDAMENTAL #3: "The model IS the strategy."

A quant fund doesn't just look at RSI and say "overbought = sell."
They combine dozens of signals into a single probability estimate
using statistical models. Here's the hierarchy:

APPROACH HIERARCHY (simple → complex):

1. HEURISTIC SCORING (what we start with)
   - Assign weights to each signal based on economic intuition
   - Fast, interpretable, hard to overfit
   - "If RSI < 30 AND momentum positive → likely bounce → UP"

2. LOGISTIC REGRESSION (our primary model)
   - Probability output between 0 and 1 — perfect for binary prediction
   - Regularized (L2) to prevent overfitting
   - Interpretable coefficients tell you which features matter

3. ENSEMBLE / GRADIENT BOOSTING (what big funds use)
   - XGBoost, LightGBM — capture nonlinear interactions
   - Risk of overfitting on small datasets
   - We skip this for now to keep it honest

CRITICAL CONCEPT: WALK-FORWARD VALIDATION
  - Never train on future data (lookahead bias)
  - Train on window [0, T], predict [T, T+step], slide forward
  - This simulates real-time decision making
"""

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import log_loss, accuracy_score, brier_score_loss
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import TRAIN_WINDOW, TEST_WINDOW, MIN_EDGE


class HeuristicScorer:
    """
    APPROACH 1: Rule-based signal combination.

    This is how many successful quant traders actually start.
    Each signal gets a score, scores are averaged into a probability.
    Simple, robust, and very hard to overfit.
    """

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        """Return P(UP) for each row. Output shape: (n, 2)."""
        scores = np.zeros(len(features))
        count = np.zeros(len(features))

        def add_signal(mask, name, signal_values):
            valid = ~np.isnan(signal_values)
            scores[valid] += signal_values[valid]
            count[valid] += 1

        f = features

        # ── Momentum signals ──
        if "ret_5" in f.columns:
            # Recent momentum: positive returns → likely continues (short-term)
            ret5 = f["ret_5"].values
            add_signal(None, "momentum_5", np.clip(ret5 * 100, -2, 2))

        if "ret_1" in f.columns:
            ret1 = f["ret_1"].values
            add_signal(None, "momentum_1", np.clip(ret1 * 200, -2, 2))

        if "macd_hist" in f.columns:
            macd_h = f["macd_hist"].values
            # Normalize by typical range
            add_signal(None, "macd", np.clip(macd_h / (np.nanstd(macd_h) + 1e-10), -2, 2))

        # ── Mean reversion signals ──
        if "rsi_norm" in f.columns:
            rsi_n = f["rsi_norm"].values
            # Extreme RSI → expect mean reversion (inverted)
            # RSI < 30 (norm < -0.4) → oversold → expect UP
            # RSI > 70 (norm > 0.4) → overbought → expect DOWN
            mr_signal = np.where(np.abs(rsi_n) > 0.4, -rsi_n * 0.5, 0)
            add_signal(None, "rsi_mr", mr_signal)

        if "bb_zscore" in f.columns:
            bbz = f["bb_zscore"].values
            # Far from mean → expect reversion
            mr_bb = np.where(np.abs(bbz) > 1.5, -bbz * 0.3, 0)
            add_signal(None, "bb_mr", mr_bb)

        # ── Volume / flow signals ──
        if "net_taker_flow" in f.columns:
            flow = f["net_taker_flow"].values
            # Aggressive buying → bullish
            add_signal(None, "flow", np.clip(flow * 4, -2, 2))

        if "vol_weighted_ret_5" in f.columns:
            vwr = f["vol_weighted_ret_5"].values
            add_signal(None, "vol_ret", np.clip(vwr * 50, -2, 2))

        # ── Volatility regime ──
        if "vol_ratio" in f.columns:
            vr = f["vol_ratio"].values
            # High vol ratio = expanding vol = momentum more likely
            # We use this to weight momentum signals
            vol_mult = np.where(vr > 1.2, 1.3, np.where(vr < 0.8, 0.7, 1.0))
            scores *= vol_mult

        # ── Candle structure ──
        if "candle_streak" in f.columns:
            streak = f["candle_streak"].values
            # Long streaks → continuation (but diminishing)
            add_signal(None, "streak", np.clip(streak * 0.15, -1, 1))

        # Convert aggregate score to probability using sigmoid
        count = np.maximum(count, 1)
        avg_score = scores / count
        prob_up = 1 / (1 + np.exp(-avg_score))  # sigmoid

        # Shrink toward 0.5 (humility adjustment — we're not that smart)
        prob_up = 0.5 + (prob_up - 0.5) * 0.6

        return np.column_stack([1 - prob_up, prob_up])


class QuantModel:
    """
    APPROACH 2: Logistic Regression with walk-forward validation.

    This is the bread and butter of quantitative finance.
    Logistic regression is used because:
      - Output is a calibrated probability (crucial for Kelly sizing)
      - L2 regularization prevents overfitting
      - Coefficients are interpretable (you know WHY it's predicting)
      - Fast to train (can retrain every day)
    """

    def __init__(self):
        self.model = None
        self.scaler = None
        self.feature_importance = None

    def train(self, X: pd.DataFrame, y: pd.Series):
        """Train on historical data."""
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        self.model = LogisticRegression(
            C=0.1,            # strong regularization (prevent overfitting)
            penalty="l2",
            max_iter=1000,
            class_weight="balanced",  # handle class imbalance
        )
        self.model.fit(X_scaled, y)

        # Feature importance = absolute coefficient values
        self.feature_importance = pd.Series(
            np.abs(self.model.coef_[0]),
            index=X.columns
        ).sort_values(ascending=False)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """Return P(DOWN), P(UP) for each row."""
        if self.model is None:
            raise RuntimeError("Model not trained")
        X_scaled = self.scaler.transform(X)
        return self.model.predict_proba(X_scaled)

    def get_top_features(self, n: int = 15) -> pd.Series:
        """Show which features the model cares about most."""
        if self.feature_importance is None:
            return pd.Series()
        return self.feature_importance.head(n)


def walk_forward_predict(df: pd.DataFrame, feature_cols: list,
                         train_window: int = TRAIN_WINDOW,
                         test_window: int = TEST_WINDOW,
                         use_heuristic: bool = False) -> pd.DataFrame:
    """
    WALK-FORWARD VALIDATION — The Gold Standard of Backtesting

    This simulates exactly what would happen in real-time:
      1. Train model on data up to time T
      2. Predict for time T to T + test_window
      3. Slide T forward by test_window
      4. Repeat

    No future information ever leaks into predictions.

    Returns DataFrame with columns: timestamp, prob_up, target, fwd_ret
    """
    results = []
    total_rows = len(df)

    if use_heuristic:
        model = HeuristicScorer()
        # Heuristic doesn't need training, predict on everything after warmup
        warmup = max(60, max([int(c.split("_")[-1]) for c in feature_cols
                              if c.split("_")[-1].isdigit()] + [0]))
        valid = df.iloc[warmup:].dropna(subset=feature_cols + ["target"])
        if len(valid) == 0:
            return pd.DataFrame()

        X = valid[feature_cols]
        probs = model.predict_proba(X)

        out = pd.DataFrame({
            "timestamp": valid["timestamp"].values,
            "prob_up": probs[:, 1],
            "target": valid["target"].values,
            "fwd_ret": valid["fwd_ret"].values,
            "close": valid["close"].values,
        })
        return out

    # Walk-forward for ML model
    start_idx = train_window
    all_importances = []

    print(f"[MODEL] Walk-forward: train={train_window}, step={test_window}, "
          f"total={total_rows} bars")

    step = 0
    while start_idx + test_window <= total_rows:
        # Define train and test windows
        train_df = df.iloc[start_idx - train_window:start_idx]
        test_df = df.iloc[start_idx:start_idx + test_window]

        # Drop NaN rows
        train_clean = train_df.dropna(subset=feature_cols + ["target"])
        test_clean = test_df.dropna(subset=feature_cols + ["target"])

        if len(train_clean) < 100 or len(test_clean) < 10:
            start_idx += test_window
            continue

        X_train = train_clean[feature_cols]
        y_train = train_clean["target"]
        X_test = test_clean[feature_cols]

        # Train fresh model each step
        model = QuantModel()
        model.train(X_train, y_train)
        all_importances.append(model.feature_importance)

        # Predict
        probs = model.predict_proba(X_test)

        step_results = pd.DataFrame({
            "timestamp": test_clean["timestamp"].values,
            "prob_up": probs[:, 1],
            "target": test_clean["target"].values,
            "fwd_ret": test_clean["fwd_ret"].values,
            "close": test_clean["close"].values,
        })
        results.append(step_results)

        step += 1
        if step % 5 == 0:
            print(f"  Step {step}: trained on {len(train_clean)}, "
                  f"predicting {len(test_clean)} bars")

        start_idx += test_window

    if not results:
        return pd.DataFrame()

    output = pd.concat(results, ignore_index=True)

    # Average feature importance across all walk-forward steps
    if all_importances:
        avg_importance = pd.concat(all_importances, axis=1).mean(axis=1).sort_values(ascending=False)
        print(f"\n[MODEL] Top 10 Features (avg |coefficient|):")
        for feat, imp in avg_importance.head(10).items():
            print(f"  {feat:30s} {imp:.4f}")

    return output
