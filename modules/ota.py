"""
OTA Firmware Update Manager
============================
Wraps zigpy's built-in OTA subsystem to provide:
- Automatic provider configuration (IKEA, LEDVANCE, Sonoff, Inovelli, etc.)
- Per-device firmware availability checking
- Manual update triggering with progress tracking via WebSocket
- Local OTA file upload support

zigpy handles the heavy lifting (image matching, block transfer, cluster commands).
This module provides the management layer and API surface.
"""
import os
import asyncio
import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from zigpy.zcl.clusters.general import Ota as OtaCluster

logger = logging.getLogger("ota")

# OTA directory for local firmware files
OTA_DIR = "./data/ota_firmware"


class OTAManager:
    """Manages OTA firmware updates for all Zigbee devices."""

    def __init__(self, zigbee_service, event_emitter: Optional[Callable] = None):
        self.service = zigbee_service
        self._emit = event_emitter
        self._update_tasks: Dict[str, asyncio.Task] = {}
        self._update_progress: Dict[str, Dict[str, Any]] = {}

        # Ensure local OTA directory exists
        os.makedirs(OTA_DIR, exist_ok=True)

        logger.info("OTA Manager initialised")

    @property
    def app(self):
        return self.service.app

    # =========================================================================
    # PROVIDER STATUS
    # =========================================================================

    def get_ota_config(self) -> dict:
        """Return current OTA provider configuration."""
        config = self.service._config.get('ota', {})
        return {
            "enabled": config.get('enabled', True),
            "local_directory": OTA_DIR,
            "providers": config.get('providers', []),
            "extra_providers": config.get('extra_providers', []),
            "local_files": self._list_local_files(),
        }

    def _list_local_files(self) -> list:
        """List firmware files in local OTA directory."""
        files = []
        ota_path = Path(OTA_DIR)
        if ota_path.exists():
            for f in ota_path.iterdir():
                if f.is_file() and f.suffix in ('.ota', '.zigbee', '.bin', '.ota1', '.sbl-ota'):
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "modified": int(f.stat().st_mtime),
                    })
        return sorted(files, key=lambda x: x['name'])

    # =========================================================================
    # CHECK FOR UPDATES
    # =========================================================================

    async def check_device_update(self, ieee: str) -> dict:
        """
        Check if a firmware update is available for a specific device.
        Uses zigpy's OTA image matching against all configured providers.
        """
        if not self.app:
            return {"available": False, "error": "Zigbee stack not running"}

        device = self.service.devices.get(ieee)
        if not device:
            return {"available": False, "error": "Device not found"}

        zigpy_dev = device.zigpy_dev

        try:
            # zigpy's app.ota.get_ota_images() checks all providers
            # Returns OtaImagesResult with .upgrades and .downgrades
            query_cmd = self._build_query_cmd(zigpy_dev)
            result = await self.app.ota.get_ota_images(zigpy_dev, query_cmd)

            if not result.upgrades:
                current_ver = self._get_current_fw_version(zigpy_dev)
                return {
                    "available": False,
                    "current_version": self._format_version(current_ver),
                    "message": "No update available",
                }

            image_meta = result.upgrades[0]  # Best match
            new_version = image_meta.firmware.header.file_version if image_meta.firmware else None

            return {
                "available": True,
                "current_version": self._format_version(self._get_current_fw_version(zigpy_dev)),
                "new_version": self._format_version(new_version),
                "image_size": image_meta.firmware.header.image_size if image_meta.firmware else 0,
                "manufacturer_id": getattr(zigpy_dev, 'manufacturer_id', 0),
            }

        except Exception as e:
            logger.warning(f"[{ieee}] OTA check failed: {e}")
            return {"available": False, "error": str(e)}

    async def check_all_updates(self) -> dict:
        """Check for firmware updates across all devices. Returns summary."""
        results = {}
        for ieee, device in self.service.devices.items():
            try:
                result = await self.check_device_update(ieee)
                if result.get("available"):
                    results[ieee] = result
            except Exception as e:
                logger.debug(f"[{ieee}] OTA check skipped: {e}")
        return {
            "devices_with_updates": len(results),
            "updates": results,
        }

    # =========================================================================
    # TRIGGER UPDATE
    # =========================================================================

    async def start_update(self, ieee: str, force: bool = False) -> dict:
        """
        Trigger a firmware update for a specific device.
        The update runs asynchronously; progress is reported via WebSocket.
        """
        if ieee in self._update_tasks and not self._update_tasks[ieee].done():
            return {"success": False, "error": "Update already in progress"}

        device = self.service.devices.get(ieee)
        if not device:
            return {"success": False, "error": "Device not found"}

        zigpy_dev = device.zigpy_dev

        # Check an image is actually available
        try:
            query_cmd = self._build_query_cmd(zigpy_dev)
            result = await self.app.ota.get_ota_images(zigpy_dev, query_cmd)

            if force:
                all_images = list(result.upgrades) + list(result.downgrades)
            else:
                all_images = list(result.upgrades)
        except Exception as e:
            return {"success": False, "error": f"Image lookup failed: {e}"}

        if not all_images:
            return {"success": False, "error": "No firmware image available"}

        image_meta = all_images[0]

        # Initialise progress tracking
        self._update_progress[ieee] = {
            "ieee": ieee,
            "status": "starting",
            "progress": 0,
            "started_at": time.time(),
        }

        self._background_task: Optional[asyncio.Task] = None
        self._check_interval = 6 * 3600  # Check every 6 hours

        # Launch update in background task
        self._update_tasks[ieee] = asyncio.create_task(
            self._run_update(ieee, zigpy_dev, image_meta, force)
        )

        return {"success": True, "message": "Update started"}

    async def _run_update(self, ieee: str, zigpy_dev, image_meta, force: bool):
        """Execute the firmware update with progress tracking."""
        try:
            self._update_progress[ieee]["status"] = "downloading"
            await self._emit_progress(ieee)

            def progress_callback(progress, total):
                pct = int((progress / total) * 100) if total > 0 else 0
                self._update_progress[ieee].update({
                    "status": "updating",
                    "progress": pct,
                    "bytes_sent": progress,
                    "bytes_total": total,
                })
                asyncio.get_event_loop().create_task(self._emit_progress(ieee))

            # zigpy's device.update_firmware() handles the full OTA transfer
            await zigpy_dev.update_firmware(
                image=image_meta,
                progress_callback=progress_callback,
                force=force,
            )

            self._update_progress[ieee].update({
                "status": "complete",
                "progress": 100,
                "completed_at": time.time(),
            })
            await self._emit_progress(ieee)
            logger.info(f"[{ieee}] OTA update completed successfully")

        except Exception as e:
            self._update_progress[ieee].update({
                "status": "failed",
                "error": str(e),
            })
            await self._emit_progress(ieee)
            logger.error(f"[{ieee}] OTA update failed: {e}")

        finally:
            self._update_tasks.pop(ieee, None)

    async def _emit_progress(self, ieee: str):
        """Push progress update to frontend via WebSocket."""
        if self._emit:
            try:
                await self._emit("ota_progress", self._update_progress.get(ieee, {}))
            except Exception:
                pass

    def get_update_status(self, ieee: str) -> dict:
        """Get current update progress for a device."""
        return self._update_progress.get(ieee, {"status": "idle"})

    async def cancel_update(self, ieee: str) -> dict:
        """Cancel an in-progress update."""
        task = self._update_tasks.get(ieee)
        if task and not task.done():
            task.cancel()
            self._update_progress[ieee]["status"] = "cancelled"
            return {"success": True}
        return {"success": False, "error": "No active update"}


    def start_background_checks(self):
        """Start periodic background OTA checks."""
        if self._background_task and not self._background_task.done():
            return
        self._background_task = asyncio.create_task(self._background_check_loop())
        logger.info(f"OTA background checks started (interval: {self._check_interval}s)")

    def stop_background_checks(self):
        """Stop periodic background OTA checks."""
        if self._background_task:
            self._background_task.cancel()
            self._background_task = None

    async def _background_check_loop(self):
        """Periodically check all devices for firmware updates."""
        # Initial delay — let network stabilise after startup
        await asyncio.sleep(300)  # 5 minutes

        while True:
            try:
                logger.info("OTA background scan starting...")
                result = await self.check_all_updates()
                count = result.get("devices_with_updates", 0)

                if count > 0:
                    logger.info(f"OTA: {count} device(s) have firmware updates available")
                    if self._emit:
                        await self._emit("ota_updates_available", result)
                else:
                    logger.info("OTA: All devices up to date")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"OTA background check failed: {e}")

            await asyncio.sleep(self._check_interval)

    # =========================================================================
    # LOCAL FILE MANAGEMENT
    # =========================================================================

    async def upload_firmware(self, filename: str, content: bytes) -> dict:
        """Save an uploaded firmware file to the local OTA directory."""
        dest = Path(OTA_DIR) / filename
        try:
            dest.write_bytes(content)
            logger.info(f"OTA firmware uploaded: {filename} ({len(content)} bytes)")
            return {"success": True, "file": filename, "size": len(content)}
        except Exception as e:
            logger.error(f"OTA upload failed: {e}")
            return {"success": False, "error": str(e)}

    async def delete_firmware(self, filename: str) -> dict:
        """Remove a firmware file from the local OTA directory."""
        target = Path(OTA_DIR) / filename
        if not target.exists():
            return {"success": False, "error": "File not found"}
        if not str(target.resolve()).startswith(str(Path(OTA_DIR).resolve())):
            return {"success": False, "error": "Invalid path"}
        try:
            target.unlink()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # =========================================================================
    # NOTIFY DEVICE (image_notify via zigpy broadcast)
    # =========================================================================

    async def notify_device(self, ieee: str) -> dict:
        """
        Send an OTA Image Notify to a device, prompting it to check for updates.
        Uses zigpy's built-in broadcast_notify method.
        """
        device = self.service.devices.get(ieee)
        if not device:
            return {"success": False, "error": "Device not found"}

        zigpy_dev = device.zigpy_dev

        try:
            await self.app.ota.broadcast_notify(zigpy_dev)
            return {"success": True, "message": "Image notify sent"}
        except Exception as e:
            logger.warning(f"[{ieee}] OTA notify failed: {e}")
            return {"success": False, "error": str(e)}

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _build_query_cmd(self, zigpy_dev):
        """Build a QueryNextImageCommand for this device."""
        return OtaCluster.QueryNextImageCommand(
            field_control=0,
            manufacturer_code=getattr(zigpy_dev, 'manufacturer_id', 0) or 0,
            image_type=self._get_image_type(zigpy_dev),
            current_file_version=self._get_current_fw_version(zigpy_dev),
            hardware_version=getattr(zigpy_dev, 'hw_version', 0) or 0,
        )

    def _get_current_fw_version(self, zigpy_dev) -> int:
        """Extract current firmware version from the OTA cluster attributes."""
        for ep_id, ep in zigpy_dev.endpoints.items():
            if ep_id == 0:
                continue
            # OTA cluster 0x0019, attribute 0x0002 = current_file_version
            ota = ep.out_clusters.get(0x0019) or ep.in_clusters.get(0x0019)
            if ota:
                cache = getattr(ota, '_attr_cache', {})
                ver = cache.get(0x0002)  # current_file_version
                if ver is not None:
                    return ver
        return 0

    def _get_image_type(self, zigpy_dev) -> int:
        """Extract image type from OTA cluster or device info."""
        for ep_id, ep in zigpy_dev.endpoints.items():
            if ep_id == 0:
                continue
            ota = ep.out_clusters.get(0x0019) or ep.in_clusters.get(0x0019)
            if ota:
                cache = getattr(ota, '_attr_cache', {})
                img_type = cache.get(0x0008)  # image_type_id
                if img_type is not None:
                    return img_type
        return 0xFFFF

    @staticmethod
    def _format_version(version) -> str:
        """Format firmware version as hex string."""
        if version is None:
            return "unknown"
        if isinstance(version, int):
            return f"0x{version:08x}"
        return str(version)


def build_ota_config(config: dict) -> dict:
    """
    Build the OTA section of the zigpy config from our config.yaml.
    Called from core.py when building EZSP/ZNP configs.
    """
    ota_conf = config.get('ota', {})

    if not ota_conf.get('enabled', True):
        return {}

    result = {}

    # Ensure local OTA directory exists
    os.makedirs(OTA_DIR, exist_ok=True)

    # Build providers list
    providers = ota_conf.get('providers', None)
    extra_providers = ota_conf.get('extra_providers', [])
    disable_default = ota_conf.get('disable_default_providers', [])

    if providers is not None:
        result['providers'] = providers
    if extra_providers:
        result['extra_providers'] = extra_providers
    if disable_default:
        result['disable_default_providers'] = disable_default

    # Always add local advanced provider for uploaded files
    local_provider = {
        "type": "advanced",
        "warning": "I understand I can *destroy* my devices by enabling OTA updates from files. "
                   "Some OTA updates can be mistakenly applied to the wrong device, breaking it. "
                   "I am consciously using this at my own risk.",
        "path": str(Path(OTA_DIR).resolve()),
    }

    if 'extra_providers' not in result:
        result['extra_providers'] = []
    result['extra_providers'].append(local_provider)

    return result