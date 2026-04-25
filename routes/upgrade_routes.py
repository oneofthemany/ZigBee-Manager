"""
Upgrade API routes.

Endpoints:
  GET  /api/upgrade/status              — combined state + host status
  POST /api/upgrade/check               — force a GitHub check
  POST /api/upgrade/build               — request background image build
  POST /api/upgrade/swap                — request container swap to new image
  POST /api/upgrade/rollback            — rollback to previous image
  POST /api/upgrade/cancel              — cancel in-progress build
  POST /api/upgrade/gc                  — garbage collect old images
  GET  /api/upgrade/log                 — stream build log (tail)
  GET  /api/upgrade/settings            — get upgrade settings
  POST /api/upgrade/settings            — update upgrade settings
  POST /api/upgrade/install-watcher     — request watcher install
"""
import logging
from typing import Any, Dict

from fastapi import FastAPI, Body, Query
from fastapi.responses import JSONResponse

from modules import upgrade_manager as um

logger = logging.getLogger("routes.upgrade")


def register_upgrade_routes(app: FastAPI):
    """Register upgrade management routes."""

    @app.get("/api/upgrade/status")
    async def get_upgrade_status():
        """Combined state: persistent settings + live host status."""
        try:
            state = um.load_state()
            host_status = um.read_status()

            return {
                "success": True,
                "current_version": state.get("current_version"),
                "latest_available": state.get("latest_available"),
                "update_available": bool(
                    state.get("latest_available")
                    and um.compare_versions(
                        state.get("latest_available"),
                        state.get("current_version") or "0.0.0",
                        ) > 0
                ),
                "previous_version": state.get("previous_version"),
                "previous_image_tag": state.get("previous_image_tag"),
                "notes": state.get("latest_release_notes"),
                "url": state.get("latest_release_url"),
                "last_check": state.get("last_check"),
                "upgrade_state": host_status.get("state") or state.get("upgrade_state"),
                "progress_percent": host_status.get("progress_percent") or 0,
                "current_step": host_status.get("current_step") or "",
                "error": host_status.get("error") or state.get("upgrade_error"),
                "host_status": host_status,
                "architecture": state.get("architecture"),
                "auto_update": state.get("auto_update"),
                "auto_update_window": state.get("auto_update_window"),
                "channel": state.get("channel"),
                "retention_count": state.get("retention_count"),
                "repo": state.get("repo"),
                "watcher_installed": um.watcher_installed() or state.get("watcher_installed", False),
            }
        except Exception as e:
            logger.error(f"Failed to get upgrade status: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/check")
    async def check_updates(force: bool = Query(True)):
        """Force a GitHub check for new versions."""
        try:
            result = await um.check_for_updates(force=force)
            return {"success": True, **result}
        except Exception as e:
            logger.error(f"Update check failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/build")
    async def start_build(data: Dict[str, Any] = Body(...)):
        """Request a background image build."""
        try:
            target = data.get("version") or data.get("target_version")
            if not target:
                # Default: build the latest known available version
                state = um.load_state()
                target = state.get("latest_available")
            if not target:
                return JSONResponse(
                    {"success": False, "error": "No target version specified or detected"},
                    status_code=400,
                )

            ok, msg = um.request_build(target)
            status_code = 200 if ok else 409
            return JSONResponse({"success": ok, "message": msg}, status_code=status_code)
        except Exception as e:
            logger.error(f"Build request failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/swap")
    async def start_swap():
        """Request a container swap to the pre-built new image."""
        try:
            ok, msg = um.request_swap()
            status_code = 200 if ok else 409
            return JSONResponse({"success": ok, "message": msg}, status_code=status_code)
        except Exception as e:
            logger.error(f"Swap request failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/rollback")
    async def start_rollback():
        """Request a rollback to the previous image."""
        try:
            ok, msg = um.request_rollback()
            status_code = 200 if ok else 409
            return JSONResponse({"success": ok, "message": msg}, status_code=status_code)
        except Exception as e:
            logger.error(f"Rollback request failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/cancel")
    async def cancel_operation():
        """Cancel the in-progress operation if possible."""
        try:
            ok, msg = um.request_cancel()
            return {"success": ok, "message": msg}
        except Exception as e:
            logger.error(f"Cancel request failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/gc")
    async def gc_old_images():
        """Garbage-collect old images per retention policy."""
        try:
            ok, msg = um.request_gc()
            return {"success": ok, "message": msg}
        except Exception as e:
            logger.error(f"GC request failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.get("/api/upgrade/log")
    async def get_build_log(lines: int = Query(500, ge=1, le=5000)):
        """Return the last N lines of the host-side build log."""
        try:
            log = um.read_build_log(max_lines=lines)
            return {"success": True, "lines": log}
        except Exception as e:
            logger.error(f"Log read failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.get("/api/upgrade/settings")
    async def get_upgrade_settings():
        """Return upgrade-related settings."""
        try:
            state = um.load_state()
            return {
                "success": True,
                "auto_update": state.get("auto_update"),
                "auto_update_window": state.get("auto_update_window"),
                "channel": state.get("channel"),
                "retention_count": state.get("retention_count"),
                "repo": state.get("repo"),
            }
        except Exception as e:
            logger.error(f"Failed to read upgrade settings: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/settings")
    async def set_upgrade_settings(data: Dict[str, Any] = Body(...)):
        """Update upgrade-related settings."""
        try:
            window = data.get("auto_update_window") or {}
            new_state = um.update_settings(
                auto_update=data.get("auto_update"),
                channel=data.get("channel"),
                retention_count=data.get("retention_count"),
                auto_window_start=window.get("start"),
                auto_window_end=window.get("end"),
                repo=data.get("repo"),
            )
            return {
                "success": True,
                "auto_update": new_state.get("auto_update"),
                "auto_update_window": new_state.get("auto_update_window"),
                "channel": new_state.get("channel"),
                "retention_count": new_state.get("retention_count"),
                "repo": new_state.get("repo"),
            }
        except ValueError as ve:
            return JSONResponse({"success": False, "error": str(ve)}, status_code=400)
        except Exception as e:
            logger.error(f"Failed to save upgrade settings: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/install-watcher")
    async def install_watcher():
        """Request host-side watcher install (requires pre-seeded script)."""
        try:
            ok, msg = um.request_install_watcher()
            return {"success": ok, "message": msg}
        except Exception as e:
            logger.error(f"Install watcher failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/reset-status")
    async def reset_status_endpoint(data: Dict[str, Any] = Body(default={})):
        """
        Dismiss a stale 'failed' status banner. By default only resets if
        the current state is 'failed' (or 'idle'); pass {"force": true}
        to override (use with extreme caution — never wipe an active build).
        """
        try:
            force = bool(data.get("force", False))
            new_state = um.reset_status(only_if_failed=not force)
            return {
                "success": True,
                "upgrade_state": new_state.get("upgrade_state"),
                "message": "Status cleared",
            }
        except Exception as e:
            logger.error(f"Reset status failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    @app.post("/api/upgrade/clear-lock")
    async def clear_lock():
        """
        Force-clear the lock file. Use when the UI shows 'Another upgrade
        operation is in progress' but you know nothing is actually running
        (e.g. after a host crash, killed process, or watcher misconfiguration).

        Auto-detects stale locks; only clears if the holder PID is dead OR
        the lock is older than 60 minutes. Won't clear a live lock.
        """
        try:
            cleared = um.clear_stale_lock()
            if cleared:
                return {"success": True, "message": "Stale lock cleared"}
            # Did nothing happen because there was no lock, or because it's live?
            import os as _os
            if _os.path.exists(um.LOCK_FILE):
                return JSONResponse(
                    {"success": False, "error": "Lock is held by a live process — refusing to force-clear"},
                    status_code=409,
                )
            return {"success": True, "message": "No lock present"}
        except Exception as e:
            logger.error(f"Clear lock failed: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)