"""
Database cleanup mixin for ZigbeeService.
Handles orphaned device detection and database table cleanup.
"""
import logging
import sqlite3

logger = logging.getLogger("core.database")


class DatabaseMixin:
    """Database cleanup and orphan management methods."""

    def _force_clean_database(self, ieee: str):
        """Force-clean all database tables for a device IEEE."""
        db_path = "zigbee.db"

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'devices_v%'")
            devices_table = cursor.fetchone()

            if not devices_table:
                logger.warning(f"[{ieee}] Could not detect zigpy table version")
                conn.close()
                return

            version = devices_table[0].split('_')[-1]

            tables = [
                f'devices_{version}', f'endpoints_{version}',
                f'clusters_{version}', f'node_descriptors_{version}',
                f'attributes_cache_{version}', f'neighbors_{version}',
                f'routes_{version}', f'relays_{version}'
            ]

            logger.info(f"[{ieee}] Force cleaning database tables (version: {version})...")

            for table in tables:
                try:
                    cursor.execute(f"DELETE FROM {table} WHERE ieee=?", (ieee,))
                    deleted = cursor.rowcount
                    if deleted > 0:
                        logger.info(f"[{ieee}] Deleted {deleted} rows from {table}")
                except sqlite3.Error as e:
                    logger.debug(f"[{ieee}] Could not clean {table}: {e}")

            conn.commit()
            conn.close()
            logger.info(f"[{ieee}] Database cleanup complete")

        except Exception as e:
            logger.error(f"[{ieee}] Database cleanup failed: {e}")

    async def find_duplicate_devices(self) -> dict:
        """Find devices that exist in database but not in active network."""
        import sqlite3
        db_path = "zigbee.db"

        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'devices_v%'")
            devices_table = cursor.fetchone()
            if not devices_table:
                conn.close()
                return {"error": "Could not detect table version"}

            version = devices_table[0].split('_')[-1]
            cursor.execute(f"SELECT ieee FROM devices_{version}")
            db_devices = [row[0] for row in cursor.fetchall()]
            conn.close()

            # Compare against BOTH zigpy and our wrapper dict
            zigpy_devices = [str(ieee).lower() for ieee in self.app.devices.keys()]
            managed_devices = [ieee.lower() for ieee in self.devices.keys()]
            db_devices_normalized = [ieee.lower() for ieee in db_devices]

            # Orphaned = in DB but not in zigpy (true DB orphans)
            db_orphaned = [ieee for ieee in db_devices_normalized if ieee not in zigpy_devices]

            # Stale = in DB/zigpy but missing from our wrapper dict (lost via device_left)
            stale = [ieee for ieee in db_devices_normalized
                     if ieee in zigpy_devices and ieee not in managed_devices]

            orphaned = list(set(db_orphaned + stale))

            return {
                "total_in_db": len(db_devices),
                "active_zigpy": len(zigpy_devices),
                "active_managed": len(managed_devices),
                "orphaned": orphaned,
                "db_orphaned": db_orphaned,
                "stale": stale,
                "count": len(orphaned)
            }
        except Exception as e:
            logger.error(f"Failed to find duplicate devices: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e)}

    async def cleanup_orphaned_devices(self) -> dict:
        """Remove orphaned devices and recover stale ones."""
        import zigpy.types
        result = await self.find_duplicate_devices()

        if "error" in result:
            return result

        removed = []
        recovered = []
        failed = []

        # 1. True DB orphans - remove from database
        for ieee in result.get("db_orphaned", []):
            try:
                self._force_clean_database(ieee)
                removed.append(ieee)
                logger.info(f"Removed orphaned device: {ieee}")
            except Exception as e:
                failed.append({"ieee": ieee, "error": str(e)})
                logger.error(f"Failed to remove {ieee}: {e}")

        # 2. Stale devices - recover by re-wrapping from zigpy
        for ieee in result.get("stale", []):
            try:
                from device import ZigManDevice
                z_ieee = zigpy.types.EUI64.convert(ieee)
                if z_ieee in self.app.devices:
                    self.devices[ieee] = ZigManDevice(self, self.app.devices[z_ieee])
                    self.devices[ieee]._available = False
                    if ieee in self.state_cache:
                        self.devices[ieee].restore_state(self.state_cache[ieee])
                    recovered.append(ieee)
                    logger.info(f"Recovered stale device: {ieee}")
            except Exception as e:
                failed.append({"ieee": ieee, "error": str(e)})
                logger.error(f"Failed to recover {ieee}: {e}")

        # Refresh frontend if anything changed
        if removed or recovered:
            self._rebuild_name_maps()

        return {
            "removed": removed,
            "recovered": recovered,
            "failed": failed,
            "count_removed": len(removed),
            "count_recovered": len(recovered),
            "count_failed": len(failed)
        }