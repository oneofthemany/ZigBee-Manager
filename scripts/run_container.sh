#!/bin/bash
# =============================================================================
# ZMM run_container helper — thin wrapper around build.sh's run_container()
#
# Called by upgrade.sh during swap/rollback. Sources build.sh so the run
# arguments (caps, sysctls, volumes, devices) come from a single source of
# truth instead of being duplicated here.
#
# Required env:
#   RUNTIME         — podman or docker
#   IMAGE_TAG       — full image ref to run (e.g. zigbee-matter-manager:2.0.1-amd64)
#   CONTAINER_NAME  — name to give the container
#   DATA_DIR        — persistent data directory (volumes)
#
# Optional env:
#   HOST_PORT        — defaults to 8000
#   HOST_MATTER_PORT — defaults to 5580
#   ZMM_APP_DIR      — where build.sh lives (defaults to /opt/zigbee-matter-manager)
#
# Device resolution:
#   Reads zigbee.port from config.yaml. Must be a /dev/* path or socket://
#   URL. Failure is intentional — there is no auto-detection because the
#   existing system is already configured for a specific device.
# =============================================================================
set -u
set -o pipefail

: "${RUNTIME:?RUNTIME env var required}"
: "${IMAGE_TAG:?IMAGE_TAG env var required}"
: "${CONTAINER_NAME:?CONTAINER_NAME env var required}"
: "${DATA_DIR:?DATA_DIR env var required}"

HOST_PORT="${HOST_PORT:-8000}"
HOST_MATTER_PORT="${HOST_MATTER_PORT:-5580}"
APP_DIR="${ZMM_APP_DIR:-/opt/.zigbee-matter-manager}"
CONFIG_FILE="${DATA_DIR}/config/config.yaml"

# ── DEVICE RESOLUTION FROM config.yaml ──────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config.yaml not found at $CONFIG_FILE" >&2
    echo "       Cannot determine which device to pass through." >&2
    exit 1
fi

DEVICE=$(grep -Po 'port:\s*\K[^\s#]+' "$CONFIG_FILE" | grep -E '^/dev/|^socket://' | head -1)
if [[ -z "$DEVICE" ]]; then
    echo "ERROR: Could not parse zigbee.port from $CONFIG_FILE" >&2
    echo "       Expected a line like: 'port: /dev/ttyACM0' or 'port: /dev/ttyUSB0'" >&2
    echo "       Found these port entries (any section):" >&2
    grep -n "port:" "$CONFIG_FILE" | sed 's/^/         /' >&2
    exit 1
fi

# Export USB_DEVICE for build.sh's run_container() to consume.
# socket:// (MultiPAN bridge) → no /dev passthrough; leave USB_DEVICE empty.
if [[ "$DEVICE" == socket://* ]]; then
    echo "Detected MultiPAN socket mode: $DEVICE"
    export USB_DEVICE=""
elif [[ ! -e "$DEVICE" ]]; then
    echo "ERROR: Configured device $DEVICE does not exist on host" >&2
    echo "       Either plug in the dongle or update zigbee.port in config.yaml." >&2
    echo "       Available tty devices on host:" >&2
    ls /dev/tty{ACM,USB}* 2>/dev/null | sed 's/^/         /' >&2 || \
        echo "         (none)" >&2
    exit 1
else
    export USB_DEVICE="$DEVICE"
    echo "Using configured device from config.yaml: $DEVICE"
fi

# ── SOURCE build.sh AND DELEGATE ─────────────────────────────────────────────
# build.sh exposes run_container(host_port, host_matter_port, image_tag).
# It reads CONTAINER_NAME, RUNTIME, DATA_DIR, USB_DEVICE from the environment.
if [[ ! -f "${APP_DIR}/build.sh" ]]; then
    echo "ERROR: build.sh not found at ${APP_DIR}/build.sh" >&2
    echo "       Set ZMM_APP_DIR if your install lives elsewhere." >&2
    exit 1
fi

# Re-export anything build.sh's run_container() reads from globals.
export RUNTIME CONTAINER_NAME DATA_DIR

# shellcheck disable=SC1091
source "${APP_DIR}/build.sh"

run_container "$HOST_PORT" "$HOST_MATTER_PORT" "$IMAGE_TAG"