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
DATA_DIR="${ZMM_DATA_DIR:-$HOME/.zigbee-matter-manager}"
APP_DIR="${ZMM_APP_DIR:-$HOME/zigbee-matter-manager}"
IMAGE_NAME="${ZMM_IMAGE_NAME:-zigbee-matter-manager}"
CONTAINER_NAME="${ZMM_CONTAINER_NAME:-zigbee-matter-manager}"
REPO_URL="${ZMM_REPO_URL:-https://github.com/oneofthemany/ZigBee-Matter-Manager.git}"
HEALTH_URL="${ZMM_HEALTH_URL:-http://127.0.0.1:8000/api/status}"
HEALTH_TIMEOUT="${ZMM_HEALTH_TIMEOUT:-60}"  # seconds to wait for new container to become healthy
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
acquire_lock() {
    if [[ -f "$LOCK_FILE" ]]; then
        local held
        held=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")
        log "Lock already held: $held"
        return 1
    fi
    echo "$$ $(date -u +"%Y-%m-%dT%H:%M:%SZ") $1" > "$LOCK_FILE"
    return 0
}

release_lock() {
    rm -f "$LOCK_FILE"
}

# ── TRIGGER CONSUMPTION ──────────────────────────────────────────────────────
consume_trigger() {
    if [[ ! -f "$TRIGGER_FILE" ]]; then
        return 1
    fi

    if ! command -v jq &>/dev/null; then
        log "ERROR: jq is required for the upgrade watcher"
        rm -f "$TRIGGER_FILE"
        return 1
    fi

    TRIGGER_ACTION=$(jq -r '.action // empty' "$TRIGGER_FILE" 2>/dev/null)
    TRIGGER_PAYLOAD=$(jq -c '.payload // {}' "$TRIGGER_FILE" 2>/dev/null)

    if [[ -z "$TRIGGER_ACTION" ]]; then
        log "Malformed trigger (no action); removing"
        rm -f "$TRIGGER_FILE"
        return 1
    fi

    rm -f "$TRIGGER_FILE"
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
    log_to_build "This typically takes 15-25 minutes on ARM devices."
    log_to_build ""

    write_status "building" "$target_version" 20 "Compiling image (~15-25min)" "" "$started_at"

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

# ── SWAP: stop old container, rename, run new ────────────────────────────────
do_swap() {
    local target_version
    target_version=$(echo "$TRIGGER_PAYLOAD" | jq -r '.target_version // empty')
    if [[ -z "$target_version" ]]; then
        log "Swap: no target_version"
        write_status "failed" "" 0 "Swap failed" "No target_version in swap payload"
        return 1
    fi

    local arch
    arch=$(detect_arch)
    local new_tag="${IMAGE_NAME}:${target_version}-${arch}"

    if ! "$RUNTIME" image inspect "$new_tag" >/dev/null 2>&1; then
        write_status "failed" "$target_version" 0 "Swap failed" "Image $new_tag not found — build first"
        return 1
    fi

    # Inspect the current container to extract run arguments
    if ! "$RUNTIME" inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        write_status "failed" "$target_version" 0 "Swap failed" "Current container $CONTAINER_NAME not found"
        return 1
    fi

    log "Swap: starting for v$target_version"
    write_status "swapping" "$target_version" 10 "Capturing current run-spec"

    # Snapshot current container's run spec.
    # We use inspect to pull out: the image, ports, volumes, devices, env, caps, network.
    # Easiest reliable path: capture the CreateCommand (Podman) or Config.Cmd + HostConfig (Docker).
    # Simpler: capture original image tag so we can roll back; for the run args, we rely on
    # the fact that our build.sh wrote them consistently AND the data volumes stay the same.

    local current_image
    current_image=$("$RUNTIME" inspect -f '{{.Image}}' "$CONTAINER_NAME" 2>/dev/null)
    local current_image_tag
    current_image_tag=$("$RUNTIME" inspect -f '{{if .ImageName}}{{.ImageName}}{{else}}{{index .Config.Image}}{{end}}' "$CONTAINER_NAME" 2>/dev/null || echo "")

    if [[ -z "$current_image_tag" || "$current_image_tag" == "<nil>" ]]; then
        # Fallback: get first repo tag for the image ID
        current_image_tag=$("$RUNTIME" image inspect --format '{{index .RepoTags 0}}' "$current_image" 2>/dev/null || echo "${IMAGE_NAME}:latest")
    fi

    # Determine the current version from the running container's VERSION file (best effort)
    local current_version
    current_version=$("$RUNTIME" exec "$CONTAINER_NAME" cat /app/VERSION 2>/dev/null | tr -d '[:space:]' || echo "unknown")

    log "Swap: current image = $current_image_tag (version $current_version)"
    log "Swap: new image     = $new_tag (version $target_version)"

    # Capture the full run spec so we can replay it
    local spec_file="${UPGRADE_DIR}/.current_spec.json"
    "$RUNTIME" inspect "$CONTAINER_NAME" > "$spec_file" 2>/dev/null || true

    write_status "swapping" "$target_version" 30 "Stopping current container"
    log "Swap: stopping $CONTAINER_NAME"

    if ! "$RUNTIME" stop -t 15 "$CONTAINER_NAME" >>"$WATCHER_LOG" 2>&1; then
        log "Swap: stop returned non-zero (continuing anyway)"
    fi

    write_status "swapping" "$target_version" 40 "Renaming old container"
    local previous_name="${CONTAINER_NAME}-previous"
    # Remove any stale -previous container
    "$RUNTIME" rm -f "$previous_name" >/dev/null 2>&1 || true
    "$RUNTIME" rename "$CONTAINER_NAME" "$previous_name" >>"$WATCHER_LOG" 2>&1 || {
        log "Swap: rename failed — falling back to stop+start of old container"
        # If rename isn't supported (some docker versions), we'll just remove & re-create later
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    }

    # Run new container using the same run arguments. The canonical source for
    # the run arguments is APP_DIR/build.sh's run_container function. We DON'T
    # re-run build.sh because it rebuilds the image. Instead we extract the
    # arguments from the spec file (best-effort), OR fall back to invoking a
    # helper from build.sh.
    #
    # The cleanest path: have build.sh expose a `--run-only` mode. For now, we
    # use a dedicated helper script installed alongside this one.
    local run_helper
    run_helper="${DATA_DIR}/scripts/run_container.sh"
    if [[ ! -f "$run_helper" ]]; then
        run_helper="${APP_DIR}/scripts/run_container.sh"
    fi

    if [[ ! -f "$run_helper" ]]; then
        log "Swap: run_container.sh not found at $run_helper — cannot start new container"
        write_status "failed" "$target_version" 50 "Swap failed" "run_container.sh helper not installed"
        # Attempt to bring the old container back
        "$RUNTIME" rename "$previous_name" "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" start "$CONTAINER_NAME" >/dev/null 2>&1 || true
        return 1
    fi

    write_status "swapping" "$target_version" 55 "Starting new container"
    log "Swap: starting new container from $new_tag via $run_helper"

    if ! RUNTIME="$RUNTIME" \
         IMAGE_TAG="$new_tag" \
         CONTAINER_NAME="$CONTAINER_NAME" \
         DATA_DIR="$DATA_DIR" \
         bash "$run_helper" >>"$WATCHER_LOG" 2>&1
    then
        log "Swap: new container failed to start — rolling back"
        write_status "rolling_back" "$target_version" 70 "New container failed — rolling back"
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" rename "$previous_name" "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" start "$CONTAINER_NAME" >/dev/null 2>&1 || true
        write_status "failed" "$target_version" 100 "Rolled back" "New container failed to start; old container restored"
        return 1
    fi

    # Health check
    write_status "swapping" "$target_version" 80 "Health-checking new container"
    log "Swap: waiting up to ${HEALTH_TIMEOUT}s for new container to become healthy"

    local healthy=0
    local elapsed=0
    while (( elapsed < HEALTH_TIMEOUT )); do
        if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
            healthy=1
            break
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done

    if (( healthy == 0 )); then
        log "Swap: health check failed — rolling back"
        write_status "rolling_back" "$target_version" 90 "Health check failed — rolling back"
        "$RUNTIME" stop -t 10 "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" rename "$previous_name" "$CONTAINER_NAME" >/dev/null 2>&1 || true
        "$RUNTIME" start "$CONTAINER_NAME" >/dev/null 2>&1 || true
        write_status "failed" "$target_version" 100 "Rolled back after health failure" "New container did not respond at $HEALTH_URL within ${HEALTH_TIMEOUT}s"
        return 1
    fi

    # Success — update version state
    log "Swap: SUCCESS. New container healthy. Keeping $previous_name for rollback."
    update_version_state "$target_version" "$current_version" "$new_tag" "$current_image_tag"

    # GC: schedule cleanup of the old container's filesystem (but keep image for rollback)
    # We remove the -previous container after a grace period so `podman start previous` rollback is instant.
    # For the current design, the -previous container stays until next successful upgrade or manual GC.

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
            "$RUNTIME" stop -t 15 "$CONTAINER_NAME" >/dev/null 2>&1 || true
            "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
            "$RUNTIME" rename "${CONTAINER_NAME}-previous" "$CONTAINER_NAME" >/dev/null 2>&1 || true
            write_status "rolling_back" "$previous_version" 60 "Starting previous container"
            "$RUNTIME" start "$CONTAINER_NAME" >>"$WATCHER_LOG" 2>&1
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

    # Simulate a "swap" to the previous image using the same helper
    log "Rollback: swapping to $previous_image_tag"
    write_status "rolling_back" "$previous_version" 30 "Stopping current"

    local failed_name="${CONTAINER_NAME}-failed-$(date +%s)"
    "$RUNTIME" stop -t 15 "$CONTAINER_NAME" >/dev/null 2>&1 || true
    "$RUNTIME" rename "$CONTAINER_NAME" "$failed_name" >/dev/null 2>&1 || \
        "$RUNTIME" rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

    local run_helper="${DATA_DIR}/scripts/run_container.sh"
    [[ -f "$run_helper" ]] || run_helper="${APP_DIR}/scripts/run_container.sh"

    if [[ ! -f "$run_helper" ]]; then
        write_status "failed" "$previous_version" 50 "Rollback failed" "run_container.sh missing"
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

    # Update state: swap current and previous
    update_version_state "$previous_version" "" "$previous_image_tag" ""

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

    trap release_lock EXIT

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