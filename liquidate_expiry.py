#!/usr/bin/env python3
"""
Liquidate all expiry_bot positions:
1. Redeem any resolved positions on-chain
2. Sell any in-play shares on CLOB (market sell at best bid)
3. Update state file
"""

import os
import sys
import json
import time
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import SELL
from web3 import Web3
from eth_account import Account

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

WALLET = os.environ["POLYMARKET_WALLET"]
CLOB_API = "https://clob.polymarket.com"

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

# On-chain redemption setup
_w3 = Web3(Web3.HTTPProvider("https://polygon.drpc.org"))
_acct = Account.from_key(os.environ["POLYMARKET_PRIVATE_KEY"])
_CTF = _w3.eth.contract(
    address=_w3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"),
    abi=json.loads('[{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]'),
)
_COLLATERAL = _w3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
_PARENT = b'\x00' * 32

STATE_FILE = "data/expiry_state.json"


def get_all_positions():
    """Get all wallet positions from data API."""
    resp = requests.get(
        f"https://data-api.polymarket.com/positions?user={WALLET}", timeout=15
    )
    return resp.json()


def redeem_all():
    """Redeem all redeemable positions on-chain."""
    positions = get_all_positions()
    redeemable = [p for p in positions if p.get("redeemable")]

    if not redeemable:
        print("[REDEEM] No redeemable positions found")
        return 0

    print(f"[REDEEM] Found {len(redeemable)} redeemable positions")
    nonce = _w3.eth.get_transaction_count(_acct.address, "latest")
    gas_price = _w3.eth.gas_price
    redeemed = 0

    for p in redeemable:
        cid = p["conditionId"]
        cid_hex = cid[2:] if cid.startswith("0x") else cid
        size = float(p.get("size", 0))
        title = p.get("title", "?")[:60]
        outcome = p.get("outcome", "?")

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
            receipt = _w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            status = "OK" if receipt.status == 1 else "REVERTED"
            print(f"  [REDEEM {status}] {outcome} {size:.2f} shares | {title}")
            nonce += 1
            redeemed += 1
            time.sleep(1)
        except Exception as e:
            print(f"  [REDEEM FAIL] {outcome}: {e}")

    return redeemed


def sell_all_in_play():
    """Sell all shares that are still in play (not yet resolved)."""
    positions = get_all_positions()

    # Filter for positions with actual shares that aren't redeemable
    in_play = [
        p for p in positions
        if not p.get("redeemable")
        and float(p.get("size", 0)) > 0
    ]

    if not in_play:
        print("[SELL] No in-play positions to sell")
        return 0

    print(f"\n[SELL] Found {len(in_play)} in-play positions to sell")
    sold = 0
    total_recovered = 0.0

    for p in in_play:
        token_id = p.get("asset", "") or p.get("tokenId", "")
        size = float(p.get("size", 0))
        outcome = p.get("outcome", "?")
        title = p.get("title", "?")[:60]

        if not token_id or size < 1:
            print(f"  [SKIP] {outcome} — {size:.2f} shares (< 1) | {title}")
            continue

        # Get best bid
        try:
            bid_r = requests.get(
                f"{CLOB_API}/price?token_id={token_id}&side=BUY", timeout=5
            )
            bid = float(bid_r.json().get("price", 0))
        except Exception:
            bid = 0

        if bid <= 0:
            print(f"  [NO BID] {outcome} {size:.0f} shares — no buyers | {title}")
            continue

        sell_shares = int(size)  # whole shares only
        # Clamp bid to 0.99 max (CLOB rejects 1.0)
        sell_price = min(round(bid, 2), 0.99)
        # If rounding pushed it to 1.0, use 0.99
        if sell_price >= 1.0:
            sell_price = 0.99
        expected = round(sell_shares * sell_price, 2)

        print(f"  [SELLING] {outcome} {sell_shares} shares @ ${sell_price:.3f} "
              f"(~${expected:.2f}) | {title}")

        # Try selling at decreasing prices
        prices_to_try = [sell_price]
        for step in [0.01, 0.02, 0.05]:
            p = round(sell_price - step, 2)
            if p > 0 and p not in prices_to_try:
                prices_to_try.append(p)

        try:
            filled = False
            for try_price in prices_to_try:
                order_args = OrderArgs(
                    token_id=token_id,
                    price=try_price,
                    size=sell_shares,
                    side=SELL,
                )
                signed_order = clob_client.create_order(order_args)
                resp = clob_client.post_order(signed_order, OrderType.FOK)

                if resp and resp.get("success"):
                    actual = round(sell_shares * try_price, 2)
                    print(f"    SOLD @ ${try_price:.2f}! Order: {resp.get('orderID', '?')[:16]}...")
                    sold += 1
                    total_recovered += actual
                    filled = True
                    break
                else:
                    print(f"    No fill @ ${try_price:.2f}, trying lower...")

            if not filled:
                print(f"    COULD NOT SELL — market may be closed/resolved")
        except Exception as e:
            print(f"    ERROR: {e}")

        time.sleep(0.5)

    print(f"\n[SELL SUMMARY] Sold {sold}/{len(in_play)} positions, ~${total_recovered:.2f} recovered")
    return sold, total_recovered


def update_state(total_recovered):
    """Clear pending positions and update bankroll."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    else:
        return

    old_pending = len(state.get("pending", []))
    old_bankroll = state.get("bankroll", 0)

    state["pending"] = []
    state["traded_tokens"] = []
    state["bankroll"] = round(old_bankroll + total_recovered, 2)

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)

    print(f"\n[STATE] Cleared {old_pending} pending positions")
    print(f"[STATE] Bankroll: ${old_bankroll:.2f} → ${state['bankroll']:.2f}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    print("=" * 60)
    print("  LIQUIDATING ALL EXPIRY BOT POSITIONS")
    print("=" * 60)

    # Step 1: Redeem resolved positions
    print("\n--- STEP 1: Redeem resolved positions ---")
    redeemed = redeem_all()

    # Step 2: Sell in-play positions
    print("\n--- STEP 2: Sell in-play positions ---")
    result = sell_all_in_play()
    sold_count = result[0] if result else 0
    total_recovered = result[1] if result else 0.0

    # Step 3: Update state
    print("\n--- STEP 3: Update state ---")
    update_state(total_recovered)

    print("\n" + "=" * 60)
    print("  DONE")
    print(f"  Redeemed: {redeemed} | Sold: {sold_count} | Recovered: ~${total_recovered:.2f}")
    print("=" * 60)
