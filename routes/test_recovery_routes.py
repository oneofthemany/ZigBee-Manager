"""
Test and Recovery API routes for the code editor.
Accepts both legacy single-file and new batch payloads.
"""
import logging
from fastapi import FastAPI
from modules.test_recovery import get_test_recovery_manager

logger = logging.getLogger("routes.test_recovery")


def register_test_recovery_routes(app: FastAPI, get_manager):
    """Register test/recovery endpoints."""

    def get_trm():
        return get_test_recovery_manager()

    @app.post("/api/editor/test-deploy")
    async def test_deploy(data: dict):
        """
        Deploy file change(s) for testing with rollback safety net.

        Payload shapes accepted:
          • {"files": [{"path": "...", "content": "..."}, ...]}   (batch)
          • {"path": "...", "content": "..."}                      (legacy single)
        """
        files = data.get("files")
        if files is None:
            path = data.get("path")
            content = data.get("content")
            if not path or content is None:
                return {
                    "success": False,
                    "error": "Provide either 'files' array or 'path'+'content'",
                }
            files = [{"path": path, "content": content}]

        if not isinstance(files, list) or not files:
            return {"success": False, "error": "'files' must be a non-empty array"}

        # Validate each entry
        for i, f in enumerate(files):
            if not isinstance(f, dict) or "path" not in f or "content" not in f:
                return {
                    "success": False,
                    "error": f"files[{i}] must be {{path, content}}",
                }

        return await get_trm().deploy_test_batch(files)

    @app.post("/api/editor/test-restart")
    async def test_restart():
        """Trigger service restart after Python-file test deploy."""
        return await get_trm().trigger_restart()

    @app.post("/api/editor/test-confirm")
    async def test_confirm():
        """Confirm that the test deployment is working."""
        return await get_trm().confirm()

    @app.post("/api/editor/test-rollback")
    async def test_rollback():
        """Manually rollback the pending batch."""
        result = await get_trm().rollback()

        if result.get("needs_restart"):
            import asyncio
            import sys
            import os as _os

            async def _restart():
                await asyncio.sleep(1)
                python = sys.executable
                _os.execl(python, python, *sys.argv)

            asyncio.create_task(_restart())
            result["message"] = (
                    (result.get("message") or "") + " Service restarting to apply rollback."
            )

        return result

    @app.get("/api/editor/test-status")
    async def test_status():
        """Get current test deployment status."""
        pending = get_trm().get_pending()
        if not pending:
            return {"pending": False}

        import time
        elapsed = time.time() - pending.get("deployed_at", 0)
        remaining = max(0, pending.get("timeout", 120) - elapsed)
        files = pending.get("files", [])

        return {
            "pending": True,
            "files": [f.get("path") for f in files],
            "count": len(files),
            "action": pending.get("action"),
            "remaining": int(remaining),
            "batch_tag": pending.get("batch_tag"),
            # Legacy convenience field (first file)
            "path": files[0].get("path") if files else None,
        }