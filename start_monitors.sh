#!/bin/bash
# =============================================================================
# start_monitors.sh -- Launch Bitcoin Puzzle #71 Public Key Monitor
# =============================================================================
#
# Starts pubkey_monitor.py in a detached tmux session so it runs 24/7.
#
# Usage:
#   bash /root/puzzle71/start_monitors.sh
#   bash /root/puzzle71/start_monitors.sh --webhook https://hooks.slack.com/...
#
# Check status:
#   tmux attach -t pubkey_monitor
#   tail -f /root/puzzle71/logs/pubkey_monitor.log
#
# Stop:
#   tmux kill-session -t pubkey_monitor
# =============================================================================

VENV="/root/btc_puzzle_env/bin/activate"
MONITOR="/root/puzzle71/pubkey_monitor.py"
SESSION_NAME="pubkey_monitor"
LOG_DIR="/root/puzzle71/logs"
LOG_FILE="${LOG_DIR}/pubkey_monitor.log"

# Pass through any extra args (e.g. --webhook URL, --interval 30)
EXTRA_ARGS="${*}"

# Ensure log directory exists
mkdir -p "${LOG_DIR}"

# Check if tmux session already exists
if tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
    echo "[!] tmux session '${SESSION_NAME}' already exists."
    echo "    To view:  tmux attach -t ${SESSION_NAME}"
    echo "    To stop:  tmux kill-session -t ${SESSION_NAME}"
    echo "    To restart: kill it first, then re-run this script."
    exit 1
fi

# Check that the venv exists
if [ ! -f "${VENV}" ]; then
    echo "[ERROR] Python venv not found at ${VENV}"
    exit 1
fi

# Check that the monitor script exists
if [ ! -f "${MONITOR}" ]; then
    echo "[ERROR] Monitor script not found at ${MONITOR}"
    exit 1
fi

echo "============================================================"
echo "  Bitcoin Puzzle #71 -- Public Key Monitor"
echo "============================================================"
echo ""
echo "  Target: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU"
echo "  H160:   f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8"
echo ""

# Launch in tmux
tmux new-session -d -s "${SESSION_NAME}" "source ${VENV} && python ${MONITOR} ${EXTRA_ARGS} 2>&1 | tee -a ${LOG_FILE}"

echo "[OK] Monitor started in tmux session: ${SESSION_NAME}"
echo ""
echo "  View live output:"
echo "    tmux attach -t ${SESSION_NAME}"
echo ""
echo "  View log file:"
echo "    tail -f ${LOG_FILE}"
echo ""
echo "  Stop the monitor:"
echo "    tmux kill-session -t ${SESSION_NAME}"
echo ""
echo "  Log path: ${LOG_FILE}"
echo "============================================================"
