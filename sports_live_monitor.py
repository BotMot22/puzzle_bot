#!/usr/bin/env python3
"""
LIVE SPORTS MONITOR — 24/7/365
  1. Scans every 60s for NBA/NHL/NCAA favorites at 95c-99c
  2. Checks ESPN live game clock: only trades with <3 min left
     - NBA: <3 min in 4th quarter
     - NHL: <3 min in 3rd period
     - NCAA: <3 min in 2nd half
  3. Stop-loss: sells any position that dips 10% from entry price
  4. Force-redeems all resolved positions (dry-run first, no gas wasted)
  5. Updates shared expiry_state.json
"""
import os, json, math, time, re, requests, traceback
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════
MIN_ASK         = 0.95
MAX_ASK         = 0.99
MAX_SPREAD      = 0.04       # 4c max
MIN_BOOK_DEPTH  = 10.0       # $10 at ask (top 3 levels)
BET_SIZE        = 10.0
POLL_INTERVAL   = 60         # seconds between scans
SCAN_AHEAD_HRS  = 6          # Scan window for Polymarket
REDEEM_INTERVAL = 240        # Force-redeem every 4 min
STOP_LOSS_PCT   = 0.10       # Sell if bid drops 10% below entry
MAX_GAME_CLOCK  = 3.0        # Minutes remaining in final period/half

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "data", "expiry_state.json")

# ESPN scoreboard endpoints (limit=200 to catch smaller conference games)
ESPN_URLS = {
    "NBA":   "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?limit=200",
    "NHL":   "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?limit=200",
    "NCAAM": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?limit=200",
}

# Final period for each sport
FINAL_PERIOD = {
    "NBA": 4,    # 4th quarter
    "NHL": 3,    # 3rd period
    "NCAAM": 2,  # 2nd half
}

# ═══════════════════════════════════════════════════════════
#  ON-CHAIN SETUP (for redemption)
# ═══════════════════════════════════════════════════════════
wallet = os.environ["POLYMARKET_WALLET"]
w3 = Web3(Web3.HTTPProvider("https://polygon.drpc.org"))
acct = Account.from_key(os.environ["POLYMARKET_PRIVATE_KEY"])

CTF = w3.eth.contract(
    address=w3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
    abi=json.loads('[{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]'),
)
COLLATERAL = w3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
PARENT = b'\x00' * 32
USDC = w3.eth.contract(
    address=COLLATERAL,
    abi=[{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}],
)

# ═══════════════════════════════════════════════════════════
#  CLOB SETUP
# ═══════════════════════════════════════════════════════════
clob = ClobClient(
    "https://clob.polymarket.com",
    key=os.environ["POLYMARKET_PRIVATE_KEY"],
    chain_id=137,
    creds=ApiCreds(
        api_key=os.environ["POLYMARKET_API_KEY"],
        api_secret=os.environ["POLYMARKET_API_SECRET"],
        api_passphrase=os.environ["POLYMARKET_PASSPHRASE"],
    ),
)

# ═══════════════════════════════════════════════════════════
#  STATE
# ═══════════════════════════════════════════════════════════
traded_tokens = set()
redeemed_cids = set()
total_trades = 0
total_redeemed = 0


def get_usdc_balance():
    return USDC.functions.balanceOf(w3.to_checksum_address(wallet)).call() / 1e6


def load_traded_tokens():
    try:
        state = json.load(open(STATE_PATH))
        return set(state.get("traded_tokens", []))
    except:
        return set()


# ═══════════════════════════════════════════════════════════
#  ESPN GAME CLOCK
# ═══════════════════════════════════════════════════════════
def normalize_team(name):
    """Normalize team name for fuzzy matching."""
    name = name.lower().strip()
    # Remove common suffixes/prefixes
    for s in ["the ", "university of ", "u. of "]:
        name = name.replace(s, "")
    # Remove punctuation
    name = re.sub(r"[''.\-()]", "", name)
    # Normalize spaces
    name = re.sub(r"\s+", " ", name).strip()
    return name


def parse_clock_minutes(clock_str):
    """Parse ESPN clock string like '2:45' or '0:30.2' to minutes remaining."""
    try:
        clock_str = clock_str.strip()
        if ":" in clock_str:
            parts = clock_str.split(":")
            mins = int(parts[0])
            secs = float(parts[1])
            return mins + secs / 60.0
        else:
            return float(clock_str) / 60.0
    except:
        return 999.0


def fetch_espn_games():
    """Fetch all live games from ESPN with clock info.
    Returns dict: { normalized_team_name: {sport, period, clock_min, status, detail} }
    """
    games = {}
    for sport, url in ESPN_URLS.items():
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            for event in data.get("events", []):
                status = event.get("status", {})
                state = status.get("type", {}).get("name", "")
                clock_str = status.get("displayClock", "0:00")
                period = status.get("period", 0)
                detail = status.get("type", {}).get("detail", "")
                clock_min = parse_clock_minutes(clock_str)

                # Get team names from competitors
                competitors = event.get("competitions", [{}])[0].get("competitors", [])
                for comp in competitors:
                    team_name = comp.get("team", {}).get("displayName", "")
                    short_name = comp.get("team", {}).get("shortDisplayName", "")
                    abbrev = comp.get("team", {}).get("abbreviation", "")
                    score = comp.get("score", "0")

                    for name_variant in [team_name, short_name]:
                        key = normalize_team(name_variant)
                        if key:
                            games[key] = {
                                "sport": sport,
                                "period": period,
                                "clock_min": clock_min,
                                "status": state,
                                "detail": detail,
                                "score": score,
                                "final_period": FINAL_PERIOD[sport],
                            }
                    # Also store abbreviation
                    if abbrev:
                        games[normalize_team(abbrev)] = games.get(normalize_team(team_name), {})

        except Exception as e:
            print(f"  [ESPN ERR] {sport}: {e}")
    return games


def match_polymarket_to_espn(question, espn_games):
    """Try to match a Polymarket question to an ESPN game.
    Returns game info dict or None. Requires BOTH teams to match the same sport
    to avoid cross-sport false positives.
    """
    q = question.lower()
    match = re.search(r"(.+?)\s+vs\.?\s+(.+?)$", q)
    if not match:
        return None

    team_a = normalize_team(match.group(1).strip())
    team_b = normalize_team(match.group(2).strip())

    def find_match(team):
        """Find ESPN entry for a team name. Returns (key, data) or None."""
        # Exact match
        if team in espn_games:
            return (team, espn_games[team])
        # Match by last word (mascot) — must be 4+ chars to avoid false positives
        team_words = team.split()
        if not team_words:
            return None
        mascot = team_words[-1]
        if len(mascot) < 4:
            return None
        # Also use second-to-last word for two-word mascots (e.g. "blue devils", "red wings")
        for espn_key, data in espn_games.items():
            espn_words = espn_key.split()
            if not espn_words:
                continue
            espn_mascot = espn_words[-1]
            # Exact mascot match
            if mascot == espn_mascot and len(mascot) >= 4:
                return (espn_key, data)
            # Two-word team match (e.g. "trail blazers")
            if len(team_words) >= 2 and len(espn_words) >= 2:
                if " ".join(team_words[-2:]) == " ".join(espn_words[-2:]):
                    return (espn_key, data)
        return None

    match_a = find_match(team_a)
    match_b = find_match(team_b)

    # Require at least one team to match
    if match_a:
        # If both match, verify same sport
        if match_b and match_a[1].get("sport") != match_b[1].get("sport"):
            return None  # Cross-sport false positive
        return match_a[1]
    if match_b:
        return match_b[1]
    return None


def is_final_minutes(espn_info):
    """Check if game is in final 3 minutes of final period/half."""
    if not espn_info:
        return False, "no ESPN match"

    status = espn_info.get("status", "")
    period = espn_info.get("period", 0)
    clock_min = espn_info.get("clock_min", 999)
    final_period = espn_info.get("final_period", 4)
    detail = espn_info.get("detail", "")

    # Already final
    if status == "STATUS_FINAL":
        return True, "FINAL"

    # OT counts as final period
    if status == "STATUS_IN_PROGRESS":
        if period >= final_period and clock_min <= MAX_GAME_CLOCK:
            return True, f"P{period} {clock_min:.1f}min left"
        elif period > final_period:
            # Overtime
            return True, f"OT (P{period}) {clock_min:.1f}min"

    return False, detail


# ═══════════════════════════════════════════════════════════
#  SPORTS FILTER
# ═══════════════════════════════════════════════════════════
def is_sports_game(question):
    """NBA/NHL/NCAA men's basketball game markets only."""
    q = question.lower()
    if "o/u" in q or "spread" in q or "(w)" in q or "draw" in q or "both teams" in q:
        return False
    if " vs. " not in q and " vs " not in q:
        return False
    esports_kw = ["lol:", "counter-strike:", "valorant:", "dota 2:", "league of legends:",
                  "map handicap:", "game handicap:", "esports", "gaming", "bo1", "bo3",
                  "map 1", "map 2", "map 3", "game 1", "game 2", "game 3",
                  "overwatch:", "rocket league:", "rainbow six:", "call of duty:"]
    if any(kw in q for kw in esports_kw):
        return False
    soccer_kw = ["fc ", "united fc", "city fc", "rovers", "wednesday", "athletic",
                 "sporting", "real ", "dynamo", "fk ", "afc ", " fc", "cd ", "deportivo",
                 "olimpia", "cerro", "nacional", "libertad", "guarani"]
    if any(kw in q for kw in soccer_kw):
        return False
    return True


# ═══════════════════════════════════════════════════════════
#  MARKET SCANNER
# ═══════════════════════════════════════════════════════════
def scan_sports_markets():
    now = datetime.now(timezone.utc)
    future = now + timedelta(hours=SCAN_AHEAD_HRS)
    all_markets = []
    for offset in range(0, 500, 100):
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={
                    "active": True, "closed": False,
                    "end_date_min": now.isoformat(),
                    "end_date_max": future.isoformat(),
                    "limit": 100, "offset": offset,
                },
                timeout=15,
            )
            markets = r.json()
            if not markets:
                break
            for m in markets:
                if is_sports_game(m.get("question", "")):
                    all_markets.append(m)
        except Exception as e:
            print(f"  [SCAN ERR] {e}")
            break
    return all_markets


# ═══════════════════════════════════════════════════════════
#  TRADE LOGIC — requires ESPN game clock confirmation
# ═══════════════════════════════════════════════════════════
def check_and_trade(market, espn_games):
    """Check CLOB + ESPN clock. Trade only if <3 min left in final period."""
    global total_trades
    q = market.get("question", "")
    end = market.get("endDate", "")
    outcomes = json.loads(market["outcomes"]) if isinstance(market["outcomes"], str) else market["outcomes"]
    prices = json.loads(market["outcomePrices"]) if isinstance(market["outcomePrices"], str) else market["outcomePrices"]
    token_ids = json.loads(market["clobTokenIds"]) if isinstance(market["clobTokenIds"], str) else market["clobTokenIds"]
    cid = market.get("conditionId", "")

    prices = [float(p) for p in prices]
    if len(prices) < 2:
        return None

    fav_idx = 0 if prices[0] >= prices[1] else 1
    fav_price = prices[fav_idx]
    fav_team = outcomes[fav_idx]
    fav_token = token_ids[fav_idx]

    if fav_token in traded_tokens:
        return None
    if fav_price < MIN_ASK or fav_price > MAX_ASK:
        return None

    # ── ESPN game clock gate ──
    espn_info = match_polymarket_to_espn(q, espn_games)
    in_final, clock_detail = is_final_minutes(espn_info)
    if not in_final:
        return None

    # ── CLOB order book ──
    try:
        book = requests.get(f"https://clob.polymarket.com/book?token_id={fav_token}", timeout=10).json()
    except:
        return None

    asks = book.get("asks", [])
    bids = book.get("bids", [])
    if not asks:
        return None

    best_ask = float(asks[0]["price"])
    best_bid = float(bids[0]["price"]) if bids else 0.0
    spread = best_ask - best_bid
    ask_depth = sum(float(a["size"]) * float(a["price"]) for a in asks[:3])

    if best_ask < MIN_ASK or best_ask > MAX_ASK:
        return None
    if spread > MAX_SPREAD:
        return None
    if ask_depth < MIN_BOOK_DEPTH:
        return None

    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600

    # Check wallet balance
    try:
        bal = get_usdc_balance()
        if bal < BET_SIZE * 1.1:
            print(f"  [LOW BALANCE] ${bal:.2f} — skipping {fav_team}")
            return None
    except:
        pass

    # ── PLACE TRADE ──
    shares = math.floor(BET_SIZE / best_ask)
    if shares < 1:
        return None
    cost = round(shares * best_ask, 2)

    sport = espn_info.get("sport", "?") if espn_info else "?"
    print(f"\n  [TRADING] {fav_team} @ {best_ask} | {shares}sh = ${cost}")
    print(f"    {q}")
    print(f"    {sport} | {clock_detail} | spread: {spread:.3f} | depth: ${ask_depth:.0f}")

    try:
        order_args = OrderArgs(price=best_ask, size=shares, side="BUY", token_id=fav_token)
        signed = clob.create_order(order_args)
        resp = clob.post_order(signed, OrderType.FOK)

        success = resp.get("success", False) if isinstance(resp, dict) else False
        order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
        status = resp.get("status", "?") if isinstance(resp, dict) else str(resp)

        print(f"    -> {status} | success={success}")

        if success:
            traded_tokens.add(fav_token)
            total_trades += 1
            try:
                state = json.load(open(STATE_PATH))
                state["pending"].append({
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "question": q,
                    "outcome": fav_team,
                    "end_date": end,
                    "gamma_price": fav_price,
                    "clob_ask": best_ask,
                    "clob_bid": best_bid,
                    "spread": round(spread, 3),
                    "roi_pct": round((1 / best_ask - 1) * 100, 2),
                    "bet_size": cost,
                    "shares": shares,
                    "potential_profit": round(shares - cost, 2),
                    "token_id": fav_token,
                    "condition_id": cid,
                    "order_id": order_id,
                    "resolved": False,
                    "neg_risk": False,
                    "hours_left": round(hours_left, 2),
                    "strategy_tier": f"SPORTS-{sport}",
                    "confirmed": True,
                    "live_price": "",
                    "game_clock": clock_detail,
                })
                state["traded_tokens"].append(fav_token)
                state["trades"] += 1
                state["daily_trades"] = state.get("daily_trades", 0) + 1
                json.dump(state, open(STATE_PATH, "w"), indent=2)
            except Exception as e:
                print(f"    [STATE ERR] {e}")

            return {"team": fav_team, "ask": best_ask, "shares": shares, "cost": cost, "clock": clock_detail}
    except Exception as e:
        print(f"    [ORDER ERR] {e}")

    return None


# ═══════════════════════════════════════════════════════════
#  STOP-LOSS: sell if bid drops 10% below entry
# ═══════════════════════════════════════════════════════════
def check_stop_losses():
    """Check all pending positions for stop-loss trigger."""
    try:
        state = json.load(open(STATE_PATH))
    except:
        return

    pending = state.get("pending", [])
    sold_indices = []

    for i, p in enumerate(pending):
        if p.get("resolved"):
            continue

        token_id = p.get("token_id", "")
        entry_ask = p.get("clob_ask", 0)
        if not token_id or not entry_ask:
            continue

        stop_price = entry_ask * (1 - STOP_LOSS_PCT)

        try:
            book = requests.get(
                f"https://clob.polymarket.com/book?token_id={token_id}", timeout=10
            ).json()
        except:
            continue

        bids = book.get("bids", [])
        if not bids:
            continue

        best_bid = float(bids[0]["price"])
        bid_depth = sum(float(b["size"]) for b in bids[:3])

        if best_bid <= stop_price and best_bid > 0.01 and bid_depth >= 1:
            shares = p.get("shares", 0)
            question = p.get("question", "?")[:50]
            outcome = p.get("outcome", "?")

            print(f"\n  [STOP-LOSS] {outcome} | bid {best_bid} <= stop {stop_price:.3f} (entry {entry_ask})")
            print(f"    {question} | selling {shares} shares")

            for price_attempt in [best_bid, best_bid - 0.01, best_bid - 0.02, best_bid - 0.05]:
                price_attempt = round(price_attempt, 2)
                if price_attempt <= 0.01:
                    break
                try:
                    order_args = OrderArgs(
                        price=price_attempt, size=shares, side="SELL", token_id=token_id
                    )
                    signed = clob.create_order(order_args)
                    resp = clob.post_order(signed, OrderType.FOK)
                    if resp.get("success", False):
                        sell_value = round(shares * price_attempt, 2)
                        loss = round(sell_value - p.get("bet_size", 0), 2)
                        print(f"    -> SOLD @ {price_attempt} | ${sell_value:.2f} | loss: ${loss:.2f}")
                        p["resolved"] = True
                        p["won"] = False
                        p["pnl"] = loss
                        p["resolved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        p["stop_loss"] = True
                        state["losses"] = state.get("losses", 0) + 1
                        state["pnl"] = round(state.get("pnl", 0) + loss, 2)
                        state["bankroll"] = round(state.get("bankroll", 0) + sell_value, 2)
                        if "resolved_trades" not in state:
                            state["resolved_trades"] = []
                        state["resolved_trades"].append(dict(p))
                        sold_indices.append(i)
                        break
                except:
                    continue

    if sold_indices:
        state["pending"] = [p for i, p in enumerate(state["pending"]) if i not in sold_indices]
        json.dump(state, open(STATE_PATH, "w"), indent=2)
        print(f"  [STOP-LOSS] {len(sold_indices)} position(s) liquidated")


# ═══════════════════════════════════════════════════════════
#  FORCE-REDEEM
# ═══════════════════════════════════════════════════════════
def force_redeem_all():
    """Try to redeem ALL on-chain positions. Dry-run first, no gas wasted."""
    global total_redeemed
    try:
        positions = requests.get(
            f"https://data-api.polymarket.com/positions?user={wallet}", timeout=15
        ).json()
    except Exception as e:
        print(f"  [REDEEM FETCH ERR] {e}")
        return 0

    redeemed_this_cycle = 0
    for p in positions:
        cid = p.get("conditionId", "")
        if cid in redeemed_cids:
            continue
        outcome = p.get("outcome", "?")
        size = float(p.get("size", 0))
        title = p.get("title", "?")[:45]
        if size <= 0:
            continue

        try:
            cid_bytes = bytes.fromhex(cid[2:] if cid.startswith("0x") else cid)
            CTF.functions.redeemPositions(
                COLLATERAL, PARENT, cid_bytes, [1, 2]
            ).call({"from": acct.address})

            nonce = w3.eth.get_transaction_count(acct.address, "latest")
            gas_price = w3.eth.gas_price
            tx = CTF.functions.redeemPositions(
                COLLATERAL, PARENT, cid_bytes, [1, 2]
            ).build_transaction({
                "from": acct.address,
                "nonce": nonce,
                "gas": 250000,
                "maxFeePerGas": gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(50, "gwei"),
                "chainId": 137,
            })
            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt.status == 1:
                bal = get_usdc_balance()
                print(f"  [REDEEMED] {outcome} {size:.0f}sh | {title} -> ${bal:.2f}")
                redeemed_cids.add(cid)
                total_redeemed += 1
                redeemed_this_cycle += 1
            else:
                print(f"  [REDEEM REVERTED] {outcome} | {title}")

        except Exception as e:
            err = str(e)
            if "revert" in err.lower() or "execution reverted" in err.lower():
                pass
            else:
                print(f"  [REDEEM ERR] {outcome} | {title} | {err[:70]}")

    return redeemed_this_cycle


# ═══════════════════════════════════════════════════════════
#  MAIN LOOP — 24/7/365
# ═══════════════════════════════════════════════════════════
traded_tokens = load_traded_tokens()

print("=" * 70)
print("  LIVE SPORTS MONITOR + AUTO-REDEEM — 24/7")
print(f"  Trade: fav {MIN_ASK}-{MAX_ASK} | spread <{MAX_SPREAD} | depth >${MIN_BOOK_DEPTH}")
print(f"  Gate:  ESPN game clock <{MAX_GAME_CLOCK:.0f} min in final period/half")
print(f"         NBA: <3min Q4 | NHL: <3min P3 | NCAA: <3min 2H")
print(f"  Bet:   ${BET_SIZE} per trade")
print(f"  Stop:  sell if bid drops {STOP_LOSS_PCT:.0%} below entry")
print(f"  Redeem: force every {REDEEM_INTERVAL}s (dry-run safe)")
print(f"  Wallet: {wallet}")
print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
print("=" * 70)

last_redeem = 0

while True:
    try:
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%H:%M:%S")
        now_ts = time.time()

        # ── Stop-loss check every cycle ──
        check_stop_losses()

        # ── Force-redeem every REDEEM_INTERVAL seconds ──
        if now_ts - last_redeem >= REDEEM_INTERVAL:
            redeemed = force_redeem_all()
            if redeemed > 0:
                bal = get_usdc_balance()
                print(f"  [{now_str}] Redeemed {redeemed} positions | Wallet: ${bal:.2f}")
            last_redeem = now_ts

        # ── Fetch ESPN live game clocks ──
        espn_games = fetch_espn_games()

        # ── Scan Polymarket sports markets ──
        markets = scan_sports_markets()

        actionable = []
        watchlist = []

        for m in markets:
            q = m.get("question", "")
            end = m.get("endDate", "")
            outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
            prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m["outcomePrices"]
            token_ids = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]

            prices_f = [float(p) for p in prices]
            if len(prices_f) < 2:
                continue

            fav_idx = 0 if prices_f[0] >= prices_f[1] else 1
            fav_price = prices_f[fav_idx]
            fav_team = outcomes[fav_idx]
            fav_token = token_ids[fav_idx]

            owned = "OWNED" if fav_token in traded_tokens else ""

            # Match to ESPN
            espn_info = match_polymarket_to_espn(q, espn_games)
            in_final, clock_detail = is_final_minutes(espn_info)

            if espn_info:
                sport = espn_info.get("sport", "?")
                period = espn_info.get("period", 0)
                clock_min = espn_info.get("clock_min", 99)
                clock_tag = f"{sport} P{period} {clock_min:.1f}m"
            else:
                clock_tag = "no ESPN"

            if fav_price >= MIN_ASK and in_final:
                actionable.append((fav_price, fav_team, q, owned, clock_tag))
            elif fav_price >= 0.85:
                watchlist.append((fav_price, fav_team, q, owned, clock_tag))

        # ── Display ──
        live_count = sum(1 for g in espn_games.values() if g.get("status") == "STATUS_IN_PROGRESS")
        print(f"\n  [{now_str}] {len(markets)} mkts | ESPN: {live_count} live | {len(actionable)} GO | {len(watchlist)} watch | T:{total_trades} R:{total_redeemed}")

        if actionable:
            print(f"  --- GO: fav>={MIN_ASK} + <{MAX_GAME_CLOCK:.0f}min final ---")
            for fp, team, q, owned, clk in sorted(actionable, key=lambda x: -x[0]):
                flag = f" [{owned}]" if owned else ""
                print(f"    {fp:.3f} | {team:28}{flag} | {clk:18} | {q[:42]}")

        if watchlist:
            top = sorted(watchlist, key=lambda x: -x[0])[:10]
            print(f"  --- WATCHLIST (>=0.85) ---")
            for fp, team, q, owned, clk in top:
                flag = f" [{owned}]" if owned else ""
                print(f"    {fp:.3f} | {team:28}{flag} | {clk:18} | {q[:42]}")

        # ── Execute trades on actionable ──
        for m in markets:
            result = check_and_trade(m, espn_games)
            if result:
                print(f"  ** TRADE #{total_trades}: {result['team']} {result['shares']}sh @ {result['ask']} = ${result['cost']} | {result['clock']}")

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print(f"\n[STOP] Trades: {total_trades} | Redeemed: {total_redeemed}")
        break
    except Exception as e:
        print(f"\n  [ERROR] {e}")
        traceback.print_exc()
        time.sleep(30)
