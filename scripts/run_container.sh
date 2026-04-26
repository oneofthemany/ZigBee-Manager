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

# ── DEVICE RESOLUTION ───────────────────────────────────────────────────────
# Priority order:
#   1. $USB_DEVICE env var (explicit override — set in systemd unit, etc.)
#   2. zigbee.port from config.yaml (USER's configured choice — primary source)
#   3. Auto-detect via /dev/serial/by-id/ (with name pattern matching)
#   4. Auto-detect first /dev/serial/by-id/ entry (last-resort)
#   5. Auto-detect raw /dev/ttyACM[0-9] / /dev/ttyUSB[0-9]
#
# If nothing found, fail loudly — silent omission causes confusing
# "adapter not detected" errors minutes later inside the container.

# Step 2: read zigbee.port from config.yaml if no override
if [[ -z "${USB_DEVICE:-}" && -f "${DATA_DIR}/config/config.yaml" ]]; then
    # Best-effort YAML parse without yq dependency. Look for:
    #   zigbee:
    #     port: /dev/ttyUSB0
    # Stop scanning when we leave the zigbee: stanza (next top-level key).
    configured_port=$(awk '
        /^zigbee:/         { in_zigbee=1; next }
        /^[a-zA-Z]/        { in_zigbee=0 }
        in_zigbee && /^  port:/ {
            # Strip "  port:", quotes, trailing comments, whitespace
            sub(/^  port:[[:space:]]*/, "")
            sub(/[[:space:]]*#.*$/, "")
            gsub(/^["'"'"']|["'"'"']$/, "")
            print
            exit
        }
    ' "${DATA_DIR}/config/config.yaml" 2>/dev/null)

    # Only use if it's a real local device path (not a socket:// URL)
    if [[ -n "$configured_port" && "$configured_port" == /dev/* ]]; then
        if [[ -e "$configured_port" ]]; then
            USB_DEVICE="$configured_port"
            echo "Using configured device from config.yaml: $USB_DEVICE"
        else
            echo "WARN: config.yaml says zigbee.port=$configured_port but that device does not exist" >&2
            echo "WARN: Falling back to auto-detection..." >&2
        fi
    elif [[ "$configured_port" == socket://* ]]; then
        # MultiPAN mode — no USB device passthrough needed; zigbeed handles it
        echo "Detected MultiPAN socket configuration (zigbee.port=$configured_port)"
        echo "Container will use the host-side cpcd/zigbeed bridge — no /dev passthrough"
        # Leave USB_DEVICE empty; we'll handle this case below
        SKIP_USB=1
    fi
fi

# Steps 3-5: auto-detect if still no device
if [[ -z "${USB_DEVICE:-}" && -z "${SKIP_USB:-}" ]]; then
    # Pass 1: known substrings in by-id name
    if [[ -d /dev/serial/by-id ]]; then
        for dev in /dev/serial/by-id/*; do
            [[ -e "$dev" ]] || continue
            label=$(basename "$dev")
            if echo "$label" | grep -qiE 'cp210|ezsp|zigbee|silabs|ember|ch340|ch341|cc253|cc265|conbee|raspbee|sonoff|tube|slzb|zzh|itead|skyconnect|nabucasa'; then
                USB_DEVICE=$(readlink -f "$dev")
                echo "Auto-detected USB device by name: $dev -> $USB_DEVICE"
                break
            fi
        done
    fi

    # Pass 2: any /dev/serial/by-id entry (last-resort heuristic)
    if [[ -z "${USB_DEVICE:-}" && -d /dev/serial/by-id ]]; then
        for dev in /dev/serial/by-id/*; do
            [[ -e "$dev" ]] || continue
            USB_DEVICE=$(readlink -f "$dev")
            echo "Auto-detected USB device (first available): $dev -> $USB_DEVICE"
            break
        done
    fi

    # Pass 3: raw device node fallback (no by-id available)
    if [[ -z "${USB_DEVICE:-}" ]]; then
        for candidate in /dev/ttyACM0 /dev/ttyACM1 /dev/ttyACM2 /dev/ttyUSB0 /dev/ttyUSB1 /dev/ttyUSB2; do
            if [[ -e "$candidate" ]]; then
                USB_DEVICE="$candidate"
                echo "Auto-detected USB device (raw node): $USB_DEVICE"
                break
            fi
        done
    fi
fi

# Fail loudly if no device found AND we're not in MultiPAN socket mode
if [[ -z "${USB_DEVICE:-}" && -z "${SKIP_USB:-}" ]]; then
    echo "ERROR: No Zigbee dongle found." >&2
    echo "       config.yaml zigbee.port: ${configured_port:-(not set)}" >&2
    echo "       Searched: /dev/serial/by-id/, /dev/ttyACM[0-2], /dev/ttyUSB[0-2]" >&2
    echo "       Pass USB_DEVICE=/path/to/dongle as an environment variable to override." >&2
    if [[ -d /dev/serial/by-id ]]; then
        echo "       Available by-id entries:" >&2
        ls /dev/serial/by-id/ 2>&1 | sed 's/^/         /' >&2
    fi
    echo "       Available raw tty nodes:" >&2
    ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | sed 's/^/         /' >&2 || \
        echo "         (none)" >&2
    exit 1
fi

if [[ -n "${USB_DEVICE:-}" ]]; then
    echo "Using USB device: $USB_DEVICE"
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