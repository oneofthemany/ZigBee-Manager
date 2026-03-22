"""
AI API - FastAPI routes for AI-assisted automation generation.

Uses module-level getters (same pattern as routes/ota_routes.py)
to avoid closure scoping issues with FastAPI lifespan.

Endpoints:
  POST /api/ai/automation       — Generate automation rule from natural language
  GET  /api/ai/status           — Check AI provider configuration status
  POST /api/ai/config           — Update AI provider settings
  GET  /api/ai/context          — Preview the device context sent to the LLM (debug)
"""

import logging
from typing import Callable, Optional

from fastapi import FastAPI, APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ============================================================================
# MODULE-LEVEL GETTERS (set by register_ai_routes)
# ============================================================================

_get_ai_assistant = None
_get_ai_automations = None
_config_saver = None

router = APIRouter(prefix="/api/ai", tags=["ai"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class AIGenerateRequest(BaseModel):
    prompt: str = Field(..., description="Natural language automation description",
                        min_length=5, max_length=2000)


class AIConfigRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


# ============================================================================
# REGISTRATION
# ============================================================================

def register_ai_routes(app: FastAPI, ai_assistant_getter: Callable,
                       ai_automations_getter: Callable,
                       config_saver: Optional[Callable] = None):
    """
    Register AI API routes on the FastAPI app.

    Args:
        app: FastAPI instance
        ai_assistant_getter: callable returning AIAssistant instance
        ai_automations_getter: callable returning AIAutomations instance
        config_saver: optional callable(ai_config_dict) to persist to config.yaml
    """
    global _get_ai_assistant, _get_ai_automations, _config_saver
    _get_ai_assistant = ai_assistant_getter
    _get_ai_automations = ai_automations_getter
    _config_saver = config_saver
    app.include_router(router)
    logger.info("AI API routes registered")


# ============================================================================
# ROUTES
# ============================================================================

@router.post("/automation")
async def generate_automation(request: AIGenerateRequest):
    """Generate an automation rule from natural language."""
    ai_auto = _get_ai_automations() if _get_ai_automations else None
    if not ai_auto:
        raise HTTPException(503, "AI automations module not available")

    ai = _get_ai_assistant() if _get_ai_assistant else None
    if not ai or not ai.is_configured():
        raise HTTPException(503, "AI provider not configured. "
                                 "Set provider and credentials in Settings → AI.")

    result = await ai_auto.generate_rule(request.prompt)
    return result


@router.get("/status")
async def ai_status():
    """Check AI provider configuration status."""
    ai = _get_ai_assistant() if _get_ai_assistant else None
    if not ai:
        return {"configured": False, "provider": None}
    return ai.get_status()


@router.post("/config")
async def update_ai_config(request: AIConfigRequest):
    """Update AI provider settings and persist to config.yaml."""
    ai = _get_ai_assistant() if _get_ai_assistant else None
    if not ai:
        raise HTTPException(503, "AI module not initialised")

    updates = {k: v for k, v in request.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "Nothing to update")

    # Apply updates to the live instance
    if "provider" in updates:
        ai.provider = updates["provider"]
        from modules.ai_assistant import PROVIDER_DEFAULTS
        defaults = PROVIDER_DEFAULTS.get(ai.provider, {})
        if not request.base_url:
            ai.base_url = defaults.get("base_url", ai.base_url)
        if not request.model:
            ai.model = defaults.get("model", ai.model)
    if "model" in updates:
        ai.model = updates["model"]
    if "api_key" in updates:
        ai.api_key = updates["api_key"]
    if "base_url" in updates:
        ai.base_url = updates["base_url"].rstrip("/")
    if "temperature" in updates:
        ai.temperature = float(updates["temperature"])
    if "max_tokens" in updates:
        ai.max_tokens = int(updates["max_tokens"])

    # Persist to config.yaml
    if _config_saver:
        ai_config = {
            "provider": ai.provider,
            "model": ai.model,
            "api_key": ai.api_key,
            "base_url": ai.base_url,
            "temperature": ai.temperature,
            "max_tokens": ai.max_tokens,
        }
        try:
            _config_saver(ai_config)
        except Exception as e:
            logger.error(f"Failed to persist AI config: {e}")

    return {"success": True, **ai.get_status()}


@router.get("/context")
async def ai_context():
    """Preview the device context that would be sent to the LLM (debug)."""
    ai_auto = _get_ai_automations() if _get_ai_automations else None
    if not ai_auto:
        raise HTTPException(503, "AI automations module not available")

    ctx = ai_auto._build_device_context()
    prompt = ai_auto._build_system_prompt()
    return {
        "device_count": ctx.count("\n- ") + (1 if ctx.startswith("- ") else 0),
        "context_chars": len(ctx),
        "prompt_chars": len(prompt),
        "device_context": ctx,
    }