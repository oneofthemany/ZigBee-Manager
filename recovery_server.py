#!/usr/bin/env python3
"""
ZMM Disaster Recovery Server (stdlib-only)
===========================================
Runs when main.py fails to start. Serves a single-page UI on :8000
that shows the last crash traceback, highlights the suspect file,
and lets the user either:
  • Restore a known-good backup from .editor_backups/
  • Upload a replacement file from their machine
  • Restart the service

NO third-party imports — survives if the venv/app is half-broken.
"""

import base64
import html
import http.server
import json
import logging
import os
import shutil
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
BACKUP_DIR = os.path.join(APP_DIR, ".editor_backups")
LOG_DIR = os.path.join(APP_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "recovery.log")
CRASH_FILE = os.path.join(DATA_DIR, "last_crash.json")
RECOVERY_MARKER = os.path.join(DATA_DIR, ".recovery_active")

PORT = int(os.environ.get("ZMM_PORT", 8000))


# ----------------------------------------------------------------------------
# SSL DETECTION (stdlib-only — survives if PyYAML / config_enhanced is broken)
# ----------------------------------------------------------------------------
def _detect_ssl_config():
    """
    Best-effort detection of SSL settings from config.yaml without depending
    on PyYAML (which may itself be broken if the venv is unhealthy).

    Looks for the structure:
        web:
          ssl:
            enabled: true
            certfile: ./data/certs/cert.pem
            keyfile:  ./data/certs/key.pem

    Returns: dict with keys {enabled, certfile, keyfile}. enabled defaults
    to False if the file is missing, malformed, or lacks the section.
    """
    result = {"enabled": False, "certfile": None, "keyfile": None}
    config_path = os.path.join(APP_DIR, "config", "config.yaml")
    if not os.path.isfile(config_path):
        return result

    try:
        with open(config_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return result

    in_web = False
    in_ssl = False
    for raw in lines:
        line = raw.rstrip("\n")
        # Top-level key (no indent)
        if line and not line.startswith(" ") and not line.startswith("#"):
            in_web = line.startswith("web:")
            in_ssl = False
            continue
        if not in_web:
            continue
        # Inside web: — look for ssl: stanza (2-space indent)
        if line.startswith("  ssl:"):
            in_ssl = True
            continue
        # Detect leaving ssl: stanza (next 2-space-indent sibling key)
        if in_ssl and line.startswith("  ") and not line.startswith("    ") and ":" in line:
            in_ssl = False
        if not in_ssl:
            continue

        # Parse keys inside ssl: (4-space indent)
        stripped = line.strip()
        if stripped.startswith("enabled:"):
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'").lower()
            result["enabled"] = val in ("true", "yes", "1")
        elif stripped.startswith("certfile:"):
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            if val:
                result["certfile"] = val
        elif stripped.startswith("keyfile:"):
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            if val:
                result["keyfile"] = val

    # Resolve relative paths against APP_DIR (matches main.py behaviour)
    for k in ("certfile", "keyfile"):
        if result[k] and not os.path.isabs(result[k]):
            result[k] = os.path.normpath(os.path.join(APP_DIR, result[k]))

    return result


# Directories the recovery UI is allowed to write into (mirrors editor_routes)
ALLOWED_WRITE_DIRS = [
    "",
    "core",
    "routes",
    "modules",
    "handlers",
    "static",
    "static/js",
    "static/js/modal",
    "static/css",
    "config",
    "config/matter_definitions",
]

MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # 4 MB

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - recovery - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, mode="a"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("recovery")


# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------

def _safe_rel_path(rel: str) -> "str | None":
    """Resolve rel against APP_DIR, reject anything outside."""
    if not rel:
        return None
    clean = rel.replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(APP_DIR, clean))
    if not full.startswith(os.path.realpath(APP_DIR) + os.sep) and full != os.path.realpath(APP_DIR):
        return None
    return full


def _path_is_writable(rel: str) -> bool:
    """Allow writes only under one of ALLOWED_WRITE_DIRS, with an editable suffix."""
    clean = rel.replace("\\", "/").lstrip("/")
    # Root .py/.js/.html/etc. files are allowed (ALLOWED_WRITE_DIRS contains "")
    ok_dir = False
    for d in ALLOWED_WRITE_DIRS:
        if d == "":
            # Root — only allow if file has no sub-dir
            if "/" not in clean:
                ok_dir = True
                break
        elif clean.startswith(d + "/"):
            ok_dir = True
            break
    if not ok_dir:
        return False
    allowed_ext = {".py", ".js", ".css", ".html", ".yaml", ".yml",
                   ".json", ".md", ".txt", ".conf", ".sh"}
    _, ext = os.path.splitext(clean)
    return ext.lower() in allowed_ext


def _read_crash() -> dict:
    try:
        if os.path.isfile(CRASH_FILE):
            with open(CRASH_FILE) as f:
                return json.load(f)
    except Exception as e:
        log.error(f"Failed to read crash file: {e}")
    return {}


def _list_backups(path_filter: str = None) -> list:
    if not os.path.isdir(BACKUP_DIR):
        return []
    filter_prefix = None
    if path_filter:
        filter_prefix = path_filter.replace("/", "_").replace("\\", "_") + "."

    items = []
    for fname in os.listdir(BACKUP_DIR):
        if not fname.endswith(".bak"):
            continue
        if filter_prefix and not fname.startswith(filter_prefix):
            continue
        full = os.path.join(BACKUP_DIR, fname)
        try:
            st = os.stat(full)
            # Extract probable original path (everything before the first timestamp)
            # names look like: "static_js_foo.js.20260423_120000.bak"
            # and:             "modules_foo.py.20260423_120000.batch_....test_recovery.bak"
            stem = fname
            # Split on ".YYYYMMDD_" to recover the safe_name prefix
            import re
            m = re.match(r"^(.+?)\.(\d{8}_\d{6})(\..+)?\.bak$", fname)
            if m:
                safe_name = m.group(1)
                ts = m.group(2)
                # Best-effort reverse of path.replace("/", "_"): not unambiguous,
                # so just show the safe_name as-is
                display_original = safe_name
            else:
                safe_name = fname
                ts = ""
                display_original = fname
            items.append({
                "name": fname,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "created": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "probable_original": display_original,
                "timestamp": ts,
            })
        except Exception:
            continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def _read_file_head(full_path: str, max_bytes: int = 200_000) -> "str | None":
    try:
        if not os.path.isfile(full_path):
            return None
        size = os.path.getsize(full_path)
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(max_bytes)
        if size > max_bytes:
            data += f"\n\n[... truncated, {size - max_bytes} bytes omitted ...]"
        return data
    except Exception as e:
        log.error(f"read_file_head({full_path}): {e}")
        return None


# ----------------------------------------------------------------------------
# HTTP HANDLER
# ----------------------------------------------------------------------------

class RecoveryHandler(http.server.BaseHTTPRequestHandler):

    # Silence default noisy access log
    def log_message(self, fmt, *args):
        log.info("%s - %s" % (self.address_string(), fmt % args))

    # ---- low-level helpers --------------------------------------------------

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, text: str):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> "dict | None":
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            return None
        if length <= 0 or length > MAX_UPLOAD_BYTES * 2:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    # ---- routing ------------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/recovery":
            return self._html(200, INDEX_HTML.encode("utf-8"))

        if path == "/api/status":
            return self._json(200, {"status": "recovery_mode", "ok": True})

        if path == "/api/recovery/status":
            crash = _read_crash()
            return self._json(200, {"mode": "recovery", "crash": crash})

        if path == "/api/recovery/backups":
            q = self._query()
            return self._json(200, {"backups": _list_backups(q.get("path"))})

        if path == "/api/recovery/backup-content":
            q = self._query()
            name = q.get("name")
            if not name or "/" in name or "\\" in name or ".." in name:
                return self._json(400, {"error": "Invalid backup name"})
            full = os.path.join(BACKUP_DIR, name)
            if not os.path.isfile(full):
                return self._json(404, {"error": "Backup not found"})
            content = _read_file_head(full)
            return self._json(200, {"name": name, "content": content or ""})

        if path == "/api/recovery/file-content":
            q = self._query()
            rel = q.get("path")
            full = _safe_rel_path(rel) if rel else None
            if not full:
                return self._json(400, {"error": "Invalid path"})
            if not os.path.isfile(full):
                return self._json(404, {"error": "File not found"})
            content = _read_file_head(full)
            return self._json(200, {"path": rel, "content": content or ""})

        if path == "/api/recovery/tail-log":
            # Last ~400 lines of zigbee.log — useful supplementary context
            logp = os.path.join(LOG_DIR, "zigbee.log")
            try:
                if os.path.isfile(logp):
                    with open(logp, "rb") as f:
                        f.seek(0, os.SEEK_END)
                        size = f.tell()
                        chunk = min(size, 64 * 1024)
                        f.seek(size - chunk)
                        data = f.read().decode("utf-8", errors="replace")
                    return self._json(200, {"log": data[-48000:]})
            except Exception as e:
                return self._json(200, {"log": f"[failed to read log: {e}]"})
            return self._json(200, {"log": "(no zigbee.log found)"})

        return self._json(404, {"error": "Not found", "path": path})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/recovery/upload-file":
            return self._upload_file()

        if path == "/api/recovery/restore-backup":
            return self._restore_backup()

        if path == "/api/recovery/clear-pending":
            return self._clear_pending()

        if path == "/api/recovery/restart":
            return self._restart()

        return self._json(404, {"error": "Not found"})

    # ---- handlers -----------------------------------------------------------

    def _upload_file(self):
        data = self._read_json_body()
        if not data:
            return self._json(400, {"error": "Invalid JSON body"})
        rel = data.get("path")
        content = data.get("content")
        content_b64 = data.get("content_base64")
        if not rel or (content is None and content_b64 is None):
            return self._json(400, {"error": "path and content (or content_base64) required"})
        if not _path_is_writable(rel):
            return self._json(403, {"error": f"Path not writable: {rel}"})

        full = _safe_rel_path(rel)
        if not full:
            return self._json(400, {"error": "Invalid path"})

        try:
            if content_b64 is not None:
                raw = base64.b64decode(content_b64)
                if len(raw) > MAX_UPLOAD_BYTES:
                    return self._json(413, {"error": "File too large"})
                os.makedirs(os.path.dirname(full), exist_ok=True)
                # Pre-upload backup of the existing file
                self._snapshot_existing(full, rel)
                with open(full, "wb") as f:
                    f.write(raw)
            else:
                if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
                    return self._json(413, {"error": "File too large"})
                os.makedirs(os.path.dirname(full), exist_ok=True)
                self._snapshot_existing(full, rel)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(content)
            log.warning(f"Recovery upload applied: {rel}")
            self._touch_marker()
            return self._json(200, {"success": True, "path": rel})
        except Exception as e:
            log.error(f"Upload failed: {e}")
            return self._json(500, {"error": str(e)})

    def _restore_backup(self):
        data = self._read_json_body()
        if not data:
            return self._json(400, {"error": "Invalid JSON body"})
        name = data.get("backup")
        rel = data.get("path")
        if not name or not rel:
            return self._json(400, {"error": "backup and path required"})
        if "/" in name or "\\" in name or ".." in name:
            return self._json(400, {"error": "Invalid backup name"})
        if not _path_is_writable(rel):
            return self._json(403, {"error": f"Path not writable: {rel}"})
        backup_full = os.path.join(BACKUP_DIR, name)
        target_full = _safe_rel_path(rel)
        if not target_full:
            return self._json(400, {"error": "Invalid target path"})
        if not os.path.isfile(backup_full):
            return self._json(404, {"error": "Backup not found"})

        try:
            os.makedirs(os.path.dirname(target_full), exist_ok=True)
            self._snapshot_existing(target_full, rel)
            shutil.copy2(backup_full, target_full)
            log.warning(f"Recovery restored {rel} from {name}")
            self._touch_marker()
            return self._json(200, {"success": True, "path": rel, "backup": name})
        except Exception as e:
            log.error(f"Restore failed: {e}")
            return self._json(500, {"error": str(e)})

    def _clear_pending(self):
        """Remove any pending test batch marker so boot_guard doesn't re-rollback
        after a manual recovery fix."""
        removed = []
        for fname in (".test_pending", ".boot_failures"):
            p = os.path.join(DATA_DIR, fname)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    removed.append(fname)
                except Exception as e:
                    log.error(f"Failed to remove {p}: {e}")
        return self._json(200, {"success": True, "removed": removed})

    def _restart(self):
        log.warning("Recovery: user requested restart — exiting 0")
        self._json(200, {"success": True, "message": "Restarting service"})
        try:
            self.wfile.flush()
        except Exception:
            pass
        # Exit cleanly so the launcher retries main.py
        threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()

    # ---- internals ----------------------------------------------------------

    def _snapshot_existing(self, full_path: str, rel: str):
        """Before overwriting, take a recovery-tagged backup."""
        if not os.path.isfile(full_path):
            return
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            safe = rel.replace("/", "_").replace("\\", "_")
            ts = time.strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(BACKUP_DIR, f"{safe}.{ts}.pre_recovery.bak")
            shutil.copy2(full_path, dest)
        except Exception as e:
            log.warning(f"Pre-recovery snapshot failed for {rel}: {e}")

    def _touch_marker(self):
        try:
            with open(RECOVERY_MARKER, "w") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "iso": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
                }))
        except Exception:
            pass


# ----------------------------------------------------------------------------
# EMBEDDED SINGLE-PAGE UI
# ----------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ZMM Recovery</title>
<style>
  :root {
    --bg:#1b1d1f; --panel:#24272a; --panel2:#2d3135; --fg:#eaeaea; --muted:#9aa0a6;
    --accent:#ff9f43; --ok:#30c75f; --err:#ff5b5b; --warn:#ffca28; --border:#3a3e43;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         font-size:14px; }
  header { background:#1a1a1a; border-bottom:2px solid var(--accent);
           padding:14px 20px; display:flex; align-items:center; gap:14px; }
  header .badge { background:var(--err); color:#fff; padding:3px 9px;
                  border-radius:4px; font-weight:700; font-size:11px; letter-spacing:0.6px; }
  header h1 { margin:0; font-size:18px; font-weight:600; }
  header .sub { color:var(--muted); font-size:12px; }
  main { padding:18px; max-width:1200px; margin:0 auto; }
  .grid { display:grid; grid-template-columns: 1fr 1fr; gap:14px; }
  @media (max-width:900px) { .grid { grid-template-columns: 1fr; } }
  .panel { background:var(--panel); border:1px solid var(--border); border-radius:8px;
           padding:14px 16px; margin-bottom:14px; }
  .panel h2 { margin:0 0 10px; font-size:14px; text-transform:uppercase;
              letter-spacing:1px; color:var(--accent); font-weight:600; }
  .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .row > * { min-width:0; }
  label { font-size:12px; color:var(--muted); display:block; margin:8px 0 4px; }
  input[type=text], select, textarea {
    background:var(--panel2); color:var(--fg); border:1px solid var(--border);
    border-radius:4px; padding:7px 9px; font-family:var(--mono); font-size:13px; width:100%;
  }
  textarea { min-height:140px; resize:vertical; white-space:pre; overflow:auto; }
  button { background:var(--accent); color:#111; border:0; padding:8px 14px;
           border-radius:4px; font-weight:600; font-size:13px; cursor:pointer; }
  button.secondary { background:var(--panel2); color:var(--fg); border:1px solid var(--border); }
  button.danger    { background:var(--err); color:#fff; }
  button.ok        { background:var(--ok); color:#fff; }
  button:disabled  { opacity:0.5; cursor:not-allowed; }
  pre.trace { background:#101214; border:1px solid var(--border); border-radius:6px;
              padding:10px 12px; font-family:var(--mono); font-size:12px; color:#d0d0d0;
              overflow:auto; max-height:340px; white-space:pre; }
  .k { color:var(--muted); font-size:12px; }
  .v { font-family:var(--mono); font-size:13px; color:var(--fg); word-break:break-all; }
  .suspect { background:rgba(255,91,91,0.12); border-left:3px solid var(--err);
             padding:10px 12px; border-radius:4px; margin:10px 0; }
  .suspect .file { font-family:var(--mono); color:var(--err); font-weight:600; font-size:14px; }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--border);
           vertical-align:middle; }
  th { color:var(--muted); font-weight:500; text-transform:uppercase; font-size:11px; letter-spacing:0.5px; }
  tr:hover td { background:rgba(255,255,255,0.02); }
  .pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px;
          background:var(--panel2); color:var(--muted); border:1px solid var(--border); }
  .notice { background:rgba(48,199,95,0.12); border:1px solid var(--ok); color:var(--ok);
            padding:8px 12px; border-radius:4px; font-size:13px; margin:10px 0; }
  .error  { background:rgba(255,91,91,0.12); border:1px solid var(--err); color:var(--err);
            padding:8px 12px; border-radius:4px; font-size:13px; margin:10px 0; }
  .spacer { height:8px; }
  .tiny { font-size:11px; color:var(--muted); }
  .filerow .path { font-family:var(--mono); font-size:12px; }
  .mono { font-family:var(--mono); }
</style>
</head>
<body>

<header>
  <span class="badge">RECOVERY MODE</span>
  <div>
    <h1>ZigBee-Matter-Manager — Disaster Recovery</h1>
    <div class="sub">Main application failed to start. Fix the broken file below and restart.</div>
  </div>
</header>

<main>

  <div id="crashPanel" class="panel">
    <h2>Last crash</h2>
    <div id="crashBody"><div class="tiny">Loading…</div></div>
  </div>

  <div class="grid">

    <div class="panel">
      <h2>1. Restore a backup</h2>
      <div class="row">
        <input id="bkFilter" type="text" placeholder="Filter by project path (e.g. modules/test_recovery.py)"/>
        <button class="secondary" onclick="loadBackups()">Refresh</button>
      </div>
      <label>Target file (project-relative)</label>
      <input id="bkTarget" type="text" placeholder="modules/test_recovery.py"/>
      <div class="spacer"></div>
      <div style="max-height:300px; overflow:auto; border:1px solid var(--border); border-radius:4px;">
        <table id="bkTable">
          <thead><tr><th>Backup</th><th>Created</th><th>Size</th><th></th></tr></thead>
          <tbody><tr><td colspan="4" class="tiny">Loading…</td></tr></tbody>
        </table>
      </div>
      <div class="tiny">The current file will be snapshotted before being overwritten.</div>
    </div>

    <div class="panel">
      <h2>2. Upload a replacement</h2>
      <label>Target file (project-relative)</label>
      <input id="upPath" type="text" placeholder="modules/test_recovery.py"/>
      <label>Pick file from disk</label>
      <input id="upFile" type="file"/>
      <label>…or paste contents</label>
      <textarea id="upContent" placeholder="# paste full file contents here"></textarea>
      <div class="spacer"></div>
      <div class="row">
        <button onclick="uploadFile()">Upload &amp; save</button>
        <span class="tiny">File is written to /app/&lt;path&gt;. Existing file is snapshotted first.</span>
      </div>
    </div>

  </div>

  <div class="panel">
    <h2>3. Restart service</h2>
    <div class="row">
      <button class="ok" onclick="clearPending()">Clear pending test markers</button>
      <span class="tiny">Only needed if a failed test-deploy is stuck.</span>
    </div>
    <div class="spacer"></div>
    <div class="row">
      <button class="danger" onclick="restart()">Restart now</button>
      <span class="tiny">Exits recovery. The launcher will re-run main.py.</span>
    </div>
    <div id="opResult"></div>
  </div>

  <div class="panel">
    <h2>Tail of zigbee.log</h2>
    <div class="row"><button class="secondary" onclick="loadLog()">Refresh log</button></div>
    <pre id="logTail" class="trace" style="max-height:260px;">(click refresh)</pre>
  </div>

</main>

<script>
const $ = sel => document.querySelector(sel);

function flash(msg, ok) {
  const el = $('#opResult');
  el.innerHTML = `<div class="${ok ? 'notice' : 'error'}">${escapeHtml(msg)}</div>`;
  if (ok) setTimeout(()=>{ el.innerHTML=''; }, 4000);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

async function loadCrash() {
  try {
    const r = await fetch('/api/recovery/status');
    const d = await r.json();
    const c = d.crash || {};
    const host = $('#crashBody');
    if (!c.timestamp && !c.traceback) {
      host.innerHTML = '<div class="tiny">No crash record found. You may be here because you hit the recovery URL directly.</div>';
      return;
    }
    const suspectFile = c.suspect_file_rel || c.suspect_file || '?';
    const suspectLine = c.suspect_line || '?';
    host.innerHTML = `
      <div class="row">
        <div><span class="k">When:</span> <span class="v">${escapeHtml(c.timestamp || '?')}</span></div>
        <div><span class="k">Exit:</span> <span class="v">${escapeHtml(String(c.exit_code ?? '?'))}</span></div>
        <div><span class="k">Source:</span> <span class="pill">${escapeHtml(c.source || '?')}</span></div>
      </div>
      <div class="suspect">
        <div><span class="k">Suspect file:</span>
          <span class="file">${escapeHtml(suspectFile)}</span>
          <span class="k"> line </span><span class="file">${escapeHtml(String(suspectLine))}</span>
        </div>
        <div class="spacer"></div>
        <div><span class="k">Exception:</span>
          <span class="v">${escapeHtml(c.exc_type || '?')}</span>
          <span class="v" style="color:var(--warn)">${c.exc_value ? ': ' + escapeHtml(c.exc_value) : ''}</span>
        </div>
        <div class="spacer"></div>
        <div class="row">
          <button class="secondary" onclick="useSuspect()">Use suspect file as target</button>
          <button class="secondary" onclick="viewSuspect()">View current content</button>
        </div>
      </div>
      <details>
        <summary class="tiny" style="cursor:pointer">Traceback</summary>
        <pre class="trace">${escapeHtml(c.traceback || '(no traceback captured)')}</pre>
      </details>
    `;
    // Pre-fill the two target inputs
    if (suspectFile && suspectFile !== '?') {
      $('#bkTarget').value = suspectFile;
      $('#upPath').value = suspectFile;
      $('#bkFilter').value = suspectFile;
      loadBackups();
    }
  } catch (e) {
    $('#crashBody').innerHTML = `<div class="error">Failed to load crash info: ${escapeHtml(e.message)}</div>`;
  }
}

function useSuspect() {
  const file = $('#bkTarget').value;
  if (file) { $('#upPath').value = file; $('#bkFilter').value = file; loadBackups(); }
}

async function viewSuspect() {
  const p = $('#bkTarget').value;
  if (!p) { flash('Set the target file first', false); return; }
  const r = await fetch('/api/recovery/file-content?path=' + encodeURIComponent(p));
  const d = await r.json();
  if (d.error) { flash('Read failed: ' + d.error, false); return; }
  $('#upContent').value = d.content || '';
  flash('Loaded current contents of ' + p + ' into the paste box. Edit and upload.', true);
}

async function loadBackups() {
  const filter = $('#bkFilter').value.trim();
  const url = filter ? '/api/recovery/backups?path=' + encodeURIComponent(filter) : '/api/recovery/backups';
  const r = await fetch(url);
  const d = await r.json();
  const body = $('#bkTable tbody');
  const rows = d.backups || [];
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="tiny">No backups match.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(b => `
    <tr>
      <td class="mono">${escapeHtml(b.name)}</td>
      <td class="tiny">${escapeHtml(b.created)}</td>
      <td class="tiny">${b.size}</td>
      <td style="text-align:right">
        <button class="secondary" onclick="previewBackup('${escapeHtml(b.name)}')">Preview</button>
        <button onclick="restoreBackup('${escapeHtml(b.name)}')">Restore</button>
      </td>
    </tr>
  `).join('');
}

async function previewBackup(name) {
  const r = await fetch('/api/recovery/backup-content?name=' + encodeURIComponent(name));
  const d = await r.json();
  if (d.error) { flash(d.error, false); return; }
  $('#upContent').value = d.content || '';
  flash('Loaded backup into paste box — edit or upload to restore it under a different path.', true);
}

async function restoreBackup(name) {
  const target = $('#bkTarget').value.trim();
  if (!target) { flash('Set the target file first.', false); return; }
  if (!confirm('Restore ' + target + ' from ' + name + ' ?')) return;
  const r = await fetch('/api/recovery/restore-backup', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({backup:name, path:target})
  });
  const d = await r.json();
  if (d.success) flash('Restored ' + target + ' from ' + name, true);
  else flash('Restore failed: ' + (d.error || 'unknown'), false);
}

async function uploadFile() {
  const path = $('#upPath').value.trim();
  if (!path) { flash('Set target path.', false); return; }
  const file = $('#upFile').files[0];
  let payload;
  if (file) {
    // Binary-safe via base64
    const buf = await file.arrayBuffer();
    const bin = new Uint8Array(buf);
    let s = ''; for (let i=0;i<bin.length;i++) s += String.fromCharCode(bin[i]);
    payload = { path, content_base64: btoa(s) };
  } else {
    const content = $('#upContent').value;
    if (!content) { flash('Provide a file or paste content.', false); return; }
    payload = { path, content };
  }
  if (!confirm('Overwrite /app/' + path + ' ?')) return;
  const r = await fetch('/api/recovery/upload-file', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  const d = await r.json();
  if (d.success) flash('Saved ' + path, true);
  else flash('Upload failed: ' + (d.error || 'unknown'), false);
}

async function clearPending() {
  if (!confirm('Remove .test_pending and .boot_failures markers?')) return;
  const r = await fetch('/api/recovery/clear-pending', {method:'POST'});
  const d = await r.json();
  if (d.success) flash('Cleared: ' + (d.removed.join(', ') || '(none)'), true);
  else flash('Failed', false);
}

async function restart() {
  if (!confirm('Restart service?\n\nThe launcher will retry main.py with the current code.')) return;
  await fetch('/api/recovery/restart', {method:'POST'});
  flash('Restarting — this page will fail to reach recovery in a moment. Reload in ~10s.', true);
  setTimeout(()=>location.reload(), 8000);
}

async function loadLog() {
  const r = await fetch('/api/recovery/tail-log');
  const d = await r.json();
  $('#logTail').textContent = d.log || '(empty)';
}

loadCrash();
loadBackups();
</script>

</body>
</html>
"""


# ----------------------------------------------------------------------------
# SERVER
# ----------------------------------------------------------------------------

class ReusableTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _write_marker():
    try:
        with open(RECOVERY_MARKER, "w") as f:
            f.write(json.dumps({
                "ts": time.time(),
                "iso": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
                "pid": os.getpid(),
            }))
    except Exception:
        pass


def _remove_marker():
    try:
        if os.path.isfile(RECOVERY_MARKER):
            os.remove(RECOVERY_MARKER)
    except Exception:
        pass


def main():
    _write_marker()
    log.warning("=" * 60)

    # Detect SSL — if the main app is configured for HTTPS, the recovery
    # server MUST also serve HTTPS, otherwise browsers with HSTS cached for
    # this host will refuse to connect (the user sees ERR_ADDRESS_UNREACHABLE
    # or a TLS error and never reaches the recovery UI).
    ssl_cfg = _detect_ssl_config()

    crash = _read_crash()
    if crash:
        log.warning(
            f"Last crash: {crash.get('exc_type')}: {crash.get('exc_value')} "
            f"@ {crash.get('suspect_file_rel')}:{crash.get('suspect_line')}"
        )

    server = ReusableTCPServer(("0.0.0.0", PORT), RecoveryHandler)

    scheme = "http"
    if ssl_cfg["enabled"]:
        certfile = ssl_cfg["certfile"]
        keyfile = ssl_cfg["keyfile"]
        if certfile and keyfile and os.path.isfile(certfile) and os.path.isfile(keyfile):
            try:
                import ssl as _ssl
                ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
                server.socket = ctx.wrap_socket(server.socket, server_side=True)
                scheme = "https"
                log.warning(f"SSL ENABLED — using {certfile}")
            except Exception as e:
                log.warning(
                    f"SSL config detected but cert load failed ({e!r}); "
                    f"falling back to plain HTTP. The browser may show a "
                    f"connection error if HSTS is cached for this host."
                )
        else:
            log.warning(
                f"SSL enabled in config.yaml but cert/key not found "
                f"(certfile={certfile}, keyfile={keyfile}); falling back to HTTP."
            )

    log.warning(f"DISASTER RECOVERY SERVER starting on {scheme}://0.0.0.0:{PORT}")
    log.warning(f"APP_DIR={APP_DIR}")
    log.warning(f"Open {scheme}://<host>:{PORT}/ in a browser to recover.")
    log.warning("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Interrupted")
    finally:
        _remove_marker()
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log.critical(f"Recovery server crashed: {e}", exc_info=True)
        # Exit non-zero → launcher won't retry main.py automatically
        sys.exit(1)