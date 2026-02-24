#!/usr/bin/env python3
"""
SCALP BOT v6 — MOMENTUM + CALIBRATION + CROSS-ASSET
=======================================================================

Three signal strategies (no ML model):

  A) PRICE MOMENTUM — Track Polymarket ask trajectory during the window.
     When a side's ask is rising fast and lands in $0.60-$0.90, buy.
     Edge: market-maker quotes lag the actual trend.

  B) HISTORICAL CALIBRATION — Log every poll (ask, secs_left, outcome).
     Build empirical table: P(win | ask, time_left).
     Trade when empirical_WR > ask + 2%.
     Needs data collection phase first — auto-learns as it runs.

  C) CROSS-ASSET LAG — BTC moves first, SOL/XRP follow.
     When BTC Binance price moves strongly mid-window but SOL/XRP
     Polymarket asks are still cheap, buy the lagging asset.

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
from collections import defaultdict

import numpy as np

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from live_quant import (
    discover_market, get_binance_price,
    CLOB_API, BINANCE_MAP,
)
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
STATE_VERSION = 7

PAPER_TRADE = True            # True = simulate fills, False = real CLOB orders
PAPER_BANKROLL = 1000.00      # Paper trading bankroll

# Ask range — we'll buy when ask is in this range
MIN_ASK = 0.60
MAX_ASK = 0.90

BET_SIZE = 5.00
POLL_INTERVAL = 2.5       # seconds between polls
WINDOW_SECS = 300
SCALP_LEAD = 90           # start polling 90s before close (need history)

ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# Signal A: Momentum thresholds
MOMENTUM_LOOKBACK = 20.0      # seconds of price history to measure velocity
MOMENTUM_MIN_RISE = 0.12      # ask must rise by at least $0.12 in lookback period
MOMENTUM_MIN_ASK = 0.60       # momentum fires in this range
MOMENTUM_MAX_ASK = 0.88       # don't buy above 0.88 on momentum alone

# Signal B: Calibration thresholds
CALIB_FILE = "data/calibration.csv"
CALIB_MIN_SAMPLES = 30        # need N samples before trusting empirical WR
CALIB_MIN_EDGE = 0.02         # empirical WR must beat ask by 2%

# Signal C: Cross-asset lag
CROSS_ASSET_BTC_MOVE = 0.10   # BTC must move 0.10% in window (5-min relative)
CROSS_ASSET_MAX_ASK = 0.85    # target asset ask must be cheap
CROSS_ASSET_ASSETS = ["SOL", "XRP"]  # assets that lag BTC

# Risk management
STARTING_BANKROLL = PAPER_BANKROLL if PAPER_TRADE else 30.85
KILL_SWITCH_MIN = 5.00
MAX_TRADES_PER_WINDOW = 2     # cap trades per window to limit exposure

LOG_FILE = "data/scalp_trades.csv"
STATE_FILE = "data/scalp_state.json"

LOG_FIELDS = [
    "strategy", "timestamp", "window_ts", "asset", "side",
    "ask_price", "bet_size", "shares", "potential_profit",
    "open_price", "price_at_trade", "price_delta",
    "resolved", "exit_price", "won", "pnl", "bankroll_after",
    "signal",
]


# ═══════════════════════════════════════════════════════════════
# CALIBRATION TABLE (Signal B)
# ═══════════════════════════════════════════════════════════════

_calib_data = []  # list of dicts loaded from CALIB_FILE

def load_calibration():
    """Load historical calibration data from CSV."""
    global _calib_data
    _calib_data = []
    if not os.path.exists(CALIB_FILE):
        return
    try:
        with open(CALIB_FILE, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                _calib_data.append(row)
        print(f"  [CALIB] Loaded {len(_calib_data)} historical observations")
    except Exception as e:
        print(f"  [CALIB] Load failed: {e}")


def save_calibration_obs(window_ts, asset, side, ask_price, secs_left, won):
    """Append one observation to calibration CSV."""
    os.makedirs("data", exist_ok=True)
    fields = ["window_ts", "asset", "side", "ask_price", "secs_left", "won"]
    write_header = not os.path.exists(CALIB_FILE)
    try:
        with open(CALIB_FILE, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if write_header:
                w.writeheader()
            w.writerow({
                "window_ts": window_ts,
                "asset": asset,
                "side": side,
                "ask_price": round(ask_price, 3),
                "secs_left": round(secs_left, 0),
                "won": int(won),
            })
    except Exception as e:
        print(f"  [CALIB] Save failed: {e}")


def get_calibration_edge(ask_price, secs_left):
    """
    Look up empirical win rate for (ask_bucket, time_bucket).
    Returns (empirical_wr, sample_count, edge) or None if insufficient data.

    Buckets:
      ask: $0.05 wide (e.g., 0.80-0.85)
      time: 15s wide (e.g., 30-45s left)
    """
    if not _calib_data:
        return None

    # Bucket the query
    ask_lo = (int(ask_price * 20) / 20)      # round down to nearest 0.05
    ask_hi = ask_lo + 0.05
    time_lo = (int(secs_left) // 15) * 15
    time_hi = time_lo + 15

    # Find matching observations
    wins = 0
    total = 0
    for obs in _calib_data:
        try:
            obs_ask = float(obs["ask_price"])
            obs_time = float(obs["secs_left"])
            obs_won = int(obs["won"])
        except (ValueError, KeyError):
            continue

        if ask_lo <= obs_ask < ask_hi and time_lo <= obs_time < time_hi:
            total += 1
            wins += obs_won

    if total < CALIB_MIN_SAMPLES:
        return None

    emp_wr = wins / total
    edge = emp_wr - ask_price
    return (emp_wr, total, edge)


# ═══════════════════════════════════════════════════════════════
# PRICE TRACKER (Signal A — momentum)
# ═══════════════════════════════════════════════════════════════

class PriceTracker:
    """Track ask price history per (asset, side) during a window."""

    def __init__(self):
        # {(asset, side): [(timestamp, ask_price), ...]}
        self.history = defaultdict(list)

    def record(self, asset, side, ask_price, ts=None):
        if ts is None:
            ts = time.time()
        self.history[(asset, side)].append((ts, ask_price))

    def get_velocity(self, asset, side, lookback_secs=MOMENTUM_LOOKBACK):
        """
        Price velocity: how much did the ask rise over the lookback period?
        Returns (rise, duration) or (0, 0) if insufficient history.
        """
        pts = self.history.get((asset, side), [])
        if len(pts) < 3:
            return (0.0, 0.0)

        now_ts, now_price = pts[-1]
        cutoff = now_ts - lookback_secs

        # Find the oldest point within the lookback window
        old_price = now_price
        old_ts = now_ts
        for ts, px in pts:
            if ts >= cutoff:
                old_price = px
                old_ts = ts
                break

        duration = now_ts - old_ts
        if duration < 5:  # need at least 5 seconds of history
            return (0.0, 0.0)

        rise = now_price - old_price
        return (rise, duration)

    def get_min_ask(self, asset, side, lookback_secs=60.0):
        """Lowest ask seen in the lookback period."""
        pts = self.history.get((asset, side), [])
        if not pts:
            return 1.0
        cutoff = pts[-1][0] - lookback_secs
        relevant = [px for ts, px in pts if ts >= cutoff and px > 0]
        return min(relevant) if relevant else 1.0


# ═══════════════════════════════════════════════════════════════
# WINDOW CONTEXT
# ═══════════════════════════════════════════════════════════════

@dataclass
class WindowCtx:
    window_ts: int
    window_end: int
    open_prices: dict = field(default_factory=dict)   # {asset: binance_price}
    markets: dict = field(default_factory=dict)
    traded: set = field(default_factory=set)           # (asset, side) combos
    trade_count: int = 0
    tracker: PriceTracker = field(default_factory=PriceTracker)
    # Snapshot asks at first poll of scalp zone for calibration logging
    first_poll_asks: dict = field(default_factory=dict)  # {(asset, side): (ask, secs_left)}


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
        if data.get("version") != STATE_VERSION:
            print(f"  [STATE] Version mismatch (have {data.get('version')}, "
                  f"need {STATE_VERSION}) — resetting state")
        else:
            return data
    return {
        "version": STATE_VERSION,
        "s1": new_strategy_state(),
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
# FAST PRICE FETCHER
# ═══════════════════════════════════════════════════════════════

def get_ask(token_id: str) -> float:
    """Fetch only the ask price. Fast, single HTTP call."""
    try:
        r = requests.get(
            f"{CLOB_API}/price?token_id={token_id}&side=SELL",
            timeout=2,
        )
        return float(r.json().get("price", 0))
    except:
        return 0.0


# ═══════════════════════════════════════════════════════════════
# TRADE EXECUTION
# ═══════════════════════════════════════════════════════════════

def execute_trade(state, ctx, asset, side, ask_price, signal_name):
    """Execute trade — paper or live depending on PAPER_TRADE flag."""
    s = state["s1"]

    if s["bankroll"] < BET_SIZE or s["bankroll"] < KILL_SWITCH_MIN:
        return False

    ask_price = round(ask_price, 2)
    shares = int(BET_SIZE / ask_price)
    if shares < 1:
        return False

    binance_sym = BINANCE_MAP[asset]
    spot = get_binance_price(binance_sym)
    open_px = ctx.open_prices.get(asset, 0)
    delta = spot - open_px if open_px > 0 else 0

    mode_tag = "PAPER" if PAPER_TRADE else "LIVE"
    order_id = ""

    if PAPER_TRADE:
        # Paper trade — simulate instant fill
        order_id = f"paper-{int(time.time())}"
    else:
        # Real trade — place FOK order on CLOB
        mkt = ctx.markets[asset]
        token_id = mkt["up_token"] if side == "UP" else mkt["down_token"]
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
                print(f"\n  [NOFILL] {asset} {side} @ ${ask_price:.3f} "
                      f"({signal_name}) — {resp}")
                return False
            order_id = resp.get("orderID", "")
        except Exception as e:
            print(f"\n  [ERROR] Order failed: {e}")
            return False

    actual_cost = round(shares * ask_price, 2)
    trade = {
        "strategy": "s1",
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
        "signal": signal_name,
    }

    s["pending"].append(trade)
    s["trades"] += 1
    s["bankroll"] -= actual_cost
    ctx.traded.add((asset, side))
    ctx.trade_count += 1

    log_trade(trade)

    print(f"\n  >>> {mode_tag} TRADE [{signal_name}] {asset} {side} @ ${ask_price:.3f}")
    print(f"      Bet: ${actual_cost:.2f} | Shares: {shares} | "
          f"Profit if win: ${shares - actual_cost:.2f}")
    print(f"      {asset} open: ${open_px:,.2f} | now: ${spot:,.2f} | "
          f"delta: ${delta:+,.2f}")
    print(f"      Bankroll: ${s['bankroll']:,.2f} | Trades: {s['trades']}")
    return True


# ═══════════════════════════════════════════════════════════════
# SIGNAL A: POLYMARKET PRICE MOMENTUM
# ═══════════════════════════════════════════════════════════════

def check_momentum(state, ctx, asset, side, ask_price, secs_left):
    """
    Buy when the ask is rising rapidly — the market is trending but
    hasn't fully priced in the move yet.

    Trigger: ask rose >= $0.12 over last 20 seconds, current ask in range.
    """
    if (asset, side) in ctx.traded:
        return False
    # Never trade both sides of the same asset in one window
    opp = "DOWN" if side == "UP" else "UP"
    if (asset, opp) in ctx.traded:
        return False
    if ctx.trade_count >= MAX_TRADES_PER_WINDOW:
        return False
    if not (MOMENTUM_MIN_ASK <= ask_price <= MOMENTUM_MAX_ASK):
        return False

    rise, duration = ctx.tracker.get_velocity(asset, side)
    if rise < MOMENTUM_MIN_RISE:
        return False

    # Extra check: ask was below 0.50 recently (confirms it was uncertain)
    min_ask = ctx.tracker.get_min_ask(asset, side, lookback_secs=40.0)
    if min_ask > 0.55:
        return False  # market was already decided, not a real momentum move

    vel_per_sec = rise / max(duration, 1)
    print(f"  [SIG-A] MOMENTUM {asset} {side}: ask={ask_price:.3f} "
          f"rise={rise:+.3f} over {duration:.0f}s (${vel_per_sec:.4f}/s) "
          f"min_ask={min_ask:.3f}")

    return execute_trade(state, ctx, asset, side, ask_price, "MOMENTUM")


# ═══════════════════════════════════════════════════════════════
# SIGNAL B: HISTORICAL CALIBRATION
# ═══════════════════════════════════════════════════════════════

def check_calibration(state, ctx, asset, side, ask_price, secs_left):
    """
    Buy when empirical win rate at this (ask, time) bucket exceeds
    the ask price by our minimum edge.
    """
    if (asset, side) in ctx.traded:
        return False
    opp = "DOWN" if side == "UP" else "UP"
    if (asset, opp) in ctx.traded:
        return False
    if ctx.trade_count >= MAX_TRADES_PER_WINDOW:
        return False
    if not (MIN_ASK <= ask_price <= MAX_ASK):
        return False

    result = get_calibration_edge(ask_price, secs_left)
    if result is None:
        return False

    emp_wr, n_samples, edge = result
    if edge < CALIB_MIN_EDGE:
        return False

    print(f"  [SIG-B] CALIBRATION {asset} {side}: ask={ask_price:.3f} "
          f"emp_wr={emp_wr:.1%} (n={n_samples}) edge={edge:+.3f}")

    return execute_trade(state, ctx, asset, side, ask_price, "CALIBRATION")


# ═══════════════════════════════════════════════════════════════
# SIGNAL C: CROSS-ASSET LAG
# ═══════════════════════════════════════════════════════════════

def check_cross_asset(state, ctx, asset, up_ask, dn_ask, secs_left):
    """
    When BTC moves strongly in one direction but SOL/XRP Polymarket
    asks are still cheap, buy the lagging asset's corresponding side.
    """
    if asset not in CROSS_ASSET_ASSETS:
        return False
    if ctx.trade_count >= MAX_TRADES_PER_WINDOW:
        return False

    btc_open = ctx.open_prices.get("BTC", 0)
    if btc_open <= 0:
        return False

    btc_now = get_binance_price("BTCUSDT")
    if btc_now <= 0:
        return False

    btc_pct_move = (btc_now - btc_open) / btc_open * 100

    if abs(btc_pct_move) < CROSS_ASSET_BTC_MOVE:
        return False

    # BTC is pumping → buy lagging asset's UP side
    if btc_pct_move > 0 and (asset, "UP") not in ctx.traded and (asset, "DOWN") not in ctx.traded:
        if MIN_ASK <= up_ask <= CROSS_ASSET_MAX_ASK:
            print(f"  [SIG-C] CROSS-ASSET {asset} UP: btc_move={btc_pct_move:+.2f}% "
                  f"ask={up_ask:.3f} (lagging)")
            return execute_trade(state, ctx, asset, "UP", up_ask, "CROSS-ASSET")

    # BTC is dumping → buy lagging asset's DOWN side
    if btc_pct_move < 0 and (asset, "DOWN") not in ctx.traded and (asset, "UP") not in ctx.traded:
        if MIN_ASK <= dn_ask <= CROSS_ASSET_MAX_ASK:
            print(f"  [SIG-C] CROSS-ASSET {asset} DOWN: btc_move={btc_pct_move:+.2f}% "
                  f"ask={dn_ask:.3f} (lagging)")
            return execute_trade(state, ctx, asset, "DOWN", dn_ask, "CROSS-ASSET")

    return False


# ═══════════════════════════════════════════════════════════════
# RESOLUTION + CALIBRATION LOGGING
# ═══════════════════════════════════════════════════════════════

def resolve_trades(state, now):
    """Resolve trades and log calibration data."""
    s = state["s1"]
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

        mark = "WIN" if won else "LOSS"
        w, l = s["wins"], s["losses"]
        wr = w / max(w + l, 1)
        print(f"\n  {'>>>' if won else 'XXX'} RESOLVED: "
              f"{t['asset']} {t['side']} [{t.get('signal', '?')}] → {mark}")
        print(f"      Open: ${open_px:,.2f} → Exit: ${exit_px:,.2f}")
        print(f"      PnL: ${pnl:+.4f} | Bank: ${s['bankroll']:,.2f} | "
              f"{w}W-{l}L ({wr:.1%})")

    s["pending"] = still_pending


def log_calibration_for_window(ctx):
    """
    After a window closes, log calibration observations.
    For each (asset, side) where we had first-poll data, determine
    whether that side actually won, and save the observation.
    """
    for (asset, side), (ask, secs_left) in ctx.first_poll_asks.items():
        if ask <= 0 or ask >= 1.0:
            continue
        # Determine actual outcome from Binance
        open_px = ctx.open_prices.get(asset, 0)
        if open_px <= 0:
            continue
        exit_px = get_binance_price(BINANCE_MAP[asset])
        if exit_px <= 0:
            continue

        went_up = exit_px > open_px
        if side == "UP":
            won = went_up
        else:
            won = not went_up

        save_calibration_obs(ctx.window_ts, asset, side, ask, secs_left, won)


# ═══════════════════════════════════════════════════════════════
# DISPLAY
# ═══════════════════════════════════════════════════════════════

def print_banner():
    calib_count = len(_calib_data)
    mode = "PAPER TRADING" if PAPER_TRADE else "LIVE TRADING"
    print("=" * 70)
    print(f"  SCALP BOT v6 — MOMENTUM + CALIBRATION + CROSS-ASSET  [{mode}]")
    print(f"  Assets: {', '.join(ASSETS)}")
    print(f"  Bet: ${BET_SIZE:.2f} fixed  |  Ask range: ${MIN_ASK:.2f}-${MAX_ASK:.2f}")
    print(f"  Signals: A=Momentum B=Calibration({calib_count} obs) C=Cross-Asset")
    print(f"  Scalp zone: last {SCALP_LEAD}s  |  Max {MAX_TRADES_PER_WINDOW} trades/window")
    print(f"  Bankroll: ${STARTING_BANKROLL:,.2f}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)


def print_dashboard(state):
    s = state["s1"]
    w, l = s["wins"], s["losses"]
    wr = w / max(w + l, 1)
    calib_count = len(_calib_data)

    mode = "PAPER" if PAPER_TRADE else "LIVE"
    print(f"\n{'=' * 70}")
    print(f"  DASHBOARD [{mode}]  |  {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}  |  "
          f"Windows: {state['windows']}  |  Calibration: {calib_count} obs")
    print(f"{'=' * 70}")
    print(f"  Bankroll: ${s['bankroll']:>9,.2f}  |  PnL: ${s['pnl']:>+9,.2f}  |  "
          f"{w}W-{l}L ({wr:.1%})  |  Trades: {s['trades']}")
    pend = len(s["pending"])
    print(f"  Pending: {pend}")
    print(f"{'=' * 70}")


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    init_log()
    load_calibration()
    state = load_state()
    print_banner()

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
                    # Log calibration data from the completed window
                    log_calibration_for_window(ctx)
                    state["windows"] += 1
                    print_dashboard(state)
                    save_state(state)
                    if not PAPER_TRADE:
                        redeem_positions()
                    # Reload calibration data periodically
                    if state["windows"] % 20 == 0:
                        load_calibration()

                current_window_ts = window_ts
                ctx = WindowCtx(window_ts=window_ts, window_end=window_end)

                # Capture open prices and discover markets
                for asset in ASSETS:
                    sym = BINANCE_MAP[asset]
                    ctx.open_prices[asset] = get_binance_price(sym)
                    mkt = discover_market(asset, window_ts)
                    if mkt:
                        ctx.markets[asset] = mkt

                found = list(ctx.markets.keys())
                prices_str = "  ".join(f"{a}=${ctx.open_prices.get(a, 0):,.2f}" for a in found)
                print(f"\n  [WINDOW {window_ts}] {prices_str}  Markets: {found}")
                print(f"  [WINDOW] Scalp zone in {secs_left - SCALP_LEAD:.0f}s  "
                      f"(close at {datetime.fromtimestamp(window_end, tz=timezone.utc).strftime('%H:%M:%S')} UTC)")

            # ── Resolve completed trades ──
            resolve_trades(state, now)

            # ── Not in scalp zone yet → sleep ──
            if secs_left > SCALP_LEAD + 1:
                sleep_dur = min(secs_left - SCALP_LEAD - 0.5, 30)
                time.sleep(max(sleep_dur, 1))
                continue

            # ── SCALP ZONE: poll and evaluate signals ──
            poll_parts = [f"  [{secs_left:5.1f}s]"]

            for asset in ASSETS:
                if asset not in ctx.markets:
                    continue
                mkt = ctx.markets[asset]

                up_ask = get_ask(mkt["up_token"])
                dn_ask = get_ask(mkt["down_token"])
                poll_parts.append(f"{asset}: U={up_ask:.3f} D={dn_ask:.3f}")

                # Record price history for momentum tracking
                ctx.tracker.record(asset, "UP", up_ask)
                ctx.tracker.record(asset, "DOWN", dn_ask)

                # Record first poll asks for calibration logging
                if (asset, "UP") not in ctx.first_poll_asks:
                    ctx.first_poll_asks[(asset, "UP")] = (up_ask, secs_left)
                    ctx.first_poll_asks[(asset, "DOWN")] = (dn_ask, secs_left)

                # Skip if already maxed out trades this window
                if ctx.trade_count >= MAX_TRADES_PER_WINDOW:
                    continue

                # ── Signal A: Price Momentum ──
                if secs_left <= 60:  # momentum needs some history first
                    check_momentum(state, ctx, asset, "UP", up_ask, secs_left)
                    check_momentum(state, ctx, asset, "DOWN", dn_ask, secs_left)

                # ── Signal B: Historical Calibration ──
                if secs_left <= 45:
                    check_calibration(state, ctx, asset, "UP", up_ask, secs_left)
                    check_calibration(state, ctx, asset, "DOWN", dn_ask, secs_left)

                # ── Signal C: Cross-Asset Lag ──
                if secs_left <= 60:
                    check_cross_asset(state, ctx, asset, up_ask, dn_ask, secs_left)

            print(" | ".join(poll_parts))
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n[SHUTDOWN] Saving state...")
            if ctx is not None:
                log_calibration_for_window(ctx)
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
