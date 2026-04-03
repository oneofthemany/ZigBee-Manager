"""
Test and Recovery API routes for the code editor.
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
        """Deploy a file change for testing with rollback safety net."""
        path = data.get("path")
        content = data.get("content")
        if not path or content is None:
            return {"success": False, "error": "path and content required"}
        return await get_trm().deploy_test(path, content)

    @app.post("/api/editor/test-restart")
    async def test_restart():
        """Trigger service restart after Python file test deploy."""
        return await get_trm().trigger_restart()

    @app.post("/api/editor/test-confirm")
    async def test_confirm():
        """Confirm that the test deployment is working."""
        return await get_trm().confirm()

    @app.post("/api/editor/test-rollback")
    async def test_rollback():
        """Manually rollback to the pre-test backup."""
        result = await get_trm().rollback()

        # If Python file was rolled back, trigger restart
        if result.get("needs_restart"):
            import asyncio, sys, os
            async def _restart():
                await asyncio.sleep(1)
                python = sys.executable
                os.execl(python, python, *sys.argv)
            asyncio.create_task(_restart())
            result["message"] += " Service restarting to apply rollback."

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

        return {
            "pending": True,
            "path": pending.get("path"),
            "action": pending.get("action"),
            "remaining": int(remaining),
            "backup": pending.get("backup_name"),
        }
