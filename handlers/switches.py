"""
Switch cluster handlers.
Handles: Groups (0x0004), Scenes (0x0005)

Note: OnOffHandler (0x0006) is in general.py since it's used by many device types
"""
import logging
from typing import Any, Dict, List

from .base import ClusterHandler, register_handler

logger = logging.getLogger("handlers.switches")


@register_handler(0x0004)
class GroupsHandler(ClusterHandler):
    CLUSTER_ID = 0x0004

    async def add_to_group(self, gid, name=""): await self.cluster.add(gid, name)

    async def remove_from_group(self, gid): await self.cluster.remove(gid)

    async def get_groups(self):
        res = await self.cluster.get_membership([])
        return res[1] if res else []


@register_handler(0x0005)
class ScenesHandler(ClusterHandler):
    CLUSTER_ID = 0x0005
    ATTR_SCENE_COUNT = 0x0000
    ATTR_CURRENT_SCENE = 0x0001

    async def recall_scene(self, gid, sid): await self.cluster.recall(gid, sid)

    async def store_scene(self, gid, sid): await self.cluster.store(gid, sid)