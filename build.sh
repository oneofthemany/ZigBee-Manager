#!/bin/bash
# =============================================================================
# Zigbee Matter Manager — Container Build & Deploy Script
# Supports: Podman (preferred) and Docker
# Internal port: 8000 (fixed). External port: auto-detected.
# =============================================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}${BOLD}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}${BOLD}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}${BOLD}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}${BOLD}[ERR ]${NC}  $*" >&2; }
die()     { error "$*"; exit 1; }

# ── Defaults ─────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/oneofthemany/ZigBee-Matter-Manager.git"
REPO_BRANCH="main"
APP_DIR="${ZMM_APP_DIR:-$HOME/zigbee-matter-manager}"
IMAGE_NAME="zigbee-matter-manager"
CONTAINER_NAME="zigbee-matter-manager"
INTERNAL_PORT=8000
MATTER_INTERNAL_PORT=5580
DATA_DIR="${ZMM_DATA_DIR:-$HOME/.zigbee-matter-manager}"

# ── Runtime detection ─────────────────────────────────────────────────────────
detect_runtime() {
    if command -v podman &>/dev/null; then
        RUNTIME="podman"
    elif command -v docker &>/dev/null; then
        RUNTIME="docker"
    else
        die "Neither podman nor docker found. Please install one and re-run."
    fi
    local ver
    ver=$("$RUNTIME" --version 2>&1 || true)
    ver="${ver%%$'\n'*}"
    ok "Container runtime: ${BOLD}$RUNTIME${NC} ($ver)"
}

# ── Port availability ─────────────────────────────────────────────────────────
port_in_use() {
    local port=$1
    if command -v ss &>/dev/null; then
        ss -tlnH "sport = :${port}" 2>/dev/null | grep -q .
    elif command -v netstat &>/dev/null; then
        netstat -tln 2>/dev/null | grep -qE ":${port}\s"
    else
        grep -qE "^\s*[0-9A-Fa-f]+:$(printf '%04X' "${port}")\s" \
            /proc/net/tcp /proc/net/tcp6 2>/dev/null
    fi
}

find_free_port() {
    local start=$1
    local port=$start
    while port_in_use "$port"; do
        ((port++))
        [[ $port -gt 65535 ]] && die "No free ports found starting from $start"
    done
    echo "$port"
}

pick_host_port() {
    local preferred=$1
    if port_in_use "$preferred"; then
        warn "Port ${preferred} is in use — scanning for next available port..."
        local found
        found=$(find_free_port "$((preferred + 1))")
        warn "Using port ${BOLD}${found}${NC} instead."
        echo "$found"
    else
        echo "$preferred"
    fi
}

# ── Dependency checks ─────────────────────────────────────────────────────────
check_deps() {
    local missing=()
    for cmd in git curl; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing required tools: ${missing[*]}"
    fi
}

# ── Clone / update repo ───────────────────────────────────────────────────────
fetch_repo() {
    if [[ -d "$APP_DIR/.git" ]]; then
        info "Repository already exists — pulling latest ${REPO_BRANCH}..."
        git -C "$APP_DIR" fetch origin
        git -C "$APP_DIR" checkout "$REPO_BRANCH"
        git -C "$APP_DIR" pull --ff-only origin "$REPO_BRANCH"
        ok "Repository updated."
    else
        info "Cloning ${REPO_URL} → ${APP_DIR} ..."
        git clone --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
        ok "Repository cloned."
    fi
}

# ── USB / Zigbee coordinator detection ───────────────────────────────────────
detect_usb_coordinator() {
    USB_DEVICE=""
    local candidates=(
        /dev/serial/by-id/*CP2102*
        /dev/serial/by-id/*EZSP*
        /dev/serial/by-id/*zigbee*
        /dev/serial/by-id/*Zigbee*
        /dev/serial/by-id/*CH340*
        /dev/serial/by-id/*CH341*
        /dev/ttyUSB0
        /dev/ttyACM0
    )
    for candidate in "${candidates[@]}"; do
        for dev in $candidate; do
            [[ -c "$dev" ]] && { USB_DEVICE="$dev"; break 2; }
        done
    done
    if [[ -n "$USB_DEVICE" ]]; then
        ok "Zigbee coordinator detected: ${BOLD}${USB_DEVICE}${NC}"
    else
        warn "No Zigbee USB coordinator detected automatically."
        warn "You can specify one with --usb /dev/ttyUSB0"
    fi
}

# ── Resolve host dialout GID ──────────────────────────────────────────────────
resolve_dialout_gid() {
    DIALOUT_GID=""
    if getent group dialout &>/dev/null; then
        DIALOUT_GID=$(getent group dialout | cut -d: -f3)
        ok "Host dialout GID: ${BOLD}${DIALOUT_GID}${NC}"
    elif getent group uucp &>/dev/null; then
        # Some distros (Arch, Alpine) use uucp instead of dialout
        DIALOUT_GID=$(getent group uucp | cut -d: -f3)
        ok "Host uucp GID (dialout equivalent): ${BOLD}${DIALOUT_GID}${NC}"
    else
        warn "Could not resolve dialout/uucp group — USB device access may fail inside container."
    fi
}

# ── Write Containerfile ───────────────────────────────────────────────────────
write_containerfile() {
    cat > "$APP_DIR/Containerfile" << 'DOCKERFILE'
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        libffi-dev \
        libssl-dev \
        logrotate \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

# /data required by CHIP SDK (Matter)
RUN mkdir -p /data /app/data/matter /app/logs /app/config

# Create zigbee user (UID 1000) and add to dialout (GID 20) for tty access.
# GID 20 is the Debian/Ubuntu dialout GID — matches most host systems.
# At runtime --group-add passes the actual host dialout GID as a supplemental group.
RUN groupadd -r -g 20 dialout 2>/dev/null || true \
 && groupadd -r zigbee \
 && useradd -r -u 1000 -g zigbee -G dialout -d /app zigbee \
 && chown -R zigbee:zigbee /app /data

USER zigbee

EXPOSE 8000 5580

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/status || exit 1

CMD ["python", "main.py"]
DOCKERFILE
    ok "Containerfile written."
}

# ── Build image ───────────────────────────────────────────────────────────────
build_image() {
    info "Building image ${BOLD}${IMAGE_NAME}${NC} ..."
    "$RUNTIME" build \
        --tag "${IMAGE_NAME}:latest" \
        --file "$APP_DIR/Containerfile" \
        "$APP_DIR"
    ok "Image built: ${IMAGE_NAME}:latest"
}

# ── Prepare host data directories ─────────────────────────────────────────────
prepare_data_dirs() {
    local dirs=("$DATA_DIR/config" "$DATA_DIR/data" "$DATA_DIR/logs")
    for d in "${dirs[@]}"; do
        mkdir -p "$d"
    done

    # Ensure the zigbee container user (UID 1000) can write to mounted volumes.
    # This is necessary when the script is run as root on the host.
    chown -R 1000:1000 "$DATA_DIR"
    chmod -R u+rwX "$DATA_DIR"
    ok "Volume permissions set (owner: 1000:1000) at ${DATA_DIR}"

    if [[ ! -f "$DATA_DIR/config/config.yaml" ]] && [[ -f "$APP_DIR/config/config.yaml" ]]; then
        cp "$APP_DIR/config/config.yaml" "$DATA_DIR/config/config.yaml"
        chown 1000:1000 "$DATA_DIR/config/config.yaml"
        ok "Default config.yaml copied to ${DATA_DIR}/config/"
    fi

    ok "Data directories ready at ${DATA_DIR}"
}

# ── Run container ─────────────────────────────────────────────────────────────
run_container() {
    local host_port=$1
    local host_matter_port=$2

    if "$RUNTIME" inspect "$CONTAINER_NAME" &>/dev/null 2>&1; then
        warn "Existing container '${CONTAINER_NAME}' found — removing..."
        "$RUNTIME" rm -f "$CONTAINER_NAME"
    fi

    local run_args=(
        --detach
        --name "$CONTAINER_NAME"
        --restart unless-stopped
        --publish "${host_port}:${INTERNAL_PORT}"
        --publish "${host_matter_port}:${MATTER_INTERNAL_PORT}"
        --volume "${DATA_DIR}/config:/app/config:Z"
        --volume "${DATA_DIR}/data:/app/data:Z"
        --volume "${DATA_DIR}/logs:/app/logs:Z"
    )

    # Pass host dialout GID as supplemental group so tty devices are accessible
    if [[ -n "${DIALOUT_GID:-}" ]]; then
        run_args+=(--group-add "$DIALOUT_GID")
    fi

    if [[ -n "${USB_DEVICE:-}" ]]; then
        run_args+=(--device "${USB_DEVICE}:${USB_DEVICE}")
        if [[ "$RUNTIME" == "podman" && -L "$USB_DEVICE" ]]; then
            local real_dev
            real_dev=$(readlink -f "$USB_DEVICE")
            run_args+=(--device "${real_dev}:${real_dev}")
        fi
    fi

    if [[ "$RUNTIME" == "podman" ]]; then
        run_args+=(--userns keep-id)
    fi

    info "Starting container '${CONTAINER_NAME}' ..."
    "$RUNTIME" run "${run_args[@]}" "${IMAGE_NAME}:latest"
    ok "Container started."
}

# ── Systemd auto-start ────────────────────────────────────────────────────────
install_autostart() {
    if ! command -v systemctl &>/dev/null; then
        warn "systemd not found — skipping auto-start setup."
        return
    fi

    local unit_name="zigbee-matter-manager-container"
    local unit_file="/etc/systemd/system/${unit_name}.service"

    if [[ "$RUNTIME" == "podman" ]]; then
        info "Generating systemd unit via podman generate systemd..."
        local unit_content
        unit_content=$("$RUNTIME" generate systemd --name "$CONTAINER_NAME" --restart-policy=always --new 2>/dev/null || true)
        if [[ -n "$unit_content" ]]; then
            echo "$unit_content" | sudo tee "$unit_file" > /dev/null
            sudo systemctl daemon-reload
            sudo systemctl enable "$unit_name"
            ok "systemd unit installed and enabled: ${unit_name}"
            return
        fi
    fi

    sudo tee "$unit_file" > /dev/null << UNIT
[Unit]
Description=Zigbee Matter Manager Container
After=network.target

[Service]
Restart=always
ExecStart=${RUNTIME} start -a ${CONTAINER_NAME}
ExecStop=${RUNTIME} stop ${CONTAINER_NAME}

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable "$unit_name"
    ok "systemd unit installed: ${unit_name}"
}

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
    cat << EOF
${BOLD}Usage:${NC} $0 [OPTIONS]

${BOLD}Options:${NC}
  --port   PORT      Preferred host port  (default: ${INTERNAL_PORT})
  --usb    DEVICE    Zigbee USB device    (default: auto-detect)
  --dir    PATH      App clone directory  (default: ${APP_DIR})
  --data   PATH      Persistent data dir  (default: ${DATA_DIR})
  --branch NAME      Git branch           (default: ${REPO_BRANCH})
  --runtime NAME     docker or podman     (default: auto-detect)
  --no-autostart     Skip systemd unit installation
  --rebuild          Force image rebuild even if up-to-date
  --help             Show this message
EOF
    exit 0
}

# ── Argument parsing ──────────────────────────────────────────────────────────
PREFERRED_PORT=$INTERNAL_PORT
INSTALL_AUTOSTART=true
FORCE_REBUILD=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)         PREFERRED_PORT="$2";     shift 2 ;;
        --usb)          USB_DEVICE="$2";         shift 2 ;;
        --dir)          APP_DIR="$2";            shift 2 ;;
        --data)         DATA_DIR="$2";           shift 2 ;;
        --branch)       REPO_BRANCH="$2";        shift 2 ;;
        --runtime)      RUNTIME="$2";            shift 2 ;;
        --no-autostart) INSTALL_AUTOSTART=false; shift ;;
        --rebuild)      FORCE_REBUILD=true;      shift ;;
        --help|-h)      usage ;;
        *) die "Unknown argument: $1. Use --help for usage." ;;
    esac
done

# ── Main ──────────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}=====================================================${NC}"
echo -e "${BOLD}   Zigbee Matter Manager — Container Build & Deploy  ${NC}"
echo -e "${BOLD}=====================================================${NC}"
echo

check_deps
detect_runtime
fetch_repo

[[ -z "${USB_DEVICE:-}" ]] && detect_usb_coordinator
resolve_dialout_gid

HOST_PORT=$(pick_host_port "$PREFERRED_PORT")
HOST_MATTER_PORT=$(pick_host_port "$MATTER_INTERNAL_PORT")

write_containerfile

if [[ "$FORCE_REBUILD" == true ]] || ! "$RUNTIME" image inspect "${IMAGE_NAME}:latest" &>/dev/null 2>&1; then
    build_image
else
    info "Image ${IMAGE_NAME}:latest already exists — skipping build (use --rebuild to force)."
fi

prepare_data_dirs
run_container "$HOST_PORT" "$HOST_MATTER_PORT"

if [[ "$INSTALL_AUTOSTART" == true ]]; then
    install_autostart
fi

# ── Summary ───────────────────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null || hostname)
HOST_IP="${HOST_IP%% *}"

echo
echo -e "${BOLD}=====================================================${NC}"
echo -e "${GREEN}${BOLD}   Deployment Complete!${NC}"
echo -e "${BOLD}=====================================================${NC}"
echo
echo -e "  ${BOLD}Web Interface:${NC}  http://${HOST_IP}:${HOST_PORT}"
echo -e "  ${BOLD}Matter Port:${NC}    ${HOST_MATTER_PORT} (internal ${MATTER_INTERNAL_PORT})"
if [[ -n "${USB_DEVICE:-}" ]]; then
    echo -e "  ${BOLD}Zigbee USB:${NC}     ${USB_DEVICE}"
fi
echo -e "  ${BOLD}Config:${NC}         ${DATA_DIR}/config/config.yaml"
echo -e "  ${BOLD}Logs:${NC}           ${DATA_DIR}/logs/"
echo -e "  ${BOLD}Runtime:${NC}        ${RUNTIME}"
echo
echo -e "  ${BOLD}Useful commands:${NC}"
echo -e "    ${RUNTIME} logs -f ${CONTAINER_NAME}       # Follow logs"
echo -e "    ${RUNTIME} exec -it ${CONTAINER_NAME} bash # Shell access"
echo -e "    ${RUNTIME} stop ${CONTAINER_NAME}          # Stop"
echo -e "    ${RUNTIME} start ${CONTAINER_NAME}         # Start"
echo -e "    ${RUNTIME} rm -f ${CONTAINER_NAME}         # Remove"
echo