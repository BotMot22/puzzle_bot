#!/bin/bash
# Bitcoin Puzzle #71 — 24/7 Multi-Instance Launch Script
# Runs multiple turbo scanner instances in tmux

VENV="/root/btc_puzzle_env/bin/activate"
SCANNER="/root/puzzle71/turbo_scanner.py"
LOGDIR="/root/puzzle71/logs"

# Config — adjust these
WORKERS_PER_INSTANCE=64
NUM_INSTANCES=3     # Run 3 instances = 192 total processes

mkdir -p "$LOGDIR"

# Kill existing sessions
for i in $(seq 1 $NUM_INSTANCES); do
    tmux kill-session -t "puzzle71_$i" 2>/dev/null
done
tmux kill-session -t "puzzle71_mon" 2>/dev/null

echo "═══════════════════════════════════════════════════════════"
echo "  Launching $NUM_INSTANCES instances × $WORKERS_PER_INSTANCE workers"
echo "  Total parallel processes: $((NUM_INSTANCES * WORKERS_PER_INSTANCE))"
echo "═══════════════════════════════════════════════════════════"

# Launch scanner instances
for i in $(seq 1 $NUM_INSTANCES); do
    SESSION="puzzle71_$i"
    LOG="$LOGDIR/turbo_instance${i}_$(date +%Y%m%d_%H%M%S).log"

    tmux new-session -d -s "$SESSION" -n scanner
    tmux send-keys -t "$SESSION:scanner" \
        "source $VENV && python3 $SCANNER -w $WORKERS_PER_INSTANCE 2>&1 | tee $LOG" C-m

    echo "  Instance $i: tmux=$SESSION workers=$WORKERS_PER_INSTANCE"
done

# Launch monitor window
tmux new-session -d -s "puzzle71_mon" -n monitor
tmux send-keys -t "puzzle71_mon:monitor" \
    "watch -n 5 'echo \"=== PUZZLE #71 STATUS ===\"; cat /root/puzzle71/data/turbo_stats.json 2>/dev/null | python3 -m json.tool; echo; echo \"=== FOUND? ===\"; cat /root/puzzle71/FOUND_KEY.txt 2>/dev/null || echo \"Not yet...\"'" C-m

echo ""
echo "  Monitor:  tmux attach -t puzzle71_mon"
echo "  Instance: tmux attach -t puzzle71_1"
echo "  Found?:   cat /root/puzzle71/FOUND_KEY.txt"
echo "═══════════════════════════════════════════════════════════"
