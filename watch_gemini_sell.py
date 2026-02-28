#!/usr/bin/env python3
"""Watch the Gemini FrontierMath position and sell as soon as bid > entry."""
import os, sys, time
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))
from expiry_bot import get_clob_prices, clob_client
from py_clob_client.order_builder.constants import BUY
from py_clob_client.clob_types import OrderArgs, OrderType

TOKEN_ID = "110408440477607554367085519704617416206951294677642733070833864587444833467181"
SHARES = 18  # 18.057 on-chain, use 18 whole shares for clean FOK fill
AVG_ENTRY = 0.9469
COST = SHARES * AVG_ENTRY  # ~$17.04
POLL_INTERVAL = 30  # check every 30s

print(f"[GEMINI WATCH] Monitoring bid for profit exit")
print(f"  Shares: {SHARES} | Entry: ${AVG_ENTRY:.4f} | Breakeven: ${AVG_ENTRY:.4f}")
print(f"  Will sell when bid > ${AVG_ENTRY:.4f}")
print()

while True:
    try:
        prices = get_clob_prices(TOKEN_ID)
        bid = prices["bid"]
        ask = prices["ask"]
        ts = time.strftime("%H:%M:%S UTC", time.gmtime())
        pnl = round(SHARES * bid - COST, 2)

        if bid > AVG_ENTRY:
            proceeds = round(SHARES * bid, 2)
            profit = round(proceeds - COST, 2)
            print(f"\n[{ts}] BID ${bid:.4f} > ENTRY ${AVG_ENTRY:.4f} — SELLING!")
            print(f"  Expected proceeds: ${proceeds:.2f} | Profit: ${profit:+.2f}")

            try:
                order_args = OrderArgs(
                    token_id=TOKEN_ID,
                    price=bid,
                    size=SHARES,
                    side="SELL",
                )
                signed_order = clob_client.create_order(order_args)
                resp = clob_client.post_order(signed_order, OrderType.FOK)
                if resp and resp.get("success"):
                    print(f"[SOLD] Order filled! ID: {resp.get('orderID', '?')}")
                    print(f"[DONE] Gemini position closed for ${profit:+.2f} profit")
                    break
                else:
                    print(f"[NOFILL] Order didn't fill at bid ${bid:.4f}, retrying next cycle...")
            except Exception as e:
                print(f"[ERROR] Sell failed: {e}")
                print(f"  Will retry next cycle...")
        else:
            print(f"[{ts}] bid={bid:.4f} ask={ask:.4f} | PnL: ${pnl:+.2f} — waiting...", end="\r")

    except Exception as e:
        print(f"[ERROR] {e}")

    time.sleep(POLL_INTERVAL)
