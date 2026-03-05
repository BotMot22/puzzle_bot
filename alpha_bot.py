#!/usr/bin/env python3
"""
ALPHA BOT — Whale-Inspired Multi-Strategy Engine
═══════════════════════════════════════════════════════════════

Three strategies reverse-engineered from top PnL traders (72h):

1. CRYPTO SPEED (BoneReader/Winner55555 model)
   - Monitor BTC/ETH/SOL spot prices on Binance in real-time
   - Trade 5-min, 15-min, and hourly up/down markets
   - When spot price makes outcome near-certain, buy the winning side
   - BoneReader: $34K/day on $2.3M vol buying at 1c when direction locked
   - Winner55555: $28K/day buying at 15-53c mid-window

2. NBA O/U PACE (bcda model — $190K PnL, 47% return on volume)
   - Pull live NBA scores + game clock from ESPN
   - Calculate current scoring pace vs Polymarket O/U line
   - When pace strongly favors Over or Under, buy at ~48c
   - bcda hit 100% on visible recent picks

3. WHALE MIRROR (follow the money)
   - Poll top traders' activity feed every 60s
   - When bcda/BoneReader/JuicySlots buy, mirror within seconds
   - Size proportional to whale confidence (bigger whale bet = bigger mirror)

Bankroll: Uses existing USDC in wallet. $10-25 per trade.
"""

import os
import sys
import json
import time
import csv
import re
import signal
import traceback
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from web3 import Web3
from eth_account import Account

# ═══════════════════════════════════════════════════════════════
# SETUP
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

_w3 = Web3(Web3.HTTPProvider("https://polygon.drpc.org"))
_acct = Account.from_key(os.environ["POLYMARKET_PRIVATE_KEY"])
WALLET = os.environ["POLYMARKET_WALLET"]

_retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
http = requests.Session()
http.mount("https://", HTTPAdapter(max_retries=_retry))

# USDC balance reader
USDC_CONTRACT = _w3.eth.contract(
    address=_w3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
    abi=[{"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
          "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}],
)

# CTF contract for auto-redeem (live mode)
CTF_CONTRACT = _w3.eth.contract(
    address=_w3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
    abi=json.loads('[{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]'),
)
CTF_COLLATERAL = _w3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CTF_PARENT = b'\x00' * 32

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

PAPER_MODE = False  # LIVE MODE

# -- Strategy toggles --
CRYPTO_SPEED_ENABLED = True
NBA_PACE_ENABLED = True
WHALE_MIRROR_ENABLED = True

# -- Crypto speed config --
CRYPTO_ASSETS = ["bitcoin", "ethereum", "solana"]
BINANCE_MAP = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT"}
# Min confidence to trade (how far the direction is locked in)
CRYPTO_MIN_CONFIDENCE = 0.70  # 70% of window elapsed with clear direction
CRYPTO_MAX_ASK = 0.45         # Tightened from 0.92 — never overpay
CRYPTO_MIN_ASK = 0.01         # Will buy as low as 1c if edge is extreme
CRYPTO_BET_SIZE = 10.00       # $10 per crypto trade (conservative for live)

# -- NBA pace config --
NBA_MIN_PACE_EDGE = 10.0      # Tightened from 8 — need stronger edge for live
NBA_MIN_GAME_MINUTES = 30     # Tightened from 24 — deeper into game = safer
NBA_MAX_ASK = 0.50            # Tightened from 0.55
NBA_BET_SIZE = 15.00          # $15 per NBA trade (conservative for live)

# -- Whale mirror config --
WHALE_WALLETS = {
    "bcda":        "0xb45a797faa52b0fd8adc56d30382022b7b12192c",
    "BoneReader":  "0xd84c2b6d65dc596f49c7b6aadd6d74ca91e407b9",
    "Winner55555": "0x4c353dd347c2e7d8bcdc5cd6ee569de7baf23e2f",
    "JuicySlots":  "0xc257ea7e3a81ca8e16df8935d44d513959fa358e",
    "768543265":   "0x5da48936d61eb18d66ca5fdd32ba2d2ba19be203",
}
WHALE_MIN_TRADE_SIZE = 500    # Only mirror whale trades > $500
WHALE_MIRROR_SIZE = 10.00     # $10 mirror bet (conservative for live)
WHALE_MAX_ASK = 0.55          # Tightened from 0.60

# -- Risk management --
BET_SIZE = 10.00
KILL_SWITCH_MIN = 25.00       # Stop at $25 remaining (tighter for live)
MAX_DAILY_TRADES = 75         # Halved from 150 — less volume, more selective
MAX_PENDING = 20              # Halved from 40 — less capital deployed at once
POLL_INTERVAL = 30            # Main loop interval (seconds)

# -- State --
STATE_FILE = "data/alpha_state.json"
LOG_FILE = "data/alpha_trades.csv"
LOG_FIELDS = [
    "timestamp", "strategy", "market", "outcome", "side",
    "price", "size_usdc", "shares", "token_id", "condition_id",
    "trigger", "resolved", "won", "pnl", "bankroll_after",
]

# ═══════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        if data.get("version") == 1:
            return data
    return {
        "version": 1,
        "bankroll": 0.0,  # Will sync from chain
        "pnl": 0.0,
        "wins": 0, "losses": 0, "trades": 0,
        "daily_trades": 0, "last_trade_date": "",
        "pending": [],
        "traded_tokens": [],
        "whale_last_seen": {},  # wallet -> last trade timestamp
    }


def save_state(state):
    import tempfile
    os.makedirs(os.path.dirname(STATE_FILE) or "data", exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE) or ".", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def init_log():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS).writeheader()


def log_trade(trade):
    with open(LOG_FILE, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=LOG_FIELDS).writerow(
            {k: trade.get(k, "") for k in LOG_FIELDS}
        )


def update_csv_resolution(token_id, condition_id, resolved_ts, won, pnl, bankroll_after):
    """Update the first matching unresolved CSV row with resolution data."""
    if not os.path.exists(LOG_FILE):
        return
    rows = []
    updated = False
    with open(LOG_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (not updated
                    and row.get("token_id") == token_id
                    and row.get("condition_id") == condition_id
                    and not row.get("resolved")):
                row["resolved"] = resolved_ts
                row["won"] = "True" if won else "False"
                row["pnl"] = f"{pnl:.4f}"
                row["bankroll_after"] = f"{bankroll_after:.2f}"
                updated = True
            rows.append(row)
    if updated:
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()
            writer.writerows(rows)


def rebuild_pending_from_csv():
    """Rebuild pending list from CSV rows that haven't been resolved."""
    if not os.path.exists(LOG_FILE):
        return []
    pending = []
    seen = set()
    with open(LOG_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("resolved"):
                continue
            cid = row.get("condition_id", "")
            tid = row.get("token_id", "")
            key = (tid, cid)
            if key in seen:
                continue
            seen.add(key)
            pending.append({
                "strategy": row.get("strategy", "UNKNOWN"),
                "token_id": tid,
                "condition_id": cid,
                "question": row.get("market", "")[:80],
                "outcome": row.get("outcome", ""),
                "entry_price": float(row.get("price", 0) or 0),
                "size": float(row.get("size_usdc", 0) or 0),
                "shares": float(row.get("shares", 0) or 0),
                "timestamp": row.get("timestamp", ""),
            })
    return pending


def get_usdc_balance():
    try:
        return USDC_CONTRACT.functions.balanceOf(
            _w3.to_checksum_address(WALLET)
        ).call() / 1e6
    except Exception:
        return 0.0


def auto_redeem_positions():
    """Auto-redeem all resolved positions on-chain. Returns USDC recovered."""
    if PAPER_MODE:
        return 0.0
    try:
        positions = http.get("https://data-api.polymarket.com/positions", params={
            "user": WALLET, "sizeThreshold": 0,
        }, timeout=15).json()
        redeemable = [p for p in positions if p.get("redeemable")]
        if not redeemable:
            return 0.0
        recovered = 0.0
        for p in redeemable:
            cid = p.get("conditionId", "")
            try:
                cid_bytes = bytes.fromhex(cid[2:] if cid.startswith("0x") else cid)
                # Dry-run first
                CTF_CONTRACT.functions.redeemPositions(
                    CTF_COLLATERAL, CTF_PARENT, cid_bytes, [1, 2]
                ).call({"from": _acct.address})
                # Real tx
                nonce = _w3.eth.get_transaction_count(_acct.address, "pending")
                gas_price = _w3.eth.gas_price
                tx = CTF_CONTRACT.functions.redeemPositions(
                    CTF_COLLATERAL, CTF_PARENT, cid_bytes, [1, 2]
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
                receipt = _w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                if receipt.status == 1:
                    val = float(p.get("currentValue", 0))
                    recovered += val
                    print(f"  [REDEEMED] {p.get('outcome','?')} | ${val:.2f} | {p.get('title','?')[:50]}")
                time.sleep(2)  # Avoid nonce collision
            except Exception as e:
                if "revert" not in str(e).lower():
                    print(f"  [REDEEM ERR] {str(e)[:60]}")
        return recovered
    except Exception as e:
        return 0.0


# ═══════════════════════════════════════════════════════════════
# PRICE FEEDS
# ═══════════════════════════════════════════════════════════════

_price_cache = {}


def get_binance_price(symbol):
    """Get real-time spot price from Binance."""
    now = time.time()
    cache_key = f"binance:{symbol}"
    if cache_key in _price_cache:
        price, ts = _price_cache[cache_key]
        if now - ts < 3:  # 3s cache
            return price
    try:
        r = http.get("https://api.binance.com/api/v3/ticker/price",
                      params={"symbol": symbol}, timeout=3)
        price = float(r.json()["price"])
        _price_cache[cache_key] = (price, now)
        return price
    except Exception:
        if cache_key in _price_cache:
            return _price_cache[cache_key][0]
        return 0.0


def get_yahoo_price(ticker):
    """Get stock/index price from Yahoo Finance."""
    now = time.time()
    cache_key = f"yahoo:{ticker}"
    if cache_key in _price_cache:
        price, ts = _price_cache[cache_key]
        if now - ts < 15:
            return price
    try:
        r = http.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=5,
        )
        price = float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
        _price_cache[cache_key] = (price, now)
        return price
    except Exception:
        if cache_key in _price_cache:
            return _price_cache[cache_key][0]
        return 0.0


# ═══════════════════════════════════════════════════════════════
# STRATEGY 1: CRYPTO SPEED TRADING
# ═══════════════════════════════════════════════════════════════

def scan_crypto_markets():
    """Find active crypto up/down markets on Polymarket (5min, 15min, hourly, daily)."""
    now = datetime.now(timezone.utc)
    end_max = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    markets = []
    try:
        # Scan gamma API for crypto up/down markets ending soon
        for offset in [0, 100]:
            r = http.get("https://gamma-api.polymarket.com/events", params={
                "active": True, "closed": False, "limit": 100, "offset": offset,
                "tag": "crypto",
                "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": end_max,
            }, timeout=10)
            events = r.json()
            for evt in events:
                for mkt in evt.get("markets", []):
                    q = mkt.get("question", "")
                    if "up or down" in q.lower():
                        markets.append(mkt)
    except Exception as e:
        print(f"  [CRYPTO] Scan error: {e}")
    return markets


def evaluate_crypto_market(mkt):
    """
    Evaluate a crypto up/down market for speed trading.

    KEY INSIGHT from whale analysis:
    - LOCK at 99c is a TRAP (1c upside vs 99c downside, lost 10/10 in testing)
    - Winner55555 model is better: buy at 20-55c with real upside
    - The edge comes from Binance spot price confirming direction

    Strategy: Compare Binance spot vs the CLOB implied direction.
    Only trade when spot price CONFIRMS the CLOB direction.
    """
    question = mkt.get("question", "")
    q_lower = question.lower()

    # Identify the crypto asset
    asset = None
    binance_sym = None
    for name, sym in BINANCE_MAP.items():
        if name in q_lower:
            asset = name
            binance_sym = sym
            break
    if not binance_sym:
        return None

    # Get current spot price from Binance
    spot = get_binance_price(binance_sym)
    if spot <= 0:
        return None

    # Parse the market end time
    end_str = mkt.get("endDate") or mkt.get("end_date_iso", "")
    if not end_str:
        return None
    try:
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
    except Exception:
        return None

    now = datetime.now(timezone.utc)
    mins_left = (end_dt - now).total_seconds() / 60

    if mins_left <= 0 or mins_left > 60:
        return None

    # Parse tokens
    outcomes = mkt.get("outcomes", "")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []

    tokens_str = mkt.get("clobTokenIds", "")
    if isinstance(tokens_str, str):
        try:
            tokens = json.loads(tokens_str)
        except Exception:
            tokens = []
    else:
        tokens = tokens_str or []

    if len(tokens) < 2 or len(outcomes) < 2:
        return None

    # Standard: outcomes[0] = Up, outcomes[1] = Down
    up_idx = None
    down_idx = None
    for i, o in enumerate(outcomes):
        if o.lower() == "up":
            up_idx = i
        elif o.lower() == "down":
            down_idx = i
    if up_idx is None or down_idx is None:
        return None

    up_token = tokens[up_idx]
    down_token = tokens[down_idx]

    # Get CLOB orderbooks
    try:
        book_up = http.get(f"https://clob.polymarket.com/book?token_id={up_token}", timeout=5).json()
        book_down = http.get(f"https://clob.polymarket.com/book?token_id={down_token}", timeout=5).json()
    except Exception:
        return None

    up_asks = book_up.get("asks", [])
    down_asks = book_down.get("asks", [])
    up_bids = book_up.get("bids", [])
    down_bids = book_down.get("bids", [])

    if not up_asks and not down_asks:
        return None

    up_best_ask = float(up_asks[0]["price"]) if up_asks else 1.0
    down_best_ask = float(down_asks[0]["price"]) if down_asks else 1.0
    up_mid = (float(up_bids[0]["price"]) if up_bids else 0 + up_best_ask) / 2 if up_asks else 0
    down_mid = (float(down_bids[0]["price"]) if down_bids else 0 + down_best_ask) / 2 if down_asks else 0

    # Compute implied direction from CLOB midpoints
    # If Up mid > Down mid, CLOB says price is going Up
    clob_says_up = up_mid > down_mid if (up_mid > 0 and down_mid > 0) else up_best_ask < down_best_ask

    # We need to know the REFERENCE PRICE (opening price of the window)
    # The market description usually says "resolve Up if price at end >= price at start"
    # We can infer: if CLOB says Up is winning at 70c, the spot price is above the open
    # If CLOB says Down at 70c, spot price is below the open

    # Determine our trade: we follow spot price momentum
    # If Up is cheap (20-55c) AND spot price is trending up, buy Up
    # If Down is cheap (20-55c) AND spot price is trending down, buy Down

    # For crypto, check recent 1-min price momentum from Binance
    try:
        r = http.get("https://api.binance.com/api/v3/klines", params={
            "symbol": binance_sym, "interval": "1m", "limit": 10,
        }, timeout=3)
        klines = r.json()
        if len(klines) >= 5:
            price_5m_ago = float(klines[-5][4])  # close price 5 min ago
            price_1m_ago = float(klines[-2][4])   # close price 1 min ago
            price_now = spot

            momentum_5m = (price_now - price_5m_ago) / price_5m_ago * 100
            momentum_1m = (price_now - price_1m_ago) / price_1m_ago * 100

            # LIVE: require stronger momentum + 1m confirmation to avoid whipsaws
            spot_says_up = momentum_5m > 0.15 and momentum_1m > 0.05
            spot_says_down = momentum_5m < -0.15 and momentum_1m < -0.05
        else:
            return None
    except Exception:
        return None

    # CLOB confirmation: if CLOB midpoint disagrees with momentum, skip
    clob_agrees_up = clob_says_up
    clob_agrees_down = not clob_says_up

    # LIVE: use actual best ask but cap max entry price
    # Buy at real ask so orders actually fill, but never overpay
    MAX_ENTRY_STRONG = 0.50    # Cap for strong signals
    MAX_ENTRY_MODERATE = 0.42  # Cap for moderate signals
    if spot_says_up and clob_agrees_up:
        side = "Up"
        token_id = up_token
        best_ask = up_best_ask
        if abs(momentum_5m) > 0.30:
            tier = "STRONG"
            max_price = MAX_ENTRY_STRONG
        elif abs(momentum_5m) > 0.20:
            tier = "MODERATE"
            max_price = MAX_ENTRY_MODERATE
        else:
            return None
        if best_ask > max_price:
            return None  # Too expensive
        limit_price = best_ask
    elif spot_says_down and clob_agrees_down:
        side = "Down"
        token_id = down_token
        best_ask = down_best_ask
        if abs(momentum_5m) > 0.30:
            tier = "STRONG"
            max_price = MAX_ENTRY_STRONG
        elif abs(momentum_5m) > 0.20:
            tier = "MODERATE"
            max_price = MAX_ENTRY_MODERATE
        else:
            return None
        if best_ask > max_price:
            return None  # Too expensive
        limit_price = best_ask
    else:
        return None  # Momentum/CLOB disagree = no edge

    # Only trade shorter windows (5-15 min) where momentum is more predictive
    if mins_left > 15:
        return None

    return (side, tier, token_id, limit_price, question, mkt.get("conditionId", ""))


# ═══════════════════════════════════════════════════════════════
# STRATEGY 2: NBA OVER/UNDER PACE TRADING
# ═══════════════════════════════════════════════════════════════

def get_espn_nba_scores():
    """Fetch live NBA scores and game clock from ESPN API."""
    try:
        r = http.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
                      timeout=10)
        data = r.json()
        games = []
        for event in data.get("events", []):
            comp = event.get("competitions", [{}])[0]
            status = comp.get("status", {})
            clock = status.get("displayClock", "0:00")
            period = status.get("period", 0)
            state = status.get("type", {}).get("state", "pre")

            if state != "in":
                continue  # Only live games

            teams = comp.get("competitors", [])
            if len(teams) < 2:
                continue

            home = teams[0]
            away = teams[1]
            home_score = int(home.get("score", 0))
            away_score = int(away.get("score", 0))
            total = home_score + away_score

            # Calculate minutes played
            try:
                parts = clock.split(":")
                mins_in_period = int(parts[0])
                secs_in_period = int(parts[1]) if len(parts) > 1 else 0
            except (ValueError, IndexError):
                mins_in_period = 0
                secs_in_period = 0

            # NBA: 4 quarters × 12 min = 48 min total
            time_remaining_in_period = mins_in_period + secs_in_period / 60
            periods_completed = period - 1
            minutes_played = periods_completed * 12 + (12 - time_remaining_in_period)
            minutes_remaining = max(0, 48 - minutes_played)

            # Projected total = current_total * (48 / minutes_played)
            if minutes_played > 0:
                pace = total * (48.0 / minutes_played)
            else:
                pace = 0

            home_name = home.get("team", {}).get("displayName", "")
            away_name = away.get("team", {}).get("displayName", "")

            games.append({
                "home": home_name, "away": away_name,
                "home_score": home_score, "away_score": away_score,
                "total": total, "pace": pace,
                "period": period, "clock": clock,
                "minutes_played": minutes_played,
                "minutes_remaining": minutes_remaining,
            })
        return games
    except Exception as e:
        print(f"  [NBA] ESPN error: {e}")
        return []


def scan_nba_ou_markets():
    """Find active NBA O/U markets on Polymarket."""
    now = datetime.now(timezone.utc)
    end_max = (now + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")

    markets = []
    try:
        r = http.get("https://gamma-api.polymarket.com/events", params={
            "active": True, "closed": False, "limit": 100,
            "tag": "nba",
        }, timeout=10)
        events = r.json()
        for evt in events:
            for mkt in evt.get("markets", []):
                q = mkt.get("question", "")
                if "o/u " in q.lower() or "over/under" in q.lower():
                    markets.append(mkt)
    except Exception as e:
        print(f"  [NBA] Market scan error: {e}")
    return markets


def match_game_to_market(game, markets):
    """Match an ESPN game to its Polymarket O/U market(s)."""
    matches = []
    home_lower = game["home"].lower()
    away_lower = game["away"].lower()

    # Extract key team words (last word is usually the mascot)
    home_key = home_lower.split()[-1] if home_lower else ""
    away_key = away_lower.split()[-1] if away_lower else ""

    for mkt in markets:
        q = mkt.get("question", "").lower()
        if home_key in q and away_key in q:
            matches.append(mkt)
        elif away_key in q and home_key in q:
            matches.append(mkt)

    return matches


def parse_ou_line(question):
    """Extract the O/U number from question like 'Kings vs. Lakers: O/U 234.5'"""
    match = re.search(r'o/u\s+([\d.]+)', question.lower())
    if match:
        return float(match.group(1))
    match = re.search(r'over/under\s+([\d.]+)', question.lower())
    if match:
        return float(match.group(1))
    return None


def evaluate_nba_ou(game, mkt):
    """
    Compare live NBA pace to the O/U line.
    Returns (side, edge, token_id, ask_price, question, condition_id) or None.

    bcda model: buy Under at ~0.48 when pace is well below the line.
    """
    question = mkt.get("question", "")
    line = parse_ou_line(question)
    if not line:
        return None

    pace = game["pace"]
    mins_played = game["minutes_played"]

    if mins_played < NBA_MIN_GAME_MINUTES:
        return None  # Wait for enough data (at least halftime)

    edge = pace - line  # positive = trending Over, negative = trending Under

    if abs(edge) < NBA_MIN_PACE_EDGE:
        return None  # Not enough edge

    # Determine side
    if edge < -NBA_MIN_PACE_EDGE:
        side = "Under"
    else:
        side = "Over"

    # Get token IDs
    outcomes = mkt.get("outcomes", "")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []
    tokens_str = mkt.get("clobTokenIds", "")
    if isinstance(tokens_str, str):
        try:
            tokens = json.loads(tokens_str)
        except Exception:
            tokens = []
    else:
        tokens = tokens_str or []

    if len(tokens) < 2 or len(outcomes) < 2:
        return None

    # Find token for our side
    side_idx = None
    for i, o in enumerate(outcomes):
        if o.lower() == side.lower():
            side_idx = i
            break
    if side_idx is None:
        return None

    token_id = tokens[side_idx]

    # Check CLOB
    try:
        book = http.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=5).json()
    except Exception:
        return None

    asks = book.get("asks", [])
    if not asks:
        return None

    best_ask = float(asks[0]["price"])
    depth = sum(float(a.get("size", 0)) * float(a.get("price", 0)) for a in asks[:5])

    if best_ask > NBA_MAX_ASK:
        return None  # Too expensive — bcda buys at ~48c
    if depth < 50:
        return None  # Not enough liquidity

    return (side, abs(edge), token_id, best_ask, question, mkt.get("conditionId", ""))


# ═══════════════════════════════════════════════════════════════
# STRATEGY 3: WHALE MIRRORING
# ═══════════════════════════════════════════════════════════════

def check_whale_activity(state):
    """
    Poll top traders' recent activity. Return new trades to mirror.
    """
    signals = []
    now_ts = int(time.time())

    for name, wallet in WHALE_WALLETS.items():
        last_seen = state.get("whale_last_seen", {}).get(wallet, 0)
        try:
            activity = http.get("https://data-api.polymarket.com/activity", params={
                "user": wallet, "limit": 10,
            }, timeout=10).json()
        except Exception:
            continue

        for trade in activity:
            ts = trade.get("timestamp", 0)
            if not isinstance(ts, (int, float)):
                continue

            # Only new trades (within last 5 minutes, after our last check)
            if ts <= last_seen:
                continue
            if now_ts - ts > 300:  # Skip anything older than 5 minutes
                continue

            side = trade.get("side", "")
            if side != "BUY":
                continue

            usdc_size = float(trade.get("usdcSize", 0))
            if usdc_size < WHALE_MIN_TRADE_SIZE:
                continue

            price = float(trade.get("price", 0))
            if price > WHALE_MAX_ASK:
                continue

            signals.append({
                "whale": name,
                "title": trade.get("title", ""),
                "outcome": trade.get("outcome", ""),
                "price": price,
                "size": usdc_size,
                "token_id": trade.get("asset", ""),
                "condition_id": trade.get("conditionId", ""),
                "timestamp": ts,
            })

        # Update last seen
        if activity:
            latest_ts = max(t.get("timestamp", 0) for t in activity
                           if isinstance(t.get("timestamp"), (int, float)))
            if latest_ts > last_seen:
                state.setdefault("whale_last_seen", {})[wallet] = latest_ts

    return signals


# ═══════════════════════════════════════════════════════════════
# ORDER EXECUTION
# ═══════════════════════════════════════════════════════════════

def place_order(token_id, price, size_usdc, state, order_type="GTC"):
    """
    Place a buy order on the CLOB.
    order_type: "FOK" (fill or kill) or "GTC" (good till cancelled - limit order)
    Returns order dict with confirmed fill info, or None.
    """
    if PAPER_MODE:
        shares = size_usdc / price if price > 0 else 0
        print(f"  [PAPER] BUY {shares:.1f} shares @ {price:.3f} = ${size_usdc:.2f} [{order_type}]")
        return {"paper": True, "price": price, "shares": shares, "size": size_usdc}

    try:
        shares = round(size_usdc / price, 2) if price > 0 else 0
        if shares <= 0:
            return None

        order_args = OrderArgs(
            price=price,
            size=shares,
            side="BUY",
            token_id=token_id,
        )
        signed = clob_client.create_order(order_args)

        if order_type == "GTC":
            resp = clob_client.post_order(signed, OrderType.GTC)
        else:
            resp = clob_client.post_order(signed, OrderType.FOK)

        if not isinstance(resp, dict):
            print(f"  [ORDER] Unexpected response type: {type(resp)}")
            return None

        status = resp.get("status", "")
        oid = resp.get("orderID", resp.get("id", ""))

        # FOK: only "matched" means filled. "live" shouldn't happen but reject it.
        if order_type == "FOK":
            if status != "matched":
                print(f"  [ORDER] FOK not matched: {status} | {str(resp)[:80]}")
                return None
        else:
            # GTC: "matched" (instant fill) or "live" (on book) are both valid
            if status not in ("matched", "live"):
                print(f"  [ORDER] GTC rejected: {status} | {str(resp)[:80]}")
                return None

        # Verify fill by checking order status after a brief pause
        if oid and status == "matched":
            time.sleep(1)
            try:
                order_info = clob_client.get_order(oid)
                if isinstance(order_info, dict):
                    filled = float(order_info.get("size_matched", 0))
                    if filled <= 0:
                        print(f"  [ORDER] Matched but 0 filled: {str(order_info)[:80]}")
                        return None
                    # Use actual filled size
                    actual_shares = filled
                    actual_cost = actual_shares * price
                    return {"order_id": oid, "price": price, "shares": actual_shares,
                            "size": actual_cost, "status": "filled"}
            except Exception as e:
                print(f"  [ORDER] Fill check failed ({e}), trusting matched status")

        return {"order_id": oid, "price": price, "shares": shares,
                "size": size_usdc, "status": status}

    except Exception as e:
        print(f"  [ORDER ERR] {str(e)[:80]}")
        return None


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run():
    global PAPER_MODE
    init_log()
    state = load_state()

    # Rebuild pending from CSV if state lost its pending list
    if not state.get("pending"):
        rebuilt = rebuild_pending_from_csv()
        if rebuilt:
            state["pending"] = rebuilt
            # Also rebuild traded_tokens to prevent re-trading
            state["traded_tokens"] = list(set(
                p["condition_id"] for p in rebuilt if p.get("condition_id")
            ))
            print(f"[REBUILD] Recovered {len(rebuilt)} pending positions from CSV")
            save_state(state)

    # Sync bankroll from chain (live mode only)
    if not PAPER_MODE:
        bal = get_usdc_balance()
        if bal > 0:
            state["bankroll"] = bal
    elif state["bankroll"] <= 0:
        # First run in paper mode: seed with chain balance
        bal = get_usdc_balance()
        if bal > 0:
            state["bankroll"] = bal
    save_state(state)

    start_time = time.time()
    go_live_at = None  # No auto-switch needed — already live

    print("=" * 66)
    print("  ALPHA BOT — Whale-Inspired Multi-Strategy Engine")
    print(f"  Wallet: {WALLET}")
    print(f"  Bankroll: ${state['bankroll']:.2f}")
    print(f"  Mode: {'PAPER' if PAPER_MODE else 'LIVE'}")
    if go_live_at:
        go_live_str = datetime.fromtimestamp(go_live_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        print(f"  Go-Live: {go_live_str} ({GO_LIVE_AFTER_HOURS}h from now)")
    print(f"  Strategies: Crypto={CRYPTO_SPEED_ENABLED} NBA={NBA_PACE_ENABLED} Whale={WHALE_MIRROR_ENABLED}")
    print("=" * 66)

    running = True
    def handle_signal(sig, frame):
        nonlocal running
        print("\n[SHUTDOWN] Saving state...")
        save_state(state)
        running = False
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    cycle = 0
    while running:
        try:
            cycle += 1
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")

            # Reset daily counter
            if state.get("last_trade_date") != today:
                state["daily_trades"] = 0
                state["last_trade_date"] = today
                state["traded_tokens"] = []  # Reset traded tokens daily

            # 24h auto-switch: paper → live
            if PAPER_MODE and go_live_at and time.time() >= go_live_at:
                win_rate = state["wins"] / max(1, state["wins"] + state["losses"])
                print("\n" + "!" * 66)
                print(f"  24H PAPER TEST COMPLETE")
                print(f"  Record: {state['wins']}W-{state['losses']}L ({win_rate*100:.1f}%)")
                print(f"  PnL: ${state['pnl']:+.2f}")
                if win_rate >= 0.55 and state["pnl"] > 0:
                    PAPER_MODE = False
                    bal = get_usdc_balance()
                    state["bankroll"] = bal if bal > 0 else state["bankroll"]
                    # Keep win/loss record but reset daily trades
                    state["daily_trades"] = 0
                    state["traded_tokens"] = []
                    print(f"  >>> SWITCHING TO LIVE MODE <<<")
                    print(f"  Chain bankroll: ${state['bankroll']:.2f}")
                else:
                    print(f"  >>> Win rate too low or negative PnL, staying in PAPER <<<")
                    go_live_at = time.time() + (24 * 3600)  # Try again in 24h
                print("!" * 66 + "\n")

            # Sync bankroll from chain every cycle (live mode) — single source of truth
            if not PAPER_MODE:
                bal = get_usdc_balance()
                if bal > 0:
                    state["bankroll"] = bal

            # Auto-redeem resolved positions every 5 min (live mode)
            if not PAPER_MODE and cycle % 10 == 0:
                recovered = auto_redeem_positions()
                if recovered > 0:
                    pass  # bankroll synced from chain
                    print(f"  [AUTO-REDEEM] Recovered ${recovered:.2f}")

            signals_found = 0
            skip_trading = False

            # ── RESOLVE PENDING (runs first, even under kill switch) ──
            try:
                still_pending = []
                for pos in state.get("pending", []):
                    cid = pos.get("condition_id", "")
                    tid = pos.get("token_id", "")
                    if not cid:
                        still_pending.append(pos)
                        continue

                    try:
                        # Check market closed status via CLOB (works for both paper & live)
                        mkt_data = http.get(
                            f"https://clob.polymarket.com/markets/{cid}",
                            timeout=5,
                        ).json()

                        if not mkt_data.get("closed"):
                            still_pending.append(pos)
                            continue

                        # Market is closed — determine winner
                        tokens = mkt_data.get("tokens", [])
                        our_outcome = pos.get("outcome", "").lower()
                        won = None
                        for t in tokens:
                            if t.get("outcome", "").lower() == our_outcome:
                                won = t.get("winner", False)
                                break

                        if won is None:
                            # Could not determine winner yet
                            still_pending.append(pos)
                            continue

                        entry = float(pos.get("size", 0))
                        shares = float(pos.get("shares", 0))
                        payout = shares if won else 0
                        pnl = payout - entry

                        if won:
                            state["wins"] += 1
                        else:
                            state["losses"] += 1
                        state["pnl"] += pnl
                        # bankroll synced from chain — don't manually adjust

                        tag = "WIN" if won else "LOSS"
                        print(f"  [{tag}] {pos.get('strategy','')} {pos.get('outcome','')} | "
                              f"PnL: ${pnl:+.2f} | {pos.get('question','')[:50]}")

                        # Update CSV with resolution data
                        update_csv_resolution(
                            tid, cid, now.isoformat(), won, pnl, state["bankroll"]
                        )

                    except Exception as e:
                        print(f"  [RESOLVE] Err checking {cid[:16]}...: {e}")
                        still_pending.append(pos)

                state["pending"] = still_pending
            except Exception as e:
                print(f"  [RESOLVE ERR] {e}")

            # Kill switch (after resolution, before new trades)
            if state["bankroll"] < KILL_SWITCH_MIN:
                skip_trading = True
            elif state["daily_trades"] >= MAX_DAILY_TRADES:
                skip_trading = True

            if not skip_trading and CRYPTO_SPEED_ENABLED:
                try:
                    crypto_mkts = scan_crypto_markets()
                    crypto_trades_this_cycle = 0
                    MAX_CRYPTO_PER_CYCLE = 2  # Reduced from 3 for live
                    for mkt in crypto_mkts:
                        if crypto_trades_this_cycle >= MAX_CRYPTO_PER_CYCLE:
                            break
                        cid = mkt.get("conditionId", "")
                        if cid in state.get("traded_tokens", []):
                            continue
                        result = evaluate_crypto_market(mkt)
                        if result:
                            side, tier, token_id, ask, question, condition_id = result
                            # Sized by tier — conservative for live
                            if tier == "STRONG_LIMIT":
                                size = min(CRYPTO_BET_SIZE, state["bankroll"] * 0.03)
                            elif tier == "MODERATE_LIMIT":
                                size = min(CRYPTO_BET_SIZE * 0.8, state["bankroll"] * 0.025)
                            else:
                                size = min(CRYPTO_BET_SIZE * 0.5, state["bankroll"] * 0.02)

                            if size > state["bankroll"]:
                                continue

                            print(f"  [CRYPTO {tier}] {side} @ {ask:.3f} | ${size:.0f} | {question[:60]}")
                            order = place_order(token_id, ask, size, state, order_type="FOK")
                            if order:
                                signals_found += 1
                                crypto_trades_this_cycle += 1
                                # bankroll synced from chain
                                state["daily_trades"] += 1
                                state["trades"] += 1
                                state["traded_tokens"].append(cid)
                                state["pending"].append({
                                    "strategy": "CRYPTO_SPEED",
                                    "token_id": token_id,
                                    "condition_id": condition_id,
                                    "question": question[:80],
                                    "outcome": side,
                                    "entry_price": ask,
                                    "size": size,
                                    "shares": order.get("shares", 0),
                                    "timestamp": now.isoformat(),
                                })
                                log_trade({
                                    "timestamp": now.isoformat(),
                                    "strategy": f"CRYPTO_{tier}",
                                    "market": question[:80],
                                    "outcome": side,
                                    "side": "BUY",
                                    "price": ask,
                                    "size_usdc": size,
                                    "shares": order.get("shares", 0),
                                    "token_id": token_id,
                                    "condition_id": condition_id,
                                    "trigger": f"tier={tier}",
                                })
                except Exception as e:
                    print(f"  [CRYPTO ERR] {e}")

            # ── STRATEGY 2: NBA O/U PACE ──
            if not skip_trading and NBA_PACE_ENABLED:
                try:
                    games = get_espn_nba_scores()
                    if games:
                        nba_mkts = scan_nba_ou_markets()
                        for game in games:
                            matched = match_game_to_market(game, nba_mkts)
                            for mkt in matched:
                                cid = mkt.get("conditionId", "")
                                if cid in state.get("traded_tokens", []):
                                    continue
                                result = evaluate_nba_ou(game, mkt)
                                if result:
                                    side, edge, token_id, ask, question, condition_id = result
                                    size = NBA_BET_SIZE
                                    if size > state["bankroll"]:
                                        continue

                                    print(f"  [NBA PACE] {side} @ {ask:.3f} | edge:{edge:+.1f}pts | "
                                          f"pace:{game['pace']:.0f} | Q{game['period']} {game['clock']} | "
                                          f"{game['away']} {game['away_score']}-{game['home_score']} {game['home']} | "
                                          f"{question[:50]}")
                                    order = place_order(token_id, ask, size, state)
                                    if order:
                                        signals_found += 1
                                        # bankroll synced from chain
                                        state["daily_trades"] += 1
                                        state["trades"] += 1
                                        state["traded_tokens"].append(cid)
                                        state["pending"].append({
                                            "strategy": "NBA_PACE",
                                            "token_id": token_id,
                                            "condition_id": condition_id,
                                            "question": question[:80],
                                            "outcome": side,
                                            "entry_price": ask,
                                            "size": size,
                                            "shares": order.get("shares", 0),
                                            "timestamp": now.isoformat(),
                                            "pace": game["pace"],
                                            "line": parse_ou_line(question),
                                        })
                                        log_trade({
                                            "timestamp": now.isoformat(),
                                            "strategy": "NBA_PACE",
                                            "market": question[:80],
                                            "outcome": side,
                                            "side": "BUY",
                                            "price": ask,
                                            "size_usdc": size,
                                            "shares": order.get("shares", 0),
                                            "token_id": token_id,
                                            "condition_id": condition_id,
                                            "trigger": f"pace={game['pace']:.0f} edge={edge:+.1f}",
                                        })
                except Exception as e:
                    print(f"  [NBA ERR] {e}")

            # ── STRATEGY 3: WHALE MIRROR ──
            if not skip_trading and WHALE_MIRROR_ENABLED:
                try:
                    whale_signals = check_whale_activity(state)
                    for sig in whale_signals:
                        token_id = sig["token_id"]
                        if not token_id:
                            continue
                        cid = sig.get("condition_id", "")
                        if cid in state.get("traded_tokens", []):
                            continue

                        # Check current CLOB ask
                        try:
                            book = http.get(
                                f"https://clob.polymarket.com/book?token_id={token_id}",
                                timeout=5
                            ).json()
                            asks = book.get("asks", [])
                            if not asks:
                                continue
                            cur_ask = float(asks[0]["price"])
                        except Exception:
                            continue

                        if cur_ask > WHALE_MAX_ASK:
                            continue

                        size = WHALE_MIRROR_SIZE
                        # Scale up for bigger whale bets
                        if sig["size"] >= 10000:
                            size = min(25.0, state["bankroll"] * 0.10)
                        elif sig["size"] >= 5000:
                            size = min(20.0, state["bankroll"] * 0.08)

                        if size > state["bankroll"]:
                            continue

                        print(f"  [WHALE] Mirror {sig['whale']}: {sig['outcome']} @ {cur_ask:.3f} "
                              f"(whale: ${sig['size']:,.0f} @ {sig['price']:.3f}) | {sig['title'][:50]}")
                        order = place_order(token_id, cur_ask, size, state)
                        if order:
                            signals_found += 1
                            # bankroll synced from chain
                            state["daily_trades"] += 1
                            state["trades"] += 1
                            state["traded_tokens"].append(cid)
                            state["pending"].append({
                                "strategy": "WHALE_MIRROR",
                                "whale": sig["whale"],
                                "token_id": token_id,
                                "condition_id": cid,
                                "question": sig["title"][:80],
                                "outcome": sig["outcome"],
                                "entry_price": cur_ask,
                                "size": size,
                                "shares": order.get("shares", 0),
                                "timestamp": now.isoformat(),
                                "whale_size": sig["size"],
                            })
                            log_trade({
                                "timestamp": now.isoformat(),
                                "strategy": "WHALE_MIRROR",
                                "market": sig["title"][:80],
                                "outcome": sig["outcome"],
                                "side": "BUY",
                                "price": cur_ask,
                                "size_usdc": size,
                                "shares": order.get("shares", 0),
                                "token_id": token_id,
                                "condition_id": cid,
                                "trigger": f"whale={sig['whale']} ${sig['size']:,.0f}",
                            })
                except Exception as e:
                    print(f"  [WHALE ERR] {e}")

            # ── DASHBOARD ──
            now_str = now.strftime("%H:%M:%S UTC")
            wins = state.get("wins", 0)
            losses = state.get("losses", 0)
            total = wins + losses
            wr = (wins / total * 100) if total > 0 else 0
            pending_val = sum(float(p.get("size", 0)) for p in state.get("pending", []))
            strats = {"CRYPTO_SPEED": 0, "NBA_PACE": 0, "WHALE_MIRROR": 0}
            for p in state.get("pending", []):
                s = p.get("strategy", "OTHER")
                strats[s] = strats.get(s, 0) + 1

            mode_str = "PAPER" if PAPER_MODE else "LIVE"
            if PAPER_MODE and go_live_at:
                hrs_left = max(0, (go_live_at - time.time()) / 3600)
                mode_str = f"PAPER ({hrs_left:.1f}h to live)"
            elapsed_h = (time.time() - start_time) / 3600

            print(f"\n{'='*66}")
            print(f"  ALPHA BOT [{mode_str}] | {now_str}")
            print(f"{'='*66}")
            print(f"  Bankroll: ${state['bankroll']:>10.2f}  |  PnL: ${state['pnl']:>+10.2f}")
            print(f"  Record:   {wins}W-{losses}L ({wr:.1f}%)  |  Trades: {state['trades']}  |  Today: {state['daily_trades']}")
            print(f"  Pending:  {len(state.get('pending',[]))} positions (${pending_val:.2f} deployed)")
            print(f"  Crypto: {strats.get('CRYPTO_SPEED',0)} | NBA: {strats.get('NBA_PACE',0)} | Whale: {strats.get('WHALE_MIRROR',0)}")
            print(f"  Uptime: {elapsed_h:.1f}h | Cycle: {cycle}")
            if signals_found:
                print(f"  >> {signals_found} new trades this cycle!")
            print(f"{'='*66}")

            save_state(state)
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"\n  [FATAL] {e}")
            traceback.print_exc()
            save_state(state)
            time.sleep(30)

    save_state(state)
    print(f"\n[EXIT] Final bankroll: ${state['bankroll']:.2f} | PnL: ${state['pnl']:+.2f}")


if __name__ == "__main__":
    run()
