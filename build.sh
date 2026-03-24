#!/bin/bash
# =============================================================================
# Zigbee Matter Manager — Container Build & Deploy Script
# Supports: Podman (preferred) and Docker
# Internal port: 8000 (fixed). External port: auto-detected.
#
# Device access strategy:
#   Podman rootless + --device + --group-add <host dialout GID>
#   This gives the container process the host's dialout GID so it can
#   open /dev/ttyACM0 etc. without running as root.
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

# =============================================================================
# PRE-FLIGHT: dialout group membership
# =============================================================================
check_dialout_group() {
    # Determine the correct serial-device group for this distro
    local serial_group=""
    if getent group dialout &>/dev/null; then
        serial_group="dialout"
    elif getent group uucp &>/dev/null; then
        serial_group="uucp"
    else
        warn "No dialout or uucp group found on this system."
        warn "Device access may fail. Continuing anyway..."
        DIALOUT_GID=""
        return 0
    fi

    DIALOUT_GID=$(getent group "$serial_group" | cut -d: -f3)
    ok "Serial device group: ${BOLD}${serial_group}${NC} (GID ${DIALOUT_GID})"

    # Check if current user is a member
    if id -nG "$USER" 2>/dev/null | grep -qw "$serial_group"; then
        ok "User ${BOLD}${USER}${NC} is in the ${BOLD}${serial_group}${NC} group"
        return 0
    fi

    # User is NOT in the group — add them
    warn "User ${BOLD}${USER}${NC} is NOT in the ${BOLD}${serial_group}${NC} group."
    info "Adding ${USER} to ${serial_group}..."

    if [[ $EUID -eq 0 ]]; then
        usermod -aG "$serial_group" "$USER"
    else
        sudo usermod -aG "$serial_group" "$USER"
    fi

    echo
    echo -e "${RED}${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo -e "${RED}${BOLD}  ACTION REQUIRED: Log out and log back in, then      ${NC}"
    echo -e "${RED}${BOLD}  re-run this script.                                 ${NC}"
    echo -e "${RED}${BOLD}                                                      ${NC}"
    echo -e "${RED}${BOLD}  The group change only takes effect after a new       ${NC}"
    echo -e "${RED}${BOLD}  login session. A full logout/login is required —     ${NC}"
    echo -e "${RED}${BOLD}  opening a new terminal is NOT sufficient.            ${NC}"
    echo -e "${RED}${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo
    exit 1
}

# =============================================================================
# RUNTIME DETECTION
# =============================================================================
detect_runtime() {
    if [[ -n "${RUNTIME:-}" ]]; then
        command -v "$RUNTIME" &>/dev/null || die "$RUNTIME not found in PATH."
        ok "Container runtime (forced): ${BOLD}$RUNTIME${NC}"
        return
    fi
    if command -v podman &>/dev/null; then
        RUNTIME="podman"
    elif command -v docker &>/dev/null; then
        RUNTIME="docker"
    else
        die "Neither podman nor docker found. Please install one and re-run."
    fi
    ok "Container runtime: ${BOLD}$RUNTIME${NC} ($(${RUNTIME} --version 2>/dev/null | head -1))"
}

# =============================================================================
# PORT HANDLING
# =============================================================================
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
    local port=$1
    while port_in_use "$port"; do
        ((port++))
        if [[ $port -gt 65535 ]]; then
            die "No free ports found."
        fi
    done
    echo "$port"
}

pick_host_port() {
    local preferred=$1
    if port_in_use "$preferred"; then
        warn "Port ${preferred} is in use — scanning..."
        local found
        found=$(find_free_port "$((preferred + 1))")
        warn "Using port ${BOLD}${found}${NC} instead."
        echo "$found"
    else
        echo "$preferred"
    fi
}

# =============================================================================
# DEPENDENCY CHECKS
# =============================================================================
check_deps() {
    local missing=()
    for cmd in git curl; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing required tools: ${missing[*]}"
    fi
}

# =============================================================================
# CLONE / UPDATE REPO
# =============================================================================
fetch_repo() {
    if [[ -d "$APP_DIR/.git" ]]; then
        info "Repository exists — pulling latest ${REPO_BRANCH}..."
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

# =============================================================================
# USB COORDINATOR DETECTION
# =============================================================================
detect_usb_coordinator() {
    USB_DEVICE=""

    local -a found_devices=()
    local -a found_labels=()

    # Scan /dev/serial/by-id for known Zigbee coordinator patterns
    if [[ -d /dev/serial/by-id ]]; then
        for dev in /dev/serial/by-id/*; do
            [[ -e "$dev" ]] || continue
            local real_dev
            real_dev=$(readlink -f "$dev")
            local label
            label=$(basename "$dev")
            # Match common coordinator USB identifiers
            if echo "$label" | grep -qiE 'cp210|ezsp|zigbee|silabs|ember|ch340|ch341|cc253|cc265|conbee|raspbee|sonoff|tube|slzb|zzh'; then
                found_devices+=("$real_dev")
                found_labels+=("$label → $real_dev")
            fi
        done
    fi

    # Also check raw /dev/ttyACM* and /dev/ttyUSB* if nothing found by-id
    if [[ ${#found_devices[@]} -eq 0 ]]; then
        for dev in /dev/ttyACM0 /dev/ttyACM1 /dev/ttyUSB0 /dev/ttyUSB1; do
            if [[ -c "$dev" ]]; then
                found_devices+=("$dev")
                found_labels+=("$dev")
            fi
        done
    fi

    local count=${#found_devices[@]}

    if [[ $count -eq 0 ]]; then
        warn "No Zigbee USB coordinator detected."
        _prompt_manual_usb
        return
    fi

    if [[ $count -eq 1 ]]; then
        USB_DEVICE="${found_devices[0]}"
        ok "Zigbee coordinator detected: ${BOLD}${found_labels[0]}${NC}"
        return
    fi

    echo
    warn "Multiple potential Zigbee coordinators found:"
    echo
    for i in "${!found_devices[@]}"; do
        echo -e "  ${BOLD}$((i+1))${NC}) ${found_labels[$i]}"
    done
    echo -e "  ${BOLD}$((count+1))${NC}) Enter device path manually"
    echo -e "  ${BOLD}$((count+2))${NC}) Skip (no USB device)"
    echo

    local choice
    while true; do
        read -rp "  Select coordinator [1-$((count+2))]: " choice
        if [[ "$choice" =~ ^[0-9]+$ ]]; then
            if [[ $choice -ge 1 && $choice -le $count ]]; then
                USB_DEVICE="${found_devices[$((choice-1))]}"
                ok "Selected: ${found_labels[$((choice-1))]}"
                break
            elif [[ $choice -eq $((count+1)) ]]; then
                _prompt_manual_usb
                break
            elif [[ $choice -eq $((count+2)) ]]; then
                warn "No USB device selected."
                break
            fi
        fi
        warn "Invalid selection, try again."
    done
}

_prompt_manual_usb() {
    echo
    warn "Available serial devices:"
    local has_devs=false
    for dev in /dev/ttyUSB* /dev/ttyACM*; do
        if [[ -c "$dev" ]]; then
            echo "    $dev"
            has_devs=true
        fi
    done
    $has_devs || echo "    (none found)"
    echo
    read -rp "  Enter device path (blank to skip): " manual_dev
    if [[ -n "$manual_dev" ]]; then
        [[ -c "$manual_dev" ]] || die "Device ${manual_dev} does not exist."
        USB_DEVICE="$manual_dev"
        ok "Using: ${USB_DEVICE}"
    else
        warn "No USB device selected."
    fi
}

# =============================================================================
# CONTAINERFILE
# =============================================================================
write_containerfile() {
    # We receive DIALOUT_GID as a build-arg so the in-container user gets
    # the same GID as the host dialout group.
    cat > "$APP_DIR/Containerfile" << 'DOCKERFILE'
FROM python:3.11-slim

ARG HOST_DIALOUT_GID=20

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        libffi-dev \
        libssl-dev \
        logrotate \
        curl \
        libglib2.0-0 \
        libnl-3-200 \
        libnl-route-3-200 \
    && rm -rf /var/lib/apt/lists/*

# Create a group with the HOST's dialout GID inside the container.
# If the GID already exists (e.g. staff=20 on Debian), just reuse it.
RUN if getent group "$HOST_DIALOUT_GID" >/dev/null 2>&1; then \
        SERIAL_GROUP=$(getent group "$HOST_DIALOUT_GID" | cut -d: -f1); \
    else \
        groupadd -g "$HOST_DIALOUT_GID" hostdialout; \
        SERIAL_GROUP=hostdialout; \
    fi \
 && groupadd -f zigbee \
 && useradd -r -g zigbee -G "$SERIAL_GROUP" -d /app -s /bin/bash zigbee

WORKDIR /app

# Dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "python-matter-server[server]"

# Application source
COPY . .

# Required directories — writable by zigbee user
RUN mkdir -p /data /app/data/matter /app/data/backups /app/logs /app/config \
 && chown -R zigbee:zigbee /app /data /app/logs /app/config

ENV ZMM_BACKUP_DIR=/app/data/backups
ENV ZMM_APP_DIR=/app

USER zigbee

EXPOSE 8000 5580

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/status || exit 1

CMD ["python", "main.py"]
DOCKERFILE
    ok "Containerfile written."
}

# =============================================================================
# BUILD IMAGE
# =============================================================================
build_image() {
    info "Building image ${BOLD}${IMAGE_NAME}${NC} ..."
    local build_args=()

    if [[ -n "${DIALOUT_GID:-}" ]]; then
        build_args+=(--build-arg "HOST_DIALOUT_GID=${DIALOUT_GID}")
    fi

    "$RUNTIME" build \
        "${build_args[@]}" \
        --tag "${IMAGE_NAME}:latest" \
        --file "$APP_DIR/Containerfile" \
        "$APP_DIR"
    ok "Image built: ${IMAGE_NAME}:latest"
}

# =============================================================================
# PREPARE DATA DIRECTORIES
# =============================================================================
prepare_data_dirs() {
    local dirs=(
        "$DATA_DIR/config"
        "$DATA_DIR/data"
        "$DATA_DIR/data/matter"
        "$DATA_DIR/logs"
    )
    for d in "${dirs[@]}"; do
        mkdir -p "$d"
    done

    # Seed config.yaml
    if [[ ! -f "$DATA_DIR/config/config.yaml" ]] && [[ -f "$APP_DIR/config/config.yaml" ]]; then
        cp "$APP_DIR/config/config.yaml" "$DATA_DIR/config/config.yaml"
        ok "Default config.yaml seeded."
    fi

    # Patch USB device into config.yaml
    if [[ -n "${USB_DEVICE:-}" && -f "$DATA_DIR/config/config.yaml" ]]; then
        sed -i "s|port:.*\/dev\/tty[A-Za-z]*[0-9]*|port: ${USB_DEVICE}|g" \
            "$DATA_DIR/config/config.yaml"
        ok "config.yaml updated with device: ${USB_DEVICE}"
    fi

    ok "Data directories ready at ${DATA_DIR}"
}

# =============================================================================
# RUN CONTAINER
# =============================================================================
run_container() {
    local host_port=$1
    local host_matter_port=$2

    # Remove existing container
    if "$RUNTIME" inspect "$CONTAINER_NAME" &>/dev/null 2>&1; then
        warn "Removing existing '${CONTAINER_NAME}' container..."
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

    # ── USB device passthrough ──
    if [[ -n "${USB_DEVICE:-}" ]]; then
        # Always pass the real device node (resolve symlinks)
        local real_dev
        real_dev=$(readlink -f "$USB_DEVICE")
        run_args+=(--device "${real_dev}:${real_dev}")

        # If the original path was a symlink (e.g. /dev/serial/by-id/...),
        # also pass that so configs referencing it still work
        if [[ "$USB_DEVICE" != "$real_dev" ]]; then
            run_args+=(--device "${USB_DEVICE}:${USB_DEVICE}")
        fi
    fi

    # ── Podman-specific flags ──
    if [[ "$RUNTIME" == "podman" ]]; then
        # keep-id: map host UID/GID into container so volumes are writable
        run_args+=(--userns keep-id)

        # Pass the host dialout GID so the container process can open
        # the serial device. This is the KEY flag for device access.
        if [[ -n "${DIALOUT_GID:-}" ]]; then
            run_args+=(--group-add "${DIALOUT_GID}")
        fi
    fi

    # ── Docker-specific flags ──
    if [[ "$RUNTIME" == "docker" ]]; then
        # Docker: --group-add with the group name or GID
        if [[ -n "${DIALOUT_GID:-}" ]]; then
            run_args+=(--group-add "${DIALOUT_GID}")
        fi
    fi

    info "Starting container '${CONTAINER_NAME}' ..."
    "$RUNTIME" run "${run_args[@]}" "${IMAGE_NAME}:latest"
    ok "Container started."

    # Verify device access
    if [[ -n "${USB_DEVICE:-}" ]]; then
        sleep 2
        local real_dev
        real_dev=$(readlink -f "$USB_DEVICE")
        info "Verifying device access inside container..."
        if "$RUNTIME" exec "$CONTAINER_NAME" test -r "$real_dev" 2>/dev/null; then
            ok "Device ${real_dev} is readable inside container."
        else
            warn "Device ${real_dev} may not be accessible. Check logs:"
            warn "  ${RUNTIME} logs ${CONTAINER_NAME}"
        fi
    fi
}

# =============================================================================
# SYSTEMD AUTO-START
# =============================================================================
install_autostart() {
    if ! command -v systemctl &>/dev/null; then
        warn "systemd not found — skipping auto-start."
        return
    fi

    if [[ "$RUNTIME" == "podman" ]]; then
        # Podman: generate a user-level systemd unit (no sudo needed)
        local unit_dir="$HOME/.config/systemd/user"
        mkdir -p "$unit_dir"

        info "Generating podman systemd unit..."
        "$RUNTIME" generate systemd \
            --name "$CONTAINER_NAME" \
            --restart-policy=always \
            --new \
            > "$unit_dir/container-${CONTAINER_NAME}.service" 2>/dev/null || {
            # Older podman: write manually
            cat > "$unit_dir/container-${CONTAINER_NAME}.service" << UNIT
[Unit]
Description=Zigbee Matter Manager Container
After=network.target

[Service]
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/podman rm -f ${CONTAINER_NAME}
ExecStart=/usr/bin/podman start -a ${CONTAINER_NAME}
ExecStop=/usr/bin/podman stop -t 15 ${CONTAINER_NAME}

[Install]
WantedBy=default.target
UNIT
        }

        systemctl --user daemon-reload
        systemctl --user enable "container-${CONTAINER_NAME}.service"

        # Enable lingering so user units start at boot (not just at login)
        if command -v loginctl &>/dev/null; then
            loginctl enable-linger "$USER" 2>/dev/null || true
        fi

        ok "Podman user systemd unit enabled."
        info "The container will start automatically at boot."
    else
        # Docker: system-level unit
        local unit_file="/etc/systemd/system/${CONTAINER_NAME}.service"
        sudo tee "$unit_file" > /dev/null << UNIT
[Unit]
Description=Zigbee Matter Manager Container
After=network.target docker.service
Requires=docker.service

[Service]
Restart=always
RestartSec=10
ExecStart=/usr/bin/docker start -a ${CONTAINER_NAME}
ExecStop=/usr/bin/docker stop -t 15 ${CONTAINER_NAME}

[Install]
WantedBy=multi-user.target
UNIT
        sudo systemctl daemon-reload
        sudo systemctl enable "${CONTAINER_NAME}.service"
        ok "Docker systemd unit enabled."
    fi
}

# =============================================================================
# USAGE
# =============================================================================
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
  --rebuild          Force image rebuild
  --help             Show this message

${BOLD}Environment:${NC}
  ZMM_APP_DIR        Override app directory
  ZMM_DATA_DIR       Override data directory
EOF
    exit 0
}

# =============================================================================
# ARGUMENT PARSING
# =============================================================================
PREFERRED_PORT=$INTERNAL_PORT
INSTALL_AUTOSTART=true
FORCE_REBUILD=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)         PREFERRED_PORT="$2";    shift 2 ;;
        --usb)          USB_DEVICE="$2";        shift 2 ;;
        --dir)          APP_DIR="$2";           shift 2 ;;
        --data)         DATA_DIR="$2";          shift 2 ;;
        --branch)       REPO_BRANCH="$2";       shift 2 ;;
        --runtime)      RUNTIME="$2";           shift 2 ;;
        --no-autostart) INSTALL_AUTOSTART=false; shift ;;
        --rebuild)      FORCE_REBUILD=true;     shift ;;
        --help|-h)      usage ;;
        *) die "Unknown argument: $1  (use --help)" ;;
    esac
done

# =============================================================================
# MAIN
# =============================================================================
echo
echo -e "${BOLD}=====================================================${NC}"
echo -e "${BOLD}   Zigbee Matter Manager — Container Build & Deploy  ${NC}"
echo -e "${BOLD}=====================================================${NC}"
echo

# Step 1: Pre-flight checks
check_deps
check_dialout_group     # ← exits here if user needs to re-login
detect_runtime

# Step 2: Get the code
fetch_repo

# Step 3: USB coordinator
if [[ -z "${USB_DEVICE:-}" ]]; then
    detect_usb_coordinator
fi

# Step 4: Ports
HOST_PORT=$(pick_host_port "$PREFERRED_PORT")
HOST_MATTER_PORT=$(pick_host_port "$MATTER_INTERNAL_PORT")

# Step 5: Build
write_containerfile

if "$FORCE_REBUILD" || ! "$RUNTIME" image inspect "${IMAGE_NAME}:latest" &>/dev/null 2>&1; then
    build_image
else
    info "Image exists — skipping build (use --rebuild to force)."
fi

# Step 6: Data + config
prepare_data_dirs

# Step 7: Run
run_container "$HOST_PORT" "$HOST_MATTER_PORT"

# Step 8: Auto-start
if [[ "$INSTALL_AUTOSTART" == true ]]; then
    install_autostart
fi

# =============================================================================
# SUMMARY
# =============================================================================
echo
echo -e "${BOLD}=====================================================${NC}"
echo -e "${GREEN}${BOLD}   Deployment Complete!${NC}"
echo -e "${BOLD}=====================================================${NC}"
echo
echo -e "  ${BOLD}Web Interface:${NC}  https://$(hostname -I 2>/dev/null | awk '{print $1}'):${HOST_PORT}"
echo -e "  ${BOLD}Matter Port:${NC}    ${HOST_MATTER_PORT}"
if [[ -n "${USB_DEVICE:-}" ]]; then
    echo -e "  ${BOLD}Zigbee USB:${NC}     ${USB_DEVICE}"
fi
echo -e "  ${BOLD}Config:${NC}         ${DATA_DIR}/config/config.yaml"
echo -e "  ${BOLD}Logs:${NC}           ${RUNTIME} logs -f ${CONTAINER_NAME}"
echo -e "  ${BOLD}Data:${NC}           ${DATA_DIR}/"
echo -e "  ${BOLD}Runtime:${NC}        ${RUNTIME}"
echo
echo -e "  ${BOLD}Commands:${NC}"
echo -e "    ${RUNTIME} logs -f ${CONTAINER_NAME}        # Follow logs"
echo -e "    ${RUNTIME} exec -it ${CONTAINER_NAME} bash  # Shell"
echo -e "    ${RUNTIME} stop ${CONTAINER_NAME}           # Stop"
echo -e "    ${RUNTIME} start ${CONTAINER_NAME}          # Start"
echo -e "    ${RUNTIME} rm -f ${CONTAINER_NAME}          # Remove"
echo