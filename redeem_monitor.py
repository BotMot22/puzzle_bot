#!/usr/bin/env python3
"""
REDEMPTION + SELL MONITOR — every 4 min:
  1. Redeem anything the data API flags as redeemable
  2. Force-try on-chain redemption for ALL positions (catches oracle before API updates)
  3. Try to sell soccer spreads on CLOB if any bid > $0.01 appears
"""
import os, json, time, requests
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

# ── On-chain setup ──
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
    abi=[{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
)

# ── CLOB setup for selling ──
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

SOCCER_KEYWORDS = ["spread:", "portsmouth", "derby county", "preston", "leicester",
                   "hull city", "swansea", "middlesbrough", "wrexham"]

POLL_INTERVAL = 240  # 4 minutes

redeemed_cids = set()
sold_tokens = set()
total_redeemed = 0
total_sold = 0

def is_soccer(p):
    title = (p.get("title", "") + " " + p.get("outcome", "")).lower()
    return any(kw in title for kw in SOCCER_KEYWORDS)

def get_balance():
    return USDC.functions.balanceOf(w3.to_checksum_address(wallet)).call() / 1e6

def try_redeem(p):
    """Try on-chain redemption. Returns True if successful."""
    cid = p["conditionId"]
    outcome = p.get("outcome", "?")
    size = float(p.get("size", 0))
    title = p.get("title", "?")[:50]
    try:
        cid_bytes = bytes.fromhex(cid[2:] if cid.startswith("0x") else cid)
        # Dry-run first to avoid wasting gas on reverts
        CTF.functions.redeemPositions(
            COLLATERAL, PARENT, cid_bytes, [1, 2]
        ).call({"from": acct.address})
        # If call() didn't revert, oracle is ready — send real tx
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
            new_bal = get_balance()
            print(f"  [REDEEMED OK] {outcome} {size:.0f} shares -> ${new_bal:.2f} | {title}")
            redeemed_cids.add(cid)
            return True
        else:
            print(f"  [REDEEM REVERTED] {outcome} | {title}")
            return False
    except Exception as e:
        err = str(e)
        if "execution reverted" in err.lower() or "revert" in err.lower():
            pass  # Oracle not ready yet — silent
        else:
            print(f"  [REDEEM ERR] {outcome} | {title} | {err[:80]}")
        return False

def try_sell(p):
    """Try to sell on CLOB. Returns True if sold."""
    token_id = p.get("asset", "")
    size = float(p.get("size", 0))
    outcome = p.get("outcome", "?")
    title = p.get("title", "?")[:50]
    if not token_id or size <= 0:
        return False
    try:
        book = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=10).json()
        bids = book.get("bids", [])
        if not bids:
            return False
        best_bid = float(bids[0]["price"])
        if best_bid <= 0.01:
            return False
        # Real bid exists — sell
        print(f"  [SELLING] {outcome} {size:.0f} shares @ ${best_bid:.3f} | {title}")
        order_args = OrderArgs(price=best_bid, size=size, side="SELL", token_id=token_id)
        signed = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed, OrderType.FOK)
        status = resp.get("status", "?") if isinstance(resp, dict) else str(resp)
        value = best_bid * size
        print(f"    -> {status} | ~${value:.2f}")
        sold_tokens.add(token_id)
        return True
    except Exception as e:
        print(f"  [SELL ERR] {outcome} | {title} | {str(e)[:80]}")
        return False

# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("  REDEEM + SELL MONITOR — every 4 min")
print(f"  Wallet: {wallet}")
print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
print("=" * 60)

while True:
    try:
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        positions = requests.get(
            f"https://data-api.polymarket.com/positions?user={wallet}", timeout=15
        ).json()

        # Filter out already handled
        remaining = [p for p in positions
                     if p.get("conditionId") not in redeemed_cids
                     and p.get("asset", "") not in sold_tokens]

        bal = get_balance()

        if not remaining:
            if total_redeemed > 0 or total_sold > 0:
                print(f"\n{'='*60}")
                print(f"  ALL POSITIONS CLEARED")
                print(f"  Redeemed: {total_redeemed} | Sold: {total_sold} | Wallet: ${bal:.2f}")
                print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
                print(f"{'='*60}")
                break
            else:
                print(f"  [{now}] No positions found | ${bal:.2f} USDC", end="\r", flush=True)
                time.sleep(POLL_INTERVAL)
                continue

        # ── Phase 1: Redeem anything flagged redeemable by data API ──
        api_redeemable = [p for p in remaining if p.get("redeemable")]
        if api_redeemable:
            print(f"\n  [{now}] {len(api_redeemable)} flagged redeemable by API")
            for p in api_redeemable:
                if try_redeem(p):
                    total_redeemed += 1

        # ── Phase 2: Force-try redemption on ALL remaining (dry-run first, no gas wasted) ──
        still_remaining = [p for p in remaining
                          if p.get("conditionId") not in redeemed_cids
                          and p.get("asset", "") not in sold_tokens
                          and not p.get("redeemable")]
        if still_remaining:
            for p in still_remaining:
                if try_redeem(p):
                    total_redeemed += 1

        # ── Phase 3: Try selling soccer positions on CLOB ──
        soccer_left = [p for p in still_remaining if is_soccer(p)
                       and p.get("conditionId") not in redeemed_cids
                       and p.get("asset", "") not in sold_tokens]
        for p in soccer_left:
            if try_sell(p):
                total_sold += 1

        # ── Status ──
        final_remaining = [p for p in positions
                          if p.get("conditionId") not in redeemed_cids
                          and p.get("asset", "") not in sold_tokens]
        bal = get_balance()
        soccer_count = sum(1 for p in final_remaining if is_soccer(p))
        print(f"  [{now}] {len(final_remaining)} positions ({soccer_count} soccer) | ${bal:.2f} USDC | redeemed:{total_redeemed} sold:{total_sold}", end="\r", flush=True)

        time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[STOP] Monitor stopped")
        break
    except Exception as e:
        print(f"\n  [ERROR] {e}")
        time.sleep(30)
