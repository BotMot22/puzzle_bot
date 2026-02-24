#!/usr/bin/env bash
###############################################################################
# multi_gpu_launch.sh — Multi-GPU BitCrack Launcher for Bitcoin Puzzle #71
###############################################################################
#
# Detects all NVIDIA GPUs on the machine, splits the puzzle #71 key range
# evenly across them, and launches one BitCrack process per GPU.
#
# Target:  1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU
# H160:    f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8
# Range:   0x400000000000000000 to 0x7FFFFFFFFFFFFFFFFF (2^70 to 2^71-1)
#
# Usage:
#   ./multi_gpu_launch.sh                  # Auto-detect GPUs, split, launch
#   ./multi_gpu_launch.sh --dry-run        # Show the plan without executing
#   ./multi_gpu_launch.sh --gpus 0,1,2     # Use specific GPU indices
#   ./multi_gpu_launch.sh --status         # Show status of running instances
#   ./multi_gpu_launch.sh --stop           # Stop all running BitCrack instances
#   ./multi_gpu_launch.sh --binary /path   # Use specific cuBitCrack binary
#
# Each GPU runs in its own screen session: bitcrack_gpu0, bitcrack_gpu1, etc.
# Progress and logs are saved per-GPU in /root/puzzle71/logs/
#
# The script monitors all processes and will alert if a key is found by any
# GPU, stopping all other processes automatically.
#
###############################################################################
set -euo pipefail

# ─── Constants ───────────────────────────────────────────────────────────────
TARGET_ADDRESS="1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU"
TARGET_H160="f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8"

# Full range: 2^70 to 2^71 - 1
RANGE_START="400000000000000000"      # 0x400000000000000000
RANGE_END="7FFFFFFFFFFFFFFFFF"         # 0x7FFFFFFFFFFFFFFFFF

WORKDIR="/root/puzzle71"
LOGDIR="${WORKDIR}/logs"
FOUND_FILE="${WORKDIR}/FOUND_KEY.txt"
BUILDDIR="${WORKDIR}/build"
PID_DIR="${WORKDIR}/pids"
MONITOR_PID_FILE="${PID_DIR}/monitor.pid"

# ─── State ───────────────────────────────────────────────────────────────────
DRY_RUN=false
MODE="launch"    # launch, status, stop
GPU_LIST=""      # Comma-separated GPU indices, or empty for auto-detect
BITCRACK_BIN=""
POINTS_PER_THREAD=512

# ─── Color Output ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }
log_header()  { echo -e "\n${BOLD}═══════════════════════════════════════════════════════════${NC}"; echo -e "${BOLD}  $*${NC}"; echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"; }

# ─── Argument Parsing ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true; shift ;;
        --gpus)
            GPU_LIST="$2"; shift 2 ;;
        --status)
            MODE="status"; shift ;;
        --stop)
            MODE="stop"; shift ;;
        --binary)
            BITCRACK_BIN="$2"; shift 2 ;;
        --points|-p)
            POINTS_PER_THREAD="$2"; shift 2 ;;
        -h|--help)
            head -30 "$0" | tail -25; exit 0 ;;
        *)
            log_error "Unknown argument: $1"; exit 1 ;;
    esac
done

# ─── Cleanup Trap ────────────────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    if [[ $exit_code -ne 0 && "$MODE" == "launch" ]]; then
        log_error "Script exited with code ${exit_code}"
        log_info "Check logs in ${LOGDIR}/"
    fi
}
trap cleanup EXIT

###############################################################################
# Hex Arithmetic (using Python for big number support)
###############################################################################

# Convert hex string to decimal
hex_to_dec() {
    python3 -c "print(int('$1', 16))"
}

# Convert decimal string to hex (uppercase, no 0x prefix)
dec_to_hex() {
    python3 -c "print(format(int('$1'), 'X'))"
}

# Split a hex range into N equal sub-ranges
# Returns lines of "START END" (hex, no 0x prefix)
split_range() {
    local start_hex="$1"
    local end_hex="$2"
    local n_splits="$3"

    python3 << PYEOF
start = int("$start_hex", 16)
end = int("$end_hex", 16)
n = int("$n_splits")

total = end - start + 1
chunk = total // n
remainder = total % n

ranges = []
current = start
for i in range(n):
    # Distribute remainder across first 'remainder' chunks
    size = chunk + (1 if i < remainder else 0)
    chunk_end = current + size - 1
    ranges.append((current, chunk_end))
    current = chunk_end + 1

for (s, e) in ranges:
    print(f"{s:X} {e:X}")
PYEOF
}

###############################################################################
# Detect GPUs
###############################################################################
detect_gpus() {
    log_header "Detecting GPUs" >&2

    if $DRY_RUN && ! command -v nvidia-smi &>/dev/null; then
        log_warn "[DRY-RUN] nvidia-smi not available, simulating 2 GPUs" >&2
        echo "0, DRY-RUN-GPU-0, 24576 MiB"
        echo "1, DRY-RUN-GPU-1, 24576 MiB"
        return
    fi

    if ! command -v nvidia-smi &>/dev/null; then
        log_error "nvidia-smi not found. No NVIDIA GPUs detected." >&2
        exit 1
    fi

    local all_gpus
    all_gpus="$(nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader 2>/dev/null)"

    if [[ -z "$all_gpus" ]]; then
        log_error "No GPUs found via nvidia-smi" >&2
        exit 1
    fi

    # If user specified GPU indices, filter
    if [[ -n "$GPU_LIST" ]]; then
        log_info "Using user-specified GPUs: ${GPU_LIST}" >&2
        echo "$all_gpus"
        return
    fi

    local count
    count="$(echo "$all_gpus" | wc -l)"
    log_ok "Found ${count} GPU(s):" >&2
    echo "$all_gpus" | while IFS= read -r line; do
        log_info "  GPU ${line}" >&2
    done

    echo "$all_gpus"
}

###############################################################################
# Find BitCrack Binary
###############################################################################
find_bitcrack() {
    if [[ -n "$BITCRACK_BIN" && -x "$BITCRACK_BIN" ]]; then
        log_ok "Using specified binary: ${BITCRACK_BIN}"
        return 0
    fi

    if $DRY_RUN; then
        BITCRACK_BIN="/dry-run/cuBitCrack"
        log_warn "[DRY-RUN] Using simulated binary: ${BITCRACK_BIN}"
        return 0
    fi

    log_info "Looking for cuBitCrack binary..."

    # Search common locations
    local search_paths=(
        "${BUILDDIR}/BitCrack"
        "${BUILDDIR}/BitCrack2"
        "${BUILDDIR}/BitCrack/build"
        "${BUILDDIR}/BitCrack2/build"
        "/usr/local/bin"
        "/root"
        "."
    )

    for dir in "${search_paths[@]}"; do
        local found
        found="$(find "$dir" -name 'cuBitCrack' -type f 2>/dev/null | head -1)" || true
        if [[ -n "$found" && -x "$found" ]]; then
            BITCRACK_BIN="$(realpath "$found")"
            log_ok "Found BitCrack: ${BITCRACK_BIN}"
            return 0
        fi
    done

    log_error "cuBitCrack binary not found."
    log_info "Build it first with: ./gpu_deploy.sh --build-only"
    log_info "Or specify path: --binary /path/to/cuBitCrack"
    exit 1
}

###############################################################################
# Get Optimal Parameters for a GPU
###############################################################################
get_gpu_params() {
    local gpu_index="$1"

    # Get GPU name
    local gpu_name
    gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader -i "$gpu_index" 2>/dev/null | xargs || echo "unknown")"

    local gpu_lower
    gpu_lower="$(echo "$gpu_name" | tr '[:upper:]' '[:lower:]')"

    local threads blocks

    case "$gpu_lower" in
        *"4090"*)          threads=512; blocks=256 ;;
        *"4080"*)          threads=512; blocks=192 ;;
        *"3090"*)          threads=512; blocks=168 ;;
        *"3080"*)          threads=256; blocks=136 ;;
        *"3070"*)          threads=256; blocks=96  ;;
        *"3060"*)          threads=256; blocks=56  ;;
        *"a100"*)          threads=512; blocks=216 ;;
        *"h100"*)          threads=512; blocks=264 ;;
        *"v100"*)          threads=512; blocks=160 ;;
        *"2080"*)          threads=256; blocks=96  ;;
        *"1080"*)          threads=256; blocks=40  ;;
        *"t4"*)            threads=256; blocks=80  ;;
        *)                 threads=256; blocks=64  ;;
    esac

    echo "${threads} ${blocks}"
}

###############################################################################
# Launch BitCrack on a Single GPU
###############################################################################
launch_gpu() {
    local gpu_index="$1"
    local start_key="$2"
    local end_key="$3"

    local gpu_name
    gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader -i "$gpu_index" 2>/dev/null | xargs)"

    local params
    params="$(get_gpu_params "$gpu_index")"
    local threads blocks
    read -r threads blocks <<< "$params"

    local session_name="bitcrack_gpu${gpu_index}"
    local timestamp
    timestamp="$(date +%Y%m%d_%H%M%S)"
    local log_file="${LOGDIR}/gpu${gpu_index}_${timestamp}.log"
    local found_file="${WORKDIR}/FOUND_KEY_gpu${gpu_index}.txt"
    local checkpoint_file="${WORKDIR}/checkpoint_gpu${gpu_index}.txt"

    # Build command
    local cmd="${BITCRACK_BIN} -d ${gpu_index} --keyspace 0x${start_key}:0x${end_key} -b ${blocks} -t ${threads} -p ${POINTS_PER_THREAD} -o ${found_file} ${TARGET_ADDRESS}"

    log_info "GPU ${gpu_index} (${gpu_name}):"
    log_info "  Range:   0x${start_key} .. 0x${end_key}"
    log_info "  Threads: ${threads}  Blocks: ${blocks}  Points: ${POINTS_PER_THREAD}"
    log_info "  Session: ${session_name}"
    log_info "  Log:     ${log_file}"
    log_info "  Cmd:     ${cmd}"

    if $DRY_RUN; then
        log_warn "  [DRY-RUN] Would launch the above command"
        return 0
    fi

    # Kill existing session for this GPU
    screen -X -S "$session_name" quit 2>/dev/null || true

    # Create a wrapper that monitors for found keys
    local wrapper="${WORKDIR}/wrapper_gpu${gpu_index}.sh"
    cat > "$wrapper" << WEOF
#!/usr/bin/env bash
# Wrapper for GPU ${gpu_index} — monitors for key found
set -uo pipefail

FOUND_MAIN="${FOUND_FILE}"
FOUND_GPU="${found_file}"
CHECKPOINT="${checkpoint_file}"
LOG="${log_file}"

echo "[\$(date)] Starting BitCrack on GPU ${gpu_index}" >> "\$LOG"
echo "[\$(date)] Range: 0x${start_key} .. 0x${end_key}" >> "\$LOG"
echo "[\$(date)] Command: ${cmd}" >> "\$LOG"

${cmd} 2>&1 | while IFS= read -r line; do
    echo "\$line"
    echo "[\$(date '+%H:%M:%S')] \$line" >> "\$LOG"

    # Save progress checkpoint
    if echo "\$line" | grep -qiE '[0-9a-fA-F]{16,}'; then
        checkpoint_key="\$(echo "\$line" | grep -oP '[0-9A-Fa-f]{18,20}' | tail -1)"
        if [[ -n "\$checkpoint_key" ]]; then
            echo "\$checkpoint_key" > "\$CHECKPOINT"
        fi
    fi

    # Detect found key
    if echo "\$line" | grep -qiE 'found|private.*key'; then
        echo "==============================" >> "\$LOG"
        echo "KEY FOUND ON GPU ${gpu_index}!" >> "\$LOG"
        echo "\$line" >> "\$LOG"
        echo "==============================" >> "\$LOG"

        # Save to main found file
        {
            echo "Bitcoin Puzzle #71 — KEY FOUND"
            echo "Timestamp: \$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
            echo "GPU: ${gpu_index} (${gpu_name})"
            echo "Target: ${TARGET_ADDRESS}"
            echo ""
            echo "\$line"
        } > "\$FOUND_MAIN"

        # Backup copies
        cp "\$FOUND_MAIN" /root/FOUND_KEY_PUZZLE71.txt 2>/dev/null || true
        cp "\$FOUND_MAIN" /tmp/FOUND_KEY_PUZZLE71.txt 2>/dev/null || true

        echo ""
        echo "================================================================"
        echo "  KEY FOUND! Saved to: \$FOUND_MAIN"
        echo "================================================================"
        cat "\$FOUND_MAIN"
    fi
done

exit_code=\${PIPESTATUS[0]}
echo "[\$(date)] BitCrack exited with code \$exit_code" >> "\$LOG"
exit \$exit_code
WEOF

    chmod +x "$wrapper"

    # Launch in screen
    screen -dmS "$session_name" bash "$wrapper"

    # Save PID
    local screen_pid
    screen_pid="$(screen -list | grep "$session_name" | grep -oP '^\s*\K\d+' || echo "unknown")"
    echo "$screen_pid" > "${PID_DIR}/gpu${gpu_index}.pid"

    sleep 1

    if screen -list | grep -q "$session_name"; then
        log_ok "  GPU ${gpu_index} launched (screen PID: ${screen_pid})"
    else
        log_error "  GPU ${gpu_index} failed to launch. Check ${log_file}"
    fi
}

###############################################################################
# Monitor All GPU Processes
###############################################################################
launch_monitor() {
    log_header "Starting Monitor"

    if $DRY_RUN; then
        log_warn "[DRY-RUN] Would launch monitoring process"
        return 0
    fi

    local monitor_script="${WORKDIR}/monitor.sh"

    cat > "$monitor_script" << 'MEOF'
#!/usr/bin/env bash
# Monitor all BitCrack GPU processes
# Checks for:
#   - Found keys (stops everything and alerts)
#   - Dead processes (alerts and optionally restarts)
#   - Progress summary

WORKDIR="/root/puzzle71"
FOUND_FILE="${WORKDIR}/FOUND_KEY.txt"
LOGDIR="${WORKDIR}/logs"
CHECK_INTERVAL=30

while true; do
    clear
    echo "================================================================"
    echo "  Bitcoin Puzzle #71 — Multi-GPU Monitor"
    echo "  $(date '+%Y-%m-%d %H:%M:%S')"
    echo "================================================================"
    echo ""

    # Check if key has been found
    if [[ -f "$FOUND_FILE" ]] && [[ -s "$FOUND_FILE" ]]; then
        echo "  *** KEY FOUND! ***"
        echo ""
        cat "$FOUND_FILE"
        echo ""
        echo "  Stopping all BitCrack processes..."

        # Kill all BitCrack screen sessions
        screen -list 2>/dev/null | grep "bitcrack_gpu" | grep -oP '^\s*\K\d+' | while read -r pid; do
            kill "$pid" 2>/dev/null || true
        done

        echo "  All processes stopped."
        echo "  Key saved to: $FOUND_FILE"
        break
    fi

    # Show status of each GPU
    echo "  GPU Status:"
    echo "  ─────────────────────────────────────────"

    local any_running=false

    for session in $(screen -list 2>/dev/null | grep -oP '\d+\.bitcrack_gpu\d+' || true); do
        any_running=true
        local gpu_num
        gpu_num="$(echo "$session" | grep -oP 'gpu\K\d+')"
        local pid
        pid="$(echo "$session" | grep -oP '^\d+')"

        # Get latest log line
        local latest_log
        latest_log="$(ls -t ${LOGDIR}/gpu${gpu_num}_*.log 2>/dev/null | head -1)"
        local last_line=""
        if [[ -n "$latest_log" ]]; then
            last_line="$(tail -1 "$latest_log" 2>/dev/null || echo "no output")"
        fi

        # Get checkpoint
        local checkpoint=""
        if [[ -f "${WORKDIR}/checkpoint_gpu${gpu_num}.txt" ]]; then
            checkpoint="$(cat "${WORKDIR}/checkpoint_gpu${gpu_num}.txt" 2>/dev/null || echo "")"
        fi

        echo "  GPU ${gpu_num}: RUNNING (PID ${pid})"
        if [[ -n "$checkpoint" ]]; then
            echo "    Checkpoint: 0x${checkpoint}"
        fi
        echo "    Last: ${last_line:0:70}"
        echo ""
    done

    if ! $any_running; then
        echo "  No BitCrack processes running."
        echo "  Exiting monitor."
        break
    fi

    # Check per-GPU found files
    for f in ${WORKDIR}/FOUND_KEY_gpu*.txt; do
        if [[ -f "$f" ]] && [[ -s "$f" ]]; then
            echo ""
            echo "  *** KEY FOUND in ${f}! ***"
            cat "$f"
            cp "$f" "$FOUND_FILE" 2>/dev/null || true
        fi
    done

    echo ""
    echo "  Press Ctrl+C to stop monitor (BitCrack keeps running)"
    echo "  To stop all: ./multi_gpu_launch.sh --stop"
    echo "  Attach GPU:  screen -r bitcrack_gpu0"

    sleep "$CHECK_INTERVAL"
done
MEOF

    chmod +x "$monitor_script"

    # Kill existing monitor
    screen -X -S "bitcrack_monitor" quit 2>/dev/null || true

    # Launch monitor in screen
    screen -dmS "bitcrack_monitor" bash "$monitor_script"

    log_ok "Monitor running in screen session: bitcrack_monitor"
    log_info "Attach: screen -r bitcrack_monitor"
}

###############################################################################
# Show Status
###############################################################################
show_status() {
    log_header "BitCrack Multi-GPU Status"

    if ! command -v screen &>/dev/null; then
        log_error "screen not installed"
        exit 1
    fi

    echo ""
    echo "  Running Sessions:"
    echo "  ─────────────────────────────────────────"

    local found_any=false

    screen -list 2>/dev/null | grep "bitcrack_" | while IFS= read -r line; do
        found_any=true
        echo "  $line"
    done

    if ! screen -list 2>/dev/null | grep -q "bitcrack_"; then
        echo "  (none)"
    fi

    echo ""

    # Show checkpoints
    echo "  Checkpoints:"
    echo "  ─────────────────────────────────────────"
    for f in "${WORKDIR}"/checkpoint_gpu*.txt; do
        if [[ -f "$f" ]]; then
            local gpu_num
            gpu_num="$(basename "$f" | grep -oP 'gpu\K\d+')"
            local checkpoint
            checkpoint="$(cat "$f" 2>/dev/null || echo "unknown")"
            echo "  GPU ${gpu_num}: 0x${checkpoint}"
        fi
    done

    echo ""

    # Check for found key
    if [[ -f "$FOUND_FILE" ]] && [[ -s "$FOUND_FILE" ]]; then
        echo ""
        log_ok "KEY FOUND!"
        cat "$FOUND_FILE"
    else
        echo "  Key not found yet."
    fi

    echo ""
    echo "  Logs: ls -la ${LOGDIR}/gpu*.log"
    echo ""
}

###############################################################################
# Stop All Instances
###############################################################################
stop_all() {
    log_header "Stopping All BitCrack Processes"

    if $DRY_RUN; then
        log_warn "[DRY-RUN] Would stop all bitcrack screen sessions"
        return 0
    fi

    local count=0

    # Kill all bitcrack screen sessions
    screen -list 2>/dev/null | grep "bitcrack_" | grep -oP '^\s*\K\d+' | while read -r pid; do
        log_info "Killing screen session PID: ${pid}"
        screen -X -S "$pid" quit 2>/dev/null || kill "$pid" 2>/dev/null || true
        count=$((count + 1))
    done

    # Also kill any stray cuBitCrack processes
    local stray
    stray="$(pgrep -f cuBitCrack 2>/dev/null || true)"
    if [[ -n "$stray" ]]; then
        log_info "Killing stray cuBitCrack processes: ${stray}"
        pkill -f cuBitCrack 2>/dev/null || true
    fi

    # Clean up PID files
    rm -f "${PID_DIR}"/*.pid 2>/dev/null || true

    log_ok "All BitCrack processes stopped"
}

###############################################################################
# Main
###############################################################################
main() {
    log_header "Bitcoin Puzzle #71 — Multi-GPU Launcher"
    echo ""
    echo "  Target: ${TARGET_ADDRESS}"
    echo "  Range:  0x${RANGE_START} .. 0x${RANGE_END}"
    echo ""

    mkdir -p "${WORKDIR}" "${LOGDIR}" "${PID_DIR}"

    case "$MODE" in
        status)
            show_status
            exit 0
            ;;
        stop)
            stop_all
            exit 0
            ;;
        launch)
            ;;  # Continue below
    esac

    # Detect GPUs
    local gpu_info
    gpu_info="$(detect_gpus)"

    # Parse GPU indices
    local gpu_indices=()
    if [[ -n "$GPU_LIST" ]]; then
        IFS=',' read -ra gpu_indices <<< "$GPU_LIST"
    else
        while IFS= read -r line; do
            local idx
            idx="$(echo "$line" | cut -d',' -f1 | xargs)"
            gpu_indices+=("$idx")
        done <<< "$gpu_info"
    fi

    local num_gpus="${#gpu_indices[@]}"

    if [[ "$num_gpus" -eq 0 ]]; then
        log_error "No GPUs to use"
        exit 1
    fi

    log_ok "Will use ${num_gpus} GPU(s): ${gpu_indices[*]}"

    # Find BitCrack binary
    find_bitcrack

    # Split the key range across GPUs
    log_header "Splitting Key Range"

    local ranges
    ranges="$(split_range "$RANGE_START" "$RANGE_END" "$num_gpus")"

    echo ""
    log_info "Range splits:"
    local i=0
    while IFS= read -r range_line; do
        local range_start range_end
        read -r range_start range_end <<< "$range_line"
        local gpu_idx="${gpu_indices[$i]}"
        log_info "  GPU ${gpu_idx}: 0x${range_start} .. 0x${range_end}"
        i=$((i + 1))
    done <<< "$ranges"

    echo ""

    if $DRY_RUN; then
        log_warn "[DRY-RUN] Would launch BitCrack on each GPU with above ranges"
        log_warn "[DRY-RUN] Would start monitor process"
        return 0
    fi

    # Stop any existing instances
    log_info "Stopping any existing BitCrack instances..."
    stop_all 2>/dev/null || true
    sleep 1

    # Launch BitCrack on each GPU
    log_header "Launching BitCrack Instances"

    i=0
    while IFS= read -r range_line; do
        local range_start range_end
        read -r range_start range_end <<< "$range_line"
        local gpu_idx="${gpu_indices[$i]}"

        launch_gpu "$gpu_idx" "$range_start" "$range_end"
        i=$((i + 1))
    done <<< "$ranges"

    echo ""

    # Launch monitor
    launch_monitor

    # Summary
    log_header "All GPUs Launched"
    echo ""
    echo "  Active GPUs:  ${num_gpus}"
    echo "  Binary:       ${BITCRACK_BIN}"
    echo "  Target:       ${TARGET_ADDRESS}"
    echo ""
    echo "  Commands:"
    echo "    Monitor:     screen -r bitcrack_monitor"
    echo "    GPU 0:       screen -r bitcrack_gpu0"
    echo "    Status:      ./multi_gpu_launch.sh --status"
    echo "    Stop all:    ./multi_gpu_launch.sh --stop"
    echo "    Found key:   cat ${FOUND_FILE}"
    echo ""
    echo "  Log files:"
    for idx in "${gpu_indices[@]}"; do
        local latest
        latest="$(ls -t "${LOGDIR}/gpu${idx}_"*.log 2>/dev/null | head -1 || echo "not yet")"
        echo "    GPU ${idx}: ${latest}"
    done
    echo ""
}

main "$@"
