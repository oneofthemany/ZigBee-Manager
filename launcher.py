#!/usr/bin/env python3
"""
ZMM Launcher (stdlib-only)
==========================
Supervises main.py. If main.py fails to stay up long enough to be
considered healthy, capture its crash traceback and launch the
disaster-recovery HTTP server instead — so the user can identify
the broken file and fix it via the browser without podman exec.

Exit codes consumed from children:
  main.py         0 = clean shutdown (exit supervisor)
                  ≠0 = died
  recovery_server 0 = user clicked "Restart service" (retry main.py)
                  ≠0 = user gave up (exit supervisor)

Sequence:
  ┌────────────────────────────────────────────────┐
  │ Boot guard          (stdlib, rolls back batch) │
  │ main.py             (FastAPI app)              │
  │   ├── clean exit   → stop                      │
  │   ├── crash <HEALTHY_SECONDS → recovery_server │
  │   └── crash >HEALTHY_SECONDS → restart main.py │
  │ recovery_server.py  (fallback UI on :8000)     │
  └────────────────────────────────────────────────┘
"""

import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)

DATA_DIR = os.path.join(APP_DIR, "data")
LOG_DIR = os.path.join(APP_DIR, "logs")
CRASH_FILE = os.path.join(DATA_DIR, "last_crash.json")
LAUNCHER_LOG = os.path.join(LOG_DIR, "launcher.log")
BOOT_GUARD = os.path.join(APP_DIR, "boot_guard.py")
MAIN_PY = os.path.join(APP_DIR, "main.py")
RECOVERY_PY = os.path.join(APP_DIR, "recovery_server.py")

# If main.py dies in under this many seconds it's a "boot crash"
HEALTHY_SECONDS = 25
# Cap on quick-retry loops (avoids tight crashloops when recovery is unavailable)
MAX_QUICK_RETRIES = 3
# Seconds to sleep between runtime-level restarts
RESTART_BACKOFF = 2


# ----------------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------------

def _log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [launcher] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LAUNCHER_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# CHILD PROCESS HANDLING
# ----------------------------------------------------------------------------

_current_child: "subprocess.Popen | None" = None


def _forward_signal(sig, frame):
    """Forward TERM/INT to the current child so shutdown is clean."""
    child = _current_child
    if child and child.poll() is None:
        try:
            child.send_signal(sig)
        except Exception:
            pass


signal.signal(signal.SIGTERM, _forward_signal)
signal.signal(signal.SIGINT, _forward_signal)


def _run_child(script: str, capture_stderr: bool) -> "tuple[int, str, float]":
    """Run a Python script. Return (returncode, stderr_text, elapsed)."""
    global _current_child
    start = time.time()

    stderr_target = subprocess.PIPE if capture_stderr else None
    proc = subprocess.Popen(
        [sys.executable, "-u", script],
        stdout=None,           # let stdout flow through
        stderr=stderr_target,
        env=os.environ.copy(),
        cwd=APP_DIR,
    )
    _current_child = proc

    stderr_chunks = []
    if capture_stderr and proc.stderr is not None:
        # Stream stderr line-by-line so the user still sees it live
        try:
            for raw in iter(proc.stderr.readline, b""):
                try:
                    sys.stderr.buffer.write(raw)
                    sys.stderr.flush()
                except Exception:
                    pass
                stderr_chunks.append(raw)
        except Exception as e:
            _log(f"stderr stream error: {e}")

    proc.wait()
    _current_child = None
    elapsed = time.time() - start
    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return proc.returncode, stderr_text, elapsed


# ----------------------------------------------------------------------------
# CRASH CAPTURE
# ----------------------------------------------------------------------------

def _parse_crash_from_stderr(stderr_text: str, exit_code: int) -> dict:
    """
    Extract the most useful crash info from a stderr dump.
    Finds the deepest in-app frame so the user sees THEIR broken file,
    not an internal framework frame.
    """
    frames = re.findall(r'File "([^"]+)", line (\d+)', stderr_text)
    suspect_file = None
    suspect_line = None
    for fn, ln in reversed(frames):
        # Prefer frames inside /app and exclude site-packages
        if ("/app/" in fn or fn.startswith("./") or not fn.startswith("/")) \
                and "site-packages" not in fn:
            suspect_file = fn
            suspect_line = int(ln)
            break
    if suspect_file is None and frames:
        suspect_file, suspect_line = frames[-1][0], int(frames[-1][1])

    # Last non-empty line of stderr is usually `ExceptionType: message`
    exc_type = "Unknown"
    exc_value = ""
    for line in reversed(stderr_text.strip().splitlines()):
        line = line.strip()
        if not line or line.startswith("  ") or line.startswith("File "):
            continue
        m = re.match(r'^([A-Za-z_][\w\.]*Error|Exception|SyntaxError|ImportError|'
                     r'NameError|AttributeError|TypeError|ValueError|KeyError|'
                     r'ModuleNotFoundError|IndentationError|RuntimeError)'
                     r'(?::\s*(.*))?$', line)
        if m:
            exc_type = m.group(1)
            exc_value = (m.group(2) or "").strip()
            break

    # Normalise suspect path to a repo-relative path where possible
    suspect_rel = suspect_file
    if suspect_rel:
        if suspect_rel.startswith(APP_DIR + "/"):
            suspect_rel = suspect_rel[len(APP_DIR) + 1:]
        elif suspect_rel.startswith("/app/"):
            suspect_rel = suspect_rel[5:]
        elif suspect_rel.startswith("./"):
            suspect_rel = suspect_rel[2:]

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "exc_type": exc_type,
        "exc_value": exc_value,
        "suspect_file": suspect_file,
        "suspect_file_rel": suspect_rel,
        "suspect_line": suspect_line,
        "traceback": stderr_text[-12000:],
        "exit_code": exit_code,
        "source": "launcher",
    }


def _write_crash(info: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CRASH_FILE, "w") as f:
            json.dump(info, f, indent=2)
        _log(f"Crash recorded: {info.get('exc_type')}: {info.get('exc_value')} "
             f"@ {info.get('suspect_file_rel')}:{info.get('suspect_line')}")
    except Exception as e:
        _log(f"Failed to write crash file: {e}")


def _main_wrote_crash_recently(elapsed_since_start: float) -> bool:
    """main.py installs its own excepthook that writes last_crash.json.
    If it succeeded, prefer that over our stderr parse."""
    try:
        if not os.path.isfile(CRASH_FILE):
            return False
        mtime = os.stat(CRASH_FILE).st_mtime
        return (time.time() - mtime) < (elapsed_since_start + 5)
    except Exception:
        return False


# ----------------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------------

def main():
    _log(f"Launcher starting (APP_DIR={APP_DIR})")

    quick_retries = 0
    while True:
        # 1. Boot guard (best effort — never blocks)
        if os.path.isfile(BOOT_GUARD):
            rc, _, _ = _run_child(BOOT_GUARD, capture_stderr=False)
            _log(f"Boot guard exit={rc}")

        # 2. Main app
        if not os.path.isfile(MAIN_PY):
            _log(f"FATAL: {MAIN_PY} not found")
            return 1

        _log("Starting main.py ...")
        code, stderr_text, elapsed = _run_child(MAIN_PY, capture_stderr=True)
        _log(f"main.py exited code={code} after {elapsed:.1f}s")

        if code == 0:
            _log("Clean shutdown — launcher exiting")
            return 0

        # Negative exit codes mean main.py was killed by a signal.
        # SIGTERM (-15) and SIGINT (-2) are POLITE shutdown requests:
        #   - `podman stop` sends SIGTERM (this is what an upgrade swap does)
        #   - Ctrl+C sends SIGINT
        # Either way, main.py didn't crash — someone asked it to leave.
        # Don't trigger recovery; just exit cleanly so the container stops.
        # SIGKILL (-9) is intentionally NOT caught here — that IS recovery-worthy
        # because it usually means OOM or a forced kill we should investigate.
        if code in (-15, -2):
            sig_name = "SIGTERM" if code == -15 else "SIGINT"
            _log(f"main.py terminated by {sig_name} after {elapsed:.1f}s "
                 f"(graceful shutdown request) — launcher exiting")
            return 0

        if elapsed >= HEALTHY_SECONDS:
            # Ran healthy then died — just restart, keep it simple
            _log(f"Runtime crash (lived {elapsed:.1f}s) — restart in {RESTART_BACKOFF}s")
            quick_retries = 0
            time.sleep(RESTART_BACKOFF)
            continue

        # Boot-time crash — capture and go to recovery
        if not _main_wrote_crash_recently(elapsed):
            _write_crash(_parse_crash_from_stderr(stderr_text, code))

        quick_retries += 1
        if quick_retries > MAX_QUICK_RETRIES:
            _log(f"Too many quick boot failures ({quick_retries}) — entering recovery mode unconditionally")

        if not os.path.isfile(RECOVERY_PY):
            _log(f"FATAL: recovery server missing at {RECOVERY_PY} — sleeping and retrying main.py")
            time.sleep(10)
            continue

        _log("Launching recovery server ...")
        rc, _, _ = _run_child(RECOVERY_PY, capture_stderr=False)
        _log(f"Recovery server exited code={rc}")

        if rc == 0:
            # User clicked "Restart service" — loop and retry main.py
            _log("Recovery requested restart — retrying main.py")
            quick_retries = 0
            continue
        else:
            _log("Recovery did not request restart — launcher exiting")
            return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _log("Interrupted — exiting")
        sys.exit(0)
    except Exception as e:
        _log(f"Launcher crashed: {e}")
        sys.exit(1)