"""
Backup & Restore routes for full network migration.
Creates a downloadable zip containing all configuration, device database,
automations, groups, zones, and state — everything needed to rebuild
the network on a new container.
"""
import io
import json
import logging
import os
import shutil
import zipfile
import httpx
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger("routes.backup")

# Core files always included
BACKUP_MANIFEST = [
    # Network credentials & config
    "config/config.yaml",

    # Zigpy device database (paired devices, network state)
    "zigbee.db",

    # Application data
    "data/names.json",
    "data/device_settings.json",
    "data/polling_config.json",
    "data/device_state_cache.json",
    "data/device_tabs.json",
    "data/automations.json",
    "data/banned_devices.json",
    "data/device_overrides.json",
    "data/zones.yaml",

    # Groups
    "groups/groups.json",
]

# Optional files (toggled via query param)
OPTIONAL_BACKUP_FILES = [
    "data/telemetry.duckdb",
    "data/zigbee_cache.duckdb",
]

APP_DIR = os.environ.get("ZMM_APP_DIR", "/app")


def register_backup_routes(app: FastAPI, get_zigbee_service):
    """Register backup & restore API routes."""

    @app.get("/api/backup/create")
    async def create_backup(include_telemetry: bool = True):
        """
        Create a full network backup as a downloadable .zip file.
        Includes: config, zigbee.db, all data/*.json, zones, groups,
        and (optionally) the telemetry DuckDB.
        """
        try:
            svc = get_zigbee_service()

            # Flush state cache to disk before backing up
            if svc and hasattr(svc, '_cache_dirty') and svc._cache_dirty:
                svc._save_state_cache()
                svc._cache_dirty = False

            # Flush zone config
            if svc and hasattr(svc, 'zone_manager') and svc.zone_manager:
                try:
                    import yaml
                    configs = svc.zone_manager.save_config()
                    with open(os.path.join(APP_DIR, "data/zones.yaml"), "w") as f:
                        yaml.dump({"zones": configs}, f)
                except Exception as e:
                    logger.warning(f"Could not flush zones before backup: {e}")

            # Flush telemetry: drain Rust appender buffers + CHECKPOINT to merge WAL
            if include_telemetry:
                try:
                    from modules.telemetry_db import flush_appender, _get_db
                    flush_appender()
                    _get_db().execute("CHECKPOINT")
                except Exception as e:
                    logger.warning(f"Could not flush telemetry DB before backup: {e}")

            # Build manifest list for this run
            manifest_files = list(BACKUP_MANIFEST)
            if include_telemetry:
                manifest_files.extend(OPTIONAL_BACKUP_FILES)

            # Build zip in memory
            buffer = io.BytesIO()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            included = []
            skipped = []

            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                meta = {
                    "created_at": datetime.now().isoformat(),
                    "version": "1.1",
                    "include_telemetry": include_telemetry,
                    "device_count": len(svc.devices) if svc else 0,
                    "files": [],
                }

                for rel_path in manifest_files:
                    full = os.path.join(APP_DIR, rel_path)
                    if os.path.isfile(full):
                        zf.write(full, rel_path)
                        size = os.path.getsize(full)
                        included.append(rel_path)
                        meta["files"].append({
                            "path": rel_path,
                            "size": size,
                        })
                    else:
                        skipped.append(rel_path)
                        logger.debug(f"Backup skip (not found): {full}")

                if skipped:
                    logger.info(f"Backup skipped {len(skipped)} missing files: {skipped}")

                meta["included"] = len(included)
                meta["skipped"] = skipped
                zf.writestr("backup_manifest.json", json.dumps(meta, indent=2))

            buffer.seek(0)
            tail = "_full" if include_telemetry else "_config"
            filename = f"zmm_backup_{ts}{tail}.zip"

            logger.info(f"Backup created: {filename} ({len(included)} files)")

            return StreamingResponse(
                buffer,
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                },
            )

        except Exception as e:
            logger.error(f"Backup creation failed: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}


    @app.post("/api/backup/restore")
    async def restore_backup(
            file: Optional[UploadFile] = File(None),
            url: Optional[str] = Form(None),
    ):
        """
        Restore a full network backup from either:
          - a directly uploaded .zip file (multipart form)
          - a remote URL (the server fetches the zip itself)
        Overwrites config, data files, groups, and zigbee.db.
        A restart is required after restore to apply the new database.
        """
        # --- Acquire the zip bytes from whichever source was provided ---
        if file is not None:
            if not file.filename.endswith(".zip"):
                return {"success": False, "error": "File must be a .zip archive"}
            contents = await file.read()

        elif url is not None:
            logger.info(f"Fetching backup from remote URL: {url}")
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    contents = resp.content
            except httpx.HTTPStatusError as e:
                return {"success": False, "error": f"Remote fetch failed (HTTP {e.response.status_code}): {url}"}
            except Exception as e:
                return {"success": False, "error": f"Failed to fetch backup from URL: {e}"}

        else:
            return {"success": False, "error": "Provide either a file upload or a 'url' field"}

        # --- Shared restore logic ---
        try:
            buffer = io.BytesIO(contents)

            with zipfile.ZipFile(buffer, "r") as zf:
                names = zf.namelist()
                if "backup_manifest.json" not in names:
                    return {
                        "success": False,
                        "error": "Invalid backup: missing backup_manifest.json",
                    }

                manifest = json.loads(zf.read("backup_manifest.json"))
                logger.info(
                    f"Restoring backup from {manifest.get('created_at', 'unknown')} "
                    f"({manifest.get('included', '?')} files, "
                    f"zip entries: {[n for n in names if n != 'backup_manifest.json']})"
                )

                allowed = set(BACKUP_MANIFEST) | set(OPTIONAL_BACKUP_FILES)
                restored = []
                errors = []

                for entry in names:
                    if entry == "backup_manifest.json":
                        continue
                    if entry not in allowed:
                        logger.warning(f"Skipping unknown file in backup: {entry}")
                        continue

                    target = os.path.join(APP_DIR, entry)
                    try:
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        data = zf.read(entry)
                        with open(target, "wb") as f:
                            f.write(data)
                        restored.append(entry)
                        logger.info(f"Restored: {entry} ({len(data)} bytes)")
                    except Exception as e:
                        errors.append({"file": entry, "error": str(e)})
                        logger.error(f"Failed to restore {entry}: {e}")

            # Clean up stale DuckDB WAL files after restore.
            # WAL references page offsets from the pre-restore main file; keeping
            # it causes DuckDB to replay stale journal entries against the new
            # file on next open, which at best drops the restored data and at
            # worst corrupts it.
            duckdb_restored = [e for e in restored if e.endswith(".duckdb")]
            for entry in duckdb_restored:
                wal_path = os.path.join(APP_DIR, entry + ".wal")
                if os.path.isfile(wal_path):
                    try:
                        os.remove(wal_path)
                        logger.info(f"Removed stale WAL: {entry}.wal")
                    except Exception as e:
                        logger.warning(f"Could not remove stale WAL {wal_path}: {e}")


            # After extracting all files, fix up config.yaml if needed
            config_target = os.path.join(APP_DIR, "config/config.yaml")
            config_warnings = []
            if os.path.isfile(config_target):
                try:
                    import yaml as _yaml
                    with open(config_target, "r") as f:
                        cfg = _yaml.safe_load(f) or {}
                    cfg_dirty = False

                    # ── MQTT enabled inference ──
                    mqtt = cfg.setdefault("mqtt", {})
                    if "enabled" not in mqtt:
                        mqtt["enabled"] = bool(mqtt.get("broker_host", ""))
                        cfg_dirty = True
                        logger.info("Patched mqtt.enabled into restored config.yaml")

                    # ── SSL safety check ──
                    # If SSL is enabled in the restored config but the referenced
                    # cert/key files do not exist on disk, the server would fail
                    # to start. Force-disable SSL so the service comes back up;
                    # surface the change to the operator via the response.
                    server = cfg.get("server", {}) or {}
                    ssl_cfg = server.get("ssl", {}) or {}
                    if ssl_cfg.get("enabled"):
                        cert_rel = ssl_cfg.get("cert_file", "./data/certs/cert.pem")
                        key_rel  = ssl_cfg.get("key_file",  "./data/certs/key.pem")
                        # Resolve relative to APP_DIR (matches main.py uvicorn launch CWD)
                        cert_abs = (cert_rel if os.path.isabs(cert_rel)
                                    else os.path.join(APP_DIR, cert_rel.lstrip("./")))
                        key_abs  = (key_rel  if os.path.isabs(key_rel)
                                    else os.path.join(APP_DIR, key_rel.lstrip("./")))
                        missing = []
                        if not os.path.isfile(cert_abs):
                            missing.append(cert_rel)
                        if not os.path.isfile(key_abs):
                            missing.append(key_rel)

                        if missing:
                            cfg["server"]["ssl"]["enabled"] = False
                            cfg_dirty = True
                            warning = (
                                f"SSL was enabled in the restored config but cert files are missing "
                                f"({', '.join(missing)}). SSL has been disabled so the server can start. "
                                f"Regenerate certificates and re-enable SSL in config.yaml."
                            )
                            config_warnings.append(warning)
                            logger.warning(warning)

                    if cfg_dirty:
                        with open(config_target, "w") as f:
                            _yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
                except Exception as e:
                    logger.warning(f"Could not patch config.yaml after restore: {e}")
                    config_warnings.append(f"Config post-processing error: {e}")


            result = {
                "success": len(errors) == 0,
                "restored": restored,
                "restored_count": len(restored),
                "errors": errors,
                "warnings": config_warnings,
                "manifest": manifest,
                "message": (
                    f"Restored {len(restored)} files. Restart the service to apply."
                    if not errors
                    else f"Restored {len(restored)} files with {len(errors)} errors."
                ),
            }

            logger.info(f"Restore complete: {len(restored)} OK, {len(errors)} errors")
            return result

        except zipfile.BadZipFile:
            return {"success": False, "error": "Corrupt or invalid zip file"}
        except Exception as e:
            logger.error(f"Restore failed: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    @app.get("/api/backup/info")
    async def backup_info():
        """
        Return what would be included in a backup and file sizes.
        Useful for the frontend to show backup status.
        """
        files = []
        total_size = 0
        telemetry_size = 0

        for rel_path in BACKUP_MANIFEST:
            full = os.path.join(APP_DIR, rel_path)
            exists = os.path.isfile(full)
            size = os.path.getsize(full) if exists else 0
            total_size += size
            files.append({"path": rel_path, "exists": exists, "size": size, "optional": False})

        for rel_path in OPTIONAL_BACKUP_FILES:
            full = os.path.join(APP_DIR, rel_path)
            exists = os.path.isfile(full)
            size = os.path.getsize(full) if exists else 0
            telemetry_size += size
            files.append({"path": rel_path, "exists": exists, "size": size, "optional": True})

        return {
            "success": True,
            "files": files,
            "total_size": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "telemetry_size": telemetry_size,
            "telemetry_size_mb": round(telemetry_size / (1024 * 1024), 2),
            "total_with_telemetry_mb": round((total_size + telemetry_size) / (1024 * 1024), 2),
        }