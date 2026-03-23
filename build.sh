#!/bin/bash
# =============================================================================
# Zigbee Matter Manager - Container Build & Deploy Script
# Supports: Podman (preferred) and Docker
# Internal port: 8000 (fixed). External port: auto-detected.
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}${BOLD}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}${BOLD}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}${BOLD}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}${BOLD}[ERR ]${NC}  $*" >&2; }
die()     { error "$*"; exit 1; }

REPO_URL="https://github.com/oneofthemany/ZigBee-Matter-Manager.git"
REPO_BRANCH="main"
APP_DIR="${ZMM_APP_DIR:-$HOME/zigbee-matter-manager}"
IMAGE_NAME="zigbee-matter-manager"
CONTAINER_NAME="zigbee-matter-manager"
INTERNAL_PORT=8000
MATTER_INTERNAL_PORT=5580
DATA_DIR="${ZMM_DATA_DIR:-$HOME/.zigbee-matter-manager}"

# Known Zigbee coordinator USB VID:PID pairs
declare -a ZIGBEE_USB_IDS=(
    "10c4:ea60|Silicon Labs CP210x (SONOFF, Tube, Electrolama, many EZSP sticks)"
    "10c4:8a2a|Silicon Labs CP210x variant"
    "1a86:7523|CH340 (ZStack/ZNP coordinators)"
    "1a86:55d4|CH9102 (ZStack/ZNP coordinators)"
    "0403:6001|FTDI FT232RL"
    "0403:6015|FTDI FT231X"
    "1cf1:0030|Dresden Elektronik ConBee II"
    "0451:16a8|Texas Instruments CC2531"
    "0451:bef3|Texas Instruments CC2652 Launchpad"
    "10c4:ea71|Silicon Labs CP2108"
    "067b:2303|Prolific PL2303"
)

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
        warn "Port ${preferred} is in use - scanning for next available port..."
        local found
        found=$(find_free_port "$((preferred + 1))")
        warn "Using port ${BOLD}${found}${NC} instead."
        echo "$found"
    else
        echo "$preferred"
    fi
}

check_deps() {
    local missing=()
    for cmd in git curl; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing required tools: ${missing[*]}"
    fi
}

fetch_repo() {
    if [[ -d "$APP_DIR/.git" ]]; then
        info "Repository already exists - pulling latest ${REPO_BRANCH}..."
        git -C "$APP_DIR" fetch origin
        git -C "$APP_DIR" checkout "$REPO_BRANCH"
        git -C "$APP_DIR" pull --ff-only origin "$REPO_BRANCH"
        ok "Repository updated."
    else
        info "Cloning ${REPO_URL} -> ${APP_DIR} ..."
        git clone --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
        ok "Repository cloned."
    fi
}

usb_sysfs_to_tty() {
    local usbpath="$1"
    local tty=""
    local old_nullglob
    old_nullglob=$(shopt -p nullglob 2>/dev/null || true)
    shopt -s nullglob
    local ttydir
    for ttydir in "${usbpath}"/*/*/tty/tty* "${usbpath}"/*/tty/tty*; do
        if [[ -d "$ttydir" ]]; then
            tty=$(basename "$ttydir")
            break
        fi
    done
    eval "$old_nullglob" 2>/dev/null || shopt -u nullglob
    if [[ -z "$tty" ]]; then
        tty=$(find "$usbpath" -maxdepth 6 -name "tty*" 2>/dev/null \
              | grep -oE 'tty[A-Z]+[0-9]+' | head -1 || true)
    fi
    echo "$tty"
}

detect_usb_coordinator() {
    USB_DEVICE=""
    info "Scanning for known Zigbee coordinators by VID:PID..."

    declare -a found_devices=()
    declare -a found_labels=()

    for devpath in /sys/bus/usb/devices/*/; do
        local vidfile="${devpath}idVendor"
        local pidfile="${devpath}idProduct"
        local productfile="${devpath}product"
        local serialfile="${devpath}serial"

        [[ -f "$vidfile" && -f "$pidfile" ]] || continue

        local vid="" pid="" vidpid="" product="" serial="" tty="" label=""
        vid=$(cat "$vidfile" 2>/dev/null || true)
        pid=$(cat "$pidfile" 2>/dev/null || true)
        vidpid="${vid}:${pid}"
        product=$(cat "$productfile" 2>/dev/null || echo "Unknown device")
        serial=$(cat "$serialfile" 2>/dev/null || true)

        for entry in "${ZIGBEE_USB_IDS[@]}"; do
            local known_id="${entry%%|*}"
            if [[ "$vidpid" == "$known_id" ]]; then
                tty=$(usb_sysfs_to_tty "$devpath")
                if [[ -n "$tty" && -c "/dev/${tty}" ]]; then
                    label="${product} [${vidpid}] -> /dev/${tty}"
                    [[ -n "$serial" ]] && label+=" (S/N: ${serial})"
                    found_devices+=("/dev/${tty}")
                    found_labels+=("$label")
                fi
                break
            fi
        done
    done

    local count=${#found_devices[@]}

    if [[ $count -eq 0 ]]; then
        warn "No known Zigbee coordinator found by VID:PID."
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
                warn "No USB device selected - Zigbee radio will not be available."
                break
            fi
        fi
        warn "Invalid selection, try again."
    done
}

_prompt_manual_usb() {
    echo
    warn "Available serial devices on this system:"
    for dev in /dev/ttyUSB* /dev/ttyACM*; do
        [[ -c "$dev" ]] && echo "    $dev"
    done
    echo
    read -rp "  Enter Zigbee coordinator device path (or leave blank to skip): " manual_dev
    if [[ -n "$manual_dev" ]]; then
        if [[ -c "$manual_dev" ]]; then
            USB_DEVICE="$manual_dev"
            ok "Using: ${USB_DEVICE}"
        else
            die "Device ${manual_dev} does not exist or is not a character device."
        fi
    else
        warn "No USB device selected - Zigbee radio will not be available."
    fi
}

resolve_dialout_gid() {
    DIALOUT_GID=""
    if getent group dialout &>/dev/null; then
        DIALOUT_GID=$(getent group dialout | cut -d: -f3)
        ok "Host dialout GID: ${BOLD}${DIALOUT_GID}${NC}"
    elif getent group uucp &>/dev/null; then
        DIALOUT_GID=$(getent group uucp | cut -d: -f3)
        ok "Host uucp GID (dialout equivalent): ${BOLD}${DIALOUT_GID}${NC}"
    else
        DIALOUT_GID=20
        warn "Could not resolve dialout/uucp group - defaulting to GID 20."
    fi
}

write_containerfile() {
    local dialout_gid="${DIALOUT_GID:-20}"

    # Note: heredoc is NOT quoted so ${dialout_gid} expands, but Dockerfile
    # variables like $HOME are not present so this is safe.
    cat > "$APP_DIR/Containerfile" << DOCKERFILE
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \\
        build-essential \\
        git \\
        libffi-dev \\
        libssl-dev \\
        logrotate \\
        curl \\
        libglib2.0-0 \\
        libnl-3-200 \\
        libnl-route-3-200 \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Install app deps then Matter [server] extra for CHIP SDK wheels
RUN pip install --no-cache-dir --upgrade pip \\
 && pip install --no-cache-dir -r requirements.txt \\
 && pip install --no-cache-dir "python-matter-server[server]"

COPY . .

# Create all required directories
RUN mkdir -p /data /app/data/matter /app/data/backups /app/logs /app/config \\
 && mkdir -p /usr/local/lib/python3.11/site-packages/credentials/development/paa-root-certs

# Create zigbee user (UID 1000).
# dialout group is created with the host's actual GID (${dialout_gid})
# so the container user can open the tty device without --privileged.
RUN groupadd -f -g ${dialout_gid} dialout \\
 && groupadd -r zigbee \\
 && useradd -r -u 1000 -g zigbee -G dialout -d /app zigbee \\
 && chown -R zigbee:zigbee /app /app/data /app/logs /app/config \\
 && chown -R zigbee:zigbee /usr/local/lib/python3.11/site-packages/credentials

# Redirect safe_deploy and app dirs to writable paths
ENV ZMM_BACKUP_DIR=/app/data/backups
ENV ZMM_APP_DIR=/app

USER zigbee

EXPOSE 8000 5580

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \\
    CMD curl -f http://localhost:8000/api/status || exit 1

CMD ["python", "main.py"]
DOCKERFILE
    ok "Containerfile written (dialout GID: ${dialout_gid})."
}

build_image() {
    info "Building image ${BOLD}${IMAGE_NAME}${NC} ..."
    "$RUNTIME" build \
        --tag "${IMAGE_NAME}:latest" \
        --file "$APP_DIR/Containerfile" \
        "$APP_DIR"
    ok "Image built: ${IMAGE_NAME}:latest"
}

prepare_data_dirs() {
    local dirs=("$DATA_DIR/config" "$DATA_DIR/data" "$DATA_DIR/logs")
    for d in "${dirs[@]}"; do
        mkdir -p "$d"
    done

    # Make host volume dirs writable by any user (UID 1000 inside container).
    # chmod instead of chown so this works without root on the host.
    chmod -R a+rwX "$DATA_DIR"
    ok "Volume permissions set at ${DATA_DIR}"


    if [[ ! -f "$DATA_DIR/config/config.yaml" ]] && [[ -f "$APP_DIR/config/config.yaml" ]]; then
        cp "$APP_DIR/config/config.yaml" "$DATA_DIR/config/config.yaml"
        ok "Default config.yaml copied to ${DATA_DIR}/config/"
    fi

    # Patch the selected USB device into config.yaml
    if [[ -n "${USB_DEVICE:-}" && -f "$DATA_DIR/config/config.yaml" ]]; then
        sed -i "s|port:.*\/dev\/tty[A-Za-z]*[0-9]*|port: ${USB_DEVICE}|g" "$DATA_DIR/config/config.yaml"
        sed -i "s|device:.*\/dev\/tty[A-Za-z]*[0-9]*|device: ${USB_DEVICE}|g" "$DATA_DIR/config/config.yaml"
        ok "Serial port patched in config.yaml -> ${BOLD}${USB_DEVICE}${NC}"
    fi

    ok "Data directories ready at ${DATA_DIR}"
}

run_container() {
    local host_port=$1
    local host_matter_port=$2

    if "$RUNTIME" inspect "$CONTAINER_NAME" &>/dev/null 2>&1; then
        warn "Existing container '${CONTAINER_NAME}' found - removing..."
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

    if [[ -n "${USB_DEVICE:-}" ]]; then
        run_args+=(--device "${USB_DEVICE}:${USB_DEVICE}")
        if [[ "$RUNTIME" == "podman" && -L "$USB_DEVICE" ]]; then
            local real_dev
            real_dev=$(readlink -f "$USB_DEVICE")
            run_args+=(--device "${real_dev}:${real_dev}")
        fi
    fi

    if [[ "$RUNTIME" == "podman" ]]; then
        run_args+=(--security-opt label=disable)
        run_args+=(--privileged)
    fi

    info "Starting container '${CONTAINER_NAME}' ..."
    "$RUNTIME" run "${run_args[@]}" "${IMAGE_NAME}:latest"
    ok "Container started."
}

install_autostart() {
    if ! command -v systemctl &>/dev/null; then
        warn "systemd not found - skipping auto-start setup."
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

usage() {
    cat << EOF
${BOLD}Usage:${NC} $0 [OPTIONS]

${BOLD}Options:${NC}
  --port   PORT      Preferred host port  (default: ${INTERNAL_PORT})
  --usb    DEVICE    Zigbee USB device    (default: auto-detect by VID:PID)
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

echo
echo -e "${BOLD}=====================================================${NC}"
echo -e "${BOLD}   Zigbee Matter Manager - Container Build & Deploy  ${NC}"
echo -e "${BOLD}=====================================================${NC}"
echo

check_deps
detect_runtime
fetch_repo

if [[ -z "${USB_DEVICE:-}" ]]; then
    detect_usb_coordinator
else
    if [[ ! -c "$USB_DEVICE" ]]; then
        die "Specified USB device ${USB_DEVICE} does not exist or is not a character device."
    fi
    ok "Using specified USB device: ${BOLD}${USB_DEVICE}${NC}"
fi

resolve_dialout_gid

HOST_PORT=$(pick_host_port "$PREFERRED_PORT")
HOST_MATTER_PORT=$(pick_host_port "$MATTER_INTERNAL_PORT")

write_containerfile

if [[ "$FORCE_REBUILD" == true ]] || ! "$RUNTIME" image inspect "${IMAGE_NAME}:latest" &>/dev/null 2>&1; then
    build_image
else
    info "Image ${IMAGE_NAME}:latest already exists - skipping build (use --rebuild to force)."
fi

prepare_data_dirs
run_container "$HOST_PORT" "$HOST_MATTER_PORT"

if [[ "$INSTALL_AUTOSTART" == true ]]; then
    install_autostart
fi

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