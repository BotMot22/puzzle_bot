#!/usr/bin/env python3
"""
SCALP BOT v5 — ML EDGE at $0.75-$0.95
=======================================================================

Strategy 1: "ML Edge"
  - Enter when ask is $0.75-$0.95 AND ML prob > ask + 2% edge
  - Market identifies the likely winner, ML confirms it's underpriced
  - $5 fixed bets, last 45s, runs on BTC/ETH/SOL/XRP

Strategy 2: "ML Edge + BTC Confirmation"
  - Same as S1, plus BTC $25+ directional confirmation
  - BTC only, last 60s

LIVE TRADING with real USDC via Polymarket CLOB.
"""

import time
import csv
import os
import sys
import json
import traceback
import warnings
from datetime import datetime, timezone
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from live_quant import (
    discover_market, get_binance_price,
    CLOB_API, BINANCE_MAP,
)
from signals.model import QuantModel, HeuristicScorer
from signals.features import compute_features, get_feature_columns
from data.fetcher import fetch_klines
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from web3 import Web3
from eth_account import Account

# ═══════════════════════════════════════════════════════════════
# CREDENTIALS
# ═══════════════════════════════════════════════════════════════
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

clob_client = ClobClient(
    "https://clob.polymarket.com",
    key=os.environ["POLYMARKET_PRIVATE_KEY"],
    chain_id=137,
    creds=ApiCreds(
        api_key=os.environ["POLYMARKET_API_KEY"],
        api_secret=os.environ["POLYMARKET_API_SECRET"],
        api_passphrase=os.environ["POLYMARKET_PASSPHRASE"],
    ),
)

# ═══════════════════════════════════════════════════════════════
# ON-CHAIN REDEMPTION (auto-redeem winning shares for USDC)
# ═══════════════════════════════════════════════════════════════
_w3 = Web3(Web3.HTTPProvider("https://polygon.drpc.org"))
_acct = Account.from_key(os.environ["POLYMARKET_PRIVATE_KEY"])
_CTF = _w3.eth.contract(
    address=_w3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
    abi=json.loads('[{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]'),
)
_COLLATERAL = _w3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
_PARENT = b'\x00' * 32


def redeem_positions():
    """Redeem all resolved positions on-chain so USDC returns to wallet."""
    try:
        wallet = os.environ["POLYMARKET_WALLET"]
        positions = requests.get(
            f"https://data-api.polymarket.com/positions?user={wallet}",
            timeout=15,
        ).json()
        redeemable = [p for p in positions if p.get("redeemable")]
        if not redeemable:
            return

        nonce = _w3.eth.get_transaction_count(_acct.address, "latest")
        gas_price = _w3.eth.gas_price

        for p in redeemable:
            cid = p["conditionId"]
            # Strip 0x prefix if present, otherwise use as-is
            cid_hex = cid[2:] if cid.startswith("0x") else cid
            try:
                tx = _CTF.functions.redeemPositions(
                    _COLLATERAL, _PARENT, bytes.fromhex(cid_hex), [1, 2]
                ).build_transaction({
                    "from": _acct.address,
                    "nonce": nonce,
                    "gas": 250000,
                    "maxFeePerGas": gas_price * 2,
                    "maxPriorityFeePerGas": _w3.to_wei(50, "gwei"),
                    "chainId": 137,
                })
                signed = _acct.sign_transaction(tx)
                tx_hash = _w3.eth.send_raw_transaction(signed.raw_transaction)
                _w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                size = float(p.get('size', 0))
                print(f"  [REDEEM] {p['outcome']} {size:.2f} shares | {p.get('title', '?')[:45]}")
                nonce += 1
            except Exception as e:
                print(f"  [REDEEM FAIL] {p.get('outcome', '?')}: {e}")
    except Exception as e:
        print(f"  [REDEEM ERROR] {e}")


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
STATE_VERSION = 5         # Auto-reset state when strategy changes

MIN_ASK = 0.75            # Lower bound — wider range catches more action
MAX_ASK = 0.95            # Upper bound — still decent profit margin
BET_SIZE = 5.00           # Fixed $5 bets
MIN_EDGE = 0.02           # Require ML prob > ask + 2%
RETRAIN_EVERY = 50        # Retrain models every N windows (~4hrs)
ENSEMBLE_ML_W = 0.7       # ML model weight in ensemble
ENSEMBLE_H_W = 0.3        # Heuristic weight in ensemble

BTC_BUFFER = 25.0
S1_LEAD = 45              # Strategy 1: last 45 seconds
S2_LEAD = 60              # Strategy 2: last 60 seconds
POLL_INTERVAL = 1.5       # seconds between polls in scalp zone
WINDOW_SECS = 300

S1_ASSETS = ["BTC", "ETH", "SOL", "XRP"]
S2_ASSETS = ["BTC"]

STARTING_BANKROLL = 30.85  # wallet split across 2 strategies
KILL_SWITCH_MIN = 5.00     # Stop trading if bankroll drops below this
LOG_FILE = "data/scalp_trades.csv"
STATE_FILE = "data/scalp_state.json"

# ═══════════════════════════════════════════════════════════════
# MODEL CACHE (per-asset QuantModel + features)
# ═══════════════════════════════════════════════════════════════
_models = {}       # {asset: {"model": QuantModel, "feature_cols": [...], "df": df}}
_heuristic = None  # HeuristicScorer instance

LOG_FIELDS = [
    "strategy", "timestamp", "window_ts", "asset", "side",
    "ask_price", "bet_size", "shares", "potential_profit",
    "open_price", "price_at_trade", "price_delta",
    "resolved", "exit_price", "won", "pnl", "bankroll_after",
]


# ═══════════════════════════════════════════════════════════════
# MODEL INIT / RETRAIN / PROBABILITY
# ═══════════════════════════════════════════════════════════════

def init_models():
    """Train QuantModel per asset at startup (~2-3s per asset)."""
    global _heuristic
    _heuristic = HeuristicScorer()

    for asset in S1_ASSETS:
        sym = BINANCE_MAP[asset]
        print(f"  [MODEL] Training {asset} ({sym})...")
        try:
            df = fetch_klines(sym, lookback_days=2)
            df = compute_features(df)
            fc = get_feature_columns(df)
            clean = df.dropna(subset=fc + ["target"])
            if len(clean) < 100:
                print(f"  [MODEL] {asset}: only {len(clean)} bars, skipping ML")
                _models[asset] = {"model": None, "feature_cols": fc, "df": df}
                continue
            model = QuantModel()
            model.train(clean[fc], clean["target"])
            _models[asset] = {"model": model, "feature_cols": fc, "df": df}
            print(f"  [MODEL] {asset} ready ({len(clean)} bars)")
        except Exception as e:
            print(f"  [MODEL] {asset} training failed: {e}")
            _models[asset] = {"model": None, "feature_cols": [], "df": None}


def retrain_models(window_count):
    """Every RETRAIN_EVERY windows, re-fetch data and retrain models."""
    if window_count % RETRAIN_EVERY != 0 or window_count == 0:
        return
    print(f"\n  [RETRAIN] Window #{window_count} — retraining all models...")
    for asset in list(_models.keys()):
        sym = BINANCE_MAP[asset]
        try:
            df = fetch_klines(sym, lookback_days=2)
            df = compute_features(df)
            fc = get_feature_columns(df)
            clean = df.dropna(subset=fc + ["target"])
            if len(clean) < 100:
                print(f"  [RETRAIN] {asset}: insufficient data ({len(clean)} bars)")
                continue
            model = QuantModel()
            model.train(clean[fc], clean["target"])
            _models[asset] = {"model": model, "feature_cols": fc, "df": df}
            print(f"  [RETRAIN] {asset} done ({len(clean)} bars)")
        except Exception as e:
            print(f"  [RETRAIN] {asset} failed: {e}")


def refresh_features():
    """Refresh cached feature DataFrames (called at window open).
    Uses same 2-day lookback as init/retrain so feature distributions
    match what the scaler was fit on."""
    for asset in list(_models.keys()):
        sym = BINANCE_MAP[asset]
        try:
            df = fetch_klines(sym, lookback_days=2)
            df = compute_features(df)
            _models[asset]["df"] = df
            _models[asset]["feature_cols"] = get_feature_columns(df)
        except Exception as e:
            print(f"  [FEATURES] {asset} refresh failed: {e}")


def estimate_probability(asset):
    """
    Ensemble P(UP) from ML model + heuristic.
    Returns 0.5 (no edge) if model unavailable.
    """
    if asset not in _models or _models[asset]["df"] is None:
        return 0.5

    m = _models[asset]
    fc = m["feature_cols"]
    df = m["df"]

    valid = df.dropna(subset=fc)
    if valid.empty:
        return 0.5

    latest = valid.iloc[-1:]
    X = latest[fc]

    # ML probability
    ml_prob = 0.5
    if m["model"] is not None:
        try:
            ml_prob = float(m["model"].predict_proba(X)[0, 1])
        except Exception as e:
            print(f"  [WARN] {asset} ML predict failed: {e}")
            ml_prob = 0.5

    # Heuristic probability
    h_prob = 0.5
    if _heuristic is not None:
        try:
            h_prob = float(_heuristic.predict_proba(X)[0, 1])
        except Exception as e:
            print(f"  [WARN] {asset} heuristic predict failed: {e}")
            h_prob = 0.5

    # Ensemble
    ens_prob = ENSEMBLE_ML_W * ml_prob + ENSEMBLE_H_W * h_prob
    return ens_prob


# ═══════════════════════════════════════════════════════════════
# FAST PRICE FETCHER (single call, not 4)
# ═══════════════════════════════════════════════════════════════

def get_ask(token_id: str) -> float:
    """Fetch only the ask price (what we'd pay to buy). Fast, single HTTP call."""
    try:
        r = requests.get(
            f"{CLOB_API}/price?token_id={token_id}&side=SELL",
            timeout=2,
        )
        return float(r.json().get("price", 0))
    except:
        return 0.0


# ═══════════════════════════════════════════════════════════════
# WINDOW CONTEXT (per 5-min cycle)
# ═══════════════════════════════════════════════════════════════

@dataclass
class WindowCtx:
    window_ts: int
    window_end: int
    open_prices: dict = field(default_factory=dict)  # {asset: price}
    markets: dict = field(default_factory=dict)
    s1_traded: set = field(default_factory=set)   # (asset, side) combos
    s2_traded: bool = False


# ═══════════════════════════════════════════════════════════════
# STATE & LOGGING
# ═══════════════════════════════════════════════════════════════

def new_strategy_state():
    return {
        "bankroll": STARTING_BANKROLL,
        "pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "trades": 0,
        "pending": [],
    }


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        # Auto-reset if strategy version changed
        if data.get("version") != STATE_VERSION:
            print(f"  [STATE] Version mismatch (have {data.get('version')}, "
                  f"need {STATE_VERSION}) — resetting state")
        else:
            return data
    return {
        "version": STATE_VERSION,
        "s1": new_strategy_state(),
        "s2": new_strategy_state(),
        "windows": 0,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def init_log():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writeheader()


def log_trade(trade: dict):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writerow({k: trade.get(k, "") for k in LOG_FIELDS})


# ═══════════════════════════════════════════════════════════════
# TRADE EXECUTION (paper)
# ═══════════════════════════════════════════════════════════════

def execute_trade(state, strat_key, ctx, asset, side, ask_price):
    """LIVE trade with fixed $5 bets: place real FOK order on Polymarket CLOB."""
    s = state[strat_key]

    if s["bankroll"] < BET_SIZE:
        return

    ask_price = round(ask_price, 2)
    shares = int(BET_SIZE / ask_price)  # floor to whole shares
    if shares < 1:
        return

    binance_sym = BINANCE_MAP[asset]
    spot = get_binance_price(binance_sym)
    open_px = ctx.open_prices.get(asset, 0)
    delta = spot - open_px if open_px > 0 else 0

    # Determine token
    mkt = ctx.markets[asset]
    token_id = mkt["up_token"] if side == "UP" else mkt["down_token"]

    # Place real order (Fill-or-Kill)
    strat_name = "S1:Last15s" if strat_key == "s1" else "S2:30s+BTC"
    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=ask_price,
            size=shares,
            side=BUY,
        )
        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order, OrderType.FOK)

        if not resp or not resp.get("success"):
            print(f"\n  [NOFILL] [{strat_name}] {asset} {side} @ ${ask_price:.3f} "
                  f"— order not filled: {resp}")
            return
    except Exception as e:
        print(f"\n  [ERROR] [{strat_name}] Order failed: {e}")
        return

    # Order filled — record it
    order_id = resp.get("orderID", "")
    actual_cost = round(shares * ask_price, 2)
    trade = {
        "strategy": strat_key,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "window_ts": ctx.window_ts,
        "asset": asset,
        "side": side,
        "ask_price": ask_price,
        "bet_size": actual_cost,
        "shares": round(shares, 4),
        "potential_profit": round(shares - actual_cost, 4),
        "open_price": open_px,
        "price_at_trade": spot,
        "price_delta": round(delta, 2),
        "resolved": False,
    }

    s["pending"].append(trade)
    s["trades"] += 1
    s["bankroll"] -= actual_cost

    log_trade(trade)

    print(f"\n  >>> LIVE TRADE [{strat_name}] {asset} {side} @ ${ask_price:.3f}")
    print(f"      Order: {order_id[:16]}...")
    print(f"      Bet: ${actual_cost:.2f} | Shares: {shares} | "
          f"Profit if win: ${shares - actual_cost:.2f}")
    print(f"      {asset} open: ${open_px:,.2f} | now: ${spot:,.2f} | "
          f"delta: ${delta:+,.2f}")
    print(f"      Bankroll: ${s['bankroll']:,.2f} | "
          f"Trades: {s['trades']}")


# ═══════════════════════════════════════════════════════════════
# STRATEGY LOGIC
# ═══════════════════════════════════════════════════════════════

def check_s1(state, ctx, asset, up_ask, dn_ask):
    """Strategy 1: Enter $0.85-$0.95 when ML prob > ask + 3% edge."""
    s = state["s1"]
    if s["bankroll"] < BET_SIZE or s["bankroll"] < KILL_SWITCH_MIN:
        return

    ens_prob = estimate_probability(asset)

    # UP side in range — require ML edge
    if (MIN_ASK <= up_ask <= MAX_ASK
            and (asset, "UP") not in ctx.s1_traded):
        edge = ens_prob - up_ask
        if edge >= MIN_EDGE:
            print(f"  [EDGE] S1 {asset} UP: ML={ens_prob:.3f} ask={up_ask:.3f} edge={edge:.3f}")
            execute_trade(state, "s1", ctx, asset, "UP", up_ask)
            ctx.s1_traded.add((asset, "UP"))
            return

    # DOWN side in range — require ML edge
    if (MIN_ASK <= dn_ask <= MAX_ASK
            and (asset, "DOWN") not in ctx.s1_traded):
        dn_prob = 1 - ens_prob
        edge = dn_prob - dn_ask
        if edge >= MIN_EDGE:
            print(f"  [EDGE] S1 {asset} DOWN: ML={dn_prob:.3f} ask={dn_ask:.3f} edge={edge:.3f}")
            execute_trade(state, "s1", ctx, asset, "DOWN", dn_ask)
            ctx.s1_traded.add((asset, "DOWN"))


def check_s2(state, ctx, up_ask, dn_ask, btc_now):
    """Strategy 2: BTC $25+ confirmation + ML edge at $0.85-$0.95."""
    s = state["s2"]
    if s["bankroll"] < BET_SIZE or s["bankroll"] < KILL_SWITCH_MIN:
        return
    btc_open = ctx.open_prices.get("BTC", 0)
    if btc_open <= 0 or btc_now <= 0:
        return

    delta = btc_now - btc_open
    if abs(delta) < BTC_BUFFER:
        return

    ens_prob = estimate_probability("BTC")

    # BTC confirms UP + ask in range + ML edge
    if delta > 0 and MIN_ASK <= up_ask <= MAX_ASK:
        edge = ens_prob - up_ask
        if edge >= MIN_EDGE:
            execute_trade(state, "s2", ctx, "BTC", "UP", up_ask)
            ctx.s2_traded = True
    # BTC confirms DOWN + ask in range + ML edge
    elif delta < 0 and MIN_ASK <= dn_ask <= MAX_ASK:
        dn_prob = 1 - ens_prob
        edge = dn_prob - dn_ask
        if edge >= MIN_EDGE:
            execute_trade(state, "s2", ctx, "BTC", "DOWN", dn_ask)
            ctx.s2_traded = True


# ═══════════════════════════════════════════════════════════════
# RESOLUTION
# ═══════════════════════════════════════════════════════════════

def resolve_trades(state, now):
    """Resolve trades from completed windows (15s buffer after close)."""
    for sk in ["s1", "s2"]:
        s = state[sk]
        still_pending = []

        for t in s["pending"]:
            window_end = t["window_ts"] + WINDOW_SECS
            if now < window_end + 15:
                still_pending.append(t)
                continue

            binance_sym = BINANCE_MAP[t["asset"]]
            exit_px = get_binance_price(binance_sym)
            if exit_px <= 0:
                still_pending.append(t)
                continue

            open_px = t["open_price"]
            went_up = exit_px > open_px

            if t["side"] == "UP":
                won = went_up
            else:
                won = not went_up

            if won:
                payout = t["shares"] * 1.0
                pnl = payout - t["bet_size"]
                s["bankroll"] += payout
                s["wins"] += 1
            else:
                pnl = -t["bet_size"]
                s["losses"] += 1

            s["pnl"] += pnl

            t["resolved"] = True
            t["exit_price"] = exit_px
            t["won"] = won
            t["pnl"] = round(pnl, 4)
            t["bankroll_after"] = round(s["bankroll"], 2)
            log_trade(t)

            strat_name = "S1" if sk == "s1" else "S2"
            mark = "WIN" if won else "LOSS"
            w, l = s["wins"], s["losses"]
            wr = w / max(w + l, 1)
            print(f"\n  {'>>>' if won else 'XXX'} RESOLVED [{strat_name}]: "
                  f"{t['asset']} {t['side']} → {mark}")
            print(f"      Open: ${open_px:,.2f} → Exit: ${exit_px:,.2f}")
            print(f"      PnL: ${pnl:+.4f} | Bank: ${s['bankroll']:,.2f} | "
                  f"{w}W-{l}L ({wr:.1%})")

        s["pending"] = still_pending


# ═══════════════════════════════════════════════════════════════
# DISPLAY
# ═══════════════════════════════════════════════════════════════

def print_banner():
    print("=" * 70)
    print("  SCALP BOT v4 — ML EDGE at $0.85-$0.95")
    print("  S1: ML Edge (BTC/ETH/SOL/XRP)  |  S2: ML Edge+BTC Confirm")
    print(f"  Bet: ${BET_SIZE:.2f} fixed  |  Ask: ${MIN_ASK:.2f}-${MAX_ASK:.2f}  |  "
          f"Min edge: {MIN_EDGE:.0%}")
    print(f"  S1: last {S1_LEAD}s  |  S2: last {S2_LEAD}s  |  "
          f"Ensemble: {ENSEMBLE_ML_W:.0%} ML + {ENSEMBLE_H_W:.0%} Heuristic")
    print(f"  Bankroll: ${STARTING_BANKROLL:,.2f} / strategy")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)


def print_dashboard(state):
    print(f"\n{'=' * 70}")
    print(f"  DASHBOARD  |  {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}  |  "
          f"Windows: {state['windows']}")
    print(f"{'=' * 70}")
    print(f"  {'Strategy':<22} {'Bank':>10} {'PnL':>10} "
          f"{'W':>4} {'L':>4} {'WR':>7} {'Trades':>7}")
    print(f"  {'-'*66}")

    for key, name in [("s1", "S1: Last 15s"), ("s2", "S2: 30s+BTC Confirm")]:
        s = state[key]
        w, l = s["wins"], s["losses"]
        wr = w / max(w + l, 1)
        print(f"  {name:<22} ${s['bankroll']:>9,.2f} ${s['pnl']:>+9,.2f} "
              f"{w:>4} {l:>4} {wr:>6.1%} {s['trades']:>7}")

    tb = state["s1"]["bankroll"] + state["s2"]["bankroll"]
    tp = state["s1"]["pnl"] + state["s2"]["pnl"]
    print(f"  {'COMBINED':<22} ${tb:>9,.2f} ${tp:>+9,.2f}")

    s1_pend = len(state["s1"]["pending"])
    s2_pend = len(state["s2"]["pending"])
    print(f"  Pending: S1={s1_pend}, S2={s2_pend}")
    print(f"{'=' * 70}")


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    init_log()
    state = load_state()
    print_banner()
    init_models()

    current_window_ts = None
    ctx = None

    while True:
        try:
            now = time.time()
            window_ts = int(now // 300) * 300
            window_end = window_ts + 300
            secs_left = window_end - now

            # ── New window detected ──
            if window_ts != current_window_ts:
                if ctx is not None:
                    state["windows"] += 1
                    print_dashboard(state)
                    save_state(state)
                    redeem_positions()
                    retrain_models(state["windows"])

                current_window_ts = window_ts
                ctx = WindowCtx(window_ts=window_ts, window_end=window_end)

                # Refresh feature cache at window open (~270s before scalp zone)
                refresh_features()

                # Capture open prices and discover markets for all assets
                for asset in S1_ASSETS:
                    sym = BINANCE_MAP[asset]
                    ctx.open_prices[asset] = get_binance_price(sym)
                    mkt = discover_market(asset, window_ts)
                    if mkt:
                        ctx.markets[asset] = mkt

                found = list(ctx.markets.keys())
                prices_str = "  ".join(f"{a}=${ctx.open_prices.get(a, 0):,.2f}" for a in found)
                print(f"\n  [WINDOW {window_ts}] {prices_str}  Markets: {found}")
                print(f"  [WINDOW] Scalp zone in {secs_left - S2_LEAD:.0f}s  "
                      f"(close at {datetime.fromtimestamp(window_end, tz=timezone.utc).strftime('%H:%M:%S')} UTC)")

            # ── Resolve completed trades ──
            resolve_trades(state, now)

            # ── Not in scalp zone yet → sleep ──
            if secs_left > S2_LEAD + 1:
                sleep_dur = min(secs_left - S2_LEAD - 0.5, 30)
                time.sleep(max(sleep_dur, 1))
                continue

            # ── SCALP ZONE: poll every 1.5s ──
            poll_parts = [f"  [{secs_left:5.1f}s]"]

            for asset in S1_ASSETS:
                if asset not in ctx.markets:
                    continue
                mkt = ctx.markets[asset]

                up_ask = get_ask(mkt["up_token"])
                dn_ask = get_ask(mkt["down_token"])
                poll_parts.append(f"{asset}: U={up_ask:.3f} D={dn_ask:.3f}")

                # Strategy 2: 30s + BTC confirmation (BTC only)
                if (secs_left <= S2_LEAD and asset in S2_ASSETS
                        and not ctx.s2_traded):
                    btc_now = get_binance_price("BTCUSDT")
                    check_s2(state, ctx, up_ask, dn_ask, btc_now)

                # Strategy 1: Last 15 seconds (BTC + ETH)
                if (secs_left <= S1_LEAD and asset in S1_ASSETS):
                    check_s1(state, ctx, asset, up_ask, dn_ask)

            print(" | ".join(poll_parts))
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n[SHUTDOWN] Saving state...")
            save_state(state)
            print_dashboard(state)
            break
        except Exception as e:
            print(f"\n[ERROR] {e}")
            traceback.print_exc()
            save_state(state)
            time.sleep(5)


if __name__ == "__main__":
    run()
