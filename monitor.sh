#!/usr/bin/env bash
LOG="/root/polymarket_quant/data/monitor.log"
mkdir -p /root/polymarket_quant/data
while true; do
    TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')
    if ! tmux has-session -t scalp_bot 2>/dev/null; then
        echo "[$TS] ALERT: scalp_bot session DEAD" | tee -a "$LOG"
        sleep 900
        continue
    fi
    OUT=$(tmux capture-pane -t scalp_bot -p -S -60 2>&1)
    TRADES=$(echo "$OUT" | grep -c "LIVE TRADE")
    WINS=$(echo "$OUT" | grep -c "WIN")
    LOSSES=$(echo "$OUT" | grep -c "LOSS")
    ERRORS=$(echo "$OUT" | grep -c "ERROR")
    DASH=$(echo "$OUT" | grep "COMBINED" | tail -1)
    echo "[$TS] T:$TRADES W:$WINS L:$LOSSES E:$ERRORS | $DASH" | tee -a "$LOG"
    sleep 900
done
