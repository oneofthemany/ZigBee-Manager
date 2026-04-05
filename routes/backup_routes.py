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

# All files to include in a full backup (relative to /app)
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

APP_DIR = os.environ.get("ZMM_APP_DIR", ".")


def register_backup_routes(app: FastAPI, get_zigbee_service):
    """Register backup & restore API routes."""

    @app.get("/api/backup/create")
    async def create_backup():
        """
        Create a full network backup as a downloadable .zip file.
        Includes: config, zigbee.db, all data/*.json, zones, groups.
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

            # Build zip in memory
            buffer = io.BytesIO()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            included = []
            skipped = []

            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                # Write manifest metadata
                meta = {
                    "created_at": datetime.now().isoformat(),
                    "version": "1.0",
                    "device_count": len(svc.devices) if svc else 0,
                    "files": [],
                }

                for rel_path in BACKUP_MANIFEST:
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
            filename = f"zmm_backup_{ts}.zip"

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

                allowed = set(BACKUP_MANIFEST)
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

            # After extracting all files, fix up config.yaml if needed
            config_target = os.path.join(APP_DIR, "config/config.yaml")
            if os.path.isfile(config_target):
                try:
                    import yaml as _yaml
                    with open(config_target, "r") as f:
                        cfg = _yaml.safe_load(f) or {}

                    mqtt = cfg.setdefault("mqtt", {})
                    if "enabled" not in mqtt:
                        # Infer: if broker_host is set, MQTT was intended to be enabled
                        mqtt["enabled"] = bool(mqtt.get("broker_host", ""))
                        with open(config_target, "w") as f:
                            _yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
                        logger.info("Patched mqtt.enabled into restored config.yaml")
                except Exception as e:
                    logger.warning(f"Could not patch config.yaml after restore: {e}")


            result = {
                "success": len(errors) == 0,
                "restored": restored,
                "restored_count": len(restored),
                "errors": errors,
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

        for rel_path in BACKUP_MANIFEST:
            full = os.path.join(APP_DIR, rel_path)
            exists = os.path.isfile(full)
            size = os.path.getsize(full) if exists else 0
            total_size += size

            files.append({
                "path": rel_path,
                "exists": exists,
                "size": size,
            })

        return {
            "success": True,
            "files": files,
            "total_size": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
        }
