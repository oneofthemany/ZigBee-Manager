"""
Topology mixin for ZigbeeService.
Handles mesh visualization data and LQI topology scanning.
"""
import asyncio
import logging
import time
from modules.packet_stats import packet_stats

logger = logging.getLogger("core.topology")


class TopologyMixin:
    """Mesh topology and network scanning methods."""

    async def scan_network_topology(self):
        """Scan network topology via Mgmt_Lqi_req to coordinator + routers."""
        if not self.app or not hasattr(self.app, 'topology'):
            return {"success": False, "error": "Topology not supported"}

        self._scan_in_progress = True
        self._scan_task = asyncio.create_task(self._run_topology_scan())
        return {"success": True, "message": "Topology scan started"}

    async def _run_topology_scan(self):
        """Active LQI scan - queries coordinator and routers for neighbor tables."""
        try:
            logger.info("Starting active topology scan (Mgmt_Lqi_req)...")
            await self.app.topology.scan()

            # Clean stale entries
            if self.app.topology.neighbors:
                stale_keys = [
                    ieee for ieee in list(self.app.topology.neighbors.keys())
                    if str(ieee) not in self.devices
                ]
                for ieee in stale_keys:
                    del self.app.topology.neighbors[ieee]
                    logger.debug(f"Removed stale topology entry: {ieee}")

            neighbor_count = len(self.app.topology.neighbors) if self.app.topology.neighbors else 0
            link_count = sum(len(n) for n in self.app.topology.neighbors.values()) if self.app.topology.neighbors else 0

            logger.info(f"Topology scan complete. {neighbor_count} devices, {link_count} links.")

            self._scan_last_completed = time.time()
            self._emit_sync("mesh_updated", self.get_simple_mesh())

        except Exception as e:
            logger.error(f"Topology scan failed: {e}")
        finally:
            self._scan_in_progress = False

    def get_scan_status(self):
        return {
            "in_progress": getattr(self, '_scan_in_progress', False),
            "last_completed": getattr(self, '_scan_last_completed', None)
        }

    def get_simple_mesh(self):
        """Get network topology for mesh visualization with packet statistics."""
        nodes = []
        connections = []
        device_stats = packet_stats.get_all_stats()

        # 1. Build Nodes with stats
        for ieee, zdev in self.devices.items():
            d = zdev.zigpy_dev
            stats = device_stats.get(ieee, {})

            nodes.append({
                "id": ieee,
                "ieee_address": ieee,
                "network_address": hex(d.nwk),
                "friendly_name": self.friendly_names.get(ieee, ieee),
                "role": zdev.get_role(),
                "manufacturer": str(d.manufacturer) if d.manufacturer else "Unknown",
                "model": str(d.model) if d.model else "Unknown",
                "lqi": getattr(d, 'lqi', 0) or 0,
                "online": zdev.is_available(),
                "polling_interval": self.polling_scheduler._intervals.get(ieee, 0),
                "packet_stats": {
                    "rx_packets": stats.get("rx_packets", 0),
                    "tx_packets": stats.get("tx_packets", 0),
                    "total_packets": stats.get("total_packets", 0),
                    "rx_rate": stats.get("rx_rate_per_min", 0),
                    "tx_rate": stats.get("tx_rate_per_min", 0),
                    "errors": stats.get("errors", 0),
                    "error_rate": stats.get("error_rate", 0)
                }
            })

        # 2. Build Links from Zigpy Topology
        if hasattr(self.app, 'topology') and self.app.topology.neighbors:
            for src_ieee, neighbors in self.app.topology.neighbors.items():
                src_str = str(src_ieee)
                for neighbor in neighbors:
                    dst_str = str(neighbor.ieee)
                    if src_str in self.devices and dst_str in self.devices:
                        connections.append({
                            "source": src_str,
                            "target": dst_str,
                            "lqi": neighbor.lqi or 0,
                            "relationship": getattr(neighbor, 'relationship', 'Unknown')
                        })

        connection_table = self._build_connection_table()

        return {
            "nodes": nodes,
            "links": connections,
            "connection_table": connection_table,
            "stats_summary": packet_stats.get_summary()
        }

    def _build_connection_table(self):
        """Build textual connection table from topology."""
        table = []
        coord_ieee = str(self.app.ieee) if hasattr(self.app, 'ieee') else None

        if hasattr(self.app, 'topology') and self.app.topology.neighbors:
            for src_ieee, neighbors in self.app.topology.neighbors.items():
                src_str = str(src_ieee)
                src_name = self.friendly_names.get(src_str, src_str[-8:])

                for neighbor in neighbors:
                    dst_str = str(neighbor.ieee)
                    if dst_str not in self.devices:
                        continue

                    dst_name = self.friendly_names.get(dst_str, dst_str[-8:])
                    relationship = getattr(neighbor, 'relationship', 0)

                    rel_str = {
                        0: "Parent", 1: "Child", 2: "Sibling",
                        3: "None", 4: "Previous Child"
                    }.get(relationship, f"Unknown({relationship})")

                    src_dev = self.devices.get(src_str)
                    dst_dev = self.devices.get(dst_str)

                    table.append({
                        "source_ieee": src_str,
                        "source_name": src_name,
                        "source_role": src_dev.get_role() if src_dev else "Unknown",
                        "target_ieee": dst_str,
                        "target_name": dst_name,
                        "target_role": dst_dev.get_role() if dst_dev else "Unknown",
                        "relationship": rel_str,
                        "lqi": neighbor.lqi or 0,
                        "depth": getattr(neighbor, 'depth', None)
                    })

        table.sort(key=lambda x: (x["source_name"], x["target_name"]))
        return table