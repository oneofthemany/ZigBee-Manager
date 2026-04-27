#!/bin/bash
# =============================================================================
# ZMM run_container helper
#
# Called by upgrade.sh during swap/rollback. Starts the container using a
# specific image tag (passed via IMAGE_TAG env var) but with the same run
# arguments as build.sh's run_container function.
#
# The run arguments MUST stay in sync with build.sh — when you edit one,
# edit the other.
#
# Required env:
#   RUNTIME         — podman or docker
#   IMAGE_TAG       — full image ref to run (e.g. zigbee-matter-manager:1.3.0-arm64)
#   CONTAINER_NAME  — name to give the container
#   DATA_DIR        — persistent data directory (volumes)
#
# Optional env:
#   HOST_PORT       — defaults to 8000
#   HOST_MATTER_PORT — defaults to 5580
#
# Device resolution:
#   Reads zigbee.port from config.yaml. Must be a tty device (or a socket://
#   URL for MultiPAN). For an upgrade, the device MUST exist — failure is
#   intentional. There is no fallback because the existing system is, by
#   definition, already configured for a specific device. Auto-detection
#   would risk passing the wrong device after a USB reshuffling.
# =============================================================================
set -u
set -o pipefail

: "${RUNTIME:?RUNTIME env var required}"
: "${IMAGE_TAG:?IMAGE_TAG env var required}"
: "${CONTAINER_NAME:?CONTAINER_NAME env var required}"
: "${DATA_DIR:?DATA_DIR env var required}"

HOST_PORT="${HOST_PORT:-8000}"
HOST_MATTER_PORT="${HOST_MATTER_PORT:-5580}"
INTERNAL_PORT=8000
MATTER_INTERNAL_PORT=5580

CONFIG_FILE="${DATA_DIR}/config/config.yaml"

# ── DEVICE RESOLUTION ───────────────────────────────────────────────────────
# Read zigbee.port from config.yaml. Same approach as build.sh.
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config.yaml not found at $CONFIG_FILE" >&2
    echo "       Cannot determine which device to pass through." >&2
    exit 1
fi

# Parse the port using the same pattern build.sh uses.
# Accept any /dev/* path (covers /dev/ttyACM0, /dev/ttyUSB0, /dev/serial/by-id/...)
# or socket:// URLs for MultiPAN mode.
DEVICE=$(grep -Po 'port:\s*\K[^\s#]+' "$CONFIG_FILE" | grep -E '^/dev/|^socket://' | head -1)

if [[ -z "$DEVICE" ]]; then
    echo "ERROR: Could not parse zigbee.port from $CONFIG_FILE" >&2
    echo "       Expected a line like: 'port: /dev/ttyACM0' or 'port: /dev/ttyUSB0'" >&2
    echo "       Found these port entries (any section):" >&2
    grep -n "port:" "$CONFIG_FILE" | sed 's/^/         /' >&2
    exit 1
fi

# MultiPAN socket mode — no device passthrough needed (zigbeed bridges)
if [[ "$DEVICE" == socket://* ]]; then
    echo "Detected MultiPAN socket mode: $DEVICE"
    echo "Container will use host-side cpcd/zigbeed bridge — no /dev passthrough"
    SKIP_USB=1
fi

# Local device — must exist on the host
if [[ -z "${SKIP_USB:-}" ]]; then
    if [[ ! -e "$DEVICE" ]]; then
        echo "ERROR: Configured device $DEVICE does not exist on host" >&2
        echo "       Either plug in the dongle or update zigbee.port in config.yaml." >&2
        echo "       Available tty devices on host:" >&2
        ls /dev/tty{ACM,USB}* 2>/dev/null | sed 's/^/         /' >&2 || \
            echo "         (none)" >&2
        exit 1
    fi
    echo "Using configured device from config.yaml: $DEVICE"
fi

# ── CONTAINER STARTUP ───────────────────────────────────────────────────────

# Remove existing container (if any)
if "$RUNTIME" inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

# Build the run arg array — MUST match build.sh run_container
run_args=(
    --detach
    --format docker
    --name "$CONTAINER_NAME"
    --network=slirp4netns
    --security-opt label=disable
    --publish "${HOST_PORT}:${INTERNAL_PORT}"
    --publish "${HOST_MATTER_PORT}:${MATTER_INTERNAL_PORT}"
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

# Bluetooth for Matter commissioning (optional)
if [[ -e /dev/hci0 ]]; then
    run_args+=(--device /dev/hci0:/dev/hci0)
fi

# USB device passthrough (skipped for MultiPAN socket mode)
if [[ -z "${SKIP_USB:-}" ]]; then
    real_dev=$(readlink -f "$DEVICE")
    run_args+=(--device "${real_dev}:${real_dev}")
    # If the configured path is a symlink (e.g. /dev/serial/by-id/...),
    # also expose the symlink path so config.yaml's port: works inside.
    if [[ "$DEVICE" != "$real_dev" ]]; then
        run_args+=(--device "${DEVICE}:${DEVICE}")
    fi
fi

# USB bus for USBDEVFS_RESET (lets the container reset the dongle if needed)
if [[ -d /dev/bus/usb ]]; then
    run_args+=(-v /dev/bus/usb:/dev/bus/usb)
fi

echo "Starting $CONTAINER_NAME from $IMAGE_TAG"
"$RUNTIME" run "${run_args[@]}" "$IMAGE_TAG"