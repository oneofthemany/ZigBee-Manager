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
#   USB_DEVICE      — auto-detected if unset
#   HOST_PORT       — defaults to 8000
#   HOST_MATTER_PORT — defaults to 5580
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

# Try to auto-detect USB device if not provided
if [[ -z "${USB_DEVICE:-}" ]]; then
    if [[ -d /dev/serial/by-id ]]; then
        for dev in /dev/serial/by-id/*; do
            [[ -e "$dev" ]] || continue
            label=$(basename "$dev")
            if echo "$label" | grep -qiE 'cp210|ezsp|zigbee|silabs|ember|ch340|ch341|cc253|cc265|conbee|raspbee|sonoff|tube|slzb|zzh'; then
                USB_DEVICE=$(readlink -f "$dev")
                break
            fi
        done
    fi
fi

# Remove existing container (if any)
if "$RUNTIME" inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
fi

# Build the run arg array — MUST match build.sh run_container
run_args=(
    --detach
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

# Bluetooth for Matter commissioning
if [[ -e /dev/hci0 ]]; then
    run_args+=(--device /dev/hci0:/dev/hci0)
fi

# USB device
if [[ -n "${USB_DEVICE:-}" ]]; then
    real_dev=$(readlink -f "$USB_DEVICE")
    run_args+=(--device "${real_dev}:${real_dev}")
    if [[ "$USB_DEVICE" != "$real_dev" ]]; then
        run_args+=(--device "${USB_DEVICE}:${USB_DEVICE}")
    fi
fi

# USB bus for USBDEVFS_RESET
if [[ -d /dev/bus/usb ]]; then
    run_args+=(-v /dev/bus/usb:/dev/bus/usb)
fi

echo "Starting $CONTAINER_NAME from $IMAGE_TAG"
"$RUNTIME" run "${run_args[@]}" "$IMAGE_TAG"