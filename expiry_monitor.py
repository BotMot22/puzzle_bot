#!/usr/bin/env python3
"""Print a live trade chart from expiry_state.json every 15 minutes."""

import json
import time
import os
from datetime import datetime, timezone

STATE_FILE = "data/expiry_state.json"

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)


def short_name(question):
    q = question
    q = q.replace(" on February 25?", "?").replace(" on February 26?", "?")
    q = q.replace(" on February 27?", "?").replace(" on February 28?", "?")
    q = q.replace("Will ", "")
    if "Up or Down" in q:
        parts = q.split("Up or Down")
        q = parts[0].strip() + " Up/Down"
    if len(q) > 42:
        q = q[:39] + "..."
    return q


def format_price(price):
    if not price or price == "":
        return ""
    p = float(price)
    if p > 1000:
        return f"${p:,.0f}"
    elif p > 1:
        return f"${p:.2f}"
    else:
        return f"${p:.4f}"


def print_chart(state):
    pending = state.get("pending", [])
    resolved = state.get("resolved_trades", [])
    w, l = state.get("wins", 0), state.get("losses", 0)
    pnl = state.get("pnl", 0)
    bankroll = state.get("bankroll", 0)
    mode = "LIVE" if not state.get("paper_mode", True) else "PAPER"
    now = datetime.now(timezone.utc)

    # Combine: resolved first (most recent at top), then pending
    all_trades = list(resolved) + list(pending)

    # Column widths
    cN = 3
    cM = 42
    cO = 10
    cA = 7
    cC = 8
    cP = 9
    cR = 8
    cT = 10
    cF = 5
    cL = 15
    cS = 12

    sep = "+" + "-"*(cN+2) + "+" + "-"*(cM+2) + "+" + "-"*(cO+2) + "+" + \
          "-"*(cA+2) + "+" + "-"*(cC+2) + "+" + "-"*(cP+2) + "+" + \
          "-"*(cR+2) + "+" + "-"*(cT+2) + "+" + "-"*(cF+2) + "+" + \
          "-"*(cL+2) + "+" + "-"*(cS+2) + "+"

    header = f"| {'#':>{cN}} | {'Market':<{cM}} | {'Outcome':<{cO}} | " \
             f"{'Ask':>{cA}} | {'Cost':>{cC}} | {'PnL':>{cP}} | " \
             f"{'ROI':>{cR}} | {'Tier':<{cT}} | {'Conf':^{cF}} | " \
             f"{'Live Price':>{cL}} | {'Status':<{cS}} |"

    total_pending = len(pending)
    total_resolved = len(resolved)
    pending_value = sum(t.get("bet_size", 0) for t in pending)

    print()
    pnl_color = GREEN if pnl >= 0 else RED
    print(f"  {BOLD}EXPIRY SCALP BOT [{mode}]{RESET} — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Bankroll: {BOLD}${bankroll:,.2f}{RESET} | "
          f"PnL: {pnl_color}{BOLD}${pnl:+,.2f}{RESET} | "
          f"{GREEN}{w}W{RESET}-{RED}{l}L{RESET} | "
          f"{total_pending} open, {total_resolved} resolved")
    print()
    print(sep)
    print(header)
    print(sep)

    # Print resolved trades section
    if resolved:
        for i, t in enumerate(resolved, 1):
            _print_trade_row(i, t, now, cN, cM, cO, cA, cC, cP, cR, cT, cF, cL, cS, is_resolved=True)

        if pending:
            # Separator between resolved and pending
            print(sep)

    # Print pending trades section
    for i, t in enumerate(pending, len(resolved) + 1):
        _print_trade_row(i, t, now, cN, cM, cO, cA, cC, cP, cR, cT, cF, cL, cS, is_resolved=False)

    print(sep)

    # Totals
    total_cost = sum(t.get("bet_size", 0) for t in all_trades)
    wr = w / max(w + l, 1)
    resolved_count = w + l
    total_count = len(all_trades)
    pnl_str = f"${pnl:+.2f}" if resolved_count > 0 else "—"
    pnl_color_t = GREEN if pnl >= 0 else RED

    print(f"| {'':>{cN}} | {'TOTALS':<{cM}} | {'':<{cO}} | "
          f"{'':>{cA}} | {f'${total_cost:.2f}':>{cC}} | "
          f"{pnl_color_t}{pnl_str:>{cP}}{RESET} | "
          f"{f'{wr:.0%}':>{cR}} | {'':<{cT}} | "
          f"{f'{resolved_count}/{total_count}':^{cF}} | "
          f"{'':>{cL}} | {f'{w}W-{l}L':>{cS}} |")
    print(sep)
    print()


def _print_trade_row(i, t, now, cN, cM, cO, cA, cC, cP, cR, cT, cF, cL, cS, is_resolved):
    market = short_name(t.get("question", ""))
    outcome = t.get("outcome", "?")[:cO]
    ask = f"${t.get('clob_ask', 0):.2f}"
    cost = f"${t.get('bet_size', 0):.2f}"
    roi = f"{t.get('roi_pct', 0):.1f}%"
    tier = t.get("strategy_tier", "")
    conf = "Y" if t.get("confirmed") else ""
    lp = format_price(t.get("live_price", ""))

    resolved = t.get("resolved", False)
    won = t.get("won", None)
    end_str = t.get("end_date", "")
    try:
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        expired = now > end_dt
    except Exception:
        expired = False

    # Status + PnL coloring
    if resolved and won:
        status = f"{GREEN}WIN{RESET}       "
        actual_pnl = t.get("pnl", 0)
        pnl_str = f"{GREEN}${actual_pnl:+.2f}{RESET}"
        # Pad to account for color codes
        pnl_str = pnl_str + " " * max(0, cP - len(f"${actual_pnl:+.2f}"))
    elif resolved and won is False:
        status = f"{RED}LOSS{RESET}      "
        actual_pnl = t.get("pnl", 0)
        pnl_str = f"{RED}${actual_pnl:+.2f}{RESET}"
        pnl_str = pnl_str + " " * max(0, cP - len(f"${actual_pnl:+.2f}"))
    elif expired:
        status = f"{YELLOW}RESOLVING{RESET}  "
        pnl_str = f"${t.get('potential_profit', 0):.2f}"
        pnl_str = f"{pnl_str:>{cP}}"
    else:
        mins_left = (end_dt - now).total_seconds() / 60
        if mins_left > 60:
            status = f"{mins_left/60:.1f}h left    "
        else:
            status = f"{CYAN}{mins_left:.0f}m left{RESET}    "
        pnl_str = f"${t.get('potential_profit', 0):.2f}"
        pnl_str = f"{pnl_str:>{cP}}"

    print(f"| {i:>{cN}} | {market:<{cM}} | {outcome:<{cO}} | "
          f"{ask:>{cA}} | {cost:>{cC}} | {pnl_str} | "
          f"{roi:>{cR}} | {tier:<{cT}} | {conf:^{cF}} | "
          f"{lp:>{cL}} | {status} |")


def run():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    while True:
        try:
            os.system("clear")
            state = load_state()
            print_chart(state)

            # Count down to next refresh
            refresh = 900  # 15 min
            next_time = datetime.now(timezone.utc).timestamp() + refresh
            while True:
                remaining = int(next_time - datetime.now(timezone.utc).timestamp())
                if remaining <= 0:
                    break
                mins = remaining // 60
                secs = remaining % 60
                print(f"\r  Next refresh in {mins}m {secs:02d}s... "
                      f"(Ctrl+C to stop)", end="", flush=True)
                time.sleep(1)

        except KeyboardInterrupt:
            print("\n\nMonitor stopped.")
            break
        except Exception as e:
            print(f"\nError: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run()
