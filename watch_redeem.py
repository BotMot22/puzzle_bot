#!/usr/bin/env python3
"""Poll Polymarket until all positions are redeemable, then redeem."""
import os, sys, time, requests
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from expiry_bot import redeem_positions

WALLET = os.environ["POLYMARKET_WALLET"]
POLL_INTERVAL = 60  # check every 60s

def check():
    positions = requests.get(
        f"https://data-api.polymarket.com/positions?user={WALLET}",
        timeout=15,
    ).json()
    held = [p for p in positions if float(p.get("size", 0)) > 0]
    redeemable = [p for p in held if p.get("redeemable")]
    return held, redeemable

print(f"[WATCH] Polling every {POLL_INTERVAL}s for redeemable positions...")
print(f"[WATCH] Wallet: {WALLET[:10]}...")

while True:
    try:
        held, redeemable = check()
        ts = time.strftime("%H:%M:%S UTC", time.gmtime())

        if redeemable:
            print(f"\n[{ts}] {len(redeemable)}/{len(held)} positions REDEEMABLE!")
            for p in redeemable:
                size = float(p.get("size", 0))
                print(f"  {p.get('outcome', '?'):>5} | {size:.1f} shares | {p.get('title', '?')[:60]}")

            print(f"\n[REDEEMING]...")
            n = redeem_positions()
            print(f"[DONE] Redeemed {n} positions")

            # Check if any left
            held2, still = check()
            unredeemed = [p for p in held2 if float(p.get("size", 0)) > 0 and not p.get("redeemable")]
            if not unredeemed:
                print(f"\n[COMPLETE] All positions redeemed! USDC returned to wallet.")
                break
            else:
                print(f"[WAIT] {len(unredeemed)} still pending, continuing to poll...")
        else:
            print(f"[{ts}] 0/{len(held)} redeemable — waiting...", end="\r")

    except Exception as e:
        print(f"[ERROR] {e}")

    time.sleep(POLL_INTERVAL)
