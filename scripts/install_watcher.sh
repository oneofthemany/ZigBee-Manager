#!/bin/bash
# =============================================================================
# ZMM Upgrade Watcher Installer
#
# Installs the host-side watcher that reacts to upgrade triggers from the
# running container.
#
# Prefers systemd-path units (event-driven, no CPU when idle). Falls back to
# a polling loop (systemd user service or nohup-ed shell) when systemd-path
# isn't available.
#
# Safe to re-run — idempotent.
# =============================================================================
set -euo pipefail

# Colours
CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${CYAN}${BOLD}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}${BOLD}[ OK ]${NC} $*"; }
warn()  { echo -e "${YELLOW}${BOLD}[WARN]${NC} $*"; }
err()   { echo -e "${RED}${BOLD}[ERR ]${NC} $*" >&2; }

DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
APP_DIR="${ZMM_APP_DIR:-/opt/zigbee-matter-manager}"

# IMPORTANT: scripts must live in a location systemd's init_t domain can
# execute under SELinux. /root/ and ~/ are labelled admin_home_t/user_home_t
# which init_t is denied execute access to. /opt/ is labelled usr_t which
# init_t can execute, and is the FHS-standard location for add-on packages.
SCRIPTS_DIR="${ZMM_SCRIPTS_DIR:-/opt/zigbee-matter-manager}"

UPGRADE_DIR="${DATA_DIR}/data/upgrade"
STATE_DIR="${DATA_DIR}/data/state"
LOG_DIR="${DATA_DIR}/logs"

# /opt/zigbee-matter-manager needs root to create — sudo if we're not already root
if [[ ! -d "$SCRIPTS_DIR" ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
        mkdir -p "$SCRIPTS_DIR"
    else
        sudo mkdir -p "$SCRIPTS_DIR"
        sudo chown "$USER:$USER" "$SCRIPTS_DIR"
    fi
fi

mkdir -p "$UPGRADE_DIR" "$STATE_DIR" "$LOG_DIR"

# Best-effort SELinux relabel of the scripts dir to ensure correct context.
# /opt/ defaults to usr_t which is fine, but if the user has customised
# things or the dir was created by tools that miss labelling, restorecon
# will reset to whatever the policy says it should be.
if command -v restorecon >/dev/null 2>&1 && [[ -e /sys/fs/selinux/enforce ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
        restorecon -R "$SCRIPTS_DIR" >/dev/null 2>&1 || true
    else
        sudo restorecon -R "$SCRIPTS_DIR" >/dev/null 2>&1 || true
    fi
fi

# ── Prerequisites ────────────────────────────────────────────────────────────
info "Checking prerequisites..."
MISSING=()
for cmd in jq curl git; do
    command -v "$cmd" >/dev/null 2>&1 || MISSING+=("$cmd")
done

if ! command -v podman >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
    err "Neither podman nor docker found. Install one and re-run."
    exit 1
fi

if (( ${#MISSING[@]} > 0 )); then
    err "Missing required tools: ${MISSING[*]}"
    warn "Install on Debian/Ubuntu:  sudo apt install ${MISSING[*]}"
    warn "Install on Fedora:         sudo dnf install ${MISSING[*]}"
    warn "Install on Alpine:         sudo apk add ${MISSING[*]}"
    exit 1
fi

ok "Prerequisites OK"

# ── Copy scripts from repo clone or current dir ──────────────────────────────
# The install script may be run directly from a curl|bash flow, from the
# cloned repo under $APP_DIR/scripts, or from anywhere.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_script() {
    local name="$1"
    for candidate in \
        "${SRC_DIR}/${name}" \
        "${APP_DIR}/scripts/${name}" \
        "./scripts/${name}" \
        "./${name}"; do
        if [[ -f "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

for script in upgrade.sh run_container.sh; do
    if src=$(find_script "$script"); then
        cp "$src" "${SCRIPTS_DIR}/${script}"
        chmod +x "${SCRIPTS_DIR}/${script}"
        ok "Installed ${script} -> ${SCRIPTS_DIR}/${script}"
    else
        err "Could not locate ${script} — clone the repo first:"
        err "  git clone https://github.com/oneofthemany/ZigBee-Matter-Manager.git $APP_DIR"
        exit 1
    fi
done

# ── Mechanism selection ──────────────────────────────────────────────────────
# Prefer user systemd (rootless-friendly), fall back to system systemd, then polling.
USE_SYSTEMD_USER=false
USE_SYSTEMD_SYSTEM=false
USE_POLLING=false

if command -v systemctl >/dev/null 2>&1; then
    if [[ "$(id -u)" -eq 0 ]]; then
        USE_SYSTEMD_SYSTEM=true
    elif systemctl --user status >/dev/null 2>&1; then
      USE_SYSTEMD_USER=true
    else
        USE_POLLING=true
    fi
else
    USE_POLLING=true
fi

# ── systemd user: path unit + service unit ───────────────────────────────────
install_systemd_user() {
    local unit_dir="/opt/.config/systemd/user"
    mkdir -p "$unit_dir"

    cat > "$unit_dir/zmm-upgrade.service" <<SERVICE
[Unit]
Description=ZMM Upgrade Worker (oneshot)
After=default.target
# If we burn through the start limit, recover automatically after 10 minutes
StartLimitIntervalSec=600
StartLimitBurst=20

[Service]
Type=oneshot
ExecStart=${SCRIPTS_DIR}/upgrade.sh
Environment=ZMM_DATA_DIR=${DATA_DIR}
Environment=ZMM_APP_DIR=${APP_DIR}
# Ensure the user's PATH includes common runtime locations
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# A trigger that produces a non-zero exit (e.g. malformed) should not be
# treated as a failure that prevents the next run. Treat any exit as success.
SuccessExitStatus=0 1 2 3
# A full image build takes 15-25 min on ARM. Default oneshot timeout would
# kill the build mid-flight and leave the lock dangling. Disable it.
TimeoutStartSec=infinity

[Install]
WantedBy=default.target
SERVICE

    cat > "$unit_dir/zmm-upgrade.path" <<PATHUNIT
[Unit]
Description=Watch for ZMM upgrade triggers

[Path]
# PathChanged fires once when the trigger file is closed after writing.
# (Unlike PathExists, this does NOT retrigger while the file remains.)
# Note: do NOT add MakeDirectory=true here — that would cause systemd to
# create the trigger path as a directory, breaking everything.
PathChanged=${UPGRADE_DIR}/trigger
Unit=zmm-upgrade.service

[Install]
WantedBy=default.target
PATHUNIT

    systemctl --user daemon-reload
    systemctl --user enable --now zmm-upgrade.path
    ok "systemd user path unit enabled (event-driven)"

    # Enable linger so it works without an active login session
    if command -v loginctl >/dev/null 2>&1; then
        if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
            warn "Enabling user linger (sudo required) so the watcher survives logout..."
            sudo loginctl enable-linger "$USER" 2>/dev/null || \
                warn "Could not enable linger — watcher will only run while you're logged in"
        fi
    fi
}

# ── systemd system: same pattern but as root ─────────────────────────────────
install_systemd_system() {
    local unit_dir="/etc/systemd/system"

    sudo tee "$unit_dir/zmm-upgrade.service" >/dev/null <<SERVICE
[Unit]
Description=ZMM Upgrade Worker (oneshot)
After=network-online.target
StartLimitIntervalSec=600
StartLimitBurst=20

[Service]
Type=oneshot
User=$USER
ExecStart=${SCRIPTS_DIR}/upgrade.sh
Environment=ZMM_DATA_DIR=${DATA_DIR}
Environment=ZMM_APP_DIR=${APP_DIR}
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
SuccessExitStatus=0 1 2 3
# A full image build takes 15-25 min on ARM. Disable the oneshot timeout
# so systemd doesn't SIGKILL us mid-build and leave a stale lock.
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
SERVICE

    sudo tee "$unit_dir/zmm-upgrade.path" >/dev/null <<PATHUNIT
[Unit]
Description=Watch for ZMM upgrade triggers

[Path]
# PathChanged fires once when the trigger file is closed after writing.
# Note: do NOT add MakeDirectory=true here — that would cause systemd to
# create the trigger path as a directory, breaking everything.
PathChanged=${UPGRADE_DIR}/trigger
Unit=zmm-upgrade.service

[Install]
WantedBy=multi-user.target
PATHUNIT

    sudo systemctl daemon-reload
    sudo systemctl enable --now zmm-upgrade.path
    ok "systemd system path unit enabled (event-driven)"
}

# ── Polling fallback: simple systemd-free watcher ────────────────────────────
install_polling() {
    local poll_script="${SCRIPTS_DIR}/zmm-upgrade-poll.sh"
    cat > "$poll_script" <<'POLL'
#!/bin/bash
# Polling watcher — runs upgrade.sh every N seconds if a trigger exists.
set -u
DATA_DIR="${ZMM_DATA_DIR:-/opt/.zigbee-matter-manager}"
APP_DIR="${ZMM_APP_DIR:-/opt/zigbee-matter-manager}"
UPGRADE_SH="${DATA_DIR}/scripts/upgrade.sh"
TRIGGER="${DATA_DIR}/data/upgrade/trigger"
INTERVAL=5

while true; do
    if [[ -f "$TRIGGER" ]]; then
        ZMM_DATA_DIR="$DATA_DIR" ZMM_APP_DIR="$APP_DIR" bash "$UPGRADE_SH" || true
    fi
    sleep "$INTERVAL"
done
POLL
    chmod +x "$poll_script"

    # Try to wrap in a systemd service even without user systemd; otherwise launch via nohup
    if [[ "$(id -u)" -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
        cat > /etc/systemd/system/zmm-upgrade-poll.service <<SVC
[Unit]
Description=ZMM Upgrade Polling Watcher
After=network-online.target

[Service]
ExecStart=${poll_script}
Restart=always
RestartSec=5
User=$USER

[Install]
WantedBy=multi-user.target
SVC
        systemctl daemon-reload
        systemctl enable --now zmm-upgrade-poll.service
        ok "Polling watcher enabled via systemd (system)"
        return
    fi

    # Last resort: nohup the poller, add to user's crontab with @reboot
    local pidfile="${DATA_DIR}/upgrade-poll.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        ok "Polling watcher already running (PID $(cat "$pidfile"))"
    else
        nohup "$poll_script" >"${LOG_DIR}/upgrade-poll.log" 2>&1 &
        echo $! > "$pidfile"
        ok "Polling watcher started (PID $!)"
    fi

    # Also register a @reboot crontab entry to survive restarts
    if command -v crontab >/dev/null 2>&1; then
        local current_cron
        current_cron=$(crontab -l 2>/dev/null || true)
        if ! echo "$current_cron" | grep -q "zmm-upgrade-poll.sh"; then
            (echo "$current_cron"; echo "@reboot $poll_script >> ${LOG_DIR}/upgrade-poll.log 2>&1") | crontab -
            ok "Added @reboot cron entry for polling watcher"
        fi
    fi
}

# ── Install based on detected mechanism ──────────────────────────────────────
if $USE_SYSTEMD_USER; then
    info "Detected: systemd --user available → using path-based watcher"
    install_systemd_user
elif $USE_SYSTEMD_SYSTEM; then
    info "Detected: root systemd → using path-based watcher"
    install_systemd_system
elif $USE_POLLING; then
    info "Detected: no systemd available → using polling watcher"
    install_polling
else
    err "Unable to determine watcher mechanism"
    exit 1
fi

# ── Drop marker so the app knows watcher is ready ────────────────────────────
touch "${UPGRADE_DIR}/.watcher_installed"

# ── Seed VERSION state if missing ────────────────────────────────────────────
if [[ ! -f "${STATE_DIR}/version.json" ]]; then
    # Try to read VERSION from the running container
    RUNTIME=""
    if command -v podman >/dev/null 2>&1; then RUNTIME=podman; fi
    if [[ -z "$RUNTIME" ]] && command -v docker >/dev/null 2>&1; then RUNTIME=docker; fi

    CUR_VER="unknown"
    if [[ -n "$RUNTIME" ]]; then
        CUR_VER=$("$RUNTIME" exec zigbee-matter-manager cat /app/VERSION 2>/dev/null | tr -d '[:space:]' || echo "unknown")
        [[ -z "$CUR_VER" ]] && CUR_VER="unknown"
    fi

    cat > "${STATE_DIR}/version.json" <<JSON
{
  "current_version": "${CUR_VER}",
  "upgrade_state": "idle",
  "auto_update": false,
  "channel": "stable",
  "retention_count": 2,
  "watcher_installed": true
}
JSON
    ok "Seeded version.json with current_version=${CUR_VER}"
fi

echo
ok "${BOLD}Watcher installation complete${NC}"
echo
info "Triggers will be watched at:  ${UPGRADE_DIR}/trigger"
info "Status will be written to:    ${UPGRADE_DIR}/status.json"
info "Build log will be written to: ${UPGRADE_DIR}/build.log"
info "Watcher log:                  ${LOG_DIR}/upgrade_watcher.log"
echo
info "Test the trigger mechanism by opening the Settings tab → Upgrade in the UI."