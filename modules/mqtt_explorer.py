"""
MQTT Explorer Service
Monitors all MQTT traffic for debugging and exploration purposes.
"""
import asyncio
import json
import logging
import time
from typing import Optional, List, Dict, Any, Callable
from collections import deque
from datetime import datetime
from contextlib import suppress

logger = logging.getLogger("mqtt_explorer")


class MQTTExplorer:
    """
    MQTT Explorer for debugging and monitoring all MQTT traffic.
    Subscribes to all topics and provides filtering, search, and inspection.
    """

    def __init__(self, mqtt_service, max_messages: int = 1000):
        self.mqtt_service = mqtt_service
        self.max_messages = max_messages

        # Message storage (circular buffer)
        self.messages: deque = deque(maxlen=max_messages)

        # Statistics
        self.stats = {
            "total_messages": 0,
            "messages_per_second": 0,
            "topics_seen": set(),
            "start_time": None,
            "last_message_time": None
        }

        # Control flags
        self._monitoring = False
        self._monitor_task: Optional[asyncio.Task] = None

        # Callbacks for real-time updates
        self._message_callbacks: List[Callable] = []

    def add_callback(self, callback: Callable):
        """Add a callback for real-time message notifications."""
        if callback not in self._message_callbacks:
            self._message_callbacks.append(callback)

    def remove_callback(self, callback: Callable):
        """Remove a message callback."""
        if callback in self._message_callbacks:
            self._message_callbacks.remove(callback)

    async def start_monitoring(self):
        """Start monitoring all MQTT traffic."""
        if self._monitoring:
            logger.warning("MQTT Explorer already monitoring")
            return False

        if not self.mqtt_service.connected:
            logger.error("Cannot start MQTT Explorer - MQTT not connected")
            return False

        self._monitoring = True
        self.stats["start_time"] = time.time()

        # Start the monitoring task
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("✓ MQTT Explorer monitoring started")
        return True

    async def stop_monitoring(self):
        """Stop monitoring MQTT traffic."""
        if not self._monitoring:
            return

        self._monitoring = False

        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None

        logger.info("MQTT Explorer monitoring stopped")

    async def _monitor_loop(self):
        """Main monitoring loop - subscribes to all topics."""
        try:
            # Create a separate MQTT client for monitoring
            from aiomqtt import Client

            async with Client(
                    hostname=self.mqtt_service.broker,
                    port=self.mqtt_service.port,
                    username=self.mqtt_service.username,
                    password=self.mqtt_service.password,
                    clean_session=True,
                    keepalive=60
            ) as client:
                # Subscribe to ALL topics
                await client.subscribe("#", qos=0)
                logger.info("📡 MQTT Explorer subscribed to all topics (#)")

                # Process incoming messages
                async for message in client.messages:
                    if not self._monitoring:
                        break

                    await self._process_message(message)

        except asyncio.CancelledError:
            logger.debug("MQTT Explorer monitor loop cancelled")
        except Exception as e:
            logger.error(f"MQTT Explorer monitor error: {e}", exc_info=True)
            self._monitoring = False

    async def _process_message(self, message):
        """Process an incoming MQTT message."""
        try:
            topic = str(message.topic)
            payload_bytes = message.payload if message.payload else b""

            # Try to decode payload
            try:
                payload_str = payload_bytes.decode('utf-8')
            except UnicodeDecodeError:
                payload_str = f"<binary data: {len(payload_bytes)} bytes>"

            # Parse JSON if possible
            parsed_payload = None
            if payload_str and payload_str.startswith(('{', '[')):
                try:
                    parsed_payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    pass

            # Create message record
            msg_record = {
                "timestamp": time.time(),
                "datetime": datetime.now().isoformat(),
                "topic": topic,
                "payload_raw": payload_str,
                "payload_parsed": parsed_payload,
                "qos": message.qos,
                "retain": message.retain,
                "size": len(payload_bytes)
            }

            # Store message
            self.messages.append(msg_record)

            # Update statistics
            self.stats["total_messages"] += 1
            self.stats["topics_seen"].add(topic)
            self.stats["last_message_time"] = time.time()

            # Calculate messages per second
            elapsed = time.time() - self.stats["start_time"]
            if elapsed > 0:
                self.stats["messages_per_second"] = self.stats["total_messages"] / elapsed

            # Notify callbacks
            for callback in self._message_callbacks:
                try:
                    await callback(msg_record)
                except Exception as e:
                    logger.error(f"Message callback error: {e}")

        except Exception as e:
            logger.error(f"Error processing MQTT Explorer message: {e}")

    def get_messages(
            self,
            topic_filter: Optional[str] = None,
            search: Optional[str] = None,
            limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get messages with optional filtering.

        Args:
            topic_filter: Filter by topic (supports wildcards)
            search: Search in payload
            limit: Maximum number of messages to return

        Returns:
            List of message records
        """
        results = []

        # Convert to list for filtering (newest first)
        messages_list = list(reversed(self.messages))

        for msg in messages_list:
            if len(results) >= limit:
                break

            # Apply topic filter
            if topic_filter and not self._topic_matches(msg["topic"], topic_filter):
                continue

            # Apply search filter
            if search:
                search_lower = search.lower()
                if (search_lower not in msg["topic"].lower() and
                        search_lower not in msg["payload_raw"].lower()):
                    continue

            results.append(msg)

        return results

    def _topic_matches(self, topic: str, pattern: str) -> bool:
        """
        Check if a topic matches a pattern with wildcards.
        Supports MQTT wildcards: + (single level), # (multi level)
        """
        if pattern == "#":
            return True

        topic_parts = topic.split('/')
        pattern_parts = pattern.split('/')

        # If pattern ends with #, it matches everything after that point
        if pattern_parts[-1] == "#":
            if len(topic_parts) < len(pattern_parts) - 1:
                return False
            for i in range(len(pattern_parts) - 1):
                if pattern_parts[i] != "+" and pattern_parts[i] != topic_parts[i]:
                    return False
            return True

        # Must have same number of levels
        if len(topic_parts) != len(pattern_parts):
            return False

        # Check each level
        for topic_part, pattern_part in zip(topic_parts, pattern_parts):
            if pattern_part != "+" and pattern_part != topic_part:
                return False

        return True

    def get_topics(self) -> List[str]:
        """Get all unique topics seen."""
        return sorted(list(self.stats["topics_seen"]))

    def get_stats(self) -> Dict[str, Any]:
        """Get monitoring statistics."""
        return {
            "monitoring": self._monitoring,
            "total_messages": self.stats["total_messages"],
            "messages_per_second": round(self.stats["messages_per_second"], 2),
            "unique_topics": len(self.stats["topics_seen"]),
            "buffer_size": len(self.messages),
            "max_buffer_size": self.max_messages,
            "start_time": self.stats["start_time"],
            "last_message_time": self.stats["last_message_time"],
            "uptime": time.time() - self.stats["start_time"] if self.stats["start_time"] else 0
        }

    def clear_messages(self):
        """Clear all stored messages."""
        self.messages.clear()
        self.stats["total_messages"] = 0
        self.stats["topics_seen"] = set()
        self.stats["start_time"] = time.time()
        logger.info("MQTT Explorer messages cleared")

    async def publish_test_message(
            self,
            topic: str,
            payload: str,
            qos: int = 0,
            retain: bool = False
    ) -> bool:
        """
        Publish a test message through the main MQTT service.

        Args:
            topic: MQTT topic
            payload: Message payload
            qos: Quality of Service (0, 1, or 2)
            retain: Whether to retain the message

        Returns:
            True if successful
        """
        try:
            if not self.mqtt_service.connected:
                logger.error("Cannot publish - MQTT not connected")
                return False

            # Use the main MQTT service to publish
            await self.mqtt_service.client.publish(
                topic,
                payload,
                qos=qos,
                retain=retain
            )
            logger.info(f"✓ Published test message to {topic}")
            return True

        except Exception as e:
            logger.error(f"Failed to publish test message: {e}")
            return False