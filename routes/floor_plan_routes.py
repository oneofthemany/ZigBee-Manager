"""
Floor-plan API routes.

Endpoints:
    GET  /api/heating/floor-plan                    — read the saved plan
    POST /api/heating/floor-plan                    — save plan; project into
                                                      circuits; return warnings
    GET  /api/heating/floor-plan/preview            — dry-run projection
    DELETE /api/heating/floor-plan                  — clear the plan

    POST /api/heating/floor-plan/image/{level_id}   — upload background image
    GET  /api/heating/floor-plan/image/{level_id}   — fetch the image
    DELETE /api/heating/floor-plan/image/{level_id} — clear the image

Storage:
    Plan metadata: ``heating.floor_plan`` in ``config/config.yaml``.
    Background images: ``data/floor_plans/{level_id}.{ext}`` — keeps YAML
    small; images are first-class files on disk.

Image limits:
    20 MB per upload. Allowed types: image/png, image/jpeg.
    PDFs MUST be rendered to PNG client-side (via pdf.js) before upload.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
from typing import Any, Dict, Optional

import yaml
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse

from modules.floor_plan import (
    clean_floor_plan,
    project_floor_plan_to_circuits,
)

logger = logging.getLogger("routes.floor_plan")

CONFIG_PATH = "./config/config.yaml"
IMAGE_DIR = "./data/floor_plans"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg"}
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


def _load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _save_config(cfg: Dict[str, Any]) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)


def _safe_level_id(level_id: str) -> Optional[str]:
    if not isinstance(level_id, str):
        return None
    s = level_id.strip().lower()
    return s if _ID_RE.match(s) else None


def _existing_image_path(level_id: str) -> Optional[str]:
    for ext in ALLOWED_IMAGE_EXTS:
        p = os.path.join(IMAGE_DIR, f"{level_id}{ext}")
        if os.path.exists(p):
            return p
    return None


def register_floor_plan_routes(app: FastAPI, get_controller=None):

    os.makedirs(IMAGE_DIR, exist_ok=True)

    def _resolve_controller():
        if not get_controller:
            return None
        c = get_controller()
        if callable(c):
            try:
                c = c()
            except Exception:
                pass
        return c

    # ─────────────────────── plan read/write ───────────────────────

    @app.get("/api/heating/floor-plan")
    async def get_floor_plan():
        """Return the saved floor plan, or null if none exists yet."""
        cfg = _load_config()
        plan = (cfg.get("heating") or {}).get("floor_plan")
        return {"success": True, "plan": plan or None}

    @app.post("/api/heating/floor-plan")
    async def post_floor_plan(req: Request):
        """
        Save the floor plan and project it into the controller circuits.

        Request body: full floor-plan dict (see modules/floor_plan.py).
        Response: ``{success, plan, warnings, projected_room_ids}``.
        """
        try:
            raw = await req.json()
        except Exception as e:
            return {"success": False, "error": f"invalid JSON: {e}"}

        cleaned = clean_floor_plan(raw)
        if cleaned is None:
            return {"success": False, "error": "floor plan is empty or invalid"}

        cfg = _load_config()
        heating = cfg.setdefault("heating", {})
        controller = heating.setdefault("controller", {})
        circuits = controller.get("circuits") or []

        try:
            updated_circuits, warnings = project_floor_plan_to_circuits(cleaned, circuits)
        except Exception as e:
            logger.exception("floor plan projection failed")
            return {"success": False, "error": f"projection failed: {e}"}

        heating["floor_plan"] = cleaned
        controller["circuits"] = updated_circuits

        try:
            _save_config(cfg)
        except Exception as e:
            logger.exception("config write failed")
            return {"success": False, "error": f"could not write config: {e}"}

        ctrl = _resolve_controller()
        if ctrl is not None:
            try:
                if hasattr(ctrl, "apply_config"):
                    controller["_floor_plan_for_thermal"] = cleaned
                    ctrl.apply_config(controller)
                    controller.pop("_floor_plan_for_thermal", None)
                elif hasattr(ctrl, "circuits"):
                    ctrl.circuits = updated_circuits
                    ctrl._floor_plan_cache = cleaned
            except Exception as e:
                logger.warning(f"controller hot-apply failed: {e}")
                warnings.append(f"controller hot-apply failed: {e}")

        projected_room_ids = [
            r["id"] for c in updated_circuits
            for r in (c.get("rooms") or [])
            if r.get("floor_plan_ref")
        ]

        return {
            "success": True,
            "plan": cleaned,
            "warnings": warnings,
            "projected_room_ids": projected_room_ids,
        }

    @app.get("/api/heating/floor-plan/preview")
    async def preview_floor_plan():
        """Dry-run projection: shows what the saved plan would write."""
        cfg = _load_config()
        heating = cfg.get("heating") or {}
        plan = heating.get("floor_plan")
        circuits = (heating.get("controller") or {}).get("circuits") or []
        if not plan:
            return {"success": False, "error": "no floor plan saved"}
        try:
            updated, warnings = project_floor_plan_to_circuits(plan, circuits)
        except Exception as e:
            return {"success": False, "error": f"projection failed: {e}"}
        return {"success": True, "circuits": updated, "warnings": warnings}

    @app.delete("/api/heating/floor-plan")
    async def delete_floor_plan():
        """Remove the saved plan (and all level background images)."""
        cfg = _load_config()
        heating = cfg.get("heating") or {}
        if "floor_plan" in heating:
            del heating["floor_plan"]
            try:
                _save_config(cfg)
            except Exception as e:
                return {"success": False, "error": f"could not write config: {e}"}
        if os.path.isdir(IMAGE_DIR):
            for fn in os.listdir(IMAGE_DIR):
                p = os.path.join(IMAGE_DIR, fn)
                try:
                    os.remove(p)
                except Exception:
                    pass
        return {"success": True}

    # ─────────────────────── background images ───────────────────────

    @app.post("/api/heating/floor-plan/image/{level_id}")
    async def upload_floor_plan_image(level_id: str, file: UploadFile = File(...)):
        """
        Upload a background image for a level. PNG and JPEG only.

        PDFs are rendered to PNG client-side via pdf.js before upload, so
        the user can drop a PDF in the UI and the first page is captured.

        Body: multipart/form-data with a single ``file`` field.
        Limits: 20 MB.
        """
        lid = _safe_level_id(level_id)
        if not lid:
            return JSONResponse({"success": False, "error": "invalid level_id"}, status_code=400)

        ctype = (file.content_type or "").lower()
        if ctype not in ALLOWED_IMAGE_TYPES:
            return JSONResponse(
                {"success": False, "error": f"unsupported content-type {ctype!r}; "
                                            "allowed: PNG, JPEG"},
                status_code=415,
            )

        ext = ".png" if ctype == "image/png" else ".jpg"
        target = os.path.join(IMAGE_DIR, f"{lid}{ext}")
        os.makedirs(IMAGE_DIR, exist_ok=True)

        for prior_ext in ALLOWED_IMAGE_EXTS:
            prior = os.path.join(IMAGE_DIR, f"{lid}{prior_ext}")
            if prior != target and os.path.exists(prior):
                try:
                    os.remove(prior)
                except Exception:
                    pass

        written = 0
        try:
            with open(target, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 64)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_IMAGE_BYTES:
                        out.close()
                        os.remove(target)
                        return JSONResponse(
                            {"success": False, "error": f"image > {MAX_IMAGE_BYTES} bytes"},
                            status_code=413,
                        )
                    out.write(chunk)
        except Exception as e:
            logger.exception("image upload write failed")
            try:
                os.remove(target)
            except Exception:
                pass
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

        return {
            "success": True,
            "level_id": lid,
            "url": f"/api/heating/floor-plan/image/{lid}",
            "bytes": written,
            "content_type": ctype,
        }

    @app.get("/api/heating/floor-plan/image/{level_id}")
    async def get_floor_plan_image(level_id: str):
        """Return the background image for a level, or 404."""
        lid = _safe_level_id(level_id)
        if not lid:
            return JSONResponse({"success": False, "error": "invalid level_id"}, status_code=400)
        path = _existing_image_path(lid)
        if not path:
            return JSONResponse({"success": False, "error": "no image"}, status_code=404)
        media_type, _ = mimetypes.guess_type(path)
        return FileResponse(path, media_type=media_type or "application/octet-stream")

    @app.delete("/api/heating/floor-plan/image/{level_id}")
    async def delete_floor_plan_image(level_id: str):
        """Remove the background image for a level."""
        lid = _safe_level_id(level_id)
        if not lid:
            return JSONResponse({"success": False, "error": "invalid level_id"}, status_code=400)
        path = _existing_image_path(lid)
        if path:
            try:
                os.remove(path)
            except Exception as e:
                return {"success": False, "error": str(e)}
        return {"success": True}