#!/usr/bin/env bash
###############################################################################
# gpu_deploy.sh — Bitcoin Puzzle #71 GPU BitCrack Deployment
###############################################################################
#
# Deploys BitCrack on a fresh GPU cloud instance (vast.ai, Lambda Labs, RunPod)
# to search for Bitcoin Puzzle #71 private key.
#
# Target:  1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU
# H160:    f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8
# Range:   0x400000000000000000 to 0x7FFFFFFFFFFFFFFFFF (2^70 to 2^71-1)
#
# Usage:
#   ./gpu_deploy.sh                  # Full setup + launch
#   ./gpu_deploy.sh --dry-run        # Print what would happen, don't execute
#   ./gpu_deploy.sh --build-only     # Build BitCrack but don't launch
#   ./gpu_deploy.sh --launch-only    # Skip build, just launch (assumes built)
#   ./gpu_deploy.sh --start KEY      # Resume from specific start key (hex)
#   ./gpu_deploy.sh --stride N       # Custom stride (for multi-instance splits)
#
# Environment variables:
#   BITCRACK_THREADS    — threads per block (default: auto-detect)
#   BITCRACK_BLOCKS     — grid blocks (default: auto-detect)
#   BITCRACK_POINTS     — points per thread (default: 512)
#   START_KEY           — override start of range (hex, no 0x prefix)
#   END_KEY             — override end of range (hex, no 0x prefix)
#
###############################################################################
set -euo pipefail

# ─── Constants ───────────────────────────────────────────────────────────────
TARGET_ADDRESS="1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU"
TARGET_H160="f6f5431d25bbf7b12e8add9af5e3475c44a0a5b8"
DEFAULT_START="400000000000000000"
DEFAULT_END="7FFFFFFFFFFFFFFFFF"

WORKDIR="/root/puzzle71"
BUILDDIR="${WORKDIR}/build"
LOGDIR="${WORKDIR}/logs"
FOUND_FILE="${WORKDIR}/FOUND_KEY.txt"
PROGRESS_LOG="${LOGDIR}/bitcrack_progress.log"
CHECKPOINT_FILE="${WORKDIR}/checkpoint.txt"

BITCRACK_REPO="https://github.com/brichard19/BitCrack.git"
BITCRACK2_REPO="https://github.com/secp8x32/BitCrack2.git"

# ─── Defaults (overridable via env) ─────────────────────────────────────────
BITCRACK_POINTS="${BITCRACK_POINTS:-512}"
START_KEY="${START_KEY:-$DEFAULT_START}"
END_KEY="${END_KEY:-$DEFAULT_END}"

# ─── State ───────────────────────────────────────────────────────────────────
DRY_RUN=false
BUILD_ONLY=false
LAUNCH_ONLY=false
BITCRACK_BIN=""
USED_FORK=""       # "original" or "bitcrack2"
GPU_NAME=""
GPU_COUNT=0
CUDA_VERSION=""

# ─── Color Output ────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

log_info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }
log_header()  { echo -e "\n${BOLD}═══════════════════════════════════════════════════════════${NC}"; echo -e "${BOLD}  $*${NC}"; echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"; }
log_dry()     { echo -e "${YELLOW}[DRY-RUN]${NC} $*"; }

# ─── Argument Parsing ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true; shift ;;
        --build-only)
            BUILD_ONLY=true; shift ;;
        --launch-only)
            LAUNCH_ONLY=true; shift ;;
        --start)
            START_KEY="$2"; shift 2 ;;
        --end)
            END_KEY="$2"; shift 2 ;;
        --stride)
            BITCRACK_STRIDE="$2"; shift 2 ;;
        -h|--help)
            head -30 "$0" | tail -25; exit 0 ;;
        *)
            log_error "Unknown argument: $1"; exit 1 ;;
    esac
done

# ─── Cleanup Trap ────────────────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        log_error "Script exited with code $exit_code"
        log_info "Logs saved to: ${LOGDIR}/"
    fi
}
trap cleanup EXIT

# ─── Key-Found Handler ──────────────────────────────────────────────────────
save_found_key() {
    local key_file="$1"
    local timestamp
    timestamp="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"

    if [[ -f "$key_file" ]] && grep -qi "found\|key" "$key_file" 2>/dev/null; then
        log_header "PRIVATE KEY FOUND"
        cat "$key_file"

        # Save to multiple locations for redundancy
        local found_content
        found_content="$(cat "$key_file")"
        local save_msg="Bitcoin Puzzle #71 — KEY FOUND
Timestamp: ${timestamp}
Target: ${TARGET_ADDRESS}
H160: ${TARGET_H160}

${found_content}
"
        for dest in \
            "${FOUND_FILE}" \
            "/root/FOUND_KEY_PUZZLE71.txt" \
            "/tmp/FOUND_KEY_PUZZLE71.txt" \
            "${HOME}/FOUND_KEY_PUZZLE71.txt"; do
            echo "$save_msg" > "$dest" 2>/dev/null || true
        done

        log_ok "Key saved to:"
        log_ok "  ${FOUND_FILE}"
        log_ok "  /root/FOUND_KEY_PUZZLE71.txt"
        log_ok "  /tmp/FOUND_KEY_PUZZLE71.txt"
        log_ok "  ${HOME}/FOUND_KEY_PUZZLE71.txt"
        return 0
    fi
    return 1
}

###############################################################################
# Phase 1: Detect GPU
###############################################################################
detect_gpu() {
    log_header "Phase 1: GPU Detection"

    if $DRY_RUN; then
        log_dry "Would run: nvidia-smi"
        log_dry "Would detect GPU model, count, and CUDA version"
        GPU_NAME="DRY-RUN-GPU"
        GPU_COUNT=1
        CUDA_VERSION="12.0"
        return 0
    fi

    # Check nvidia-smi exists
    if ! command -v nvidia-smi &>/dev/null; then
        log_error "nvidia-smi not found. Is this a GPU instance?"
        log_info "If NVIDIA drivers are not installed, run:"
        log_info "  apt-get update && apt-get install -y nvidia-driver-535"
        exit 1
    fi

    # Get GPU info
    GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1 | xargs)"
    GPU_COUNT="$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l)"
    local gpu_memory
    gpu_memory="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | xargs)"

    # Get CUDA version from nvidia-smi
    CUDA_VERSION="$(nvidia-smi | grep -oP 'CUDA Version: \K[0-9]+\.[0-9]+' || echo "unknown")"

    log_ok "GPU:          ${GPU_NAME}"
    log_ok "GPU Count:    ${GPU_COUNT}"
    log_ok "GPU Memory:   ${gpu_memory} MiB"
    log_ok "CUDA Version: ${CUDA_VERSION}"

    # Print all GPUs if multiple
    if [[ "$GPU_COUNT" -gt 1 ]]; then
        log_info "All GPUs:"
        nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader | while read -r line; do
            log_info "  GPU $line"
        done
    fi

    nvidia-smi | tee "${LOGDIR}/nvidia-smi.txt"
}

###############################################################################
# Phase 2: Install Dependencies
###############################################################################
install_deps() {
    log_header "Phase 2: Installing Dependencies"

    if $DRY_RUN; then
        log_dry "Would install: build-essential git cmake libgmp-dev"
        log_dry "Would install CUDA toolkit if not present"
        return 0
    fi

    # Update package lists
    log_info "Updating package lists..."
    apt-get update -qq 2>/dev/null || {
        log_warn "apt-get update failed (might be non-Debian). Trying alternatives..."
    }

    # Install build dependencies
    log_info "Installing build tools..."
    apt-get install -y -qq \
        build-essential \
        git \
        cmake \
        libgmp-dev \
        libssl-dev \
        pkg-config \
        curl \
        wget \
        screen \
        2>/dev/null || {
        log_warn "Some packages may have failed to install. Continuing..."
    }

    # Check if CUDA toolkit is installed (nvcc)
    if command -v nvcc &>/dev/null; then
        local nvcc_version
        nvcc_version="$(nvcc --version | grep -oP 'release \K[0-9]+\.[0-9]+' || echo "unknown")"
        log_ok "CUDA toolkit already installed (nvcc ${nvcc_version})"
    else
        log_warn "nvcc not found. Installing CUDA toolkit..."
        install_cuda_toolkit
    fi
}

install_cuda_toolkit() {
    if $DRY_RUN; then
        log_dry "Would install CUDA toolkit"
        return 0
    fi

    # Try installing from the existing NVIDIA repo first (common on cloud instances)
    if apt-get install -y -qq cuda-toolkit 2>/dev/null; then
        log_ok "CUDA toolkit installed via apt"
        return 0
    fi

    # Try nvidia-cuda-toolkit (Ubuntu package)
    if apt-get install -y -qq nvidia-cuda-toolkit 2>/dev/null; then
        log_ok "CUDA toolkit installed via nvidia-cuda-toolkit package"
        return 0
    fi

    # Manual install: try CUDA 12.x keyring
    log_info "Attempting manual CUDA 12 installation..."
    local distro
    distro="$(. /etc/os-release 2>/dev/null && echo "${ID}${VERSION_ID//.}" || echo "ubuntu2204")"

    wget -q "https://developer.download.nvidia.com/compute/cuda/repos/${distro}/x86_64/cuda-keyring_1.1-1_all.deb" -O /tmp/cuda-keyring.deb 2>/dev/null && \
        dpkg -i /tmp/cuda-keyring.deb 2>/dev/null && \
        apt-get update -qq 2>/dev/null && \
        apt-get install -y -qq cuda-toolkit-12-4 2>/dev/null && {
            log_ok "CUDA 12.4 toolkit installed"
            return 0
        }

    # If all else fails, check if CUDA libs are present even without nvcc
    if [[ -d "/usr/local/cuda" ]]; then
        export PATH="/usr/local/cuda/bin:$PATH"
        export LD_LIBRARY_PATH="/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}"
        if command -v nvcc &>/dev/null; then
            log_ok "Found CUDA at /usr/local/cuda"
            return 0
        fi
    fi

    # Search for any cuda installation
    for cuda_dir in /usr/local/cuda-*; do
        if [[ -d "$cuda_dir" && -x "${cuda_dir}/bin/nvcc" ]]; then
            export PATH="${cuda_dir}/bin:$PATH"
            export LD_LIBRARY_PATH="${cuda_dir}/lib64:${LD_LIBRARY_PATH:-}"
            log_ok "Found CUDA at ${cuda_dir}"
            return 0
        fi
    done

    log_error "Could not install CUDA toolkit automatically."
    log_info "Please install manually:"
    log_info "  https://developer.nvidia.com/cuda-downloads"
    exit 1
}

###############################################################################
# Phase 3: Build BitCrack
###############################################################################
build_bitcrack() {
    log_header "Phase 3: Building BitCrack"

    if $DRY_RUN; then
        log_dry "Would clone ${BITCRACK_REPO}"
        log_dry "Would attempt make/cmake build"
        log_dry "Would fall back to ${BITCRACK2_REPO} if needed"
        BITCRACK_BIN="/dry-run/cuBitCrack"
        USED_FORK="dry-run"
        return 0
    fi

    mkdir -p "${BUILDDIR}"

    # Ensure CUDA paths are set
    for cuda_dir in /usr/local/cuda /usr/local/cuda-12 /usr/local/cuda-12.*; do
        if [[ -d "$cuda_dir" ]]; then
            export PATH="${cuda_dir}/bin:${PATH}"
            export LD_LIBRARY_PATH="${cuda_dir}/lib64:${LD_LIBRARY_PATH:-}"
            export CUDA_HOME="${cuda_dir}"
            break
        fi
    done

    # Attempt 1: Original BitCrack
    if try_build_original; then
        return 0
    fi

    # Attempt 2: BitCrack2 fork (more maintained, broader GPU support)
    log_warn "Original BitCrack build failed. Trying BitCrack2 fork..."
    if try_build_bitcrack2; then
        return 0
    fi

    log_error "Both BitCrack builds failed."
    log_info "Build logs are in ${LOGDIR}/"
    log_info "Common issues:"
    log_info "  - CUDA toolkit not properly installed (nvcc missing)"
    log_info "  - Incompatible CUDA/GPU architecture"
    log_info "  - Missing build dependencies"
    exit 1
}

try_build_original() {
    log_info "Attempting to build original BitCrack..."
    local src_dir="${BUILDDIR}/BitCrack"
    local build_log="${LOGDIR}/build_bitcrack_original.log"

    # Clone if not present
    if [[ ! -d "$src_dir" ]]; then
        log_info "Cloning ${BITCRACK_REPO}..."
        git clone --depth 1 "$BITCRACK_REPO" "$src_dir" 2>&1 | tee -a "$build_log" || {
            log_warn "Failed to clone original BitCrack repo"
            return 1
        }
    fi

    cd "$src_dir"

    # Detect GPU compute capability for optimal build
    local compute_cap
    compute_cap="$(detect_compute_capability)"

    # Try cmake build first
    if [[ -f "CMakeLists.txt" ]]; then
        log_info "Building with cmake (compute capability: ${compute_cap})..."
        mkdir -p build && cd build
        if cmake .. -DCMAKE_CUDA_ARCHITECTURES="${compute_cap}" 2>&1 | tee -a "$build_log" && \
           make -j"$(nproc)" 2>&1 | tee -a "$build_log"; then
            # Find the binary
            local bin
            bin="$(find . -name 'cuBitCrack' -type f 2>/dev/null | head -1)"
            if [[ -n "$bin" && -x "$bin" ]]; then
                BITCRACK_BIN="$(realpath "$bin")"
                USED_FORK="original"
                log_ok "BitCrack (original) built: ${BITCRACK_BIN}"
                cd "${WORKDIR}"
                return 0
            fi
        fi
        cd "$src_dir"
    fi

    # Try make build
    log_info "Trying Makefile build..."
    if [[ -f "Makefile" ]]; then
        make clean 2>/dev/null || true
        if COMPUTE_CAP="${compute_cap}" make -j"$(nproc)" 2>&1 | tee -a "$build_log"; then
            local bin
            bin="$(find . -name 'cuBitCrack' -type f 2>/dev/null | head -1)"
            if [[ -n "$bin" && -x "$bin" ]]; then
                BITCRACK_BIN="$(realpath "$bin")"
                USED_FORK="original"
                log_ok "BitCrack (original) built: ${BITCRACK_BIN}"
                cd "${WORKDIR}"
                return 0
            fi
        fi
    fi

    cd "${WORKDIR}"
    return 1
}

try_build_bitcrack2() {
    log_info "Attempting to build BitCrack2..."
    local src_dir="${BUILDDIR}/BitCrack2"
    local build_log="${LOGDIR}/build_bitcrack2.log"

    # Clone if not present
    if [[ ! -d "$src_dir" ]]; then
        log_info "Cloning ${BITCRACK2_REPO}..."
        git clone --depth 1 "$BITCRACK2_REPO" "$src_dir" 2>&1 | tee -a "$build_log" || {
            log_warn "Failed to clone BitCrack2 repo"
            return 1
        }
    fi

    cd "$src_dir"

    local compute_cap
    compute_cap="$(detect_compute_capability)"

    # BitCrack2 typically uses make with CCAP variable
    if [[ -f "Makefile" ]]; then
        log_info "Building BitCrack2 with make (compute capability: ${compute_cap})..."
        make clean 2>/dev/null || true
        if CCAP="${compute_cap}" BUILD_CUDA=1 make -j"$(nproc)" 2>&1 | tee -a "$build_log"; then
            local bin
            bin="$(find . -name 'cuBitCrack' -type f 2>/dev/null | head -1)"
            if [[ -n "$bin" && -x "$bin" ]]; then
                BITCRACK_BIN="$(realpath "$bin")"
                USED_FORK="bitcrack2"
                log_ok "BitCrack2 built: ${BITCRACK_BIN}"
                cd "${WORKDIR}"
                return 0
            fi
        fi
    fi

    # Try cmake if available
    if [[ -f "CMakeLists.txt" ]]; then
        log_info "Building BitCrack2 with cmake..."
        mkdir -p build && cd build
        if cmake .. -DCMAKE_CUDA_ARCHITECTURES="${compute_cap}" 2>&1 | tee -a "$build_log" && \
           make -j"$(nproc)" 2>&1 | tee -a "$build_log"; then
            local bin
            bin="$(find . -name 'cuBitCrack' -type f 2>/dev/null | head -1)"
            if [[ -n "$bin" && -x "$bin" ]]; then
                BITCRACK_BIN="$(realpath "$bin")"
                USED_FORK="bitcrack2"
                log_ok "BitCrack2 built: ${BITCRACK_BIN}"
                cd "${WORKDIR}"
                return 0
            fi
        fi
        cd "$src_dir"
    fi

    cd "${WORKDIR}"
    return 1
}

detect_compute_capability() {
    # Returns the CUDA compute capability (e.g., "86" for RTX 3090)
    # This determines which GPU architecture to compile for
    local cap

    # Try querying the GPU directly
    cap="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '.' | xargs)" || true

    if [[ -n "$cap" && "$cap" != "N/A" ]]; then
        echo "$cap"
        return
    fi

    # Fallback: infer from GPU name
    local gpu_lower
    gpu_lower="$(echo "$GPU_NAME" | tr '[:upper:]' '[:lower:]')"

    case "$gpu_lower" in
        *"4090"*|*"4080"*|*"4070"*|*"l40"*)   echo "89" ;;
        *"a100"*|*"a30"*)                        echo "80" ;;
        *"3090"*|*"3080"*|*"3070"*|*"3060"*|*"a5000"*|*"a4000"*) echo "86" ;;
        *"a6000"*)                               echo "86" ;;
        *"2080"*|*"2070"*|*"2060"*|*"t4"*)     echo "75" ;;
        *"1080"*|*"1070"*|*"1060"*|*"p100"*)   echo "61" ;;
        *"v100"*)                                echo "70" ;;
        *"h100"*)                                echo "90" ;;
        *)
            log_warn "Unknown GPU '${GPU_NAME}', defaulting to compute cap 75"
            echo "75"
            ;;
    esac
}

###############################################################################
# Phase 4: Configure and Launch BitCrack
###############################################################################
configure_launch_params() {
    # Set optimal threads/blocks based on GPU
    local gpu_lower
    gpu_lower="$(echo "$GPU_NAME" | tr '[:upper:]' '[:lower:]')"

    # Defaults
    local threads="${BITCRACK_THREADS:-0}"
    local blocks="${BITCRACK_BLOCKS:-0}"
    local points="${BITCRACK_POINTS}"

    if [[ "$threads" -eq 0 || "$blocks" -eq 0 ]]; then
        case "$gpu_lower" in
            *"4090"*)
                threads=512; blocks=256; points=1024 ;;
            *"4080"*)
                threads=512; blocks=192; points=1024 ;;
            *"3090"*)
                threads=512; blocks=168; points=512 ;;
            *"3080"*)
                threads=256; blocks=136; points=512 ;;
            *"3070"*)
                threads=256; blocks=96;  points=512 ;;
            *"3060"*)
                threads=256; blocks=56;  points=512 ;;
            *"a100"*)
                threads=512; blocks=216; points=1024 ;;
            *"h100"*)
                threads=512; blocks=264; points=1024 ;;
            *"v100"*)
                threads=512; blocks=160; points=512 ;;
            *"2080"*)
                threads=256; blocks=96;  points=256 ;;
            *"1080"*)
                threads=256; blocks=40;  points=256 ;;
            *"t4"*)
                threads=256; blocks=80;  points=256 ;;
            *)
                # Conservative defaults
                threads=256; blocks=64;  points=256 ;;
        esac
    fi

    echo "${threads} ${blocks} ${points}"
}

launch_bitcrack() {
    log_header "Phase 4: Launching BitCrack"

    # Check for checkpoint to resume from
    local start_key="$START_KEY"
    if [[ -f "$CHECKPOINT_FILE" && "$start_key" == "$DEFAULT_START" ]]; then
        local saved_key
        saved_key="$(cat "$CHECKPOINT_FILE" | xargs)"
        if [[ -n "$saved_key" ]]; then
            log_info "Resuming from checkpoint: 0x${saved_key}"
            start_key="$saved_key"
        fi
    fi

    local params
    params="$(configure_launch_params)"
    local threads blocks points
    read -r threads blocks points <<< "$params"

    # Build the command
    local cmd_args=(
        "${BITCRACK_BIN}"
        "--keyspace" "0x${start_key}:0x${END_KEY}"
        "-b" "${blocks}"
        "-t" "${threads}"
        "-p" "${points}"
        "-o" "${FOUND_FILE}"
    )

    # Add stride if specified (for multi-instance range splitting)
    if [[ -n "${BITCRACK_STRIDE:-}" ]]; then
        cmd_args+=("--stride" "${BITCRACK_STRIDE}")
    fi

    # Add the target address
    cmd_args+=("${TARGET_ADDRESS}")

    log_info "Configuration:"
    log_info "  Binary:     ${BITCRACK_BIN} (${USED_FORK})"
    log_info "  GPU:        ${GPU_NAME}"
    log_info "  Threads:    ${threads}"
    log_info "  Blocks:     ${blocks}"
    log_info "  Points:     ${points}"
    log_info "  Start Key:  0x${start_key}"
    log_info "  End Key:    0x${END_KEY}"
    log_info "  Target:     ${TARGET_ADDRESS}"
    log_info "  Output:     ${FOUND_FILE}"
    log_info ""
    log_info "  Command: ${cmd_args[*]}"

    if $DRY_RUN; then
        log_dry "Would execute the above command"
        log_dry "Would monitor output for key discovery"
        return 0
    fi

    # Estimate search space
    log_info ""
    log_info "Search space: 2^70 keys (1,180,591,620,717,411,303,424 total)"
    log_info "At ~800 MKey/s (RTX 3090), full range would take ~46,800 years"
    log_info "We're searching for ONE specific key in this range."
    log_info "Starting search now..."
    log_info ""

    # Create a wrapper script that handles checkpoint saving and key detection
    local wrapper="${WORKDIR}/bitcrack_wrapper.sh"
    cat > "$wrapper" << 'WRAPPER_EOF'
#!/usr/bin/env bash
# BitCrack wrapper — monitors output, saves checkpoints, detects found keys
set -uo pipefail

FOUND_FILE="__FOUND_FILE__"
CHECKPOINT_FILE="__CHECKPOINT_FILE__"
PROGRESS_LOG="__PROGRESS_LOG__"

# Run BitCrack and process output line by line
"$@" 2>&1 | while IFS= read -r line; do
    echo "$line"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $line" >> "$PROGRESS_LOG"

    # Save checkpoint from progress updates (extract current key position)
    if echo "$line" | grep -qiE '^\[.*\].*[0-9a-fA-F]{16,}'; then
        local_key="$(echo "$line" | grep -oP '[0-9A-Fa-f]{18,20}' | tail -1)"
        if [[ -n "$local_key" ]]; then
            echo "$local_key" > "$CHECKPOINT_FILE"
        fi
    fi

    # Detect found key
    if echo "$line" | grep -qiE 'found|private.*key|result'; then
        echo "=== KEY POTENTIALLY FOUND ===" >> "$PROGRESS_LOG"
        echo "$line" >> "$FOUND_FILE.raw"
    fi
done

# After BitCrack exits, check if key was found
exit_code=${PIPESTATUS[0]}
if [[ -f "$FOUND_FILE" ]] && [[ -s "$FOUND_FILE" ]]; then
    echo ""
    echo "================================================================"
    echo "  PRIVATE KEY FOUND! Saved to: $FOUND_FILE"
    echo "================================================================"
    cat "$FOUND_FILE"

    # Copy to backup locations
    cp "$FOUND_FILE" /root/FOUND_KEY_PUZZLE71.txt 2>/dev/null || true
    cp "$FOUND_FILE" /tmp/FOUND_KEY_PUZZLE71.txt 2>/dev/null || true
    cp "$FOUND_FILE" "${HOME}/FOUND_KEY_PUZZLE71.txt" 2>/dev/null || true
fi

exit $exit_code
WRAPPER_EOF

    # Substitute paths into wrapper
    sed -i "s|__FOUND_FILE__|${FOUND_FILE}|g" "$wrapper"
    sed -i "s|__CHECKPOINT_FILE__|${CHECKPOINT_FILE}|g" "$wrapper"
    sed -i "s|__PROGRESS_LOG__|${PROGRESS_LOG}|g" "$wrapper"
    chmod +x "$wrapper"

    # Launch in a screen session so it survives SSH disconnects
    local session_name="bitcrack_puzzle71"
    local timestamp
    timestamp="$(date +%Y%m%d_%H%M%S)"
    local run_log="${LOGDIR}/bitcrack_run_${timestamp}.log"

    # Kill existing session if any
    screen -X -S "$session_name" quit 2>/dev/null || true

    log_info "Launching in screen session: ${session_name}"
    log_info "Run log: ${run_log}"
    log_info ""
    log_info "To attach:  screen -r ${session_name}"
    log_info "To detach:  Ctrl+A, D"
    log_info "To check:   cat ${FOUND_FILE}"
    log_info ""

    # Launch
    screen -dmS "$session_name" bash -c \
        "${wrapper} ${cmd_args[*]} 2>&1 | tee ${run_log}; echo 'BitCrack exited with code $?' >> ${run_log}"

    sleep 2

    # Verify it's running
    if screen -list | grep -q "$session_name"; then
        log_ok "BitCrack is running in screen session '${session_name}'"
    else
        log_error "Failed to start BitCrack. Check ${run_log}"
        # Try running directly as fallback
        log_info "Attempting direct launch (foreground)..."
        exec "${cmd_args[@]}" 2>&1 | tee "${run_log}"
    fi

    log_header "Deployment Complete"
    echo ""
    echo "  Monitor:    screen -r ${session_name}"
    echo "  Progress:   tail -f ${PROGRESS_LOG}"
    echo "  Run log:    tail -f ${run_log}"
    echo "  Found key:  cat ${FOUND_FILE}"
    echo "  Checkpoint: cat ${CHECKPOINT_FILE}"
    echo ""
}

###############################################################################
# Main
###############################################################################
main() {
    log_header "Bitcoin Puzzle #71 — GPU BitCrack Deployment"
    echo ""
    echo "  Target:  ${TARGET_ADDRESS}"
    echo "  H160:    ${TARGET_H160}"
    echo "  Range:   0x${START_KEY} .. 0x${END_KEY}"
    echo ""

    if $DRY_RUN; then
        log_warn "DRY-RUN MODE — nothing will be executed"
        echo ""
    fi

    # Create directories
    mkdir -p "${WORKDIR}" "${BUILDDIR}" "${LOGDIR}"

    # Phase 1: Detect GPU
    detect_gpu

    if ! $LAUNCH_ONLY; then
        # Phase 2: Install deps
        install_deps

        # Phase 3: Build BitCrack
        build_bitcrack
    else
        # Launch-only: find existing binary
        BITCRACK_BIN="$(find "${BUILDDIR}" -name 'cuBitCrack' -type f 2>/dev/null | head -1)"
        if [[ -z "$BITCRACK_BIN" ]]; then
            log_error "No cuBitCrack binary found in ${BUILDDIR}. Run without --launch-only first."
            exit 1
        fi
        USED_FORK="pre-built"
        log_ok "Using existing binary: ${BITCRACK_BIN}"
    fi

    if $BUILD_ONLY; then
        log_header "Build Complete (--build-only)"
        log_ok "Binary: ${BITCRACK_BIN}"
        log_info "Run with --launch-only to start searching"
        exit 0
    fi

    # Phase 4: Launch
    launch_bitcrack
}

main "$@"
