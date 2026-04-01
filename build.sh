#!/bin/bash
# =============================================================================
# Zigbee Matter Manager — Container Build & Deploy Script
# Supports: Podman (preferred) and Docker
# Internal port: 8000 (fixed). External port: auto-detected.
#
# Device access strategy:
#   Build: bake host UID:GID into image (--build-arg HOST_UID/HOST_GID)
#   Run:   --group-add <dialout GID> + --security-opt label=disable
#   No --userns keep-id, no --privileged, no UID remapping headaches.
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

get_port_process() {
    local port=$1
    local proc=""

    # Try lsof first (cleanest output if installed)
    if command -v lsof &>/dev/null; then
        proc=$(sudo lsof -i :"${port}" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1" (PID: "$2")"}')
    fi

    # Fallback to ss (standard on modern Linux)
    if [[ -z "$proc" ]] && command -v ss &>/dev/null; then
        # Parses the bizarre ss output: users:(("process_name",pid=1234,fd=X))
        proc=$(sudo ss -lptn "sport = :${port}" 2>/dev/null | grep -o 'users:((".*"))' | sed 's/users:(("//; s/",pid=/ (PID: /; s/,.*//' | head -n 1)
    fi

    # Return the process, or a fallback warning if permissions blocked the lookup
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

pick_host_port() {
    local preferred=$1
    if port_in_use "$preferred"; then
        # 1. Find out who is hogging the port
        local blocker
        blocker=$(get_port_process "$preferred")

        # 2. Tell the user exactly what is blocking it
        warn "Port ${preferred} is currently blocked by: ${BOLD}${blocker}${NC}" >&2
        warn "Scanning for the next available port..." >&2

        # 3. Find and report the new port
        local found
        found=$(find_free_port "$((preferred + 1))")
        warn "Using port ${BOLD}${found}${NC} instead." >&2

        # 4. Return the safely found port to stdout
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
    cat > "$APP_DIR/Containerfile" << 'DOCKERFILE'
# 1. PINNED TO DEBIAN BOOKWORM
FROM python:3.11-slim-bookworm

ARG HOST_UID=1000
ARG HOST_GID=1000
ARG HOST_DIALOUT_GID=20

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
    # Fetch exact v4.7.1 tag (no .0 at the end for Git!)
    git clone --depth 1 --branch v4.7.1 https://github.com/SiliconLabs/cpc-daemon.git /tmp/cpc-daemon && \
    # Force the compiled version string to be 4.7.1.0 to perfectly match the .deb daemon
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

# Create app user with the HOST's exact UID:GID + dialout group membership.
RUN groupadd -g "$HOST_GID" -o appgroup \
 && if ! getent group "$HOST_DIALOUT_GID" >/dev/null 2>&1; then \
        groupadd -g "$HOST_DIALOUT_GID" hostdialout; \
    fi \
 && SERIAL_GROUP=$(getent group "$HOST_DIALOUT_GID" | cut -d: -f1) \
 && useradd -u "$HOST_UID" -g "$HOST_GID" -G "$SERIAL_GROUP" \
        -d /app -s /bin/bash -o appuser

WORKDIR /app

# Dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "python-matter-server[server]"

# Application source
COPY . .

# Required directories
RUN mkdir -p /data /app/data/matter /app/data/backups /app/logs /app/config /var/lib/thread \
        /usr/local/lib/python3.11/site-packages/credentials/development/paa-root-certs \
 && chown -R ${HOST_UID}:${HOST_GID} /app /data /app/data /app/logs /app/config \
        /usr/local/lib/python3.11/site-packages/credentials /var/lib/thread

ENV ZMM_BACKUP_DIR=/app/data/backups
ENV ZMM_APP_DIR=/app

USER appuser

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
    info "  UID:GID = ${HOST_UID}:${HOST_GID}, Dialout GID = ${DIALOUT_GID:-?}"
    local build_args=(
        --build-arg "HOST_UID=${HOST_UID}"
        --build-arg "HOST_GID=${HOST_GID}"
    )

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
    # Reclaim ownership — previous runs with :Z or different UID may have
    # changed ownership to root/container user
    if [[ -d "$DATA_DIR" ]]; then
        if ! [[ -w "$DATA_DIR" ]]; then
            info "Reclaiming ownership of ${DATA_DIR}..."
            sudo chown -R "$(id -u):$(id -g)" "$DATA_DIR"
        fi
    fi

    local dirs=(
        "$DATA_DIR/config"
        "$DATA_DIR/data"
        "$DATA_DIR/data/matter"
        "$DATA_DIR/logs"
    )
    for d in "${dirs[@]}"; do
        mkdir -p "$d"
    done

    # Ensure all subdirs are writable (catches partial ownership issues)
    for d in "${dirs[@]}"; do
        if ! [[ -w "$d" ]]; then
            sudo chown -R "$(id -u):$(id -g)" "$d"
        fi
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
# DEVICE BIND-MOUNT (Podman rootless workaround)
# =============================================================================
# Rootless Podman can't access /dev/ device nodes directly via --device.
# Workaround: bind-mount the device into /mnt/devices/ with correct ownership,
# then pass the bind-mounted path to --device instead:
# https://github.com/containers/podman/discussions/22379
# =============================================================================
DEVICE_MOUNT_DIR="/mnt/devices"

prepare_device_mount() {
    # Only needed for Podman (Docker doesn't have this issue)
    if [[ "$RUNTIME" != "podman" ]]; then
        return 0
    fi

    if [[ -z "${USB_DEVICE:-}" ]]; then
        return 0
    fi

    local real_dev
    real_dev=$(readlink -f "$USB_DEVICE")
    local dev_basename
    dev_basename=$(basename "$real_dev")
    local mount_path="${DEVICE_MOUNT_DIR}/${dev_basename}"

    info "Setting up device bind-mount for rootless Podman..."

    # Create the mount directory if it doesn't exist
    if [[ ! -d "$DEVICE_MOUNT_DIR" ]]; then
        info "Creating ${DEVICE_MOUNT_DIR} ..."
        sudo mkdir -p "$DEVICE_MOUNT_DIR"
        sudo chown root:root "$DEVICE_MOUNT_DIR"
        sudo chmod 755 "$DEVICE_MOUNT_DIR"
    fi

    # Clean up any stale mount at this path
    if mountpoint -q "$mount_path" 2>/dev/null; then
        info "Removing stale bind-mount at ${mount_path} ..."
        sudo umount "$mount_path"
    fi

    # Create the mount-point file (touch, not mkdir — it's a device node)
    if [[ ! -e "$mount_path" ]]; then
        sudo touch "$mount_path"
    fi

    # Bind-mount the real device node
    sudo mount --bind "$real_dev" "$mount_path"

    # Set ownership so the rootless Podman user can access it
    sudo chown "$(id -u):${DIALOUT_GID:-$(id -g)}" "$mount_path"

    # Store the mapped path for run_container to use
    DEVICE_MOUNT_PATH="$mount_path"

    ok "Device bind-mounted: ${real_dev} → ${mount_path}"
}

# =============================================================================
# TEARDOWN CLEANUP
# =============================================================================
cleanup_device_mount() {
    if [[ -n "${DEVICE_MOUNT_PATH:-}" ]] && mountpoint -q "$DEVICE_MOUNT_PATH" 2>/dev/null; then
        info "Unmounting device bind-mount: ${DEVICE_MOUNT_PATH}"
        sudo umount "$DEVICE_MOUNT_PATH"
        sudo rm -f "$DEVICE_MOUNT_PATH"
    fi
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
        --network=host
        --cap-add=NET_ADMIN
        --cap-add=NET_RAW
        --cap-add=SYS_ADMIN
        --restart unless-stopped
        --device /dev/net/tun:/dev/net/tun
        --sysctl net.ipv6.conf.all.disable_ipv6=0
        --sysctl net.ipv6.conf.all.forwarding=1
        --sysctl net.ipv4.conf.all.forwarding=1
        --volume /dev/shm:/dev/shm
        --volume /run/dbus:/run/dbus
        --volume "${DATA_DIR}/config:/app/config"
        --volume "${DATA_DIR}/data:/app/data"
        --volume "${DATA_DIR}/logs:/app/logs"
    )

    # ── Dynamic ports via environment variables ──────────────────────
    # With --network=host the container shares the host's network stack.
    # No port mapping — the app listens directly on the host interface.
    # If the default ports (8000/5580) are busy, build.sh picks free ones
    # and tells the app to listen there via environment variables.
    run_args+=(-e "ZMM_PORT=${host_port}")
    run_args+=(-e "ZMM_MATTER_PORT=${host_matter_port}")

    ok "Networking: host (ZMM port: ${host_port}, Matter port: ${host_matter_port})"

    # ── UID mapping: keep host UID inside container (Podman only) ──
    if [[ "$RUNTIME" == "podman" ]]; then
        run_args+=(--userns=keep-id)
    fi

    # ── USB device passthrough ──
    if [[ -n "${USB_DEVICE:-}" ]]; then
        local real_dev
        real_dev=$(readlink -f "$USB_DEVICE")

        if [[ "$RUNTIME" == "podman" && -n "${DEVICE_MOUNT_PATH:-}" ]]; then
            # Podman rootless: use the bind-mounted device path
            run_args+=(--device "${DEVICE_MOUNT_PATH}:${real_dev}")
            ok "Using bind-mounted device: ${DEVICE_MOUNT_PATH} → ${real_dev}"

            # If the original was a symlink, also map it inside the container
            if [[ "$USB_DEVICE" != "$real_dev" ]]; then
                run_args+=(--device "${DEVICE_MOUNT_PATH}:${USB_DEVICE}")
            fi
        else
            # Docker or fallback: pass device nodes directly
            run_args+=(--device "${real_dev}:${real_dev}")
            if [[ "$USB_DEVICE" != "$real_dev" ]]; then
                run_args+=(--device "${USB_DEVICE}:${USB_DEVICE}")
            fi
        fi
    fi

    # ── USB bus access for USBDEVFS_RESET (MultiPAN CPC state cleanup) ──
    if [[ -d /dev/bus/usb ]]; then
        run_args+=(-v /dev/bus/usb:/dev/bus/usb)
        ok "Mounted /dev/bus/usb for USB device reset support"
    fi

    # ── Device access: --group-add for dialout, disable SELinux label ──
    if [[ -n "${DIALOUT_GID:-}" ]]; then
        run_args+=(--group-add "${DIALOUT_GID}")
    fi
    run_args+=(--security-opt label=disable)

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
        local unit_dir="$HOME/.config/systemd/user"
        mkdir -p "$unit_dir"

        # Write the ExecStartPre helper script
        local pre_script="/usr/local/bin/zmm-remount-device.sh"
        local mount_path="/mnt/devices/ttyUSB0"
        local invoking_uid
        invoking_uid=$(id -u)
        local invoking_gid
        invoking_gid="${DIALOUT_GID:-$(id -g)}"

        sudo tee "$pre_script" > /dev/null << SCRIPT
#!/bin/bash
# Recreates the bind mount at the fixed path the container was built with.
# Scans for any /dev/ttyUSB* or /dev/ttyACM* and mounts the first found
# at /mnt/devices/ttyUSB0 — the path baked into the container config.
set -e

MOUNT_PATH="${mount_path}"
MOUNT_DIR="\$(dirname \$MOUNT_PATH)"
DEVICE=""

for dev in /dev/ttyUSB* /dev/ttyACM*; do
    if [[ -c "\$dev" ]]; then
        DEVICE="\$dev"
        break
    fi
done

if [[ -z "\$DEVICE" ]]; then
    echo "zmm-remount: ERROR — no serial device found" >&2
    exit 1
fi

echo "zmm-remount: found \$DEVICE"

# Tear down stale mount if present
if mountpoint -q "\$MOUNT_PATH" 2>/dev/null; then
    echo "zmm-remount: unmounting stale \$MOUNT_PATH"
    umount "\$MOUNT_PATH"
fi

# Ensure mount dir and mount point file exist
mkdir -p "\$MOUNT_DIR"
touch "\$MOUNT_PATH"

# Bind-mount the found device at the fixed path
mount --bind "\$DEVICE" "\$MOUNT_PATH"

# Set ownership so rootless Podman can access it
chown ${invoking_uid}:${invoking_gid} "\$MOUNT_PATH"
chmod 660 "\$MOUNT_PATH"

echo "zmm-remount: \$DEVICE → \$MOUNT_PATH OK"
SCRIPT
        sudo chmod +x "$pre_script"
        bash_bin=$(which bash)

        # Allow script to run without password prompt from systemd user unit
        sudo tee /etc/sudoers.d/zmm-remount > /dev/null << EOF
                $USER ALL=(ALL) NOPASSWD: $bash_bin $pre_script
EOF
        sudo chmod 440 /etc/sudoers.d/zmm-remount
        ok "Device remount script written: ${pre_script}"

        info "Generating podman systemd unit..."
        "$RUNTIME" generate systemd \
            --name "$CONTAINER_NAME" \
            --restart-policy=always \
            --new \
            > "$unit_dir/container-${CONTAINER_NAME}.service" 2>/dev/null || {

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

        local bash_bin
        bash_bin=$(which bash)

        # Inject ExecStartPre remount before the first ExecStart line
        sed -i "/^ExecStart=/i ExecStartPre=sudo -n ${bash_bin} ${pre_script}" \
            "$unit_dir/container-${CONTAINER_NAME}.service"

        systemctl --user daemon-reload
        systemctl --user enable "container-${CONTAINER_NAME}.service"

        if command -v loginctl &>/dev/null; then
            loginctl enable-linger "$USER" 2>/dev/null || true
        fi

        ok "Podman user systemd unit enabled."
        info "The container will start automatically at boot."
    else
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
  --uid    UID:GID   Container user ID    (default: current user $(id -u):$(id -g))
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
HOST_UID=$(id -u)
HOST_GID=$(id -g)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)         PREFERRED_PORT="$2";    shift 2 ;;
        --usb)          USB_DEVICE="$2";        shift 2 ;;
        --uid)          HOST_UID="${2%%:*}"; HOST_GID="${2##*:}"; shift 2 ;;
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

# Step 6: Prepare data dirs + device mount
prepare_data_dirs

# Step 7: Bind-mount device for rootless Podman
prepare_device_mount


# Step 8: Run
run_container "$HOST_PORT" "$HOST_MATTER_PORT"

# Step 9: Auto-start
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
echo -e "  ${BOLD}Container UID:${NC}  ${HOST_UID}:${HOST_GID}"
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