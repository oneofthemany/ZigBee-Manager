#!/bin/bash
# =============================================================================
# ZMM Upgrade — Host-Side Orchestrator
#
# Reads trigger files written by the running container and performs:
#   build     — clone target tag, build new image tagged with version
#   swap      — stop current container, rename, run new image, health-check
#   rollback  — swap back to previous image
#   cancel    — best-effort kill of in-progress build
#   gc        — prune old images per retention count
#
# Runs on the host (user or root systemd, or fallback polling wrapper).
# NEVER runs inside the container. Has full access to podman/docker.
#
# Works with Podman (preferred) and Docker, rootless or root.
# =============================================================================
set -u  # NOTE: not -e; we want to catch errors and report them cleanly.
set -o pipefail

# ── CONFIG ───────────────────────────────────────────────────────────────────
DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
APP_DIR="${ZMM_APP_DIR:-/opt/zigbee-matter-manager}"
IMAGE_NAME="${ZMM_IMAGE_NAME:-zigbee-matter-manager}"
CONTAINER_NAME="${ZMM_CONTAINER_NAME:-zigbee-matter-manager}"
REPO_URL="${ZMM_REPO_URL:-https://github.com/oneofthemany/ZigBee-Matter-Manager.git}"
HEALTH_TIMEOUT="${ZMM_HEALTH_TIMEOUT:-60}"  # seconds to wait for new container to become healthy

# Health check URL is auto-detected from config.yaml at health-check time —
# see detect_health_url(). Override with $ZMM_HEALTH_URL if needed.
HEALTH_URL="${ZMM_HEALTH_URL:-}"
# The port published by the previous container — discovered from inspect if possible.

# ── IPC paths (shared with container via volume mount) ───────────────────────
UPGRADE_DIR="${DATA_DIR}/data/upgrade"
TRIGGER_FILE="${UPGRADE_DIR}/trigger"
STATUS_FILE="${UPGRADE_DIR}/status.json"
BUILD_LOG="${UPGRADE_DIR}/build.log"
LOCK_FILE="${UPGRADE_DIR}/lock"
WATCHER_MARKER="${UPGRADE_DIR}/.watcher_installed"

# State file used by the app (read-only for us, but we update current/previous on swap)
STATE_DIR="${DATA_DIR}/data/state"
VERSION_STATE_FILE="${STATE_DIR}/version.json"

# Log for the watcher itself (separate from build.log)
WATCHER_LOG="${DATA_DIR}/logs/upgrade_watcher.log"

mkdir -p "$UPGRADE_DIR" "$STATE_DIR" "$(dirname "$WATCHER_LOG")"

# ── LOGGING ──────────────────────────────────────────────────────────────────
log() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "[$ts] $*" | tee -a "$WATCHER_LOG" >&2
}

log_to_build() {
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    echo "[$ts] $*" | tee -a "$BUILD_LOG" >&2
}

# ── RUNTIME DETECTION (rootless podman / root podman / docker) ───────────────
detect_runtime() {
    if [[ -n "${RUNTIME:-}" ]]; then
        command -v "$RUNTIME" &>/dev/null || { log "RUNTIME $RUNTIME not found"; return 1; }
    elif command -v podman &>/dev/null; then
        RUNTIME="podman"
    elif command -v docker &>/dev/null; then
        RUNTIME="docker"
    else
        log "ERROR: Neither podman nor docker found in PATH"
        return 1
    fi
    log "Using container runtime: $RUNTIME"
}

# ── STATUS WRITER ────────────────────────────────────────────────────────────
write_status() {
    local state="$1"
    local target_version="${2:-null}"
    local progress="${3:-0}"
    local step="${4:-}"
    local err="${5:-}"
    local started_at="${6:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"

    # Quote target_version correctly
    local tv_json
    if [[ "$target_version" == "null" || -z "$target_version" ]]; then
        tv_json="null"
    else
        tv_json="\"${target_version}\""
    fi

    local err_json
    if [[ -z "$err" ]]; then
        err_json="null"
    else
        # escape quotes
        err_json="\"$(echo "$err" | sed 's/\\/\\\\/g; s/"/\\"/g')\""
    fi

    local step_json
    step_json="\"$(echo "$step" | sed 's/\\/\\\\/g; s/"/\\"/g')\""

    local tmp="${STATUS_FILE}.tmp"
    cat > "$tmp" <<JSON
{
  "state": "${state}",
  "target_version": ${tv_json},
  "started_at": "${started_at}",
  "updated_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "progress_percent": ${progress},
  "current_step": ${step_json},
  "error": ${err_json}
}
JSON
    mv "$tmp" "$STATUS_FILE"
}

# ── LOCKING ──────────────────────────────────────────────────────────────────
# A stale lock from a killed/crashed previous run will block ALL future runs
# unless we detect-and-clear it. Lock file format: "PID TIMESTAMP ACTION"
acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local held_pid
        held_pid=$(awk '{print $1}' "$LOCK_FILE" 2>/dev/null || echo "")
        local held
        held=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")

        # Is the holder still alive?
        if [[ -n "$held_pid" ]] && kill -0 "$held_pid" 2>/dev/null; then
            log "Lock held by live PID $held_pid: $held"
            return 1
        fi

        # Stale lock — clear it
        log "Removing stale lock (PID $held_pid not running): $held"
        rm -f "$LOCK_FILE"
    fi
    echo "$$ $(date -u +"%Y-%m-%dT%H:%M:%SZ") $1" > "$LOCK_FILE"
    return 0
}

release_lock() {
    rm -f "$LOCK_FILE"
}

# ── TRIGGER CONSUMPTION ──────────────────────────────────────────────────────
# CRITICAL: we MUST delete the trigger file before doing anything else.
# Otherwise the systemd-path unit re-fires us in a tight loop.
consume_trigger() {
    if [[ ! -f "$TRIGGER_FILE" ]]; then
        return 1
    fi

    # Read contents into memory FIRST, then delete the file.
    # If we crash after this, the path unit won't re-fire because the file is gone.
    local trigger_content
    trigger_content=$(cat "$TRIGGER_FILE" 2>/dev/null || echo "")
    rm -f "$TRIGGER_FILE"

    if [[ -z "$trigger_content" ]]; then
        log "Empty trigger file; ignoring"
        return 1
    fi

    if ! command -v jq &>/dev/null; then
        log "ERROR: jq is required for the upgrade watcher"
        write_status "failed" "" 0 "" "jq not installed on host"
        return 1
    fi

    TRIGGER_ACTION=$(echo "$trigger_content" | jq -r '.action // empty' 2>/dev/null || echo "")
    TRIGGER_PAYLOAD=$(echo "$trigger_content" | jq -c '.payload // {}' 2>/dev/null || echo "{}")

    if [[ -z "$TRIGGER_ACTION" ]]; then
        log "Malformed trigger (no action)"
        write_status "failed" "" 0 "" "Malformed trigger file"
        return 1
    fi

    log "Consumed trigger: action=$TRIGGER_ACTION payload=$TRIGGER_PAYLOAD"
    return 0
}

# ── ARCHITECTURE DETECTION ───────────────────────────────────────────────────
detect_arch() {
    local m
    m=$(uname -m)
    case "$m" in
        x86_64|amd64)           echo "amd64" ;;
        aarch64|arm64)          echo "arm64" ;;
        armv7l|armv7)           echo "armv7" ;;
        *)                       echo "$m" ;;
    esac
}

# ── HEALTH CHECK URL DETECTION ──────────────────────────────────────────────
# The new container may be running plain HTTP, or HTTPS (with a self-signed
# cert). config.yaml tells us which — but we read it defensively because
# config.yaml shape can vary across versions.
#
# We build a list of candidate URLs to try, in priority order:
#   1. $ZMM_HEALTH_URL (if set explicitly — overrides everything)
#   2. https://127.0.0.1:${port}/api/system/health   (if web.ssl.enabled is true)
#   3. http://127.0.0.1:${port}/api/system/health    (fallback for non-SSL setups)
#
# is_app_healthy() returns 0 if ANY of the candidates returns 200.
detect_health_urls() {
    local config="${DATA_DIR}/config/config.yaml"
    local port="8000"
    local ssl_enabled="false"

    # If user has set ZMM_HEALTH_URL, use only that
    if [[ -n "${HEALTH_URL:-}" ]]; then
        echo "$HEALTH_URL"
        return 0
    fi

    # Best-effort YAML parsing without yq dependency. Look for:
    #   web:
    #     port: 8000
    #     ssl:
    #       enabled: true            (or "enabled", "yes", etc.)
    #       certfile: ./...          (or cert_file: with underscore)
    #       keyfile: ./...           (or key_file: with underscore)
    #
    # Accept both naming conventions because real configs use either.
    # Treat "enabled" as truthy unless the value is explicitly falsy.
    if [[ -f "$config" ]]; then
        # Extract port (anywhere under "web:" stanza). Keep simple — first hit wins.
        local p
        p=$(awk '
            /^web:/         { in_web=1; next }
            /^[a-zA-Z]/     { in_web=0 }
            in_web && /^  port:/ { gsub(/[^0-9]/,"",$2); print $2; exit }
        ' "$config" 2>/dev/null)
        [[ -n "$p" && "$p" =~ ^[0-9]+$ ]] && port="$p"

        # Extract ssl.enabled. Look for the nested key.
        local s
        s=$(awk '
            /^web:/         { in_web=1; next }
            /^[a-zA-Z]/     { in_web=0; in_ssl=0 }
            in_web && /^  ssl:/ { in_ssl=1; next }
            in_web && /^  [a-zA-Z]/ { in_ssl=0 }
            in_web && in_ssl && /^    enabled:/ { print $2; exit }
        ' "$config" 2>/dev/null | tr -d '"' | tr -d "'" | tr '[:upper:]' '[:lower:]')
        # Truthy unless explicitly falsy. "enabled" itself is truthy.
        case "$s" in
            ""|false|no|0|off|disabled|none|null)
                ssl_enabled="false"
                ;;
            *)
                ssl_enabled="true"
                ;;
        esac
    fi

    # Output candidate URLs, one per line, in priority order.
    # We only check /api/system/health — the canonical health endpoint.
    if [[ "$ssl_enabled" == "true" ]]; then
        echo "https://127.0.0.1:${port}/api/system/health"
    fi
    echo "http://127.0.0.1:${port}/api/system/health"
}

# Try each candidate URL once. Returns 0 if any succeeds, prints the URL that
# worked to stdout (so caller can log it).
is_app_healthy() {
    local urls=("$@")
    for url in "${urls[@]}"; do
        # -k: accept self-signed certs. Most home setups use them.
        # --max-time 3: don't wait more than 3s per URL per attempt.
        # -fsS: silent, fail-on-non-2xx, but show error if curl itself fails.
        if curl -fsS -k --max-time 3 "$url" >/dev/null 2>&1; then
            echo "$url"
            return 0
        fi
    done
    return 1
}

# ── HELPER LOCATION ─────────────────────────────────────────────────────────
# Locate run_container.sh in the canonical location ${APP_DIR}/scripts/.
# Falls back to legacy ${DATA_DIR}/scripts/ for older installs that ran
# install_watcher.sh before the single-source-of-truth migration.
find_run_helper() {
    for candidate in \
        "${APP_DIR}/scripts/run_container.sh" \
        "${DATA_DIR}/scripts/run_container.sh"; do
        if [[ -n "$candidate" && -f "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# ── PORT FREE WAITER ────────────────────────────────────────────────────────
# After podman stop, the host TCP sockets can stay in TIME_WAIT for up to ~60s,
# AND rogue child processes (or rootlessport itself) may still be holding the
# port. We poll until the ports are actually bindable, with a timeout.
#
# Returns 0 if all ports become free within $timeout seconds, 1 otherwise.
wait_for_ports_free() {
    local timeout="${1:-90}"
    local elapsed=0
    local sleep_interval=2
    local ports=("8000" "5580")
    local stable_required=2  # require N consecutive checks to pass before declaring free
    local stable_count=0

    # Initial settle delay — after SIGKILL, rootlessport needs ~1-2s to fully
    # release its sockets before they appear truly free. The kernel may
    # report no-LISTEN before the socket is actually bindable.
    sleep 1

    while (( elapsed < timeout )); do
        local all_free=1

        # Check 1: Are any sockets listening or in active states on these ports?
        # We check ANY state (not just LISTEN) because TIME_WAIT/CLOSE_WAIT also
        # block bind. ss with -a includes all states.
        for port in "${ports[@]}"; do
            if command -v ss >/dev/null 2>&1; then
                # Look for any non-empty result on either IPv4 or IPv6
                if ss -tan "( sport = :$port or dport = :$port )" 2>/dev/null | \
                   awk 'NR>1 && $1!="LISTEN" {found=1} END {exit !found}' >/dev/null 2>&1; then
                    all_free=0
                    break
                fi
                if ss -ltn "( sport = :$port )" 2>/dev/null | grep -q LISTEN; then
                    all_free=0
                    break
                fi
            elif command -v netstat >/dev/null 2>&1; then
                if netstat -tan 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"; then
                    all_free=0
                    break
                fi
            fi
        done

        # Check 2: Definitive test — try to actually bind to each port
        # Only do this if Check 1 says ports look free, otherwise it's wasted effort
        if (( all_free == 1 )); then
            for port in "${ports[@]}"; do
                # Use python or perl to attempt a real bind. Falls through to
                # a /dev/tcp probe if neither is available.
                if command -v python3 >/dev/null 2>&1; then
                    if ! python3 -c "
import socket, sys
for af in (socket.AF_INET, socket.AF_INET6):
    try:
        s = socket.socket(af, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('::' if af == socket.AF_INET6 else '0.0.0.0', $port))
        s.close()
    except OSError:
        sys.exit(1)
" 2>/dev/null; then
                        all_free=0
                        break
                    fi
                fi
            done
        fi

        if (( all_free == 1 )); then
            stable_count=$((stable_count + 1))
            if (( stable_count >= stable_required )); then
                log "Ports ${ports[*]} are free and bindable (waited ${elapsed}s, ${stable_count} stable checks)"
                return 0
            fi
        else
            stable_count=0
        fi

        sleep "$sleep_interval"
        elapsed=$((elapsed + sleep_interval))
    done

    log "WARN: Ports still in use after ${timeout}s — proceeding anyway"
    return 1
}

# Best-effort kill of any host processes holding the ports we need.
# Used as a last resort if wait_for_ports_free times out.
kill_port_squatters() {
    local ports=("$@")
    if [[ ${#ports[@]} -eq 0 ]]; then
        ports=("8000" "5580")
    fi
    for port in "${ports[@]}"; do
        local pids
        if command -v ss >/dev/null 2>&1; then
            pids=$(ss -ltnp "( sport = :$port )" 2>/dev/null | \
                   grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
        elif command -v fuser >/dev/null 2>&1; then
            pids=$(fuser -n tcp "$port" 2>/dev/null | tr -d ' ')
        else
            pids=""
        fi

        for pid in $pids; do
            # Sanity: never kill PID 1 or systemd
            if [[ "$pid" == "1" ]] || [[ "$pid" == "$$" ]]; then
                continue
            fi
            log "Killing port-squatter PID $pid on port $port"
            kill -TERM "$pid" 2>/dev/null || true
            sleep 1
            kill -KILL "$pid" 2>/dev/null || true
        done
    done
}

# ── BUILD: clone target tag, build image, tag with version ──────────────────
do_build() {
    local target_version
    target_version=$(echo "$TRIGGER_PAYLOAD" | jq -r '.target_version // empty')
    if [[ -z "$target_version" ]]; then
        log "Build: no target_version in payload"
        write_status "failed" "" 0 "" "No target_version specified"
        return 1
    fi

    local arch
    arch=$(detect_arch)
    local new_tag="${IMAGE_NAME}:${target_version}-${arch}"
    local started_at
    started_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # Reset build log
    : > "$BUILD_LOG"
    log_to_build "=== ZMM Upgrade Build ==="
    log_to_build "Target version: $target_version"
    log_to_build "Architecture:   $arch"
    log_to_build "Target tag:     $new_tag"
    log_to_build "Runtime:        $RUNTIME"
    log_to_build ""

    write_status "building" "$target_version" 5 "Preparing" "" "$started_at"

    # Clone target tag to a temp dir
    local work_dir
    work_dir="${DATA_DIR}/upgrade_build"
    rm -rf "$work_dir"
    mkdir -p "$work_dir"

    log_to_build "Cloning $REPO_URL at tag v${target_version}..."
    write_status "building" "$target_version" 10 "Cloning repository" "" "$started_at"

    if ! git clone --depth 1 --branch "v${target_version}" "$REPO_URL" "$work_dir" >>"$BUILD_LOG" 2>&1; then
        log_to_build "ERROR: git clone failed for tag v${target_version}"
        # Try without the 'v' prefix as a fallback
        rm -rf "$work_dir"
        mkdir -p "$work_dir"
        if ! git clone --depth 1 --branch "${target_version}" "$REPO_URL" "$work_dir" >>"$BUILD_LOG" 2>&1; then
            log_to_build "ERROR: git clone failed for tag ${target_version} as well"
            write_status "failed" "$target_version" 10 "Clone failed" "git clone failed for tag v${target_version}" "$started_at"
            return 1
        fi
    fi

    # Stamp VERSION file into the clone so the image knows its own version
    echo "$target_version" > "$work_dir/VERSION"

    # Ensure Containerfile exists — if the tag pre-dates migration, use a fallback.
    # In your repo the Containerfile is written by build.sh at deploy time; for
    # upgrade, we reuse the current local Containerfile if present, otherwise
    # try to regenerate via build.sh's write_containerfile.
    if [[ ! -f "$work_dir/Containerfile" ]]; then
        if [[ -f "$APP_DIR/Containerfile" ]]; then
            log_to_build "Reusing existing Containerfile from $APP_DIR"
            cp "$APP_DIR/Containerfile" "$work_dir/Containerfile"
        elif [[ -f "$work_dir/build.sh" ]]; then
            log_to_build "Generating Containerfile by invoking target tag's build.sh in write-only mode"
            # Source build.sh's write_containerfile with APP_DIR=work_dir.
            # This is fragile; we isolate it by running in a subshell with set +e.
            (
                set +u
                APP_DIR="$work_dir"
                # shellcheck disable=SC1090
                source "$work_dir/build.sh" --help >/dev/null 2>&1 || true
                type write_containerfile >/dev/null 2>&1 && write_containerfile
            ) >>"$BUILD_LOG" 2>&1 || true
        fi
    fi

    if [[ ! -f "$work_dir/Containerfile" ]]; then
        log_to_build "ERROR: No Containerfile found or generatable"
        write_status "failed" "$target_version" 15 "Containerfile missing" "No Containerfile in tag v${target_version} and none to reuse" "$started_at"
        return 1
    fi

    # Detect build jobs
    local build_jobs
    if command -v nproc >/dev/null 2>&1; then
        build_jobs=$(nproc)
    else
        build_jobs=2
    fi
    (( build_jobs > 8 )) && build_jobs=8

    log_to_build ""
    log_to_build "Building image with $build_jobs parallel jobs..."
    log_to_build "Build time depends on host hardware (typically ~2-25 minutes)."
    log_to_build ""

    write_status "building" "$target_version" 20 "Compiling image (varies by host hardware)" "" "$started_at"

    # Run the build
    if ! "$RUNTIME" build \
            --format docker \
            --build-arg BUILD_JOBS="$build_jobs" \
            --tag "$new_tag" \
            --file "$work_dir/Containerfile" \
            "$work_dir" >>"$BUILD_LOG" 2>&1
    then
        log_to_build ""
        log_to_build "ERROR: Image build failed. See log above."
        write_status "failed" "$target_version" 50 "Build failed" "$RUNTIME build returned non-zero; see build.log" "$started_at"
        return 1
    fi

    # Also tag as :latest-<arch> for convenience
    "$RUNTIME" tag "$new_tag" "${IMAGE_NAME}:latest-${arch}" >>"$BUILD_LOG" 2>&1 || true

    log_to_build ""
    log_to_build "Build complete. Image tagged as $new_tag"
    log_to_build "Container swap has NOT happened yet. Swap is a separate action."

    # Clean up the clone
    rm -rf "$work_dir"

    write_status "ready_to_swap" "$target_version" 100 "Image ready — awaiting swap" "" "$started_at"
    return 0
}

# ── SUPERVISOR UNIT HANDLING ─────────────────────────────────────────────────
ZMM_CONTAINER_UNIT="${ZMM_CONTAINER_UNIT:-}"
UNIT_WAS_MASKED=0
UNIT_OVERRIDE_DIR=""

detect_container_unit() {
    if [[ -n "$ZMM_CONTAINER_UNIT" ]]; then
        echo "$ZMM_CONTAINER_UNIT"
        return 0
    fi
    local candidates=(
        "container-${CONTAINER_NAME}.service"
        "${CONTAINER_NAME}.service"
    )
    # System scope first (we run as root). User scope only if explicitly non-root.
    for unit in "${candidates[@]}"; do
        if systemctl --system cat "$unit" >/dev/null 2>&1; then
            echo "--system $unit"
            return 0
        fi
    done
    if [[ "$(id -u)" -ne 0 ]]; then
        for unit in "${candidates[@]}"; do
            if systemctl --user cat "$unit" >/dev/null 2>&1; then
                echo "--user $unit"
                return 0
            fi
        done
    fi
    return 1
}

# Drop a runtime override that disables Restart= for the swap window, then
# stop the unit. Pair with unmask_unit_if_needed.
container_unit_mask_and_stop() {
    local unit_desc; unit_desc=$(detect_container_unit) || {
        log "Supervisor: no unit detected (continuing without override)"
        return 0
    }
    local scope unit
    read -r scope unit <<< "$unit_desc"
    log "Supervisor: disabling auto-restart on $scope $unit (runtime drop-in)"

    local override_dir
    if [[ "$scope" == "--system" ]]; then
        override_dir="/run/systemd/system/${unit}.d"
    else
        override_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/systemd/user/${unit}.d"
    fi
    mkdir -p "$override_dir"
    cat > "${override_dir}/zzz-zmm-upgrade-norestart.conf" <<EOF
[Service]
Restart=no
EOF
    systemctl "$scope" daemon-reload >>"$WATCHER_LOG" 2>&1 || true
    UNIT_WAS_MASKED=1
    UNIT_OVERRIDE_DIR="$override_dir"

    systemctl "$scope" stop "$unit" >>"$WATCHER_LOG" 2>&1 || true
}

# Remove the runtime override if we placed one. Idempotent.
unmask_unit_if_needed() {
    if [[ "${UNIT_WAS_MASKED:-0}" == "1" ]]; then
        local unit_desc; unit_desc=$(detect_container_unit) || { UNIT_WAS_MASKED=0; return 0; }
        local scope unit
        read -r scope unit <<< "$unit_desc"
        log "Supervisor: removing auto-restart override on $scope $unit"

        if [[ -n "${UNIT_OVERRIDE_DIR:-}" && -d "$UNIT_OVERRIDE_DIR" ]]; then
            rm -f "${UNIT_OVERRIDE_DIR}/zzz-zmm-upgrade-norestart.conf"
            rmdir "$UNIT_OVERRIDE_DIR" 2>/dev/null || true
        fi
        systemctl "$scope" daemon-reload >>"$WATCHER_LOG" 2>&1 || true
        systemctl "$scope" reset-failed "$unit" >/dev/null 2>&1 || true
        UNIT_WAS_MASKED=0
        UNIT_OVERRIDE_DIR=""
    fi
}

# Start the supervisor unit. Call only after unmask_unit_if_needed.
container_unit_start() {
    local unit_desc; unit_desc=$(detect_container_unit) || return 0
    local scope unit
    read -r scope unit <<< "$unit_desc"
    log "Supervisor: starting $scope $unit"
    systemctl "$scope" start "$unit" >>"$WATCHER_LOG" 2>&1 || \
        log "Supervisor: warn — start failed for $scope $unit"
}

# ── SWAP: stop old container, rename, run new ────────────────────────────────
# Linear flow:
#   1. Mask + stop supervisor (so it can't auto-restart the old container)
#   2. Stop the old container, rename to -previous
#   3. Start new container
#   4. Health check
#   5. On failure → rollback. On success → unmask + start supervisor.
do_swap() {
    local target_version
    target_version=$(echo "$TRIGGER_PAYLOAD" | jq -r '.target_version // empty')
    if [[ -z "$target_version" ]]; then
        write_status "failed" "" 0 "Swap failed" "No target_version in swap payload"
        return 1
    fi

    local arch new_tag
    arch=$(detect_arch)
    new_tag="${IMAGE_NAME}:${target_version}-${arch}"

    if ! "$RUNTIME" image inspect "$new_tag" >/dev/null 2>&1; then
        write_status "failed" "$target_version" 0 "Swap failed" "Image $new_tag not found — build first"
        return 1
    fi

    if ! "$RUNTIME" inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        write_status "failed" "$target_version" 0 "Swap failed" "Current container $CONTAINER_NAME not found"
        return 1
    fi

    log "Swap: starting for v$target_version"

    # Capture current state for rollback
    local current_image_tag current_version previous_name
    current_image_tag=$("$RUNTIME" inspect -f '{{.ImageName}}' "$CONTAINER_NAME" 2>/dev/null || echo "")
    if [[ -z "$current_image_tag" || "$current_image_tag" == "<nil>" ]]; then
        local current_image
        current_image=$("$RUNTIME" inspect -f '{{.Image}}' "$CONTAINER_NAME" 2>/dev/null)
        current_image_tag=$("$RUNTIME" image inspect --format '{{index .RepoTags 0}}' "$current_image" 2>/dev/null || echo "${IMAGE_NAME}:latest")
    fi
    current_version=$("$RUNTIME" exec "$CONTAINER_NAME" cat /app/VERSION 2>/dev/null | tr -d '[:space:]' || echo "unknown")
    previous_name="${CONTAINER_NAME}-previous"

    log "Swap: current image = $current_image_tag (version $current_version)"
    log "Swap: new image     = $new_tag (version $target_version)"

    # ── STEP 1: Mask the supervisor for the entire swap window ───────────────
    write_status "swapping" "$target_version" 20 "Disabling supervisor auto-restart"
    container_unit_mask_and_stop

    # ── STEP 2: Stop and rename the old container ────────────────────────────
    write_status "swapping" "$target_version" 35 "Stopping current container"
    log "Swap: stopping $CONTAINER_NAME (45s graceful)"
    if ! "$RUNTIME" stop -t 45 "$CONTAINER_NAME" >>"$WATCHER_LOG" 2>&1; then
        log "Swap: stop returned non-zero (continuing)"
    fi

    write_status "swapping" "$target_version" 45 "Renaming old container to -previous"
    "$RUNTIME" rm -f "$previous_name" >/dev/null 2>&1 || true
    if ! "$RUNTIME" rename "$CONTAINER_NAME" "$previous_name" >>"$WATCHER_LOG" 2>&1; then
        log "Swap: rename failed — removing old container instead"
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    fi

    # ── STEP 3: Start the new container ──────────────────────────────────────
    local run_helper
    if ! run_helper=$(find_run_helper); then
        log "Swap: run_container.sh not found in any known location"
        write_status "failed" "$target_version" 50 "Swap failed" "run_container.sh helper not installed"
        # Restore old container
        "$RUNTIME" rename "$previous_name" "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" start "$CONTAINER_NAME" >/dev/null 2>&1 || true
        unmask_unit_if_needed
        container_unit_start
        return 1
    fi

    write_status "swapping" "$target_version" 60 "Starting new container"
    log "Swap: starting new container from $new_tag via $run_helper"

    log_to_build ""
    log_to_build "=== Starting new container ==="
    log_to_build "Image: $new_tag"
    log_to_build "Helper: $run_helper"
    log_to_build ""

    if ! RUNTIME="$RUNTIME" \
         IMAGE_TAG="$new_tag" \
         CONTAINER_NAME="$CONTAINER_NAME" \
         DATA_DIR="$DATA_DIR" \
         bash "$run_helper" 2>&1 | tee -a "$BUILD_LOG" >>"$WATCHER_LOG"
    then
        log "Swap: new container failed to start — rolling back"

        log_to_build ""
        log_to_build "=== NEW CONTAINER FAILED TO START — capturing logs ==="
        "$RUNTIME" logs --tail=100 "$CONTAINER_NAME" >>"$BUILD_LOG" 2>&1 || \
            log_to_build "(no logs available — container did not exist or runtime returned error)"
        log_to_build ""
        log_to_build "=== Failed container inspect ==="
        "$RUNTIME" inspect "$CONTAINER_NAME" 2>>"$BUILD_LOG" | head -100 >>"$BUILD_LOG" || true

        write_status "rolling_back" "$target_version" 70 "New container failed — rolling back"
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" rename "$previous_name" "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" start "$CONTAINER_NAME" >/dev/null 2>&1 || true
        unmask_unit_if_needed
        container_unit_start
        write_status "failed" "$target_version" 100 "Rolled back" "New container failed to start; old container restored. See build.log for details."
        return 1
    fi

    # ── STEP 4: Health check ─────────────────────────────────────────────────
    write_status "swapping" "$target_version" 80 "Health-checking new container"

    local health_candidates=()
    while IFS= read -r url; do
        [[ -n "$url" ]] && health_candidates+=("$url")
    done < <(detect_health_urls)

    log "Swap: health-check candidates: ${health_candidates[*]}"
    log "Swap: waiting up to ${HEALTH_TIMEOUT}s for new container to become healthy"

    local healthy=0 elapsed=0 working_url=""
    while (( elapsed < HEALTH_TIMEOUT )); do
        if working_url=$(is_app_healthy "${health_candidates[@]}"); then
            healthy=1
            log "Swap: health check passed via $working_url"
            break
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done

    if (( healthy == 0 )); then
        log "Swap: health check failed — rolling back"
        write_status "rolling_back" "$target_version" 90 "Health check failed — rolling back"

        log_to_build ""
        log_to_build "=== HEALTH CHECK FAILED — capturing failed container logs ==="
        log_to_build "Tried URLs: ${health_candidates[*]}"
        "$RUNTIME" logs --tail=100 "$CONTAINER_NAME" >>"$BUILD_LOG" 2>&1 || \
            log_to_build "(no container logs available)"

        "$RUNTIME" stop -t 10 "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" rename "$previous_name" "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" start "$CONTAINER_NAME" >/dev/null 2>&1 || true
        unmask_unit_if_needed
        container_unit_start
        local tried="${health_candidates[*]}"
        write_status "failed" "$target_version" 100 "Rolled back after health failure" "New container did not respond at any of: ${tried} within ${HEALTH_TIMEOUT}s. See build.log for the new container's startup log."
        return 1
    fi

    # ── STEP 5: Success ──────────────────────────────────────────────────────
    log "Swap: SUCCESS. New container healthy. Keeping $previous_name for rollback."
    update_version_state "$target_version" "$current_version" "$new_tag" "$current_image_tag"
    unmask_unit_if_needed
    container_unit_start
    write_status "idle" "$target_version" 100 "Upgrade complete" ""
    return 0
}

# ── ROLLBACK: swap back to previous image ────────────────────────────────────
do_rollback() {
    local previous_image_tag
    previous_image_tag=$(echo "$TRIGGER_PAYLOAD" | jq -r '.previous_image_tag // empty')
    local previous_version
    previous_version=$(echo "$TRIGGER_PAYLOAD" | jq -r '.previous_version // empty')

    if [[ -z "$previous_image_tag" ]]; then
        # Fallback: look for -previous container
        if "$RUNTIME" inspect "${CONTAINER_NAME}-previous" >/dev/null 2>&1; then
            log "Rollback: using ${CONTAINER_NAME}-previous"
            write_status "rolling_back" "$previous_version" 20 "Stopping current container"
            container_unit_mask_and_stop
            "$RUNTIME" stop -t 15 "$CONTAINER_NAME" >/dev/null 2>&1 || true
            "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
            "$RUNTIME" rename "${CONTAINER_NAME}-previous" "$CONTAINER_NAME" >/dev/null 2>&1 || true
            write_status "rolling_back" "$previous_version" 60 "Starting previous container"
            "$RUNTIME" start "$CONTAINER_NAME" >>"$WATCHER_LOG" 2>&1
            unmask_unit_if_needed
            container_unit_start
            write_status "idle" "$previous_version" 100 "Rollback complete" ""
            return 0
        fi
        write_status "failed" "$previous_version" 0 "Rollback failed" "No previous image tag or container available"
        return 1
    fi

    if ! "$RUNTIME" image inspect "$previous_image_tag" >/dev/null 2>&1; then
        write_status "failed" "$previous_version" 0 "Rollback failed" "Previous image $previous_image_tag not found"
        return 1
    fi

    log "Rollback: swapping to $previous_image_tag"
    write_status "rolling_back" "$previous_version" 30 "Stopping current"
    container_unit_mask_and_stop

    local failed_name="${CONTAINER_NAME}-failed-$(date +%s)"
    "$RUNTIME" stop -t 15 "$CONTAINER_NAME" >/dev/null 2>&1 || true
    "$RUNTIME" rename "$CONTAINER_NAME" "$failed_name" >/dev/null 2>&1 || \
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

    local run_helper
    if ! run_helper=$(find_run_helper); then
        write_status "failed" "$previous_version" 50 "Rollback failed" "run_container.sh missing"
        unmask_unit_if_needed
        container_unit_start
        return 1
    fi

    write_status "rolling_back" "$previous_version" 60 "Starting previous image"
    RUNTIME="$RUNTIME" \
    IMAGE_TAG="$previous_image_tag" \
    CONTAINER_NAME="$CONTAINER_NAME" \
    DATA_DIR="$DATA_DIR" \
    bash "$run_helper" >>"$WATCHER_LOG" 2>&1

    # Clean up failed container
    "$RUNTIME" rm -f "$failed_name" >/dev/null 2>&1 || true

    update_version_state "$previous_version" "" "$previous_image_tag" ""
    unmask_unit_if_needed
    container_unit_start
    write_status "idle" "$previous_version" 100 "Rollback complete" ""
    return 0
}

# ── CANCEL: kill in-progress build ───────────────────────────────────────────
do_cancel() {
    log "Cancel requested"
    # Kill any running podman build for our image name
    pkill -f "$RUNTIME build.*$IMAGE_NAME" 2>/dev/null || true
    write_status "idle" "" 0 "Cancelled by user" ""
}

# ── GC: prune old images beyond retention ────────────────────────────────────
do_gc() {
    local keep
    keep=$(echo "$TRIGGER_PAYLOAD" | jq -r '.retention_count // 2')
    log "GC: keeping $keep most recent image tags"

    # List tags matching IMAGE_NAME:<version>-<arch>, sort by created desc, keep first N
    local to_remove
    to_remove=$("$RUNTIME" images --format '{{.Repository}}:{{.Tag}} {{.CreatedAt}}' 2>/dev/null \
        | grep "^${IMAGE_NAME}:" \
        | grep -vE ":(latest|latest-[a-z0-9_]+)$" \
        | sort -k2 -r \
        | awk -v keep="$keep" 'NR > keep {print $1}')

    if [[ -z "$to_remove" ]]; then
        log "GC: nothing to remove"
        return 0
    fi

    for tag in $to_remove; do
        log "GC: removing $tag"
        "$RUNTIME" rmi "$tag" >>"$WATCHER_LOG" 2>&1 || log "GC: failed to remove $tag"
    done
}

# ── INSTALL WATCHER: drop marker so the app knows we're alive ────────────────
do_install_watcher() {
    touch "$WATCHER_MARKER"
    log "Watcher install marker written"
    write_status "idle" "" 0 "Watcher installed" ""
}

# ── VERSION STATE UPDATE ─────────────────────────────────────────────────────
update_version_state() {
    local new_version="$1"
    local old_version="$2"
    local new_image_tag="$3"
    local old_image_tag="$4"

    # Use jq to update the version.json file atomically
    if [[ ! -f "$VERSION_STATE_FILE" ]]; then
        echo '{}' > "$VERSION_STATE_FILE"
    fi

    local now
    now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    local updated
    updated=$(jq --arg cv "$new_version" \
                 --arg ct "$new_image_tag" \
                 --arg pv "$old_version" \
                 --arg pt "$old_image_tag" \
                 --arg at "$now" \
                 '.current_version = $cv
                  | .current_image_tag = $ct
                  | .previous_version = (if $pv == "" then .previous_version else $pv end)
                  | .previous_image_tag = (if $pt == "" then .previous_image_tag else $pt end)
                  | .installed_at = $at
                  | .upgrade_state = "idle"
                  | .upgrade_error = null
                  | .latest_available = null' \
                 "$VERSION_STATE_FILE")

    echo "$updated" > "$VERSION_STATE_FILE"
}

# ── MAIN DISPATCH ────────────────────────────────────────────────────────────
# Cleanup handler — runs on EXIT (any reason), and explicitly on SIGTERM.
# Without explicit signal traps, a SIGKILL'd process won't run cleanup, but
# SIGTERM (the polite signal systemd sends first) will.
cleanup_on_exit() {
    local exit_code=$?
    release_lock
    if (( exit_code != 0 )); then
        log "upgrade.sh exiting with code $exit_code"
    fi
    exit $exit_code
}

main() {
    detect_runtime || exit 1

    if ! consume_trigger; then
        # No trigger present
        exit 0
    fi

    if ! acquire_lock "$TRIGGER_ACTION"; then
        log "Trigger dropped: lock held"
        exit 2
    fi

    # Install traps AFTER acquiring the lock — we want cleanup even on signal
    trap cleanup_on_exit EXIT
    trap 'log "Received SIGTERM — cleaning up"; exit 130' TERM
    trap 'log "Received SIGINT — cleaning up";  exit 130' INT
    trap 'log "Received SIGHUP — cleaning up";  exit 130' HUP

    case "$TRIGGER_ACTION" in
        build)              do_build ;;
        swap)               do_swap ;;
        rollback)           do_rollback ;;
        cancel)             do_cancel ;;
        gc)                 do_gc ;;
        install_watcher)    do_install_watcher ;;
        *)
            log "Unknown action: $TRIGGER_ACTION"
            write_status "failed" "" 0 "" "Unknown action: $TRIGGER_ACTION"
            exit 3
            ;;
    esac
}

main "$@"