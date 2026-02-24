#!/usr/bin/env python3
"""
EXPIRY SCALP BOT — Volume Over Win Size
========================================================================

Strategy: Scan ALL of Polymarket for events closing soon with one outcome
priced at $0.95+. Buy the near-certain outcome, collect $1 at resolution.

Thesis:
  - At $0.95 ask → 5.3% ROI per trade, need 95% WR to break even
  - At $0.97 ask → 3.1% ROI per trade, need 97% WR to break even
  - Near-expiry markets are ALREADY decided (game over, event happened)
  - Volume > win size: do 20-50 trades/day at 1-5% ROI each

LIVE TRADING with real USDC via Polymarket CLOB.
"""

import time
import csv
import os
import sys
import json
import traceback
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

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
# ON-CHAIN REDEMPTION
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
            return 0

        nonce = _w3.eth.get_transaction_count(_acct.address, "latest")
        gas_price = _w3.eth.gas_price
        redeemed = 0

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
                print(f"  [REDEEM] {p.get('outcome', '?')} {size:.2f} shares | {p.get('title', '?')[:50]}")
                nonce += 1
                redeemed += 1
            except Exception as e:
                print(f"  [REDEEM FAIL] {p.get('outcome', '?')}: {e}")
        return redeemed
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Scanning
SCAN_AHEAD_HOURS = 6          # How far ahead to look for closing markets
SCAN_INTERVAL = 60            # Seconds between full scans (faster to catch live ticks)
MIN_GAMMA_PRICE = 0.90        # Gamma pre-filter (looser — CLOB ask is the real gate)

# Trading
MIN_ASK = 0.95                # Min CLOB ask to buy at
MAX_ASK = 0.99                # Max CLOB ask (above this, ROI too thin)
BET_SIZE = 5.00               # Fixed bet per trade
MAX_SPREAD = 0.05             # Skip if spread > 5 cents
MIN_LIQUIDITY = 1000          # Minimum gamma liquidity in USD
MIN_BOOK_DEPTH = 10.0         # Min $ available at ask in orderbook

# Live watch: markets not yet at $0.95 gamma, but we watch CLOB live
WATCH_GAMMA_MIN = 0.88        # Pre-filter for watchlist (might tick up to $0.95)
WATCH_AHEAD_HOURS = 3         # Shorter window for live watch

# Risk
STARTING_BANKROLL = 30.00     # Starting capital for this strategy
KILL_SWITCH_MIN = 5.00        # Stop if bankroll drops below
MAX_PENDING = 50              # Max simultaneous pending positions
MAX_DAILY_TRADES = 100        # Circuit breaker

# Files
STATE_FILE = "data/expiry_state.json"
LOG_FILE = "data/expiry_trades.csv"

LOG_FIELDS = [
    "timestamp", "question", "outcome", "end_date",
    "gamma_price", "clob_ask", "clob_bid", "spread", "roi_pct",
    "bet_size", "shares", "potential_profit",
    "token_id", "condition_id", "order_id",
    "resolved", "won", "pnl", "bankroll_after",
    "neg_risk",
]


# ═══════════════════════════════════════════════════════════════
# STATE & LOGGING
# ═══════════════════════════════════════════════════════════════

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        if data.get("version") == 1:
            return data
    return {
        "version": 1,
        "bankroll": STARTING_BANKROLL,
        "pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "trades": 0,
        "daily_trades": 0,
        "last_trade_date": "",
        "pending": [],
        "traded_tokens": [],  # tokens already bought (avoid dupes)
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
# MARKET SCANNER
# ═══════════════════════════════════════════════════════════════

def scan_markets() -> list:
    """
    Scan Polymarket for active markets closing within SCAN_AHEAD_HOURS
    with at least one outcome priced at MIN_GAMMA_PRICE or higher.
    Returns list of candidate dicts sorted by end_date (soonest first).
    """
    now = datetime.now(timezone.utc)
    future = now + timedelta(hours=SCAN_AHEAD_HOURS)

    params = {
        "active": True,
        "closed": False,
        "end_date_min": now.isoformat(),
        "end_date_max": future.isoformat(),
        "limit": 100,
    }

    all_markets = []
    offset = 0
    while True:
        params["offset"] = offset
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets", params=params, timeout=15
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        except Exception as e:
            print(f"  [WARN] Gamma scan failed at offset {offset}: {e}")
            break

    # Filter for high-confidence outcomes
    candidates = []
    for m in all_markets:
        if not m.get("acceptingOrders", False):
            continue

        liq = float(m.get("liquidityNum", 0) or 0)
        if liq < MIN_LIQUIDITY:
            continue

        prices = json.loads(m.get("outcomePrices", "[]"))
        outcomes = json.loads(m.get("outcomes", "[]"))
        tokens = json.loads(m.get("clobTokenIds", "[]"))

        for i, p_str in enumerate(prices):
            p = float(p_str)
            if p >= MIN_GAMMA_PRICE and i < len(tokens):
                candidates.append({
                    "question": m.get("question", ""),
                    "outcome": outcomes[i] if i < len(outcomes) else "?",
                    "end_date": m.get("endDate", ""),
                    "gamma_price": p,
                    "token_id": tokens[i],
                    "condition_id": m.get("conditionId", ""),
                    "liquidity": liq,
                    "neg_risk": m.get("negRisk", False),
                    "min_order_size": m.get("orderMinSize", 5),
                })

    # Sort by end_date (soonest first) — we want the ones about to close
    candidates.sort(key=lambda x: x["end_date"])
    return candidates


def get_clob_prices(token_id: str) -> dict:
    """Get real bid/ask from CLOB. Returns {"bid": float, "ask": float}."""
    result = {"bid": 0.0, "ask": 0.0}
    try:
        ask_r = requests.get(
            f"{CLOB_API}/price?token_id={token_id}&side=SELL", timeout=3
        )
        result["ask"] = float(ask_r.json().get("price", 0))

        bid_r = requests.get(
            f"{CLOB_API}/price?token_id={token_id}&side=BUY", timeout=3
        )
        result["bid"] = float(bid_r.json().get("price", 0))
    except Exception:
        pass
    return result


def check_book_depth(token_id: str, side: str = "asks") -> float:
    """
    Check orderbook depth — how many $ available near the best price.
    Returns total $ on the given side of the book within 2 cents of best.
    This catches thin/illiquid books where the gamma liquidity number lies.
    """
    try:
        r = requests.get(
            f"{CLOB_API}/book?token_id={token_id}", timeout=5
        )
        book = r.json()
        orders = book.get(side, [])
        if not orders:
            return 0.0

        # Sum size * price for orders within 2 cents of best
        best_price = float(orders[0]["price"])
        total = 0.0
        for o in orders:
            price = float(o["price"])
            size = float(o["size"])
            if abs(price - best_price) <= 0.02:
                total += size * price
        return total
    except Exception:
        return 0.0


def scan_watchlist() -> list:
    """
    Scan for markets NOT YET at $0.95 gamma but close (0.88-0.95).
    These might tick up to $0.95+ live on CLOB even if gamma hasn't updated.
    Shorter lookahead — focus on markets closing soon where the outcome
    is becoming clearer (e.g., halftime in a blowout game).
    """
    now = datetime.now(timezone.utc)
    future = now + timedelta(hours=WATCH_AHEAD_HOURS)

    params = {
        "active": True,
        "closed": False,
        "end_date_min": now.isoformat(),
        "end_date_max": future.isoformat(),
        "limit": 100,
    }

    all_markets = []
    offset = 0
    while True:
        params["offset"] = offset
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets", params=params, timeout=15
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        except Exception:
            break

    watchlist = []
    for m in all_markets:
        if not m.get("acceptingOrders", False):
            continue
        liq = float(m.get("liquidityNum", 0) or 0)
        if liq < MIN_LIQUIDITY:
            continue

        prices = json.loads(m.get("outcomePrices", "[]"))
        outcomes = json.loads(m.get("outcomes", "[]"))
        tokens = json.loads(m.get("clobTokenIds", "[]"))

        for i, p_str in enumerate(prices):
            p = float(p_str)
            # Between WATCH_GAMMA_MIN and MIN_GAMMA_PRICE — not in main scan
            # but might be at $0.95+ on CLOB right now
            if WATCH_GAMMA_MIN <= p < MIN_GAMMA_PRICE and i < len(tokens):
                watchlist.append({
                    "question": m.get("question", ""),
                    "outcome": outcomes[i] if i < len(outcomes) else "?",
                    "end_date": m.get("endDate", ""),
                    "gamma_price": p,
                    "token_id": tokens[i],
                    "condition_id": m.get("conditionId", ""),
                    "liquidity": liq,
                    "neg_risk": m.get("negRisk", False),
                    "min_order_size": m.get("orderMinSize", 5),
                })

    watchlist.sort(key=lambda x: (-x["gamma_price"], x["end_date"]))
    return watchlist


# ═══════════════════════════════════════════════════════════════
# TRADE EXECUTION
# ═══════════════════════════════════════════════════════════════

def execute_trade(state, candidate, clob_ask, clob_bid):
    """Place a real FOK buy order on the high-confidence outcome."""
    ask_price = round(clob_ask, 2)
    shares = int(BET_SIZE / ask_price)
    if shares < 1:
        return False

    token_id = candidate["token_id"]
    actual_cost = round(shares * ask_price, 2)
    spread = round(clob_ask - clob_bid, 3)
    roi_pct = round((1 - ask_price) / ask_price * 100, 2)

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
            print(f"  [NOFILL] {candidate['outcome']} @ ${ask_price:.2f} "
                  f"— {candidate['question'][:50]}")
            return False
    except Exception as e:
        print(f"  [ERROR] Order failed: {e}")
        return False

    order_id = resp.get("orderID", "")

    trade = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "question": candidate["question"][:100],
        "outcome": candidate["outcome"],
        "end_date": candidate["end_date"],
        "gamma_price": candidate["gamma_price"],
        "clob_ask": ask_price,
        "clob_bid": clob_bid,
        "spread": spread,
        "roi_pct": roi_pct,
        "bet_size": actual_cost,
        "shares": shares,
        "potential_profit": round(shares - actual_cost, 4),
        "token_id": token_id,
        "condition_id": candidate["condition_id"],
        "order_id": order_id,
        "resolved": False,
        "neg_risk": candidate["neg_risk"],
    }

    state["pending"].append(trade)
    state["trades"] += 1
    state["daily_trades"] += 1
    state["bankroll"] -= actual_cost
    state["traded_tokens"].append(token_id)

    # Keep traded_tokens bounded — but protect pending trade tokens
    if len(state["traded_tokens"]) > 500:
        pending_tokens = {t["token_id"] for t in state["pending"]}
        keep = [t for t in state["traded_tokens"] if t in pending_tokens]
        non_pending = [t for t in state["traded_tokens"] if t not in pending_tokens]
        state["traded_tokens"] = keep + non_pending[-250:]

    # Persist immediately — real money was just spent
    save_state(state)

    log_trade(trade)

    print(f"\n  >>> TRADE: {candidate['outcome']} @ ${ask_price:.2f} "
          f"({roi_pct:.1f}% ROI)")
    print(f"      {candidate['question'][:60]}")
    print(f"      Order: {order_id[:16]}... | "
          f"${actual_cost:.2f} for {shares} shares | "
          f"Profit if win: ${shares - actual_cost:.2f}")
    print(f"      Closes: {candidate['end_date'][:16]} | "
          f"Spread: ${spread:.3f} | "
          f"Bankroll: ${state['bankroll']:.2f}")

    return True


# ═══════════════════════════════════════════════════════════════
# RESOLUTION
# ═══════════════════════════════════════════════════════════════

def resolve_trades(state):
    """
    Check pending trades for resolution by querying wallet positions.
    Markets resolve via Polymarket's oracle — we check the data API.
    """
    if not state["pending"]:
        return

    try:
        wallet = os.environ["POLYMARKET_WALLET"]
        positions = requests.get(
            f"https://data-api.polymarket.com/positions?user={wallet}",
            timeout=15,
        ).json()
    except Exception as e:
        print(f"  [WARN] Data API unavailable for resolution: {e}")
        return

    # Build lookup: token_id -> position data (not conditionId, which
    # can collide if wallet holds both sides of the same condition)
    pos_map = {}
    for p in positions:
        tid = p.get("asset", "") or p.get("tokenId", "")
        if tid:
            pos_map[tid] = p

    still_pending = []
    for t in state["pending"]:
        tid = t.get("token_id", "")
        pos = pos_map.get(tid)

        # Check if resolved (redeemable or curValue indicates settlement)
        if pos and pos.get("redeemable"):
            # Market resolved and we can redeem
            won = float(pos.get("curValue", 0)) > 0
            if won:
                payout = t["shares"] * 1.0
                pnl = payout - t["bet_size"]
                state["bankroll"] += payout
                state["wins"] += 1
            else:
                pnl = -t["bet_size"]
                state["losses"] += 1

            state["pnl"] += pnl

            t["resolved"] = True
            t["won"] = won
            t["pnl"] = round(pnl, 4)
            t["bankroll_after"] = round(state["bankroll"], 2)
            log_trade(t)

            mark = "WIN" if won else "LOSS"
            w, l = state["wins"], state["losses"]
            wr = w / max(w + l, 1)
            print(f"\n  {'>>>' if won else 'XXX'} RESOLVED: {t['outcome']} → {mark}")
            print(f"      {t['question'][:50]}")
            print(f"      PnL: ${pnl:+.4f} | Bank: ${state['bankroll']:.2f} | "
                  f"{w}W-{l}L ({wr:.1%})")
        else:
            # Check if market has been closed for a while (stale)
            try:
                end = datetime.fromisoformat(t["end_date"].replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - end).total_seconds()
                if age > 259200:  # > 72h past end date — oracle may be slow
                    print(f"  [STALE] Marking as loss (>72h): {t['question'][:50]}")
                    pnl = -t["bet_size"]
                    state["losses"] += 1
                    state["pnl"] += pnl
                    t["resolved"] = True
                    t["won"] = False
                    t["pnl"] = round(pnl, 4)
                    t["bankroll_after"] = round(state["bankroll"], 2)
                    log_trade(t)
                    continue
            except Exception:
                pass
            still_pending.append(t)

    state["pending"] = still_pending


# ═══════════════════════════════════════════════════════════════
# DISPLAY
# ═══════════════════════════════════════════════════════════════

def print_banner():
    print("=" * 70)
    print("  EXPIRY SCALP BOT — Volume Over Win Size")
    print(f"  Scan: every {SCAN_INTERVAL}s | "
          f"Lookahead: {SCAN_AHEAD_HOURS}h | "
          f"Ask: ${MIN_ASK:.2f}-${MAX_ASK:.2f}")
    print(f"  Bet: ${BET_SIZE:.2f} fixed | "
          f"Max spread: ${MAX_SPREAD:.2f} | "
          f"Min liq: ${MIN_LIQUIDITY:,.0f}")
    print(f"  Bankroll: ${STARTING_BANKROLL:.2f} | "
          f"Max pending: {MAX_PENDING} | "
          f"Max daily: {MAX_DAILY_TRADES}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)


def print_dashboard(state, candidates_count=0):
    w, l = state["wins"], state["losses"]
    wr = w / max(w + l, 1)
    pending = len(state["pending"])
    pending_value = sum(t["bet_size"] for t in state["pending"])

    print(f"\n{'=' * 70}")
    print(f"  DASHBOARD | {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"{'=' * 70}")
    print(f"  Bankroll:  ${state['bankroll']:>10,.2f}  |  PnL: ${state['pnl']:>+10,.2f}")
    print(f"  Record:    {w}W-{l}L ({wr:.1%})  |  "
          f"Trades: {state['trades']}  |  Today: {state['daily_trades']}")
    print(f"  Pending:   {pending} positions (${pending_value:,.2f} deployed)")
    print(f"  Scanned:   {candidates_count} candidates this cycle")
    print(f"{'=' * 70}")


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    init_log()
    state = load_state()

    # Reset daily counter if new day
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state["last_trade_date"] != today:
        state["daily_trades"] = 0
        state["last_trade_date"] = today

    print_banner()
    print(f"\n  Loaded state: {state['trades']} trades, "
          f"${state['bankroll']:.2f} bankroll, "
          f"{len(state['pending'])} pending")

    scan_count = 0

    while True:
        try:
            scan_count += 1
            now_utc = datetime.now(timezone.utc)

            # Reset daily counter at midnight
            today = now_utc.strftime("%Y-%m-%d")
            if state["last_trade_date"] != today:
                state["daily_trades"] = 0
                state["last_trade_date"] = today
                print(f"\n  [NEW DAY] {today} — daily counter reset")

            # ── Resolve completed trades ──
            resolve_trades(state)

            # ── Redeem settled positions ──
            redeemed = redeem_positions()
            if redeemed:
                print(f"  [REDEEM] Redeemed {redeemed} positions")

            # ── Safety checks ──
            if state["bankroll"] < KILL_SWITCH_MIN:
                print(f"\n  [KILL SWITCH] Bankroll ${state['bankroll']:.2f} "
                      f"< ${KILL_SWITCH_MIN:.2f} — pausing trades")
                print_dashboard(state)
                save_state(state)
                time.sleep(SCAN_INTERVAL)
                continue

            if state["daily_trades"] >= MAX_DAILY_TRADES:
                print(f"\n  [CIRCUIT BREAKER] {state['daily_trades']} daily trades "
                      f"— max {MAX_DAILY_TRADES}")
                print_dashboard(state)
                save_state(state)
                time.sleep(SCAN_INTERVAL)
                continue

            if len(state["pending"]) >= MAX_PENDING:
                print(f"  [MAX PENDING] {len(state['pending'])} positions — "
                      f"waiting for resolutions")
                print_dashboard(state)
                save_state(state)
                time.sleep(SCAN_INTERVAL)
                continue

            # ── Scan for opportunities ──
            print(f"\n  [SCAN #{scan_count}] "
                  f"{now_utc.strftime('%H:%M:%S UTC')} — "
                  f"scanning markets closing in {SCAN_AHEAD_HOURS}h...")

            candidates = scan_markets()
            traded_set = set(state["traded_tokens"])

            # Filter out already-traded tokens
            fresh = [c for c in candidates if c["token_id"] not in traded_set]

            print(f"  [SCAN] {len(candidates)} candidates, "
                  f"{len(fresh)} fresh (not yet traded)")

            # ── Evaluate and trade (main scan: gamma >= 0.90) ──
            trades_this_cycle = 0
            skipped_depth = 0

            def can_trade(state):
                return (state["bankroll"] >= BET_SIZE
                        and len(state["pending"]) < MAX_PENDING
                        and state["daily_trades"] < MAX_DAILY_TRADES)

            for c in fresh:
                if not can_trade(state):
                    break

                # Get real CLOB price
                prices = get_clob_prices(c["token_id"])
                ask = prices["ask"]
                bid = prices["bid"]

                if ask <= 0 or ask >= 1:
                    continue

                # Filter: CLOB ask must be in $0.95-$0.99
                if ask < MIN_ASK or ask > MAX_ASK:
                    continue

                spread = ask - bid
                if spread > MAX_SPREAD:
                    continue

                # Check real orderbook depth — don't trade illiquid books
                depth = check_book_depth(c["token_id"], side="asks")
                if depth < MIN_BOOK_DEPTH:
                    skipped_depth += 1
                    continue

                # Execute
                success = execute_trade(state, c, ask, bid)
                if success:
                    trades_this_cycle += 1
                    time.sleep(0.5)

            if skipped_depth:
                print(f"  [DEPTH] Skipped {skipped_depth} thin books "
                      f"(< ${MIN_BOOK_DEPTH:.0f} at ask)")

            # ── Live watch: check markets at $0.88-$0.95 gamma ──
            # These might have ticked up to $0.95+ on CLOB
            if can_trade(state):
                watchlist = scan_watchlist()
                watch_fresh = [w for w in watchlist
                               if w["token_id"] not in traded_set]
                live_hits = 0

                for w in watch_fresh[:30]:  # Cap CLOB calls
                    if not can_trade(state):
                        break

                    prices = get_clob_prices(w["token_id"])
                    ask = prices["ask"]
                    bid = prices["bid"]

                    if ask <= 0 or ask >= 1:
                        continue
                    if ask < MIN_ASK or ask > MAX_ASK:
                        continue
                    spread = ask - bid
                    if spread > MAX_SPREAD:
                        continue

                    depth = check_book_depth(w["token_id"], side="asks")
                    if depth < MIN_BOOK_DEPTH:
                        continue

                    print(f"  [LIVE HIT] {w['outcome']} gamma=${w['gamma_price']:.2f} "
                          f"→ CLOB ask=${ask:.2f} | {w['question'][:50]}")
                    success = execute_trade(state, w, ask, bid)
                    if success:
                        trades_this_cycle += 1
                        live_hits += 1
                        time.sleep(0.5)

                if watch_fresh:
                    print(f"  [WATCH] Checked {min(30, len(watch_fresh))} "
                          f"sub-$0.95 markets, {live_hits} hit $0.95+ live")

            # ── Dashboard ──
            print_dashboard(state, len(candidates))
            save_state(state)

            # ── Wait for next scan ──
            print(f"\n  Next scan in {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n[SHUTDOWN] Saving state...")
            save_state(state)
            print_dashboard(state)
            break
        except Exception as e:
            print(f"\n[ERROR] {e}")
            traceback.print_exc()
            save_state(state)
            time.sleep(30)


if __name__ == "__main__":
    run()
