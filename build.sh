#!/bin/bash
# =============================================================================
# Zigbee Matter Manager — Container Build & Deploy Script
# Supports: Podman (preferred) and Docker
#
# Runs as a privileged container (required for OTBR network namespaces,
# ipset, iptables, and Thread border routing).
# Uses --network=host for direct Thread/mDNS/IPv6 access.
# =============================================================================

CURRENT_USER=$(whoami)
export XDG_RUNTIME_DIR=/run/user/$(id -u "$CURRENT_USER")
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"

set -euo pipefail

# ── Build host capability detection ──
detect_build_jobs() {
    local cores
    if command -v nproc >/dev/null 2>&1; then
        cores=$(nproc)
    elif [[ -r /proc/cpuinfo ]]; then
        cores=$(grep -c ^processor /proc/cpuinfo)
    else
        cores=2
    fi
    # Cap at 8 — diminishing returns past that, and DuckDB's compile
    # link step occasionally OOMs on -j16+ with only a few GB free.
    (( cores > 8 )) && cores=8
    echo "$cores"
}

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}${BOLD}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}${BOLD}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}${BOLD}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}${BOLD}[ERR ]${NC}  $*" >&2; }
die()     { error "$*"; exit 1; }

BUILD_JOBS=$(detect_build_jobs)
info "Detected ${BUILD_JOBS} build jobs for parallel compile"

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
    # Container runs as root so device access inside is guaranteed.
    # We still detect the group for informational purposes and to help
    # the host user manage the container (e.g. podman exec).
    local serial_group=""
    if getent group dialout &>/dev/null; then
        serial_group="dialout"
    elif getent group uucp &>/dev/null; then
        serial_group="uucp"
    else
        warn "No dialout or uucp group found on this system."
        return 0
    fi

    DIALOUT_GID=$(getent group "$serial_group" | cut -d: -f3)
    ok "Serial device group: ${BOLD}${serial_group}${NC} (GID ${DIALOUT_GID})"

    if ! id -nG "$USER" 2>/dev/null | grep -qw "$serial_group"; then
        warn "User ${BOLD}${USER}${NC} is NOT in ${BOLD}${serial_group}${NC}."
        warn "Not required for the container, but recommended for host-side device access."
    fi
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

get_port_process() {
    local port=$1
    local proc=""

    if command -v lsof &>/dev/null; then
        proc=$(sudo lsof -i :"${port}" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1" (PID: "$2")"}')
    fi

    if [[ -z "$proc" ]] && command -v ss &>/dev/null; then
        proc=$(sudo ss -lptn "sport = :${port}" 2>/dev/null | grep -o 'users:((".*"))' | sed 's/users:(("//; s/",pid=/ (PID: /; s/,.*//' | head -n 1)
    fi

    if [[ -n "$proc" ]]; then
        echo "$proc"
    else
        echo "an unknown process (run script with sudo to see details)"
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

check_host_port() {
    # With --network=host the container binds directly to host ports.
    # Verify the port is free; if not, find an alternative and pass via env var.
    local preferred=$1
    if port_in_use "$preferred"; then
        local blocker
        blocker=$(get_port_process "$preferred")
        warn "Port ${preferred} is currently blocked by: ${BOLD}${blocker}${NC}" >&2
        warn "Scanning for the next available port..." >&2
        local found
        found=$(find_free_port "$((preferred + 1))")
        warn "Using port ${BOLD}${found}${NC} instead." >&2
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
            if echo "$label" | grep -qiE 'cp210|ezsp|zigbee|silabs|ember|ch340|ch341|cc253|cc265|conbee|raspbee|sonoff|tube|slzb|zzh'; then
                found_devices+=("$real_dev")
                found_labels+=("$label → $real_dev")
            fi
        done
    fi

    # Fallback to raw /dev/ttyACM* and /dev/ttyUSB*
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
    cat > "$APP_DIR/Containerfile" << 'DOCKERFILE'
# Zigbee Matter Manager — Root Container
FROM python:3.11-slim-bookworm

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        lsb-release \
        sudo \
        git \
        ca-certificates \
        cmake \
        ninja-build \
        g++ \
        libffi-dev \
        libmbedtls-dev \
        libssl-dev \
        libdbus-1-dev \
        libavahi-client-dev \
        libreadline-dev \
        libboost-dev \
        libboost-filesystem-dev \
        libboost-system-dev \
        libnetfilter-queue-dev \
        libsystemd-dev \
        ipset \
        iptables \
        dbus \
        avahi-daemon \
        logrotate \
        curl \
        wget \
        unzip \
        jq \
        libglib2.0-0 \
        libnl-3-200 \
        libnl-route-3-200 \
        socat \
        procps \
        strace \
        iproute2 \
        net-tools \
        pkg-config \
        bluez \
    && rm -rf /var/lib/apt/lists/*

# Fetch and install Silicon Labs packages matching Bookworm
RUN DOWNLOAD_URL=$(curl -s https://api.github.com/repos/SiliconLabs/simplicity_sdk/releases/latest | jq -r '.assets[] | select(.name=="debian-bookworm.zip") | .browser_download_url') \
    && wget "$DOWNLOAD_URL" -O debian-bookworm.zip \
    && unzip debian-bookworm.zip -d /tmp/silabs \
    && ARCH=$(dpkg --print-architecture) \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        /tmp/silabs/debian-bookworm/deb/libcpc3_*_${ARCH}.deb \
        /tmp/silabs/debian-bookworm/deb/libcpc-dev_*_${ARCH}.deb \
        /tmp/silabs/debian-bookworm/deb/cpcd_*_${ARCH}.deb \
        /tmp/silabs/debian-bookworm/deb/zigbeed_*_${ARCH}.deb \
    && rm -rf /tmp/silabs debian-bookworm.zip /var/lib/apt/lists/*

# ── OTBR with SiLabs CPC MultiPAN support ──────────────────────────────
ENV SDK_DIR=/tmp/silabs_sdk

# 1. Sparse clone SiLabs SDK just to get the CPC vendor extension files
RUN git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/SiliconLabs/simplicity_sdk.git ${SDK_DIR} && \
    cd ${SDK_DIR} && \
    git sparse-checkout set protocol/openthread/platform-abstraction/posix

# 2. Clone official OTBR, init submodules, clone matching cpc-daemon, then build
RUN echo '#!/bin/sh' > /usr/local/bin/sudo && \
    echo 'if echo "$*" | grep -Eq "/proc/sys|sysctl"; then exit 0; fi' >> /usr/local/bin/sudo && \
    echo 'exec /usr/bin/sudo "$@"' >> /usr/local/bin/sudo && \
    chmod +x /usr/local/bin/sudo && \
    git clone --depth 1 --branch v4.7.1 https://github.com/SiliconLabs/cpc-daemon.git /tmp/cpc-daemon && \
    sed -i 's/VERSION 4\.7\.1\b/VERSION 4.7.1.0/g' /tmp/cpc-daemon/CMakeLists.txt && \
    git clone --depth=1 https://github.com/openthread/ot-br-posix /tmp/otbr && \
    cd /tmp/otbr && \
    git submodule update --init --recursive && \
    cp ${SDK_DIR}/protocol/openthread/platform-abstraction/posix/openthread-core-silabs-posix-config.h \
       /tmp/otbr/third_party/openthread/repo/src/posix/platform/ && \
    ./script/bootstrap && \
    INFRA_IF_NAME=eth0 \
    OTBR_OPTIONS=" \
        -DOT_THREAD_VERSION=1.4 \
        -DOT_MULTIPAN_RCP=ON \
        -DCPCD_SOURCE_DIR=/tmp/cpc-daemon \
        -DOT_POSIX_RCP_VENDOR_BUS=ON \
        -DOT_POSIX_CONFIG_RCP_VENDOR_DEPS_PACKAGE=${SDK_DIR}/protocol/openthread/platform-abstraction/posix/posix_vendor_rcp.cmake \
        -DOT_POSIX_CONFIG_RCP_VENDOR_INTERFACE=${SDK_DIR}/protocol/openthread/platform-abstraction/posix/cpc_interface.cpp \
        -DOT_PLATFORM_CONFIG=openthread-core-silabs-posix-config.h" \
    ./script/setup && \
    rm -f /usr/local/bin/sudo

# 3. Disable systemd service (ZMM manages otbr-agent lifecycle) and clean up
RUN systemctl disable otbr-agent 2>/dev/null || true
RUN rm -rf ${SDK_DIR} /tmp/otbr /tmp/cpc-daemon

WORKDIR /app

# ── Application requirements (layer cache) ──
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "python-matter-server[server]"

# ── Build zmm_telemetry from source inside the container ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-dev \
        pkg-config \
 && rm -rf /var/lib/apt/lists/* \
 && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain stable --profile minimal \
 && pip install --no-cache-dir maturin \
 && pip

ENV PATH="/root/.cargo/bin:${PATH}"

ARG BUILD_JOBS=4
ENV CMAKE_BUILD_PARALLEL_LEVEL=${BUILD_JOBS}
ENV MAKEFLAGS="-j${BUILD_JOBS}"

COPY zmm_telemetry/ /tmp/zmm_telemetry/
RUN cd /tmp/zmm_telemetry \
 && maturin build --release --out /tmp/wheels \
 && pip install --no-cache-dir /tmp/wheels/zmm_telemetry-*.whl \
 && rm -rf /tmp/zmm_telemetry /tmp/wheels /root/.cargo /root/.rustup /root/.cache

# Application source
COPY . .

# Application version control - used for upgrades
COPY VERSION /app/VERSION

# Required directories
RUN mkdir -p /data /app/data/matter /app/data/backups /app/data/certs /app/logs /app/config /var/lib/thread \
        /usr/local/lib/python3.11/site-packages/credentials/development/paa-root-certs

ENV ZMM_BACKUP_DIR=/app/data/backups
ENV ZMM_APP_DIR=/app

EXPOSE 8000 5580

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${ZMM_PORT:-8000}/api/status || exit 1

CMD ["python", "launcher.py"]
DOCKERFILE
    ok "Containerfile written."
}

# =============================================================================
# BUILD IMAGE
# =============================================================================
build_image() {
    info "Building image ${BOLD}${IMAGE_NAME}${NC} with ${BUILD_JOBS} parallel jobs ..."

    "$RUNTIME" build \
        --format docker \
        --build-arg BUILD_JOBS="${BUILD_JOBS}" \
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
        "$DATA_DIR/data/certs"
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
# HOST DBUS POLICY FOR OTBR
# =============================================================================
prepare_otbr_dbus_policy() {
    local policy_file="/etc/dbus-1/system.d/otbr-agent.conf"

    if [[ -f "$policy_file" ]] && grep -q "context=\"default\"" "$policy_file" 2>/dev/null; then
        ok "OTBR D-Bus policy already configured"
        return
    fi

    info "Installing D-Bus policy for Thread border router..."
    sudo tee "$policy_file" > /dev/null << 'DBUS_POLICY'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy context="default">
    <allow own_prefix="io.openthread.BorderRouter"/>
    <allow send_destination="io.openthread.BorderRouter.wpan0"/>
    <allow send_interface="io.openthread.BorderRouter"/>
    <allow send_interface="org.freedesktop.DBus.Properties"/>
    <allow send_interface="org.freedesktop.DBus.Introspectable"/>
  </policy>
</busconfig>
DBUS_POLICY
    sudo systemctl reload dbus 2>/dev/null || true
    ok "OTBR D-Bus policy installed"
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
        --network=slirp4netns
        --security-opt label=disable
        --publish "${host_port}:${INTERNAL_PORT}"
        --publish "${host_matter_port}:${MATTER_INTERNAL_PORT}"
        --cap-add=NET_ADMIN
        --cap-add=NET_RAW
        --cap-add=SYS_ADMIN
        --sysctl net.ipv6.conf.all.disable_ipv6=0
        --sysctl net.ipv6.conf.all.forwarding=1
        --sysctl net.ipv4.conf.all.forwarding=1
        --device /dev/net/tun:/dev/net/tun
        --volume /dev/shm:/dev/shm
        --volume /run/dbus:/run/dbus
        --volume "${DATA_DIR}/config:/app/config"
        --volume "${DATA_DIR}/data:/app/data"
        --volume "${DATA_DIR}/data/certs:/app/data/certs"
        --volume "${DATA_DIR}/logs:/app/logs"
    )

    ok "Networking: host (ZMM: ${host_port}, Matter: ${host_matter_port})"

    # ── Bluetooth for Matter commissioning ──
    if [[ -e /dev/hci0 ]]; then
        run_args+=(--device /dev/hci0:/dev/hci0)
        ok "Bluetooth adapter available for Matter commissioning"
    fi

    # ── USB device passthrough (direct — root container has full access) ──
    if [[ -n "${USB_DEVICE:-}" ]]; then
        local real_dev
        real_dev=$(readlink -f "$USB_DEVICE")
        run_args+=(--device "${real_dev}:${real_dev}")

        # If the original path was a symlink, also map that
        if [[ "$USB_DEVICE" != "$real_dev" ]]; then
            run_args+=(--device "${USB_DEVICE}:${USB_DEVICE}")
        fi
    fi

    # ── USB bus access for USBDEVFS_RESET (MultiPAN CPC state cleanup) ──
    if [[ -d /dev/bus/usb ]]; then
        run_args+=(-v /dev/bus/usb:/dev/bus/usb)
        ok "Mounted /dev/bus/usb for USB device reset support"
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

    local runtime_bin
    runtime_bin=$(which "$RUNTIME")

    local unit_file="/etc/systemd/system/${CONTAINER_NAME}.service"
    sudo tee "$unit_file" > /dev/null << UNIT
[Unit]
Description=Zigbee Matter Manager Container
After=network-online.target
Wants=network-online.target

[Service]
Restart=always
RestartSec=10
ExecStart=${runtime_bin} start -a ${CONTAINER_NAME}
ExecStop=${runtime_bin} stop -t 15 ${CONTAINER_NAME}

[Install]
WantedBy=multi-user.target
UNIT

    sudo systemctl daemon-reload
    sudo systemctl enable "${CONTAINER_NAME}.service"
    ok "Systemd unit installed and enabled: ${unit_file}"
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
check_dialout_group
detect_runtime

# Step 2: Get the code
fetch_repo

# Step 3: USB coordinator
if [[ -z "${USB_DEVICE:-}" ]]; then
    detect_usb_coordinator
fi

# Step 4: Ports (--network=host — verify ports are free on host)
HOST_PORT=$(check_host_port "$PREFERRED_PORT")
HOST_MATTER_PORT=$(check_host_port "$MATTER_INTERNAL_PORT")

# Step 5: Build
write_containerfile

BUILD_JOBS=$(detect_build_jobs)
info "Detected ${BUILD_JOBS} build jobs for parallel compile"

if "$FORCE_REBUILD" || ! "$RUNTIME" image inspect "${IMAGE_NAME}:latest" &>/dev/null 2>&1; then
    build_image
else
    info "Image exists — skipping build (use --rebuild to force)."
fi

# Step 6: Prepare data dirs
prepare_data_dirs

# Step 7: OTBR D-Bus policy on host
prepare_otbr_dbus_policy

# Step 8: Run
run_container "$HOST_PORT" "$HOST_MATTER_PORT"

# Step 9: Auto-start
if [[ "$INSTALL_AUTOSTART" == true ]]; then
    install_autostart
fi

# Step 10: Install upgrade watcher (first-time only)
if [[ ! -f "${DATA_DIR}/data/upgrade/.watcher_installed" ]]; then
    info "Installing in-app upgrade watcher ..."
    mkdir -p "${DATA_DIR}/scripts"
    # Copy the scripts from the cloned repo
    if [[ -f "${APP_DIR}/scripts/upgrade.sh" ]]; then
        cp "${APP_DIR}/scripts/upgrade.sh" "${DATA_DIR}/scripts/"
        cp "${APP_DIR}/scripts/run_container.sh" "${DATA_DIR}/scripts/"
        chmod +x "${DATA_DIR}/scripts/"*.sh
        # Run the installer
        if [[ -f "${APP_DIR}/scripts/install_watcher.sh" ]]; then
            ZMM_DATA_DIR="$DATA_DIR" ZMM_APP_DIR="$APP_DIR" \
                bash "${APP_DIR}/scripts/install_watcher.sh" || \
                warn "Watcher install encountered issues — you can re-run it later from the Settings tab"
        fi
    else
        warn "scripts/upgrade.sh not found in repo — upgrade feature will not be available"
        warn "You can enable it later by running: bash ${APP_DIR}/scripts/install_watcher.sh"
    fi
else
    ok "Upgrade watcher already installed"
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
echo -e "  ${BOLD}Network:${NC}        host"
echo
echo -e "  ${BOLD}Commands:${NC}"
echo -e "    ${RUNTIME} logs -f ${CONTAINER_NAME}        # Follow logs"
echo -e "    ${RUNTIME} exec -it ${CONTAINER_NAME} bash  # Shell"
echo -e "    ${RUNTIME} stop ${CONTAINER_NAME}           # Stop"
echo -e "    ${RUNTIME} start ${CONTAINER_NAME}          # Start"
echo -e "    ${RUNTIME} rm -f ${CONTAINER_NAME}          # Remove"
echo
echo
echo -e "${RED}${BOLD}=====================================================${NC}"
echo -e "${RED}${BOLD}  !!! NOTICE !!! ${NC}"
echo -e "${RED}${BOLD}=====================================================${NC}"
echo
echo -e "Should you wish to rebuild the container please use the teardown script"
echo -e "  ${BOLD}Data:${NC}           ${DATA_DIR}/teardown.sh"
echo