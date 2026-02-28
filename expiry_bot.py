#!/usr/bin/env python3
"""
EXPIRY SCALP BOT v3 — Kitchen Sink Strategy
========================================================================

Combined strategy for high win rate + high volume near-expiry scalping:

1. TIME-DECAY LADDER — Scale min ask by time to expiry:
   <1h left: 92c+ | 1-3h: 95c+ | 3-6h: 97c+

2. LIVE PRICE CONFIRMATION — Cross-reference asset price vs strike:
   Crypto: Binance spot price | Stocks: Yahoo Finance
   If confirmed (price far from strike), override ladder to 92c+ at any horizon.
   Unverifiable markets (politics, etc.): floor at 96c+.

3. SPREAD GATEKEEPER — Veto trades with wide bid-ask spreads:
   ≤2c: trade at threshold | 2-4c: require 95c+ min | >4c: skip

Scans ALL Polymarket markets closing in 6h, excluding soccer.
Paper trading mode for validation.
"""

import time
import csv
import os
import re
import sys
import json
import traceback
from datetime import datetime, timezone, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from web3 import Web3
from eth_account import Account

# ── Systemd watchdog ──
def _watchdog_ping():
    """Notify systemd watchdog that the process is alive."""
    try:
        import socket
        addr = os.environ.get("NOTIFY_SOCKET")
        if not addr:
            return
        if addr[0] == "@":
            addr = "\0" + addr[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.sendto(b"WATCHDOG=1", addr)
        finally:
            sock.close()
    except Exception:
        pass

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
        positions = _http.get(
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
# RETRY-ENABLED HTTP SESSION
# ═══════════════════════════════════════════════════════════════
_retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
_http = requests.Session()
_http.mount("https://", HTTPAdapter(max_retries=_retry_strategy))
_http.mount("http://", HTTPAdapter(max_retries=_retry_strategy))

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

PAPER_MODE = False

# Scanning
SCAN_AHEAD_HOURS = 6
SCAN_INTERVAL = 300
MIN_GAMMA_PRICE = 0.88        # Loose pre-filter; real gate is strategy eval
MIN_LIQUIDITY = 500

# Time-Decay Ladder
TIER1_HOURS = 1               # <1h
TIER1_ASK = 0.92
TIER2_HOURS = 3               # 1-3h
TIER2_ASK = 0.95
TIER3_ASK = 0.97              # 3-6h
MAX_ASK = 0.97                # Cap: never pay more than 97c (was 99c)
MIN_ROI_PCT = 3.0             # Skip trades with <3% potential ROI

# Unverifiable market floor (no price feed to confirm)
UNVERIFIABLE_MIN_ASK = 0.96

# Spread gatekeeper
SPREAD_VETO = 0.04            # >4c spread: skip
SPREAD_PREMIUM_THRESHOLD = 0.02  # 2-4c spread: floor at 95c
SPREAD_PREMIUM_ASK = 0.95

# Price confirmation distance (% of strike)
CONFIRM_DIST_TIER1 = 0.3      # <1h: 0.3% away from strike
CONFIRM_DIST_TIER2 = 0.8      # 1-3h: 0.8%
CONFIRM_DIST_TIER3 = 1.5      # 3-6h: 1.5%

# Trading
BET_SIZE = 20.00              # Flat bet size per trade (raised — fewer but better trades)
MIN_BET_SIZE = 1.00
MAX_BET_SIZE = 25.00
MIN_BOOK_DEPTH = 10.0         # Min $ at ask in orderbook
STOCKS_ONLY = True            # Only trade markets with stock/crypto price feeds
SPORTS_ENABLED = True         # Also allow NBA, NCAAB, NHL markets
SPORTS_MIN_ASK = 0.95         # 95c+ floor for sports markets

# Stop-loss
STOP_LOSS_PCT = 0.10          # Sell if price drops 10% below entry
STOP_LOSS_CHECK_INTERVAL = 10 # Check positions every 10s (was 30s)

# Risk
STARTING_BANKROLL = 100.00
KILL_SWITCH_MIN = 5.00
MAX_PENDING = 50
MAX_DAILY_TRADES = 200

# Resolution
RESOLVE_INTERVAL = 300        # Check for resolution every 5 min after event ends

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
    "hours_left", "strategy_tier", "confirmed", "live_price",
]


# ═══════════════════════════════════════════════════════════════
# STATE & LOGGING
# ═══════════════════════════════════════════════════════════════

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        if data.get("version") == 3:
            return data
    return {
        "version": 3,
        "bankroll": STARTING_BANKROLL,
        "pnl": 0.0,
        "wins": 0,
        "losses": 0,
        "trades": 0,
        "daily_trades": 0,
        "last_trade_date": "",
        "pending": [],
        "resolved_trades": [],
        "traded_tokens": [],
        "paper_mode": PAPER_MODE,
    }


def save_state(state):
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(STATE_FILE) or ".", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, STATE_FILE)
    except Exception as e:
        print(f"  [WARN] Failed to save state: {e}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


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
# SOCCER BLACKLIST
# ═══════════════════════════════════════════════════════════════

_SOCCER_BLOCK = [
    # Market formats
    "spread:",          # Soccer spread/lines
    "o/u ",             # Over/under (goals)
    "over/under",
    "total goals",
    "both teams to score",
    "clean sheet",
    "first goal",
    "match result",
    # Team identifiers
    " fc ", " fc,", " fc?", "(fc)",
    " afc ", " afc,", " afc?",
    " cf ", " sc ",
    " ca ",             # Club Atlético (Argentine soccer)
    "united ",
    "rovers", "wanderers",
    "athletic",
    "atletico",
    "real madrid", "barcelona",
    "esgrima",          # Argentine soccer clubs
    "barracas", "tigre",
    "rosario", "platense",
    "boca juniors", "river plate",
    # Leagues
    "premier league", "la liga", "serie a", "bundesliga",
    "champions league", "europa league", "ligue 1",
    "eredivisie", "efl ", "league cup",
    "copa america", "copa libertadores",
    "world cup", "liga profesional",
    "superliga", "primera division",
    # Generic
    "soccer", "football club", "futbol",
    "goalkeeper", "penalty kick",
]


def _is_soccer(question: str) -> bool:
    q = " " + question.strip().lower() + " "
    if q.strip().startswith("spread:"):
        return True
    if q.strip().startswith("o/u "):
        return True
    # Check for "X vs. Y: O/U" pattern (soccer over/under)
    if " vs." in q and ("o/u " in q or "over/under" in q):
        return True
    return any(pat in q for pat in _SOCCER_BLOCK)


# ═══════════════════════════════════════════════════════════════
# SPORTS ALLOW LIST (NBA, NCAAB, NHL)
# ═══════════════════════════════════════════════════════════════

_NBA_TEAMS = [
    "celtics", "nets", "knicks", "76ers", "sixers", "raptors",
    "bulls", "cavaliers", "cavs", "pistons", "pacers", "bucks",
    "hawks", "hornets", "heat", "magic", "wizards",
    "nuggets", "timberwolves", "thunder", "trail blazers", "blazers", "jazz",
    "warriors", "clippers", "lakers", "suns", "kings",
    "mavericks", "mavs", "rockets", "grizzlies", "pelicans", "spurs",
]

_NHL_TEAMS = [
    "bruins", "sabres", "red wings", "panthers", "canadiens", "habs",
    "senators", "maple leafs", "lightning",
    "hurricanes", "blue jackets", "devils", "islanders", "rangers",
    "flyers", "penguins", "capitals", "caps",
    "blackhawks", "avalanche", "stars", "wild", "predators",
    "blues", "jets", "coyotes",
    "ducks", "flames", "oilers", "kraken", "sharks", "canucks",
    "golden knights", "kings",
]

_NCAAB_PATTERNS = [
    # Conference/league tags
    "ncaa", "march madness", "final four",
    # Common team name suffixes that indicate college basketball
    "bulldogs", "wildcats", "eagles", "tigers", "bears", "panthers",
    "hawks", "blue devils", "tar heels", "hoosiers", "jayhawks",
    "spartans", "wolverines", "boilermakers", "fighting irish",
    "longhorns", "aggies", "sooners", "razorbacks", "volunteers",
    "crimson tide", "gators", "seminoles", "terrapins", "huskies",
    "cougars", "red raiders", "mountaineers", "cyclones",
    "golden eagles", "bobcats", "antelopes", "titans",
    "paladins", "commodores", "coyotes", "warhawks",
]


def _is_sports_allowed(question: str) -> bool:
    """Detect NBA, NCAAB, and NHL moneyline markets (not O/U or spreads)."""
    q = question.strip().lower()

    # Skip O/U and spread markets — only moneyline (straight win)
    if "o/u " in q or "over/under" in q or q.startswith("spread:"):
        return False
    # Skip women's games (W) suffix
    if question.rstrip().endswith("(W)"):
        return False
    # Skip esports / Counter-Strike / LoL / Honor of Kings
    if any(x in q for x in ["counter-strike", "valorant", "lol:", "dota",
                             "honor of kings", "map handicap", "map 1",
                             "map 2", "map 3", "(bo3)", "(bo5)"]):
        return False
    # Skip half-time / quarter markets
    if "1h moneyline" in q or "1q moneyline" in q:
        return False
    # Skip AHL (minor league hockey)
    if "ahl:" in q:
        return False

    # Must have "vs." to be a game market
    if " vs." not in q and " vs " not in q:
        return False

    # Check NBA
    if any(team in q for team in _NBA_TEAMS):
        return True
    # Check NHL
    if any(team in q for team in _NHL_TEAMS):
        return True
    # Check NCAAB
    if any(pat in q for pat in _NCAAB_PATTERNS):
        return True

    return False


# ═══════════════════════════════════════════════════════════════
# PRICE FEEDS
# ═══════════════════════════════════════════════════════════════

_price_cache = {}  # key -> (price, timestamp)


def _cached_price(key, fetch_fn, ttl=5):
    now = time.time()
    if key in _price_cache:
        price, ts = _price_cache[key]
        if now - ts < ttl and price > 0:
            return price
    try:
        price = fetch_fn()
    except Exception:
        price = 0.0
    if price and price > 0:
        _price_cache[key] = (price, now)
        return price
    # Return stale cache if fetch failed
    if key in _price_cache:
        return _price_cache[key][0]
    return 0.0


# ── Crypto via Binance ──

_CRYPTO_MAP = {
    # Crypto disabled — stocks only perform better
    # "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    # "ethereum": "ETHUSDT", "eth": "ETHUSDT",
    # "solana": "SOLUSDT", "sol": "SOLUSDT",
    # "dogecoin": "DOGEUSDT", "doge": "DOGEUSDT",
    # "xrp": "XRPUSDT",
    # "cardano": "ADAUSDT", "ada": "ADAUSDT",
    # "avalanche": "AVAXUSDT", "avax": "AVAXUSDT",
    # "polygon": "MATICUSDT", "matic": "MATICUSDT",
    # "chainlink": "LINKUSDT", "link": "LINKUSDT",
}


def _extract_crypto_symbol(question: str):
    q = question.lower()
    for name, sym in _CRYPTO_MAP.items():
        if re.search(r'\b' + re.escape(name) + r'\b', q):
            return sym
    return None


def _fetch_binance(symbol):
    r = _http.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": symbol}, timeout=3,
    )
    return float(r.json()["price"])


def get_crypto_price(question: str) -> float:
    sym = _extract_crypto_symbol(question)
    if not sym:
        return 0.0
    return _cached_price(f"crypto:{sym}", lambda: _fetch_binance(sym), ttl=5)


# ── Stocks via Yahoo Finance ──

_KNOWN_TICKERS = {
    "SPY", "QQQ", "DIA", "VTI",
    "AAPL", "MSFT", "GOOG", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    "AMD", "NFLX", "DIS", "BA", "INTC", "CRM", "ORCL", "UBER", "ABNB",
    "JPM", "GS", "MS", "BAC", "WFC", "C", "V", "MA", "AXP",
    "XOM", "CVX", "COP",
    "PFE", "JNJ", "UNH", "ABBV", "LLY", "MRK", "BMY",
    "WMT", "TGT", "COST", "HD", "LOW",
    "COIN", "MSTR", "MARA", "RIOT",
    "GME", "AMC",
}

# Map common names to tickers
_NAME_TO_TICKER = {
    "s&p 500": "SPY", "s&p": "SPY", "sp500": "SPY",
    "nasdaq": "QQQ",
    "dow jones": "DIA", "dow": "DIA",
    "apple": "AAPL", "microsoft": "MSFT", "google": "GOOG",
    "alphabet": "GOOG", "amazon": "AMZN", "nvidia": "NVDA",
    "tesla": "TSLA",
}


def _extract_stock_ticker(question: str):
    q_lower = question.lower()
    # Check name mappings first (word-boundary match to avoid "Down" → "dow")
    for name, ticker in _NAME_TO_TICKER.items():
        if re.search(r'\b' + re.escape(name) + r'\b', q_lower):
            return ticker
    # Check for explicit tickers in question
    for word in question.split():
        clean = word.strip('.,!?()[]"\'')
        if clean in _KNOWN_TICKERS:
            return clean
    # Check parenthetical tickers: (AAPL)
    match = re.search(r'\(([A-Z]{1,5})\)', question)
    if match and match.group(1) in _KNOWN_TICKERS:
        return match.group(1)
    return None


def _fetch_yahoo(ticker):
    r = _http.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"interval": "1m", "range": "1d"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=5,
    )
    data = r.json()
    return float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])


def get_stock_price(question: str) -> float:
    ticker = _extract_stock_ticker(question)
    if not ticker:
        return 0.0
    return _cached_price(f"stock:{ticker}", lambda: _fetch_yahoo(ticker), ttl=30)


# ── General ──

def get_live_price(question: str) -> float:
    """Try crypto first, then stocks. Returns 0 if no feed available."""
    p = get_crypto_price(question)
    if p > 0:
        return p
    return get_stock_price(question)


def parse_strike_price(question: str):
    """Extract dollar amount from question like 'Will BTC close above $95,000?'"""
    match = re.search(r'\$(\d+(?:,\d{3})*(?:\.\d+)?)', question)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def has_price_feed(question: str) -> bool:
    return bool(_extract_crypto_symbol(question) or _extract_stock_ticker(question))


# ═══════════════════════════════════════════════════════════════
# STRATEGY EVALUATION
# ═══════════════════════════════════════════════════════════════

def get_time_tier_ask(hours_left: float) -> float:
    """Time-decay ladder: base min ask from hours remaining."""
    if hours_left <= TIER1_HOURS:
        return TIER1_ASK
    elif hours_left <= TIER2_HOURS:
        return TIER2_ASK
    else:
        return TIER3_ASK


def get_confirm_distance_pct(hours_left: float) -> float:
    """Min % distance from strike needed for price confirmation."""
    if hours_left <= TIER1_HOURS:
        return CONFIRM_DIST_TIER1
    elif hours_left <= TIER2_HOURS:
        return CONFIRM_DIST_TIER2
    else:
        return CONFIRM_DIST_TIER3


def check_price_confirmation(question: str, hours_left: float):
    """
    Check if live asset price confirms the market outcome.
    Returns (confirmed: bool, live_price: float).
    """
    live_price = get_live_price(question)
    if live_price <= 0:
        return False, 0.0

    strike = parse_strike_price(question)
    if not strike or strike <= 0:
        return False, live_price  # Have price feed but no strike to compare

    distance_pct = abs(live_price - strike) / strike * 100
    min_dist = get_confirm_distance_pct(hours_left)

    return distance_pct >= min_dist, live_price


def evaluate_candidate(question, clob_ask, clob_bid, hours_left):
    """
    Run all three strategy filters. Returns:
      (should_trade: bool, tier: str, confirmed: bool, live_price: float, reason: str)
    """
    # ── Filter 0: Stocks/crypto + allowed sports ──
    is_sports = SPORTS_ENABLED and _is_sports_allowed(question)
    if STOCKS_ONLY and not has_price_feed(question) and not is_sports:
        return False, "", False, 0.0, "no_price_feed"

    spread = clob_ask - clob_bid

    # ── Filter 3: Spread Gatekeeper ──
    if spread > SPREAD_VETO:
        return False, "", False, 0.0, f"spread ${spread:.3f} > ${SPREAD_VETO:.2f}"

    # ── Filter 1: Time-Decay Ladder (base threshold) ──
    base_ask = get_time_tier_ask(hours_left)
    tier = f"T1(<{TIER1_HOURS}h)" if hours_left <= TIER1_HOURS else \
           f"T2(<{TIER2_HOURS}h)" if hours_left <= TIER2_HOURS else \
           f"T3(<{SCAN_AHEAD_HOURS}h)"

    # ── Filter 2: Price Confirmation ──
    confirmed, live_price = check_price_confirmation(question, hours_left)

    if confirmed:
        # Override: allow 92c+ when confirmed
        min_ask = TIER1_ASK
    elif has_price_feed(question):
        # Has price feed but not confirmed (too close to strike)
        min_ask = base_ask
    elif is_sports:
        # Sports: flat 95c+ floor
        min_ask = SPORTS_MIN_ASK
    else:
        # No price feed: unverifiable market → higher floor
        min_ask = max(base_ask, UNVERIFIABLE_MIN_ASK)

    # ── Spread premium: bump floor if spread 2-4c ──
    if spread > SPREAD_PREMIUM_THRESHOLD:
        min_ask = max(min_ask, SPREAD_PREMIUM_ASK)

    # ── Final check ──
    if clob_ask < min_ask:
        return False, tier, confirmed, live_price, \
            f"ask ${clob_ask:.2f} < min ${min_ask:.2f} ({tier})"
    if clob_ask > MAX_ASK:
        return False, tier, confirmed, live_price, \
            f"ask ${clob_ask:.2f} > max ${MAX_ASK:.2f}"

    roi = (1 - clob_ask) / clob_ask * 100
    if roi < MIN_ROI_PCT:
        return False, tier, confirmed, live_price, \
            f"ROI {roi:.1f}% < min {MIN_ROI_PCT:.0f}%"

    return True, tier, confirmed, live_price, "OK"


# ═══════════════════════════════════════════════════════════════
# MARKET SCANNER
# ═══════════════════════════════════════════════════════════════

def scan_markets() -> list:
    """Scan Gamma API for markets closing within SCAN_AHEAD_HOURS."""
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
            resp = _http.get(
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

    candidates = []
    soccer_skipped = 0
    longdated_skipped = 0

    # Build date strings for same-day/same-week validation
    _today = now.strftime("%B %-d")          # e.g. "February 27"
    _today_alt = now.strftime("%b %-d")      # e.g. "Feb 27"
    _weekday = now.strftime("%A")            # e.g. "Thursday"
    _month_name = now.strftime("%B")         # e.g. "February"
    _month_day = now.day
    # Week-ending markets: "week of February 23" etc.
    _week_start = (now - timedelta(days=now.weekday())).strftime("%B %-d")

    def _is_near_term(question: str, end_dt) -> bool:
        """Only allow markets that clearly resolve today/this week."""
        q = question.lower()
        # Must mention a specific near-term date anchor
        # Today: "on February 27", "February 27"
        if _today.lower() in q or _today_alt.lower() in q:
            return True
        # "week of February 23" (current week)
        if _week_start.lower() in q:
            return True
        # "Up or Down" daily markets mention the date
        if "up or down" in q and _today.lower().split()[-1] in q:
            return True
        # "end of February" is OK if we're in the last 2 days of Feb
        if "end of february" in q and _month_name == "February" and _month_day >= 26:
            return True
        if "end of march" in q and _month_name == "March" and _month_day >= 29:
            return True
        # Sports markets with today's date are OK
        if "tonight" in q or "today" in q:
            return True
        return False

    for m in all_markets:
        if not m.get("acceptingOrders", False):
            continue

        liq = float(m.get("liquidityNum", 0) or 0)
        if liq < MIN_LIQUIDITY:
            continue

        question = m.get("question", "")
        if _is_soccer(question):
            soccer_skipped += 1
            continue

        prices = json.loads(m.get("outcomePrices", "[]"))
        outcomes = json.loads(m.get("outcomes", "[]"))
        tokens = json.loads(m.get("clobTokenIds", "[]"))

        end_str = m.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except Exception:
            continue

        # Reject markets that don't clearly resolve today/this week
        if not _is_near_term(question, end_dt):
            longdated_skipped += 1
            continue

        for i, p_str in enumerate(prices):
            p = float(p_str)
            if p >= MIN_GAMMA_PRICE and i < len(tokens):
                candidates.append({
                    "question": question,
                    "outcome": outcomes[i] if i < len(outcomes) else "?",
                    "end_date": end_str,
                    "end_dt": end_dt,
                    "gamma_price": p,
                    "token_id": tokens[i],
                    "condition_id": m.get("conditionId", ""),
                    "liquidity": liq,
                    "neg_risk": m.get("negRisk", False),
                })

    candidates.sort(key=lambda x: x["end_date"])

    if soccer_skipped:
        print(f"  [FILTER] Skipped {soccer_skipped} soccer markets")
    if longdated_skipped:
        print(f"  [FILTER] Skipped {longdated_skipped} long-dated/ambiguous markets")

    return candidates


def get_clob_prices(token_id: str) -> dict:
    result = {"bid": 0.0, "ask": 0.0}
    try:
        ask_r = _http.get(
            f"{CLOB_API}/price?token_id={token_id}&side=SELL", timeout=8
        )
        result["ask"] = float(ask_r.json().get("price", 0))
        bid_r = _http.get(
            f"{CLOB_API}/price?token_id={token_id}&side=BUY", timeout=8
        )
        result["bid"] = float(bid_r.json().get("price", 0))
    except Exception:
        pass
    return result


def check_book_depth(token_id: str) -> float:
    try:
        r = _http.get(
            f"{CLOB_API}/book?token_id={token_id}", timeout=5
        )
        book = r.json()
        orders = book.get("asks", [])
        if not orders:
            return 0.0
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


# ═══════════════════════════════════════════════════════════════
# KELLY CRITERION BET SIZING
# ═══════════════════════════════════════════════════════════════

def kelly_bet_size(ask_price, bankroll, resolved_trades, tier=""):
    """
    Calculate bet size using fractional Kelly criterion.
    Returns (bet_amount, win_rate, edge_info_str).

    Kelly: f* = (b*p - q) / b
      b = net odds = (1 - ask) / ask
      p = estimated win probability (from historical tier data)
      q = 1 - p
    """
    # Need enough data to estimate win rate
    if len(resolved_trades) < KELLY_MIN_TRADES:
        bet = min(BET_SIZE, bankroll)
        return bet, 0.0, "fixed (insufficient data)"

    # Calculate win rate for this tier, fall back to overall
    tier_trades = [t for t in resolved_trades if t.get("strategy_tier") == tier] if tier else []
    if len(tier_trades) >= 5:
        wins = sum(1 for t in tier_trades if t.get("won"))
        p = wins / len(tier_trades)
        source = f"tier:{tier} ({wins}/{len(tier_trades)})"
    else:
        wins = sum(1 for t in resolved_trades if t.get("won"))
        p = wins / len(resolved_trades)
        source = f"overall ({wins}/{len(resolved_trades)})"

    b = (1.0 - ask_price) / ask_price  # net odds
    q = 1.0 - p

    kelly_f = (b * p - q) / b if b > 0 else 0.0

    if kelly_f <= KELLY_MIN_EDGE:
        return 0.0, p, f"no edge (kelly={kelly_f:.3f}, p={p:.1%}, {source})"

    # Fractional Kelly
    fraction = kelly_f * KELLY_FRACTION
    bet = round(bankroll * fraction, 2)
    bet = max(MIN_BET_SIZE, min(bet, MAX_BET_SIZE, bankroll))

    return bet, p, f"kelly={kelly_f:.3f}×{KELLY_FRACTION}={fraction:.3f}, p={p:.1%}, {source}"


# ═══════════════════════════════════════════════════════════════
# TRADE EXECUTION
# ═══════════════════════════════════════════════════════════════

def execute_trade(state, candidate, clob_ask, clob_bid,
                  tier="", confirmed=False, live_price=0.0, hours_left=0.0):
    ask_price = round(clob_ask, 2)

    # Flat bet sizing
    bet = min(BET_SIZE, state["bankroll"])
    shares = int(bet / ask_price)
    if shares < 1:
        return False

    token_id = candidate["token_id"]
    actual_cost = round(shares * ask_price, 2)
    spread = round(clob_ask - clob_bid, 3)
    roi_pct = round((1 - ask_price) / ask_price * 100, 2)
    order_id = ""

    if PAPER_MODE:
        order_id = f"PAPER-{int(time.time() * 1000)}"
        print(f"  [PAPER] Simulated fill: {shares} shares @ ${ask_price:.2f}")
    else:
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
                print(f"  [NOFILL] {candidate['outcome']} @ ${ask_price:.2f}")
                return False
            order_id = resp.get("orderID", "")
        except Exception as e:
            err_msg = str(e).lower()
            if "not enough balance" in err_msg or "allowance" in err_msg:
                print(f"  [LOW BALANCE] Wallet has insufficient USDC — "
                      f"halting trades this cycle")
                return "low_balance"
            print(f"  [ERROR] Order failed: {e}")
            return False

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
        "hours_left": round(hours_left, 2),
        "strategy_tier": tier,
        "confirmed": confirmed,
        "live_price": round(live_price, 2) if live_price else "",
    }

    state["pending"].append(trade)
    state["trades"] += 1
    state["daily_trades"] += 1
    state["bankroll"] -= actual_cost
    state["traded_tokens"].append(token_id)

    if len(state["traded_tokens"]) > 500:
        pending_tokens = {t["token_id"] for t in state["pending"]}
        keep = [t for t in state["traded_tokens"] if t in pending_tokens]
        non_pending = [t for t in state["traded_tokens"] if t not in pending_tokens]
        state["traded_tokens"] = keep + non_pending[-250:]

    save_state(state)
    log_trade(trade)

    mode = "PAPER" if PAPER_MODE else "LIVE"
    conf = " CONFIRMED" if confirmed else ""
    print(f"\n  >>> [{mode}] TRADE ({tier}{conf}): "
          f"{candidate['outcome']} @ ${ask_price:.2f} ({roi_pct:.1f}% ROI)")
    print(f"      {candidate['question'][:60]}")
    print(f"      ${actual_cost:.2f} for {shares} shares | "
          f"Profit: ${shares - actual_cost:.2f} | "
          f"Spread: ${spread:.3f}")
    print(f"      {hours_left:.1f}h left | "
          f"Bankroll: ${state['bankroll']:.2f}"
          + (f" | Price: ${live_price:,.2f}" if live_price else ""))
    print(f"      [BET] flat ${BET_SIZE:.2f}")

    return True


# ═══════════════════════════════════════════════════════════════
# STOP-LOSS MONITOR
# ═══════════════════════════════════════════════════════════════

def check_stop_losses(state):
    """Sell positions that have dropped 2c below entry to cap losses."""
    if not state["pending"] or PAPER_MODE:
        return

    still_pending = []
    for t in state["pending"]:
        entry = t.get("clob_ask", 0)
        token_id = t.get("token_id", "")
        stop_price = round(entry * (1.0 - STOP_LOSS_PCT), 2)

        try:
            prices = get_clob_prices(token_id)
            current_bid = prices["bid"]
        except Exception:
            still_pending.append(t)
            continue

        if current_bid <= 0:
            # Bid is zero — market likely resolved against us, dump at any price
            # Try to sell at 0.01 (penny) to recover anything, otherwise mark as loss
            try:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=0.01,
                    size=t["shares"],
                    side="SELL",
                )
                signed_order = clob_client.create_order(order_args)
                resp = clob_client.post_order(signed_order, OrderType.FOK)
                if resp and resp.get("success"):
                    proceeds = round(t["shares"] * 0.01, 2)
                    pnl = round(proceeds - t["bet_size"], 2)
                else:
                    proceeds = 0
                    pnl = round(-t["bet_size"], 2)
            except Exception:
                proceeds = 0
                pnl = round(-t["bet_size"], 2)

            state["bankroll"] += proceeds
            state["losses"] += 1
            state["pnl"] += pnl
            t["resolved"] = True
            t["won"] = False
            t["pnl"] = pnl
            t["bankroll_after"] = round(state["bankroll"], 2)
            t["resolved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            log_trade(t)
            state.setdefault("resolved_trades", []).append(t)
            print(f"\n  [STOP-LOSS] BID=0 — dumped {t['outcome']} | PnL: ${pnl:+.2f}")
            print(f"      {t['question'][:60]}")
            continue

        if current_bid < stop_price:
            # Price dropped below stop — sell at market bid
            try:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=current_bid,
                    size=t["shares"],
                    side="SELL",
                )
                signed_order = clob_client.create_order(order_args)
                resp = clob_client.post_order(signed_order, OrderType.FOK)
                if resp and resp.get("success"):
                    proceeds = round(t["shares"] * current_bid, 2)
                    pnl = round(proceeds - t["bet_size"], 2)
                    state["bankroll"] += proceeds
                    state["losses"] += 1
                    state["pnl"] += pnl
                    t["resolved"] = True
                    t["won"] = False
                    t["pnl"] = pnl
                    t["bankroll_after"] = round(state["bankroll"], 2)
                    t["resolved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    log_trade(t)
                    state.setdefault("resolved_trades", []).append(t)

                    print(f"\n  [STOP-LOSS] Sold {t['outcome']} | "
                          f"entry=${entry:.2f} → exit=${current_bid:.2f} | "
                          f"PnL: ${pnl:+.2f}")
                    print(f"      {t['question'][:60]}")
                else:
                    print(f"  [STOP-LOSS] Sell failed for {t['outcome'][:30]}, keeping position")
                    still_pending.append(t)
            except Exception as e:
                print(f"  [STOP-LOSS] Error selling: {e}")
                still_pending.append(t)
        elif current_bid >= entry:
            # Price went up — log it so we know
            still_pending.append(t)
        else:
            still_pending.append(t)

    state["pending"] = still_pending
    if len(state["pending"]) != len(still_pending):
        save_state(state)


# ═══════════════════════════════════════════════════════════════
# RESOLUTION
# ═══════════════════════════════════════════════════════════════

def resolve_trades(state):
    if not state["pending"]:
        return

    now = datetime.now(timezone.utc)
    still_pending = []

    for t in state["pending"]:
        try:
            end = datetime.fromisoformat(t["end_date"].replace("Z", "+00:00"))
        except Exception:
            still_pending.append(t)
            continue

        age = (now - end).total_seconds()

        if PAPER_MODE:
            if age > 60:
                pnl = t["shares"] - t["bet_size"]
                state["bankroll"] += t["shares"] * 1.0
                state["wins"] += 1
                state["pnl"] += pnl
                t["resolved"] = True
                t["won"] = True
                t["pnl"] = round(pnl, 4)
                t["bankroll_after"] = round(state["bankroll"], 2)
                t["resolved_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                log_trade(t)
                state.setdefault("resolved_trades", []).append(t)
                w, l = state["wins"], state["losses"]
                wr = w / max(w + l, 1)
                print(f"\n  >>> PAPER WIN: {t['outcome']} | "
                      f"PnL: ${pnl:+.4f} | {w}W-{l}L ({wr:.1%}) | "
                      f"Bank: ${state['bankroll']:.2f}")
            else:
                still_pending.append(t)
            continue

        # Live mode resolution
        if age < 0:
            still_pending.append(t)
            continue

        try:
            wallet = os.environ["POLYMARKET_WALLET"]
            positions = _http.get(
                f"https://data-api.polymarket.com/positions?user={wallet}",
                timeout=15,
            ).json()
        except Exception:
            still_pending.append(t)
            continue

        pos_map = {}
        for p in positions:
            tid = p.get("asset", "") or p.get("tokenId", "")
            if tid:
                pos_map[tid] = p

        tid = t.get("token_id", "")
        pos = pos_map.get(tid)

        if pos and pos.get("redeemable"):
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
            t["resolved_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            log_trade(t)
            state.setdefault("resolved_trades", []).append(t)

            mark = "WIN" if won else "LOSS"
            w, l = state["wins"], state["losses"]
            wr = w / max(w + l, 1)
            print()
            print("  " + "=" * 60)
            print(f"  ORACLE RESOLVED: {t['question'][:55]}")
            print(f"  Outcome: {t['outcome']} → {mark}")
            print(f"  Bought @ ${t['clob_ask']:.2f} × {t['shares']} shares "
                  f"= ${t['bet_size']:.2f}")
            print(f"  PnL: ${pnl:+.2f} | Bankroll: ${state['bankroll']:.2f} | "
                  f"{w}W-{l}L ({wr:.1%})")
            print("  " + "=" * 60)
        elif age > 259200:
            pnl = -t["bet_size"]
            state["losses"] += 1
            state["pnl"] += pnl
            t["resolved"] = True
            t["won"] = False
            t["pnl"] = round(pnl, 4)
            t["bankroll_after"] = round(state["bankroll"], 2)
            t["resolved_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            log_trade(t)
            state.setdefault("resolved_trades", []).append(t)
            print(f"  [STALE] Loss (>72h): {t['question'][:50]}")
        else:
            still_pending.append(t)

    state["pending"] = still_pending
    # Keep only last 50 resolved trades in state
    if len(state.get("resolved_trades", [])) > 50:
        state["resolved_trades"] = state["resolved_trades"][-50:]


# ═══════════════════════════════════════════════════════════════
# DISPLAY
# ═══════════════════════════════════════════════════════════════

def print_banner():
    mode = "PAPER" if PAPER_MODE else "LIVE"
    print("=" * 70)
    print(f"  EXPIRY SCALP BOT v3 — Kitchen Sink Strategy [{mode}]")
    print(f"  Ladder: <{TIER1_HOURS}h={TIER1_ASK:.0%} | "
          f"<{TIER2_HOURS}h={TIER2_ASK:.0%} | "
          f"<{SCAN_AHEAD_HOURS}h={TIER3_ASK:.0%}")
    print(f"  Confirmation: crypto+stocks → override to {TIER1_ASK:.0%}")
    print(f"  Spread gate: >{SPREAD_VETO*100:.0f}c=skip | "
          f">{SPREAD_PREMIUM_THRESHOLD*100:.0f}c=floor {SPREAD_PREMIUM_ASK:.0%}")
    print(f"  Unverifiable floor: {UNVERIFIABLE_MIN_ASK:.0%}")
    print(f"  Max ask: {MAX_ASK:.0%} | Min ROI: {MIN_ROI_PCT:.0f}% | Stop-loss: {STOP_LOSS_PCT:.0%} every {STOP_LOSS_CHECK_INTERVAL}s")
    print(f"  Bet: ${BET_SIZE:.2f} flat | Scan: {SCAN_INTERVAL}s | Soccer: blocked")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)


def print_dashboard(state, n_candidates=0, n_evaluated=0, n_passed=0):
    w, l = state["wins"], state["losses"]
    wr = w / max(w + l, 1)
    pending = len(state["pending"])
    pending_value = sum(t["bet_size"] for t in state["pending"])
    mode = "PAPER" if PAPER_MODE else "LIVE"

    print(f"\n{'=' * 70}")
    print(f"  DASHBOARD [{mode}] | {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"{'=' * 70}")
    print(f"  Bankroll:  ${state['bankroll']:>10,.2f}  |  PnL: ${state['pnl']:>+10,.2f}")
    print(f"  Record:    {w}W-{l}L ({wr:.1%})  |  "
          f"Trades: {state['trades']}  |  Today: {state['daily_trades']}")
    print(f"  Pending:   {pending} positions (${pending_value:,.2f} deployed)")
    print(f"  Pipeline:  {n_candidates} scanned → "
          f"{n_evaluated} CLOB checked → {n_passed} traded")
    print(f"{'=' * 70}")


# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

def run():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    init_log()
    state = load_state()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state["last_trade_date"] != today:
        state["daily_trades"] = 0
        state["last_trade_date"] = today

    print_banner()
    print(f"\n  Loaded state: {state['trades']} trades, "
          f"${state['bankroll']:.2f} bankroll, "
          f"{len(state['pending'])} pending")

    scan_count = 0
    last_resolve_ts = 0

    while True:
        try:
            _watchdog_ping()
            scan_count += 1
            now_utc = datetime.now(timezone.utc)
            now_ts = time.time()

            today = now_utc.strftime("%Y-%m-%d")
            if state["last_trade_date"] != today:
                state["daily_trades"] = 0
                state["last_trade_date"] = today
                print(f"\n  [NEW DAY] {today} — daily counter reset")

            # ── Stop-loss + Resolve + Redeem (every cycle) ──
            if state["pending"]:
                check_stop_losses(state)
            resolve_trades(state)

            if not PAPER_MODE:
                redeemed = redeem_positions()
                if redeemed:
                    print(f"  [REDEEM] Redeemed {redeemed} positions")

            # ── Safety ──
            if state["bankroll"] < KILL_SWITCH_MIN:
                print(f"\n  [KILL SWITCH] Bankroll ${state['bankroll']:.2f} "
                      f"< ${KILL_SWITCH_MIN:.2f}")
                print_dashboard(state)
                save_state(state)
                # Sleep in chunks to keep watchdog alive
                _deadline = time.time() + SCAN_INTERVAL
                while time.time() < _deadline:
                    time.sleep(min(30, _deadline - time.time()))
                    _watchdog_ping()
                continue

            if state["daily_trades"] >= MAX_DAILY_TRADES:
                print(f"\n  [CIRCUIT BREAKER] {state['daily_trades']} trades today")
                print_dashboard(state)
                save_state(state)
                # Sleep in chunks to keep watchdog alive
                _deadline = time.time() + SCAN_INTERVAL
                while time.time() < _deadline:
                    time.sleep(min(30, _deadline - time.time()))
                    _watchdog_ping()
                continue

            # ── Scan ──
            print(f"\n  [SCAN #{scan_count}] "
                  f"{now_utc.strftime('%H:%M:%S UTC')} — "
                  f"markets closing in {SCAN_AHEAD_HOURS}h | "
                  f"stocks/crypto only...")

            candidates = scan_markets()
            traded_set = set(state["traded_tokens"])
            fresh = [c for c in candidates if c["token_id"] not in traded_set]

            print(f"  [SCAN] {len(candidates)} candidates, "
                  f"{len(fresh)} fresh")

            # ── Evaluate each candidate ──
            trades_this_cycle = 0
            evaluated = 0
            skip_reasons = {}

            def can_trade():
                return (state["bankroll"] >= MIN_BET_SIZE
                        and len(state["pending"]) < MAX_PENDING
                        and state["daily_trades"] < MAX_DAILY_TRADES)

            low_balance_halt = False

            for c in fresh:
                if not can_trade():
                    break
                if low_balance_halt:
                    break

                hours_left = (c["end_dt"] - now_utc).total_seconds() / 3600
                if hours_left <= 0:
                    continue

                # Get CLOB price
                prices = get_clob_prices(c["token_id"])
                ask = prices["ask"]
                bid = prices["bid"]
                evaluated += 1

                if ask <= 0 or ask >= 1:
                    continue

                # Run strategy evaluation
                should_trade, tier, confirmed, live_price, reason = \
                    evaluate_candidate(c["question"], ask, bid, hours_left)

                if not should_trade:
                    bucket = reason.split(" ")[0]  # First word for grouping
                    skip_reasons[bucket] = skip_reasons.get(bucket, 0) + 1
                    continue

                # Check orderbook depth
                depth = check_book_depth(c["token_id"])
                if depth < MIN_BOOK_DEPTH:
                    skip_reasons["thin_book"] = skip_reasons.get("thin_book", 0) + 1
                    continue

                # Execute
                result = execute_trade(
                    state, c, ask, bid,
                    tier=tier, confirmed=confirmed,
                    live_price=live_price, hours_left=hours_left,
                )
                if result == "low_balance":
                    low_balance_halt = True
                    break
                if result:
                    trades_this_cycle += 1
                    traded_set.add(c["token_id"])
                    time.sleep(0.5)

            # Show skip summary
            if skip_reasons:
                parts = [f"{k}={v}" for k, v in
                         sorted(skip_reasons.items(), key=lambda x: -x[1])]
                print(f"  [SKIPS] {', '.join(parts)}")

            # ── Dashboard ──
            print_dashboard(state, len(candidates), evaluated, trades_this_cycle)
            save_state(state)

            # ── Wait: check stop-losses every 30s, resolve every 5 min ──
            next_scan = now_ts + SCAN_INTERVAL
            last_resolve_check = 0
            while time.time() < next_scan:
                sleep_secs = min(STOP_LOSS_CHECK_INTERVAL, next_scan - time.time())
                if sleep_secs <= 0:
                    break
                time.sleep(sleep_secs)
                _watchdog_ping()

                # Stop-loss check on every tick (every 30s)
                if state["pending"]:
                    check_stop_losses(state)

                # Resolve + redeem less frequently
                elapsed = time.time() - last_resolve_check
                if elapsed >= RESOLVE_INTERVAL and state["pending"]:
                    has_expired = any(
                        datetime.fromisoformat(
                            t["end_date"].replace("Z", "+00:00")
                        ) < datetime.now(timezone.utc)
                        for t in state["pending"]
                    )
                    if has_expired:
                        print(f"\n  [RESOLVE CHECK] "
                              f"{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
                        resolve_trades(state)
                        if not PAPER_MODE:
                            redeemed = redeem_positions()
                            if redeemed:
                                print(f"  [REDEEM] Redeemed {redeemed} positions")
                        save_state(state)
                    last_resolve_check = time.time()

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
    while True:
        try:
            run()
            break  # Clean exit (KeyboardInterrupt handled inside run())
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"\n[FATAL] Top-level crash: {e}")
            traceback.print_exc()
            print("[RESTART] Restarting in 60 seconds...")
            time.sleep(60)
