# In-App Upgrades — Architecture & Mechanics

## Overview

ZigBee Matter Manager supports in-app upgrades via the **Settings → Upgrade** tab. The system pulls a tagged release from GitHub, builds a new container image **in the background while the running app keeps serving traffic**, then performs an atomic container swap with health-check-gated automatic rollback.

This is a blue-green deployment model adapted for self-hosted single-host containerised applications. It is designed around three constraints that make the standard solutions a poor fit:

1. **No registry image to pull.** ZMM is built locally per-host because the Containerfile compiles per-architecture Rust modules and per-version Python wheels. There's no `docker pull`-style pre-built image hosted somewhere.
2. **No Kubernetes / Swarm / multi-node infrastructure.** A single Rock 5B (or similar) is the entire fleet.
3. **No privileged container.** The container that serves the UI is fully unprivileged and must not be granted access to the host's container runtime, even via a mounted socket.

The upgrade flow has to work under **all of**: rootless Podman + SELinux, rootless Podman + AppArmor, root Podman, Docker, with or without systemd, on any modern Linux distro. The architecture below is what falls out of those constraints.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  CONTAINER (unprivileged, slirp4netns)                               │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  modules/upgrade_manager.py                                    │  │
│  │    - Polls GitHub releases/tags every 6h                       │  │
│  │    - Writes JSON trigger files                                 │  │
│  │    - Polls status.json for host-side progress                  │  │
│  │    - Stale-lock detection (PID liveness + age)                 │  │
│  │    - Background asyncio loops via FastAPI lifespan             │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  routes/upgrade_routes.py (FastAPI)                            │  │
│  │    GET  /api/upgrade/status                                    │  │
│  │    POST /api/upgrade/{check,build,swap,rollback,cancel,gc}     │  │
│  │    POST /api/upgrade/{settings,reset-status,clear-lock}        │  │
│  │    GET  /api/upgrade/log                                       │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  static/js/upgrade.js (Settings tab card)                      │  │
│  │    - Bootstrap UI, state-machine-driven                        │  │
│  │    - Progress bar, build log streaming, action buttons         │  │
│  │    - WebSocket event hook for real-time updates                │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                  bind-mounted volume (file-based IPC)
              /app/data/upgrade/  ↔  /opt/.zigbee-matter-manager/data/upgrade/
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  HOST                                                                │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  systemd-path unit  OR  polling fallback                       │  │
│  │    PathChanged=/.../upgrade/trigger                            │  │
│  │    StartLimitIntervalSec=600 / StartLimitBurst=20              │  │
│  │    TimeoutStartSec=infinity                                    │  │
│  │    Fires zmm-upgrade.service oneshot on file write-close       │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  /opt/zmm/scripts/upgrade.sh (host orchestrator                │  │
│  │    - Atomic trigger consume (read → delete → parse)            │  │
│  │    - Stale lock detection (PID alive + age check)              │  │
│  │    - Action dispatch: build / swap / rollback / cancel / gc    │  │
│  │    - Signal traps for SIGTERM / SIGINT / SIGHUP                │  │
│  │    - Captures failed container logs into build.log             │  │
│  └────────────────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  /opt/zmm/scripts/run_container.sh (run-args helper)           │  │
│  │    - Replays build.sh's run_container() args with chosen tag   │  │
│  │    - Auto-detects USB device by-id pattern matching            │  │
│  │    - Conditional Bluetooth (/dev/hci0) inclusion               │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  CONTAINER RUNTIME (podman or docker)                                │
│    Drop Restart=no override → /run/systemd/system/<unit>.d/          │
│    systemctl stop <unit>    → kills supervisor first                 │
│    podman stop  → stop -t 45 (clean shutdown for matter-server)      │
│    podman rename → preserve old container as -previous               │
│    podman run   → start new container with same volumes/devices      │
│    Health check → curl /api/system/health with 60s window            │
│    Remove drop-in + start <unit> → re-arm supervisor on success      │
│    Rollback     → swap names back if run/health fails                │
└──────────────────────────────────────────────────────────────────────┘
```

**Note:** the supervisor unit (`zigbee-matter-manager.service`) typically has
`Restart=always`. Without disabling that for the swap window, it relaunches
the old container the moment we stop it and binds the ports the new
container needs. Suppression via runtime drop-in is the only reliable
mechanism — `systemctl mask` fails when the unit is a real file at
`/etc/systemd/system/<unit>.service`.

**Path note:** `/opt/zmm/scripts` is shorthand for
`/opt/zigbee-matter-manager/scripts/`.

### Why file-based IPC instead of socket mounting

The container could in theory mount the host's podman socket at `${XDG_RUNTIME_DIR}/podman/podman.sock` and drive builds via the libpod REST API. Three reasons we don't:

1. **Privilege equivalence.** A container with the podman socket mounted is effectively root on the host — it can spawn privileged containers, mount host filesystems, etc. This breaks the security model.
2. **Runtime API divergence.** Docker and Podman expose different REST APIs. File-based triggers are runtime-agnostic.
3. **SELinux complications.** Cross-namespace socket access requires `--security-opt label=disable` or relabelling, and the policies vary by distro.

File-based IPC is boring, debuggable (`cat trigger.json`), works identically across runtimes, and requires no elevated privileges in the container.

---

## File system layout

### Inside the container (`/app/`)

```
/app/
├── VERSION                          # baked in at build time (single line: "1.3.2")
├── modules/
│   └── upgrade_manager.py           # core module
├── routes/
│   └── upgrade_routes.py            # FastAPI endpoints
├── static/
│   ├── js/upgrade.js                # frontend
│   └── css/upgrade.css
└── data/upgrade/                    # bind-mount; shared with host
    ├── trigger                      # transient — written by app, deleted by upgrade.sh
    ├── status.json                  # host writes, app polls
    ├── build.log                    # host writes, app reads via /api/upgrade/log
    ├── lock                         # in-flight operation marker (PID + timestamp)
    └── .watcher_installed           # marker indicating host side is set up
```

### On the host

```
opt/zigbee-matter-manager/scripts/   # SELinux usr_t — systemd can execute these
├── upgrade.sh                       # orchestrator
└── run_container.sh                 # run-args replayer

~/.zigbee-matter-manager/            # data dir (admin_home_t — readable, not executable)
├── data/upgrade/                    # bind-mounted into container at /app/data/upgrade/
│   ├── trigger                      # (transient)
│   ├── status.json
│   ├── build.log
│   └── lock
├── data/state/
│   └── version.json                 # persistent: current/previous versions, settings
└── logs/
    └── upgrade_watcher.log          # systemd-execed upgrade.sh writes here

/etc/systemd/system/                 # OR ~/.config/systemd/user/
├── zmm-upgrade.path                 # PathChanged=... → triggers .service
└── zmm-upgrade.service              # Type=oneshot, ExecStart=/opt/zigbee-matter-manager/scripts/upgrade.sh
```

### State separation rationale

`config.yaml` is **user-owned configuration** (Zigbee credentials, MQTT broker, room layouts). It must be backupable and round-trippable through human review.

`version.json` is **system-managed state** (current version, previous version, last GitHub check timestamp, auto-update settings). It must never appear in a config backup, and should not be hand-edited.

Mixing them risks: backup-restore cycles overwriting current version state; user YAML errors breaking version tracking; merge conflicts when config schema evolves. The two are kept entirely separate.

---

## State machine

The upgrade flow is a state machine with seven states:

```
                ┌────────────────────────────────────────┐
                │                                        │
                │                 ┌──────────┐           │
                ├───────────────▶│ checking │           │
                │  (poll GitHub)  └────┬─────┘           │
                │                      │                 │
                │            new tag found               │
                │                      │                 │
                │                      ▼                 │
                │                 ┌──────────┐           │
   user click  ─┼───────────────▶│ building │           │
   "Build"      │                 └────┬─────┘           │
                │                      │                 │
                │                build succeeds          │
                │                      │                 │
                │                      ▼                 │
                │              ┌───────────────┐         │
                │              │ ready_to_swap │         │
                │              └───────┬───────┘         │
                │                      │                 │
                │              user click "Swap"         │
                │                      │                 │
                │                      ▼                 │
   ┌─ idle ◀───┤                ┌──────────┐            │
   │            │                │ swapping │            │
   │            │                └────┬─────┘            │
   │            │                     │                  │
   │            │      ┌──────────────┼─────────────┐    │
   │            │      │              │             │    │
   │            │      ▼              ▼             ▼    │
   │            │ health pass     run fails    health    │
   │            │      │           │            fail     │
   │            │  (success)       │             │       │
   │            │      │           ▼             ▼       │
   │            │      │     ┌───────────────────┐       │
   │            │      │     │   rolling_back    │       │
   │            │      │     └─────────┬─────────┘       │
   │            │      │               │                 │
   │            │      │               ▼                 │
   │            │      │           ┌────────┐            │
   │            └──────┴─────────▶│ failed │────────────┘
   │                               └────┬───┘     (user
   │                                    │      dismisses /
   │                                    │       retries)
   └────────────────────────────────────┘
```

States:

| State | Source of truth | Set by | Cleared by |
|:------|:----------------|:-------|:-----------|
| `idle` | both | swap success, reset_status | — |
| `checking` | app (transient) | periodic_check_loop | check completes |
| `building` | both | request_build → write_status | upgrade.sh do_build completion |
| `ready_to_swap` | host status | upgrade.sh do_build success | request_swap |
| `swapping` | both | request_swap → write_status | health check pass/fail |
| `rolling_back` | host status | health failure | swap-back complete |
| `failed` | both | any error path | reset_status (manual or auto on retry) |

The container-side `upgrade_state` (in `version.json`) is kept in sync with the host-side `state` (in `status.json`) by the `status_watcher_loop` background task that polls `status.json` every 2 seconds.

---

## Trigger file lifecycle

The single most important piece of the architecture is the trigger file at `/opt/.zigbee-matter-manager/data/upgrade/trigger`. Every upgrade operation flows through it. Getting the lifecycle right was the source of most of the bugs hit during initial implementation.

### Write side (Python)

```python
# modules/upgrade_manager.py: write_trigger()
trigger = {
    "action": "build",          # build | swap | rollback | cancel | gc | install_watcher
    "payload": {                # action-specific data
        "target_version": "1.3.2",
        "architecture": "amd64",
        "repo": "oneofthemany/ZigBee-Matter-Manager",
    },
    "requested_at": "2026-04-25T13:09:53Z",
    "requested_by": "zmm-app",
}
# Atomic write: write to .tmp, then os.replace (rename)
with open(TRIGGER_FILE + ".tmp", "w") as f:
    json.dump(trigger, f, indent=2)
os.replace(TRIGGER_FILE + ".tmp", TRIGGER_FILE)
```

The atomic write pattern (`write to tmp + rename`) is critical because the host-side `systemd-path` unit watches for **file close-after-write**. If we `open()` the trigger file directly and write to it incrementally, systemd may fire the watcher mid-write and read a truncated JSON.

### Detection side (systemd)

```ini
# /etc/systemd/system/zmm-upgrade.path
[Path]
PathChanged=/opt/.zigbee-matter-manager/data/upgrade/trigger
Unit=zmm-upgrade.service
# Note: do NOT add MakeDirectory=true — systemd will create the trigger
# path itself as a directory, breaking the entire flow.
```

`PathChanged=` (not `PathExists=`) is essential. `PathExists=` retriggers continuously while the file exists, causing infinite loops if the consumer fails before deleting it. `PathChanged=` only fires when the file is closed-after-write — once per write, regardless of whether the file persists.

### Consume side (bash)

```bash
# /opt/zigbee-matter-manager/scripts/upgrade.sh: consume_trigger()
consume_trigger() {
    [[ -f "$TRIGGER_FILE" ]] || return 1

    # CRITICAL: read-then-delete in two steps. If we crash after this,
    # the path unit won't re-fire because the file is gone.
    local trigger_content
    trigger_content=$(cat "$TRIGGER_FILE" 2>/dev/null)
    rm -f "$TRIGGER_FILE"

    TRIGGER_ACTION=$(echo "$trigger_content" | jq -r '.action')
    TRIGGER_PAYLOAD=$(echo "$trigger_content" | jq -c '.payload')
    [[ -n "$TRIGGER_ACTION" ]] || return 1
}
```

Reading the contents into a shell variable **before** deleting the file means a script crash mid-parse cannot orphan the trigger and cause an infinite path-unit fire loop. This was a hard-won lesson — the original implementation parsed-then-deleted, and any malformed trigger or jq error left the file on disk forever.

---

## Locking

A second piece of state — `lock` — prevents concurrent operations. The lock format is `"PID TIMESTAMP ACTION"` written by upgrade.sh on operation start, removed on completion via the EXIT trap.

### Why both Python and Bash check the lock

The Python side (in the container) checks the lock before writing a trigger, returning HTTP 409 if held. The Bash side (on the host) checks the lock before starting an operation, returning early if held. The two checks aren't redundant — they catch different races:

- **Python check** prevents the user clicking "Build" twice in quick succession (the second click sees a held lock and 409s immediately).
- **Bash check** handles the case where two trigger files were written before either was consumed (rare, but possible if the watcher was paused/restarted with multiple pending triggers).

### Stale lock detection

A lock can become stale if upgrade.sh is killed by SIGKILL (the only signal the EXIT trap can't catch). Both the Python and Bash sides implement the same staleness algorithm:

1. **PID liveness check** — `kill -0 $PID` (or `os.kill(pid, 0)` in Python). If the holder PID is dead, the lock is stale.
2. **Age check** — if the lock is older than 60 minutes (longer than any legitimate build), treat as stale.

Either condition triggers automatic clearing. The user can also force-clear via `POST /api/upgrade/clear-lock` (which still refuses to clear a *live* lock — the safety guard remains).

---

## Build flow (`do_build`)

The `build` action triggered when the user clicks "Build" in the UI:

```
1. Validate payload (target_version, architecture, repo)
2. Reset build.log
3. Write status: building / 5% / "Preparing"
4. git clone --depth 1 --branch v${target_version} ${repo} ${work_dir}
   - Falls back to non-v-prefixed tag if first attempt fails
5. Stamp VERSION file into the clone
6. Write status: building / 20% / "Compiling image (varies by host hardware)"
7. podman build --format docker --build-arg BUILD_JOBS=${nproc} \
     --tag ${image}:${version}-${arch} \
     --file ${work_dir}/Containerfile ${work_dir}
8. podman tag ${image}:${version}-${arch} ${image}:latest-${arch}
9. Write status: ready_to_swap / 100% / "Image ready"
10. Clean up clone directory
```

Build time depends entirely on host hardware:
- **x86_64 NUC / desktop**: ~2 minutes with warm cache
- **Rock 5B (aarch64)**: 3–8 minutes with warm cache
- **Cold cache (any host)**: 15–25 minutes (full toolchain rebuild)

The first 9–11 layers are usually cached from the previous build (apt packages, OpenThread bootstrap, Python deps, Rust toolchain), so only `COPY . .` and onward actually run on a warm cache. The "varies by host hardware" status text deliberately avoids quoting a number — empirical times across the Rock 5B, NUC, and Unraid hosts span an order of magnitude.

### Build cache behaviour

Critical detail: the build cache lives in `~/.local/share/containers/storage/` (rootless) or `/var/lib/containers/storage/` (root). It persists across upgrades. This is what makes incremental upgrades fast — the per-version delta is only the layers downstream of `COPY . .` (lines 19–26 of the Containerfile).

If the cache becomes corrupted, `podman system prune -a` will force a full rebuild on the next upgrade (worst case: a 25-minute first build).

### Why we don't use `--pull=always`

Each build does NOT do `--pull=always` on the base `python:3.11-slim-bookworm` image, because that would re-download the base image every upgrade and discard the cache. A fresh base image is a separate concern handled by occasional manual `podman pull python:3.11-slim-bookworm`.

---

## Swap flow (`do_swap`)

The most operationally-sensitive part of the system. Every step has a failure mode that triggers a rollback path.

```
1. Verify target image exists (podman image inspect)
2. Verify current container exists (podman inspect)
3. Capture current image tag and version (for rollback)
4. Suppress supervisor auto-restart                 [STEP A]
5. podman stop -t 45 ${name}                        [STEP B]
6. Rename old: podman rename ${name} ${name}-previous
7. Write status: swapping / 60% / "Starting new container"
8. RUNTIME=podman IMAGE_TAG=... bash run_container.sh   [STEP C]
9. (if step 8 fails) capture failed container logs      [STEP D]
10. Write status: swapping / 80% / "Health-checking"
11. Poll health URL until 200 OR HEALTH_TIMEOUT (60s default)  [STEP E]
12. (if health fails) rollback: stop new, rm new, rename previous back
13. Restore supervisor: remove drop-in, daemon-reload, systemctl start
14. Update version.json: current/previous version + image tags
15. Write status: idle / 100% / "Upgrade complete"
```

### Step A — suppress the supervisor

`zigbee-matter-manager.service` is configured `Restart=always` with `ExecStart=podman start -a zigbee-matter-manager`. When we `podman stop` the container, the attached `podman start -a` exits and systemd treats the service as failed → `RestartSec=10` later it `podman start`s the old container again, binding port 8000 (and 5580) before our new container can.

Suppression is done by writing a runtime drop-in:

```
/run/systemd/system/zigbee-matter-manager.service.d/zzz-zmm-upgrade-norestart.conf
[Service]
Restart=no
```

Then `systemctl daemon-reload` and `systemctl stop zigbee-matter-manager.service`. With `Restart=no` overriding `Restart=always`, the stop is a real stop. `/run/systemd/system/` is wiped at reboot, so no permanent state.

`systemctl mask` was tried first but fails:
```
Failed to mask unit: File '/etc/systemd/system/<unit>.service' already exists
```
Mask works by creating a symlink to `/dev/null`. systemctl refuses to overwrite a real unit file. Drop-in override is the working alternative.

### Step B — the stop timeout

`podman stop -t 45` gives the container 45 seconds for clean shutdown via SIGTERM before escalating to SIGKILL. The 45s figure was chosen empirically: uvicorn + python-matter-server (subprocess) + zigpy + DuckDB writes + MQTT flush all need to drain.

The original implementation used `-t 15`, which caused SIGKILL escalation. SIGKILL leaves rootlessport (the userspace network proxy in slirp4netns) holding the published ports in a half-closed state. The new container then fails to bind because the port is "in use" — even though no process is actually serving on it. Combining `-t 45` with Step A's supervisor suppression eliminated the entire class of port-binding failures.

### Step C — run_container.sh

This script holds the canonical run arguments — caps, sysctls, devices, volumes. It must stay synchronised with `build.sh`'s `run_container()` function. Currently this is a manual sync; a future improvement is to refactor `build.sh` to expose `run_container` as a sourceable function.

The run args include:
- `--network=slirp4netns` (rootless networking)
- `--cap-add=NET_ADMIN,NET_RAW,SYS_ADMIN` (for OTBR and netfilter)
- `--sysctl net.ipv6.conf.all.forwarding=1` (Thread border routing)
- `--device /dev/net/tun` (OTBR tun interface)
- `--device /dev/serial/by-id/...` or `/dev/ttyACM0` / `/dev/ttyUSB0` (Zigbee dongle from `config.yaml`)
- `--device /dev/hci0` (Bluetooth, conditional on existence)
- `--volume /run/dbus:/run/dbus` (otbr-agent D-Bus)
- `--volume ${DATA_DIR}/{config,data,certs,logs}:/app/...` (persistent state)

### Step D — failed container log capture

When `podman run` fails (or runs but exits immediately), the script captures the new container's logs into `build.log` before rolling back:

```bash
"$RUNTIME" logs --tail=100 "$CONTAINER_NAME" >>"$BUILD_LOG" 2>&1
"$RUNTIME" inspect "$CONTAINER_NAME" 2>>"$BUILD_LOG" | head -100 >>"$BUILD_LOG"
```

This means the user sees the actual Python startup error (or OOM kill, or import failure) in the **View log** modal, instead of just a generic "container failed to start" message.

### Step E — health check

Polls `/api/system/health` from the host, every 3 seconds, up to `HEALTH_TIMEOUT` seconds total (default 60). Both `https://127.0.0.1:8000/api/system/health` and `http://127.0.0.1:8000/api/system/health` are tried — the URL is determined from `web.ssl.enabled` in `config.yaml`. The endpoint returns 200 as soon as the FastAPI app is listening — even before all services are fully initialized. This is intentional: full readiness can take 30+ seconds with 40+ devices, longer than the swap window allows.

If health check fails, rollback is automatic — no user action needed. The drop-in override is removed and the supervisor is restarted as part of the rollback so the previous container is supervised properly.

---

## Rollback flow

Two paths trigger rollback:

1. **Automatic** — the swap script catches a failure (run failed, health failed) and immediately swaps back.
2. **Manual** — the user clicks "Rollback to v1.3.1" in the UI.

Both paths use the same primitive: rename the failed/current container out of the way, rename the previous container back, start it.

```bash
# Manual rollback (do_rollback)
podman stop -t 15 zigbee-matter-manager
podman rename zigbee-matter-manager zigbee-matter-manager-failed-${timestamp}
podman start zigbee-matter-manager-previous   # OR re-run from previous_image_tag
podman rename zigbee-matter-manager-previous zigbee-matter-manager
```

The `-failed-${timestamp}` rename preserves the broken container for forensics — you can `podman logs` it to debug what went wrong, then `podman rm -f` it when done.

### What's retained for rollback

After every successful upgrade:
- `zigbee-matter-manager-previous` — the stopped previous container, ready to start instantly
- The previous version's image tag (`zigbee-matter-manager:1.3.1-amd64`)

Both are cleared on the *next* successful upgrade or by a manual `podman rm` / `gc` action. The retention policy keeps only the configured number of old images (default 2); the previous container itself is always retained as a single instance.

### What rollback does NOT restore

Rollback restores the **container and image**. It does NOT restore:

- `config.yaml` — user data is in a bind-mounted volume; both versions share it
- The Zigbee network state — same network database, same paired devices
- DuckDB telemetry — same files, same history
- Logs — appended, not version-scoped

This is by design. The user wants to revert to "the previous working version of the app code" — they don't want to lose three days of telemetry data because they rolled back. If a version introduces a breaking schema change to `config.yaml` or DuckDB, the rollback from that version is going to need manual intervention. The upgrade manager doesn't try to handle that case automatically; it would require schema versioning across all state files which is out of scope.

---

## GitHub polling

Every 6 hours, `periodic_check_loop` queries:

- `GET https://api.github.com/repos/${repo}/releases/latest` (channel: `stable`)
- `GET https://api.github.com/repos/${repo}/tags` (channel: `prerelease`)

Rate limit: GitHub allows 60 unauthenticated requests per hour per IP. With a 6-hour interval we use 4 requests per day, well under the limit. The check is also rate-limited internally to once per hour minimum (a force-check from the UI bypasses this).

### Version comparison

Tags like `v1.3.2` or `1.3.2` are parsed via the regex `^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$`. Pre-release suffixes are accepted but ignored for ordering — `v1.3.2-rc1` parses to `(1, 3, 2)` and is treated as equal to `v1.3.2`. This is technically incorrect per semver but acceptable for this use case (we never mix release and pre-release tags).

If a tag doesn't match the regex (e.g., `latest`, `stable`, `weekly`), it's silently skipped during channel scanning.

### What gets surfaced to the user

When a newer version is found, `version.json` is updated with:

```json
{
  "latest_available": "1.3.2",
  "latest_release_notes": "...first 500 chars of release body...",
  "latest_release_url": "https://github.com/.../releases/tag/v1.3.2",
  "last_check": "2026-04-25T12:00:00Z"
}
```

A WebSocket broadcast is also sent (`type: "upgrade_available"`) so the UI surfaces a toast notification immediately, not just on next page load.

---

## Auto-update

Off by default. When enabled in **Settings → Upgrade**:

1. Periodic check finds a new version
2. Check the configured quiet window (default 03:00–05:00 local time)
3. If inside the window AND `upgrade_state == "idle"`, automatically call `request_build`
4. The build runs in the background like any other build
5. Currently the auto-flow stops at `ready_to_swap` — auto-swap is **not** automatic

The deliberate choice not to auto-swap: the swap is the only step that interrupts service. We want the user to be aware of it, even if the build was unattended. A future enhancement could add an "auto-swap during quiet window" option for users who want fully unattended upgrades.

The quiet window check correctly handles wrap-around midnight (e.g., 23:00–05:00 means "between 11pm and 5am" not "never").

---

## SELinux interaction

Critical detail that bit hard during initial implementation: **SELinux blocks systemd from executing scripts under `/root/` or `~/`**.

The default targeted policy:
- `init_t` (systemd) cannot execute files labelled `admin_home_t` (`/root/`) or `user_home_t` (`~/`)
- `init_t` CAN execute files labelled `usr_t` (`/opt/`, `/usr/local/`)

If you place `upgrade.sh` under `/opt/.zigbee-matter-manager/scripts/` and reference it from a systemd unit, you'll see this in the audit log:

```
type=AVC: avc: denied { execute } for pid=1633047 comm="(grade.sh)"
  name="upgrade.sh"
  scontext=system_u:system_r:init_t:s0
  tcontext=unconfined_u:object_r:admin_home_t:s0
  tclass=file permissive=0
```

The fix is to put scripts under `/opt/zigbee-matter-manager/scripts/`, which is the FHS-standard location for add-on application packages and gets `usr_t` from the default policy. State files (lock, status, build.log) can stay in `~/.zigbee-matter-manager/` — SELinux only blocks *execution*, not read/write.

The `install_watcher.sh` script handles all of this:

```bash
SCRIPTS_DIR="${ZMM_SCRIPTS_DIR:-/opt/zigbee-matter-manager/scripts/}"

if [[ ! -d "$SCRIPTS_DIR" ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
        mkdir -p "$SCRIPTS_DIR"
    else
        sudo mkdir -p "$SCRIPTS_DIR"
        sudo chown "$USER:$USER" "$SCRIPTS_DIR"
    fi
fi

# Belt-and-braces relabel
if command -v restorecon >/dev/null 2>&1 && [[ -e /sys/fs/selinux/enforce ]]; then
    sudo restorecon -R "$SCRIPTS_DIR" >/dev/null 2>&1
fi
```

On non-SELinux systems (Ubuntu, Debian default), `restorecon` is a no-op and nothing changes.

---

## Cross-distro watcher

The host-side watcher must work on:
- Modern systemd-based distros (Fedora, RHEL, Ubuntu, Debian, Arch) — uses `systemd-path`
- systemd as root only (containers in containers, restricted environments) — same path unit but in `/etc/systemd/system/`
- Non-systemd distros (Alpine, some embedded) — falls back to a polling loop

`install_watcher.sh` detects which mode applies:

```bash
USE_SYSTEMD_USER=false
USE_SYSTEMD_SYSTEM=false
USE_POLLING=false

if command -v systemctl >/dev/null 2>&1; then
    if systemctl --user status >/dev/null 2>&1; then
        USE_SYSTEMD_USER=true
    elif [[ "$(id -u)" -eq 0 ]]; then
        USE_SYSTEMD_SYSTEM=true
    else
        USE_POLLING=true
    fi
else
    USE_POLLING=true
fi
```

The polling fallback is a simple `while true; do [[ -f trigger ]] && bash upgrade.sh; sleep 5; done` loop, daemonised via systemd-as-root if available, or via `nohup` + `@reboot` crontab as a last resort.

---

## Supervisor unit (the one that runs the container)

The container itself is run as a system-managed service. A typical unit at
`/etc/systemd/system/zigbee-matter-manager.service` looks like:

```ini
[Unit]
Description=Zigbee Matter Manager Container
After=network-online.target
Wants=network-online.target

[Service]
Restart=always
RestartSec=10
ExecStart=/usr/sbin/podman start -a zigbee-matter-manager
ExecStop=/usr/sbin/podman stop -t 15 zigbee-matter-manager

[Install]
WantedBy=multi-user.target
```

`upgrade.sh` finds this unit by trying these names in order (and both system
and user scope, system first):
1. `container-${CONTAINER_NAME}.service` (Podman quadlet / generated naming)
2. `${CONTAINER_NAME}.service` (manually-written, as above)

Override detection by exporting `ZMM_CONTAINER_UNIT="--system my-unit.service"` in the environment before invoking the watcher.

If no supervisor unit is found, swaps still work — the new container just won't be auto-restarted on host reboot. You'll see this in the watcher log:
```
Supervisor: no unit detected (continuing without mask)
```

---

## Container restart semantics

A subtle but important point: the running app **does not need to be aware of the upgrade**.

Specifically:
1. The Python upgrade_manager runs in the running container — version 1.3.1.
2. It writes a trigger; the host script builds the v1.3.2 image.
3. The host script stops the running container (1.3.1) and starts the new one (1.3.2).
4. The new container's Python upgrade_manager reads `/app/VERSION` (now `1.3.2`) and `version.json` (which the host updated during step 3) and presents the new state.

There is no "graceful handover" or "the old version finishing the upgrade" — the old version is just stopped. All upgrade state lives on disk in `version.json` and `status.json`, both bind-mounted, both read by whichever container is running. The Python module is stateless with respect to version transitions.

This is why hot-reloading wouldn't work and isn't attempted: Python module imports are sticky, and the running container is on stale code by definition during an upgrade. A clean container restart is the only sane path.

---

## Failure modes & recovery

A non-exhaustive list of failure modes, with diagnostic and recovery steps. Each was discovered the hard way during initial implementation.

### `unit-start-limit-hit` on the path unit

```
× zmm-upgrade.path
   Active: failed (Result: unit-start-limit-hit)
```

**Cause:** the systemd-path unit fired the service repeatedly because either (a) the trigger file persists due to a script crash, or (b) the path directive was wrong (`PathExists=` instead of `PathChanged=`).

**Recovery:**
```bash
sudo systemctl reset-failed zmm-upgrade.path zmm-upgrade.service
rm -f ~/.zigbee-matter-manager/data/upgrade/trigger
rm -f ~/.zigbee-matter-manager/data/upgrade/lock
sudo systemctl start zmm-upgrade.path
```

### `203/EXEC: Permission denied`

```
zmm-upgrade.service: Unable to locate executable '/opt/zigbee-matter-manager/scripts/upgrade.sh': Permission denied
```

**Cause:** SELinux denying execute, OR the file is missing the executable bit.

**Diagnose:**
```bash
ls -laZ /opt/zigbee-matter-manager/scripts/upgrade.sh
# Check 1: -rwxr-xr-x (executable bit)
# Check 2: system_u:object_r:usr_t:s0 (NOT *_home_t)

ausearch -m AVC -ts recent | grep upgrade.sh
# If you see "scontext=...:init_t ... tcontext=...:admin_home_t ... denied { execute }"
# → SELinux issue. Move scripts to /opt/zigbee-matter-manager/scripts/ and run install_watcher.sh.
```

**Recovery:**
```bash
sudo chmod 755 /opt/zigbee-matter-manager/scripts/*.sh
sudo restorecon -Rv /opt/zigbee-matter-manager/scripts/
```

### Supervisor unit fights the swap (`bind: address already in use`)

```
Error: rootlessport listen tcp 0.0.0.0:5580: bind: address already in use
```
or, in the watcher log:
```
Killing port-squatter PID NNNNN on port 8000
```

**Cause:** the supervisor systemd unit (`zigbee-matter-manager.service`) has `Restart=always`. When `upgrade.sh` does `podman stop`, the supervisor's `ExecStart=podman start -a` exits, systemd treats it as a service failure, and `RestartSec=10` later it re-launches the old container — binding the ports the new container is about to need.

**The current `upgrade.sh` handles this** by writing a runtime drop-in to `/run/systemd/system/<unit>.service.d/zzz-zmm-upgrade-norestart.conf` containing `Restart=no`, then stopping the unit. After a successful (or rolled-back) swap the drop-in is removed and the unit is started again.

If the watcher log shows `Failed to mask unit: File ... already exists`, you're on an older `upgrade.sh` that tried `systemctl mask`. Mask creates a symlink to `/dev/null` and refuses if a real unit file already lives at that path. Re-run `install_watcher.sh` to deploy the drop-in-based version.

**If the supervisor-fight has already left you with two containers running:**
```bash
sudo systemctl stop zigbee-matter-manager.service
sudo podman rm -f zigbee-matter-manager zigbee-matter-manager-previous

# Bring up a known-good version manually (substitute the version you trust)
sudo RUNTIME=podman \
     IMAGE_TAG=localhost/zigbee-matter-manager:2.0.1-amd64 \
     CONTAINER_NAME=zigbee-matter-manager \
     DATA_DIR=/opt/.zigbee-matter-manager \
     bash /opt/.zigbee-matter-manager/scripts/run_container.sh

# Verify
sudo podman ps | grep zigbee
curl -fsk https://127.0.0.1:8000/api/system/health && echo OK

# Re-arm the supervisor so reboot-resume works
sudo systemctl reset-failed zigbee-matter-manager.service
sudo systemctl enable zigbee-matter-manager.service
```

Note: do **not** `systemctl start` the supervisor while a manually-launched container with the same name is already running — the supervisor's `ExecStart=podman start -a` will fail with "container is already in the running state". Either stop the manual container first or just leave the supervisor stopped until next reboot.

### `Unknown command verb '<unit>.service'`

```
Unknown command verb 'zigbee-matter-manager.service'.
```

**Cause:** systemctl was called with the verb in the wrong position, e.g. `systemctl --system zigbee-matter-manager.service mask` instead of `systemctl --system mask zigbee-matter-manager.service`. systemctl interprets the unit name as the verb because that's where it expects the verb.

**Fix:** the supervisor helpers in `upgrade.sh` parse `unit_desc` (which holds e.g. `"--system zigbee-matter-manager.service"`) into separate `scope` and `unit` variables and place the verb between them:
```bash
read -r scope unit <<< "$unit_desc"
systemctl "$scope" mask "$unit"     # correct
```
If you're still seeing this, your deployed `upgrade.sh` is older than the one that introduced the split. Verify with:
```bash
grep -c 'read -r scope unit' /opt/.zigbee-matter-manager/scripts/upgrade.sh
# Should print 3 or more
```

### Wrong `ExecStart=` path in `zmm-upgrade.service`

Symptom: you edit `upgrade.sh` and the watcher log STILL shows old behaviour (e.g. messages from removed code paths).

**Cause:** the systemd unit's `ExecStart=` points to a different copy of the script than the one you're editing. `install_watcher.sh` historically used `/opt/zigbee-matter-manager/scripts/` (no dot), then later `/opt/.zigbee-matter-manager/scripts/` (with dot). If both copies exist, the unit runs whichever the unit file references.

**Diagnose:**
```bash
sudo systemctl cat zmm-upgrade.service | grep ExecStart
md5sum /opt/zigbee-matter-manager/scripts/upgrade.sh \
       /opt/.zigbee-matter-manager/scripts/upgrade.sh 2>/dev/null
```

**Recovery:**
```bash
# Re-deploy from a known canonical source and remove the dead copy
sudo install -m755 ./upgrade.sh /opt/.zigbee-matter-manager/scripts/upgrade.sh
sudo rm -f /opt/zigbee-matter-manager/scripts/upgrade.sh
sudo bash ./install_watcher.sh   # rewrites unit with current paths
sudo systemctl daemon-reload
```

### Dongle wedged after a SIGKILL'd swap (`NcpResetCode.ERROR_EXCEEDED_MAXIMUM_ACK_TIMEOUT_COUNT`)

Symptom in the new container's log:
```
WARNING - core - Startup Attempt 1 failed: NcpResetCode.ERROR_EXCEEDED_MAXIMUM_ACK_TIMEOUT_COUNT
```

**Cause:** an earlier process holding `/dev/ttyACM0` (or equivalent) was SIGKILL'd mid-session. The kernel's `cdc_acm` driver releases the device but the EFR32 firmware is mid-frame — bellows opens the port and the NCP doesn't ACK because it's still expecting frames from the dead session.

This typically follows a supervisor-fight scenario where `kill_port_squatters` (legacy code) or a manual `kill -9` killed a container that owned the dongle.

**Recovery:**
```bash
# Stop everything touching the dongle
sudo systemctl stop zigbee-matter-manager.service
sudo podman stop zigbee-matter-manager 2>/dev/null

# USB-bus reset (find the device first)
lsusb | grep -i 'CP210\|EFR32\|Sonoff'   # note the bus / device nums
# or unbind/rebind the cdc_acm driver:
ls /sys/bus/usb/drivers/cp210x/   # look for the entry like '1-1.4:1.0'
echo '1-1.4:1.0' | sudo tee /sys/bus/usb/drivers/cp210x/unbind
sleep 2
echo '1-1.4:1.0' | sudo tee /sys/bus/usb/drivers/cp210x/bind

# Restart container
sudo systemctl start zigbee-matter-manager.service
```

If the dongle keeps wedging across upgrades, the swap is killing it dirty. Confirm `upgrade.sh` is current (no `kill_port_squatters` calls in `do_swap`) — the new flow doesn't SIGKILL anything.

### Trigger directory instead of file

```bash
ls -la ~/.zigbee-matter-manager/data/upgrade/
drwxr-xr-x  ... trigger      # ← directory, not file!
-rw-r--r--  ... trigger.tmp  # accumulates because os.replace can't replace dir
```

**Cause:** the systemd-path unit had `MakeDirectory=true` set, which causes systemd to create the watched path **as a directory**. This breaks all subsequent file-based IPC.

**Recovery:**
```bash
sudo rm -rf ~/.zigbee-matter-manager/data/upgrade/trigger
sudo rm -f ~/.zigbee-matter-manager/data/upgrade/trigger.tmp
# Then re-run install_watcher.sh — the new version omits MakeDirectory=true
sudo bash ~/zigbee-matter-manager/scripts/install_watcher.sh
```

The current Python upgrade_manager also defensively checks for this on every trigger write:

```python
if os.path.isdir(TRIGGER_FILE):
    shutil.rmtree(TRIGGER_FILE)  # warn-and-recover
```

### Container OOM killed (`code=-9`)

```
[launcher] main.py exited code=-9 after 9.9s
```

**Cause:** the kernel OOM killer terminated the Python process. The launcher correctly falls back to the recovery server.

**Diagnose:**
```bash
sudo dmesg | grep -iE "out of memory|killed process" | tail -10
free -h                                 # how tight is RAM?
sudo podman stats --no-stream           # memory usage of running containers
```

**Mitigation:** set a memory limit on the container via `run_container.sh`:
```bash
--memory=1g --memory-swap=1.5g
```

This doesn't fix the OOM, but bounds it to the container instead of system-wide.

### `409 Conflict — Another upgrade in progress`

The UI shows this when `request_build` / `request_swap` finds the lock file held.

**Diagnose:**
```bash
ps aux | grep -E "podman build|upgrade.sh" | grep -v grep
```

If anything is running, **wait for it**. Builds typically take 2–10 minutes on x86_64 / Rock 5B with warm cache; up to 25 minutes on a cold cache.

If nothing is running, the lock is stale. The Python side auto-detects stale locks (PID dead OR age > 60min) and clears them. If you want to force-clear immediately, the UI surfaces a "Force-clear lock" option after a 409, or:

```bash
rm -f ~/.zigbee-matter-manager/data/upgrade/lock
```

### Stale "Failed" banner

The UI shows "Failed / Rolled back" indefinitely.

**Cause:** `status.json` retains `state: "failed"` until something writes over it.

**Recovery:** click **Dismiss** in the UI (it calls `POST /api/upgrade/reset-status`). Or manually:

```bash
echo '{"state":"idle","target_version":null,"updated_at":"'"$(date -u +%FT%TZ)"'","progress_percent":0,"current_step":"","error":null}' \
    > ~/.zigbee-matter-manager/data/upgrade/status.json
```

The next click of Build / Swap / Rollback also auto-clears stale failed state via `reset_status(only_if_failed=True)` — the user is implicitly retrying.

---

## Diagnostic commands

A condensed reference for live debugging:

```bash
# State of the trigger flow
ls -la /opt/.zigbee-matter-manager/data/upgrade/

# Watcher activity (host-side)
tail -100 /opt/.zigbee-matter-manager/logs/upgrade_watcher.log

# Build log (host-side, also surfaced in the UI)
tail -100 /opt/.zigbee-matter-manager/data/upgrade/build.log

# systemd unit state
systemctl status zmm-upgrade.path zmm-upgrade.service
systemctl cat zmm-upgrade.service

# Supervisor unit (the one that runs the container) and any active drop-in
systemctl status zigbee-matter-manager.service --no-pager
systemctl cat zigbee-matter-manager.service
ls /run/systemd/system/zigbee-matter-manager.service.d/ 2>/dev/null
# A file ending in -norestart.conf means a swap is in flight or aborted

# Lock contents
cat /opt/.zigbee-matter-manager/data/upgrade/lock

# Live status (what the UI is seeing)
cat /opt/.zigbee-matter-manager/data/upgrade/status.json | jq

# Persistent app state
cat /opt/.zigbee-matter-manager/data/state/version.json | jq

# What images exist?
podman images | grep zigbee

# What containers exist?
podman ps -a | grep zigbee

# Verify the deployed upgrade.sh matches the repo copy
md5sum /opt/.zigbee-matter-manager/scripts/upgrade.sh \
       ~/zigbee-matter-manager/scripts/upgrade.sh

# Test that upgrade.sh runs at all from systemd's perspective
sudo /opt/.zigbee-matter-manager/scripts/upgrade.sh
# Should print "[timestamp] Using container runtime: podman" and exit
```

---

## Manual recovery to a known-good version

When an upgrade has left the system in a broken state — two containers running on the same ports, a half-renamed `-previous`, a stuck `Restart=always` loop, or the supervisor unit refusing to come up — the recovery procedure is always the same:

```bash
# 1. Stop the supervisor and remove any stale drop-in
sudo systemctl stop zigbee-matter-manager.service
sudo rm -rf /run/systemd/system/zigbee-matter-manager.service.d/
sudo systemctl daemon-reload

# 2. Remove any container that's currently named zigbee-matter-manager
#    or zigbee-matter-manager-previous
sudo podman rm -f zigbee-matter-manager zigbee-matter-manager-previous 2>/dev/null

# 3. Pick a known-good image (check `podman images | grep zigbee` for tags)
#    and bring it up via run_container.sh — same args the swap flow uses.
sudo RUNTIME=podman \
     IMAGE_TAG=localhost/zigbee-matter-manager:2.0.1-amd64 \
     CONTAINER_NAME=zigbee-matter-manager \
     DATA_DIR=/opt/.zigbee-matter-manager \
     bash /opt/.zigbee-matter-manager/scripts/run_container.sh

# 4. Verify the app responds
sudo podman ps | grep zigbee
curl -fsk https://127.0.0.1:8000/api/system/health && echo OK

# 5. Re-arm the supervisor unit so a host reboot brings it back automatically.
#    Don't `start` it — the container is already running, and the unit's
#    ExecStart=podman start -a will fail with "already in the running state".
sudo systemctl reset-failed zigbee-matter-manager.service
sudo systemctl enable zigbee-matter-manager.service
```

Before retrying an upgrade, check `version.json` reflects reality:

```bash
sudo jq '.current_version, .current_image_tag' \
    /opt/.zigbee-matter-manager/data/state/version.json
```

If those fields don't match what's actually running, edit them by hand or trigger an upgrade to a different version (the swap will overwrite them on success).

---

## Roadmap

Known limitations and planned improvements:

1. **`run_container.sh` / `build.sh` sync.** Currently a manual sync between two files. Refactor `build.sh` to expose `run_container` as a sourceable function so there's one source of truth.
2. **Schema migration hooks.** No mechanism for "v1.4.0 needs to migrate `config.yaml` from schema v1 to v2 before starting". Currently relies on backward-compatible config parsing in the app.
3. **Auto-swap option.** Currently auto-update stops at `ready_to_swap`. Add an "auto-swap during quiet window" toggle for users who want fully unattended upgrades.
4. **Multi-step rollback.** Currently retains only the immediate previous version. Could add a "rollback chain" of N versions, but disk usage on a Rock 5B is the limiting factor (each image is ~1.5–2 GB).
5. **Health check granularity.** `/api/status` returning 200 means "the app started". Could add a `/api/status?ready=true` variant that only returns 200 when all services are fully initialized — but with 40+ devices this can take 30+ seconds, longer than the 60-second swap window allows.
6. **Build resumability.** A failed build today must be re-run from scratch. Podman caches layers internally, so the rebuild is fast — but the framework could expose a "Resume" button instead of "Build again".
7. **GitHub authentication.** Currently unauthenticated (60 req/hr). For users hitting the limit, add an optional `github.token` to `config.yaml` for 5000 req/hr.

---

## Related Documentation

- [README — In-App Upgrades](../README.md#-in-app-upgrades) — feature overview and quick start
- [docs/structure.md](structure.md) — full project file layout
- [docs/multipan.md](multipan.md) — MultiPAN container internals (relevant for understanding the swap timing on Sonoff MG24 systems)