#!/usr/bin/env bash
###############################################################################
# gpu_vast_ai.sh — Vast.ai GPU Instance Launcher for Bitcoin Puzzle #71
###############################################################################
#
# Searches for cheap GPU instances on vast.ai, spins one up, uploads and runs
# the gpu_deploy.sh BitCrack deployment script.
#
# Prerequisites:
#   pip install vastai
#   vastai set api-key YOUR_API_KEY
#
# Usage:
#   ./gpu_vast_ai.sh                     # Search, create, deploy (interactive)
#   ./gpu_vast_ai.sh --search            # Just search for instances, don't create
#   ./gpu_vast_ai.sh --gpu 4090          # Prefer specific GPU model
#   ./gpu_vast_ai.sh --gpu 3090          # (default: search 3090 and 4090)
#   ./gpu_vast_ai.sh --max-cost 0.50     # Max $/hr (default: 0.60)
#   ./gpu_vast_ai.sh --deploy ID         # Deploy to an existing instance by ID
#   ./gpu_vast_ai.sh --destroy ID        # Destroy a running instance
#   ./gpu_vast_ai.sh --list              # List your running instances
#   ./gpu_vast_ai.sh --estimate          # Cost estimate for running 24h/7d/30d
#   ./gpu_vast_ai.sh --dry-run           # Show what would happen
#
# Instance requirements:
#   - NVIDIA GPU with >= 8GB VRAM (RTX 3090/4090 preferred)
#   - Docker image: nvidia/cuda:12.2.0-devel-ubuntu22.04
#   - Disk: 20 GB
#   - Internet access for cloning BitCrack repo
#
###############################################################################
set -euo pipefail

# ─── Constants ───────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="${SCRIPT_DIR}/gpu_deploy.sh"
MULTI_GPU_SCRIPT="${SCRIPT_DIR}/multi_gpu_launch.sh"

# Docker image with CUDA development tools pre-installed
DOCKER_IMAGE="nvidia/cuda:12.2.0-devel-ubuntu22.04"

# Default search parameters
DEFAULT_GPU="3090 4090"
DEFAULT_MAX_COST="0.60"       # $/hr
DEFAULT_MIN_VRAM=10           # GB
DEFAULT_DISK_GB=20
DEFAULT_MIN_UPLOAD=100        # Mbps
DEFAULT_MIN_DOWNLOAD=200      # Mbps

# ─── State ───────────────────────────────────────────────────────────────────
GPU_FILTER=""
MAX_COST="$DEFAULT_MAX_COST"
MODE="full"      # full, search, deploy, destroy, list, estimate
INSTANCE_ID=""
DRY_RUN=false

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
        --search)
            MODE="search"; shift ;;
        --deploy)
            MODE="deploy"; INSTANCE_ID="$2"; shift 2 ;;
        --destroy)
            MODE="destroy"; INSTANCE_ID="$2"; shift 2 ;;
        --list)
            MODE="list"; shift ;;
        --estimate)
            MODE="estimate"; shift ;;
        --gpu)
            GPU_FILTER="$2"; shift 2 ;;
        --max-cost)
            MAX_COST="$2"; shift 2 ;;
        --dry-run)
            DRY_RUN=true; shift ;;
        -h|--help)
            head -35 "$0" | tail -30; exit 0 ;;
        *)
            log_error "Unknown argument: $1"; exit 1 ;;
    esac
done

###############################################################################
# Preflight Checks
###############################################################################
preflight() {
    log_header "Preflight Checks"

    # Check vastai CLI
    if ! command -v vastai &>/dev/null; then
        log_error "vastai CLI not found."
        echo ""
        echo "  Install:"
        echo "    pip install vastai"
        echo ""
        echo "  Configure:"
        echo "    vastai set api-key YOUR_API_KEY"
        echo ""
        echo "  Get API key from: https://cloud.vast.ai/account/"
        echo ""
        exit 1
    fi
    log_ok "vastai CLI found"

    # Check API key is configured
    if ! vastai show instances &>/dev/null 2>&1; then
        log_error "vastai API key not configured or invalid."
        echo ""
        echo "  Set your API key:"
        echo "    vastai set api-key YOUR_API_KEY"
        echo ""
        echo "  Get key from: https://cloud.vast.ai/account/"
        echo ""
        exit 1
    fi
    log_ok "vastai API key configured"

    # Check deploy script exists
    if [[ ! -f "$DEPLOY_SCRIPT" ]]; then
        log_error "Deploy script not found: ${DEPLOY_SCRIPT}"
        log_info "Run this from the puzzle71 directory, or ensure gpu_deploy.sh exists."
        exit 1
    fi
    log_ok "Deploy script found: ${DEPLOY_SCRIPT}"
}

###############################################################################
# Search for GPU Instances
###############################################################################
search_instances() {
    log_header "Searching for GPU Instances"

    local gpu_models="${GPU_FILTER:-$DEFAULT_GPU}"
    log_info "Looking for: ${gpu_models}"
    log_info "Max cost:    \$${MAX_COST}/hr"
    log_info "Min VRAM:    ${DEFAULT_MIN_VRAM} GB"
    log_info "Docker:      ${DOCKER_IMAGE}"
    echo ""

    # Build the search query
    # vast.ai search syntax: field operator value
    local search_query="reliability>0.95 num_gpus>=1 gpu_ram>=${DEFAULT_MIN_VRAM} dph<=${MAX_COST} inet_down>=${DEFAULT_MIN_DOWNLOAD} inet_up>=${DEFAULT_MIN_UPLOAD} disk_space>=${DEFAULT_DISK_GB} cuda_vers>=12.0"

    # GPU name filter
    local gpu_name_filter=""
    for gpu in $gpu_models; do
        if [[ -z "$gpu_name_filter" ]]; then
            gpu_name_filter="gpu_name=~${gpu}"
        fi
    done

    if [[ -n "$gpu_name_filter" ]]; then
        search_query="${search_query} ${gpu_name_filter}"
    fi

    log_info "Search query: vastai search offers '${search_query}'"
    echo ""

    if $DRY_RUN; then
        log_warn "[DRY-RUN] Would search vast.ai with above query"
        return 0
    fi

    # Run search, sort by price
    local results
    results="$(vastai search offers "${search_query}" --order 'dph' --limit 20 2>&1)" || {
        log_error "Search failed. Output: ${results}"
        return 1
    }

    if [[ -z "$results" ]] || echo "$results" | grep -qi "no results\|error"; then
        log_warn "No instances found matching criteria."
        log_info "Try relaxing constraints:"
        log_info "  --max-cost 1.00    (increase budget)"
        log_info "  --gpu 3060         (cheaper GPU)"
        echo "$results"
        return 1
    fi

    echo "$results"
    echo ""

    # Parse the cheapest option
    local cheapest_id cheapest_cost cheapest_gpu
    cheapest_id="$(echo "$results" | awk 'NR==2 {print $1}')"
    cheapest_cost="$(echo "$results" | awk 'NR==2 {print $3}')"
    cheapest_gpu="$(echo "$results" | awk 'NR==2 {for(i=1;i<=NF;i++) if($i ~ /RTX|GTX|A100|H100|V100|T4/) print $i}')"

    if [[ -n "$cheapest_id" ]]; then
        log_ok "Cheapest instance: ID=${cheapest_id} GPU=${cheapest_gpu} Cost=\$${cheapest_cost}/hr"
        echo "$cheapest_id"  # Return the ID for use by create_instance
    fi
}

###############################################################################
# Cost Estimation
###############################################################################
estimate_cost() {
    log_header "Cost Estimation"

    echo ""
    echo "  Typical vast.ai GPU pricing (as of late 2025):"
    echo "  ─────────────────────────────────────────────"
    echo "  RTX 3090 (24GB):   \$0.20 - \$0.40/hr"
    echo "  RTX 4090 (24GB):   \$0.35 - \$0.60/hr"
    echo "  A100 80GB:         \$0.80 - \$1.50/hr"
    echo "  H100 80GB:         \$2.00 - \$3.50/hr"
    echo ""
    echo "  BitCrack speed estimates:"
    echo "  ─────────────────────────────────────────────"
    echo "  RTX 3090:  ~600-800 MKey/s"
    echo "  RTX 4090:  ~1,200-1,500 MKey/s"
    echo "  A100:      ~800-1,000 MKey/s"
    echo "  H100:      ~1,500-2,000 MKey/s"
    echo ""
    echo "  Search space: 2^70 = 1,180,591,620,717,411,303,424 keys"
    echo ""
    echo "  Time to search ENTIRE range (theoretical):"
    echo "  ─────────────────────────────────────────────"

    # Use Python for big-number math across all GPUs at once
    python3 << 'PYEOF'
total = 1180591620717411303424  # 2^70

gpus = [
    ("RTX 3090",  700, 0.30),
    ("RTX 4090", 1300, 0.45),
    ("A100",      900, 1.00),
    ("H100",     1700, 2.50),
]

for name, speed_mkeys, cost_hr in gpus:
    rate = speed_mkeys * 1_000_000
    seconds = total / rate
    hours = seconds / 3600
    days = hours / 24
    years = days / 365.25
    print(f"  {name:12s}: {years:,.0f} years | ${cost_hr}/hr")
    print(f"  {'':12s}  Speed: {speed_mkeys} MKey/s")
    print(f"  {'':12s}  24hr cost: ${cost_hr * 24:.2f} | 7d: ${cost_hr * 24 * 7:.2f} | 30d: ${cost_hr * 24 * 30:.2f}")
    print()
PYEOF

    echo "  NOTE: You don't search the entire range. You search randomly or"
    echo "  from a starting point and hope to get lucky. The puzzle is designed"
    echo "  so that the key exists somewhere in the 2^70 - 2^71 range."
    echo ""
    echo "  Cost for running 1x RTX 4090 at \$0.45/hr:"
    echo "    1 hour:   \$0.45"
    echo "    24 hours: \$10.80"
    echo "    7 days:   \$75.60"
    echo "    30 days:  \$324.00"
    echo ""
}

###############################################################################
# Create Instance
###############################################################################
create_instance() {
    log_header "Creating Vast.ai Instance"

    local instance_id="$1"

    if $DRY_RUN; then
        log_warn "[DRY-RUN] Would create instance from offer ID: ${instance_id}"
        log_warn "[DRY-RUN] Docker image: ${DOCKER_IMAGE}"
        log_warn "[DRY-RUN] Disk: ${DEFAULT_DISK_GB} GB"
        return 0
    fi

    log_info "Creating instance from offer: ${instance_id}"
    log_info "Docker image: ${DOCKER_IMAGE}"
    log_info "Disk: ${DEFAULT_DISK_GB} GB"

    local result
    result="$(vastai create instance "$instance_id" \
        --image "$DOCKER_IMAGE" \
        --disk "$DEFAULT_DISK_GB" \
        --onstart-cmd "apt-get update && apt-get install -y openssh-server && service ssh start" \
        2>&1)" || {
        log_error "Failed to create instance: ${result}"
        return 1
    }

    echo "$result"

    # Extract instance ID from creation output
    local new_id
    new_id="$(echo "$result" | grep -oP "new contract id: \K\d+" || echo "$result" | grep -oP '\d{5,}'  | head -1)"

    if [[ -n "$new_id" ]]; then
        log_ok "Instance created: ID=${new_id}"
        log_info "Waiting for instance to start..."

        # Poll until running
        local max_wait=300  # 5 minutes
        local waited=0
        while [[ $waited -lt $max_wait ]]; do
            local status
            status="$(vastai show instance "$new_id" 2>/dev/null | grep -i "status" | head -1 || true)"
            if echo "$status" | grep -qi "running"; then
                log_ok "Instance ${new_id} is running!"
                echo "$new_id"
                return 0
            fi
            sleep 10
            waited=$((waited + 10))
            log_info "Waiting... (${waited}s / ${max_wait}s)"
        done

        log_warn "Instance may still be starting. Check with: vastai show instance ${new_id}"
        echo "$new_id"
    else
        log_error "Could not parse instance ID from output"
        return 1
    fi
}

###############################################################################
# Deploy to Instance
###############################################################################
deploy_to_instance() {
    local inst_id="$1"

    log_header "Deploying to Instance ${inst_id}"

    if $DRY_RUN; then
        log_warn "[DRY-RUN] Would copy gpu_deploy.sh to instance"
        log_warn "[DRY-RUN] Would execute gpu_deploy.sh remotely"
        return 0
    fi

    # Get instance SSH info
    log_info "Getting instance connection info..."
    local ssh_info
    ssh_info="$(vastai ssh-url "$inst_id" 2>&1)" || {
        log_error "Could not get SSH URL: ${ssh_info}"
        log_info "Check instance status: vastai show instance ${inst_id}"
        return 1
    }

    log_ok "SSH URL: ${ssh_info}"

    # Parse SSH host and port
    local ssh_host ssh_port
    ssh_host="$(echo "$ssh_info" | grep -oP '[\w.-]+@[\w.-]+' | head -1)"
    ssh_port="$(echo "$ssh_info" | grep -oP ':\K\d+' | head -1 || echo "22")"

    if [[ -z "$ssh_host" ]]; then
        log_error "Could not parse SSH connection info from: ${ssh_info}"
        log_info "You may need to manually connect and run the deploy script."
        echo ""
        echo "  Manual deployment steps:"
        echo "  1. Connect: ssh ${ssh_info}"
        echo "  2. Copy script: scp -P ${ssh_port:-22} ${DEPLOY_SCRIPT} ${ssh_host}:/root/"
        echo "  3. Run: bash /root/gpu_deploy.sh"
        return 1
    fi

    local ssh_opts="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=30"

    # Upload deploy script and multi-gpu script
    log_info "Uploading deploy scripts..."
    scp ${ssh_opts} -P "${ssh_port}" \
        "${DEPLOY_SCRIPT}" \
        "${MULTI_GPU_SCRIPT}" \
        "${ssh_host}:/root/" 2>&1 || {
        log_error "Failed to upload scripts via SCP"
        log_info "Try manually: scp -P ${ssh_port} ${DEPLOY_SCRIPT} ${ssh_host}:/root/"
        return 1
    }
    log_ok "Scripts uploaded"

    # Execute deploy script remotely
    log_info "Executing gpu_deploy.sh on remote instance..."
    ssh ${ssh_opts} -p "${ssh_port}" "${ssh_host}" \
        "chmod +x /root/gpu_deploy.sh /root/multi_gpu_launch.sh && bash /root/gpu_deploy.sh" 2>&1 | \
        tee "${SCRIPT_DIR}/logs/vastai_deploy_${inst_id}.log" || {
        log_warn "Remote execution may have been interrupted. Check instance directly."
    }

    log_header "Deployment Complete"
    echo ""
    echo "  Instance ID: ${inst_id}"
    echo "  Connect:     ssh ${ssh_opts} -p ${ssh_port} ${ssh_host}"
    echo "  Monitor:     ssh ${ssh_opts} -p ${ssh_port} ${ssh_host} 'screen -r bitcrack_puzzle71'"
    echo "  Check key:   ssh ${ssh_opts} -p ${ssh_port} ${ssh_host} 'cat /root/puzzle71/FOUND_KEY.txt'"
    echo "  Destroy:     vastai destroy instance ${inst_id}"
    echo ""
}

###############################################################################
# List Running Instances
###############################################################################
list_instances() {
    log_header "Your Vast.ai Instances"

    if $DRY_RUN; then
        log_warn "[DRY-RUN] Would run: vastai show instances"
        return 0
    fi

    vastai show instances 2>&1 || {
        log_error "Failed to list instances"
        return 1
    }
}

###############################################################################
# Destroy Instance
###############################################################################
destroy_instance() {
    local inst_id="$1"

    log_header "Destroying Instance ${inst_id}"

    if $DRY_RUN; then
        log_warn "[DRY-RUN] Would destroy instance: ${inst_id}"
        return 0
    fi

    log_warn "Destroying instance ${inst_id}..."
    read -rp "Are you sure? (y/N): " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        log_info "Cancelled."
        return 0
    fi

    vastai destroy instance "$inst_id" 2>&1 || {
        log_error "Failed to destroy instance"
        return 1
    }

    log_ok "Instance ${inst_id} destroyed"
}

###############################################################################
# Main
###############################################################################
main() {
    log_header "Bitcoin Puzzle #71 — Vast.ai GPU Launcher"
    echo ""
    echo "  Target: 1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU"
    echo "  Range:  2^70 to 2^71-1"
    echo ""

    mkdir -p "${SCRIPT_DIR}/logs"

    case "$MODE" in
        estimate)
            estimate_cost
            ;;

        search)
            preflight
            search_instances
            ;;

        list)
            preflight
            list_instances
            ;;

        destroy)
            preflight
            destroy_instance "$INSTANCE_ID"
            ;;

        deploy)
            preflight
            deploy_to_instance "$INSTANCE_ID"
            ;;

        full)
            preflight

            # Step 1: Show cost estimate
            estimate_cost

            # Step 2: Search for instances
            local offer_id
            offer_id="$(search_instances | tail -1)"

            if [[ -z "$offer_id" || ! "$offer_id" =~ ^[0-9]+$ ]]; then
                log_error "No suitable instance found."
                log_info "Try: --gpu 3060 or --max-cost 1.00"
                exit 1
            fi

            # Step 3: Confirm
            echo ""
            log_info "Ready to create instance from offer: ${offer_id}"
            read -rp "Proceed? (y/N): " confirm
            if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
                log_info "Cancelled. To create later: ./gpu_vast_ai.sh --deploy ${offer_id}"
                exit 0
            fi

            # Step 4: Create
            local inst_id
            inst_id="$(create_instance "$offer_id" | tail -1)"

            if [[ -z "$inst_id" || ! "$inst_id" =~ ^[0-9]+$ ]]; then
                log_error "Failed to create instance."
                exit 1
            fi

            # Step 5: Deploy
            sleep 5  # Give instance a moment to initialize SSH
            deploy_to_instance "$inst_id"
            ;;
    esac
}

main "$@"
