"""
MQTT Publish Queue - Non-Blocking Fast Path
============================================
Based on ZHA's MQTT management patterns for time-critical sensors.

This module provides:
- Non-blocking publish queue with automatic batching
- Intelligent message dropping for queue overflow
- Background worker for actual MQTT I/O
- Minimal latency for motion/radar events
"""
import asyncio
import logging
import time
from typing import Tuple, Optional
from collections import deque

logger = logging.getLogger("mqtt_queue")


class MQTTPublishQueue:
    """
    Non-blocking MQTT publish queue with batching and overflow handling.

    Design Goals:
    1. publish_nowait() returns in < 1ms (never blocks event loop)
    2. Automatic batching of rapid updates (10-50ms window)
    3. Graceful degradation under load (drops oldest messages)
    4. Background worker handles all network I/O

    Based on ZHA's async MQTT patterns.
    """

    def __init__(self, mqtt_service, max_queue_size: int = 1000, batch_window_ms: float = 10):
        """
        Initialize the publish queue.

        Args:
            mqtt_service: Parent MQTTService instance
            max_queue_size: Maximum queued messages (older dropped if exceeded)
            batch_window_ms: Time window for batching messages (milliseconds)
        """
        self.mqtt_service = mqtt_service
        self.max_size = max_queue_size
        self.batch_window = batch_window_ms / 1000.0  # Convert to seconds

        # Use deque with maxlen for automatic overflow handling
        # When full, oldest items are automatically dropped (O(1) operation)
        self._queue = deque(maxlen=max_queue_size)
        self._lock = asyncio.Lock()

        # Worker task management
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

        # Statistics
        self._stats = {
            'published': 0,
            'dropped': 0,
            'batches': 0,
            'queue_full_events': 0,
            'errors': 0
        }

        logger.info(f"MQTT Publish Queue initialized: max_size={max_queue_size}, batch_window={batch_window_ms}ms")

    async def start(self):
        """Start the background publish worker."""
        if self._running:
            logger.warning("Publish queue already running")
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._publish_worker())
        logger.info("MQTT publish queue worker started")

    async def stop(self):
        """Stop the publish worker and flush remaining messages."""
        if not self._running:
            return

        logger.info("Stopping MQTT publish queue...")
        self._running = False

        # Flush remaining messages
        remaining = len(self._queue)
        if remaining > 0:
            logger.info(f"Flushing {remaining} remaining messages...")
            await self._flush_all()

        # Cancel worker task
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        logger.info(f"MQTT publish queue stopped. Stats: {self._stats}")

    def publish_nowait(self, topic: str, payload: str, qos: int = 0, retain: bool = True) -> bool:
        """
        Queue a message for publishing (non-blocking).

        This method NEVER blocks - it returns immediately.
        If the queue is full, the oldest message is automatically dropped.

        Args:
            topic: MQTT topic (can be full or relative)
            payload: Message payload (usually JSON)
            qos: Quality of Service (0, 1, or 2)
            retain: Whether to retain the message

        Returns:
            True if queued successfully, False on error

        Performance:
            - Typical: < 100 microseconds
            - Worst case: < 1 millisecond
        """
        try:
            # Check if queue is full (will drop oldest)
            was_full = len(self._queue) >= self.max_size

            # Append to queue (deque with maxlen drops oldest automatically)
            self._queue.append((topic, payload, qos, retain, time.time()))

            if was_full:
                self._stats['queue_full_events'] += 1
                self._stats['dropped'] += 1
                logger.debug(f"Queue full, dropped oldest message (total dropped: {self._stats['dropped']})")

            return True

        except Exception as e:
            logger.error(f"Failed to queue message: {e}")
            self._stats['errors'] += 1
            return False

    async def _publish_worker(self):
        """
        Background worker that publishes queued messages.

        Strategy:
        1. Wait for messages with batch window timeout
        2. Collect up to 50 messages in a batch
        3. Publish batch asynchronously
        4. Repeat

        This ensures:
        - Minimal latency (batch window is small)
        - Efficient publishing (batching reduces overhead)
        - Non-blocking event loop (all I/O is async)
        """
        logger.info("MQTT publish worker started")
        batch = []

        while self._running:
            try:
                # Calculate deadline for this batch
                batch_deadline = time.time() + self.batch_window

                # Collect messages until batch is full or deadline reached
                while len(batch) < 50:  # Max 50 messages per batch
                    try:
                        # Calculate remaining time in batch window
                        now = time.time()
                        remaining_time = batch_deadline - now

                        if remaining_time <= 0:
                            # Batch window expired
                            break

                        # Try to get messages from queue
                        if self._queue:
                            async with self._lock:
                                if self._queue:
                                    # Pop from left (oldest first)
                                    batch.append(self._queue.popleft())
                                    continue

                        # No messages available, wait for batch window
                        await asyncio.sleep(min(0.001, remaining_time))

                    except Exception as e:
                        logger.debug(f"Error collecting batch: {e}")
                        break

                # Publish collected batch
                if batch:
                    await self._publish_batch(batch)
                    self._stats['batches'] += 1
                    batch.clear()
                else:
                    # No messages, short sleep to avoid CPU spinning
                    await asyncio.sleep(0.01)

            except asyncio.CancelledError:
                logger.info("Publish worker cancelled")
                break
            except Exception as e:
                logger.error(f"Publish worker error: {e}")
                self._stats['errors'] += 1
                await asyncio.sleep(1)  # Back off on error

        logger.info("MQTT publish worker stopped")

    async def _publish_batch(self, batch: list):
        """
        Publish a batch of messages.

        Uses fire-and-forget for QoS 0 (motion sensors) and
        awaits confirmation for QoS 1+ (critical messages).

        Args:
            batch: List of (topic, payload, qos, retain, queued_time) tuples
        """
        if not self.mqtt_service.connected:
            logger.debug(f"Skipping batch of {len(batch)} messages - MQTT not connected")
            self._stats['dropped'] += len(batch)
            return

        published_count = 0
        error_count = 0

        # Separate by QoS for different handling
        qos0_tasks = []
        qos1_messages = []

        for topic, payload, qos, retain, queued_time in batch:
            try:
                # Add base topic if needed
                if not topic.startswith(self.mqtt_service.base_topic):
                    full_topic = f"{self.mqtt_service.base_topic}/{topic}"
                else:
                    full_topic = topic

                # Calculate age of message
                age_ms = (time.time() - queued_time) * 1000

                if qos == 0:
                    # Fire-and-forget for QoS 0 (motion sensors)
                    # Create task but don't await - let it run in background
                    task = asyncio.create_task(
                        self.mqtt_service.client.publish(
                            full_topic, payload, qos=qos, retain=retain
                        )
                    )
                    qos0_tasks.append(task)
                    published_count += 1

                    if age_ms > 50:
                        logger.debug(f"Published aged message: {full_topic} ({age_ms:.1f}ms old)")
                else:
                    # QoS 1+ - await confirmation
                    qos1_messages.append((full_topic, payload, qos, retain))

            except Exception as e:
                logger.debug(f"Failed to publish {topic}: {e}")
                error_count += 1

        # Publish QoS 1+ messages with confirmation
        for full_topic, payload, qos, retain in qos1_messages:
            try:
                await self.mqtt_service.client.publish(
                    full_topic, payload, qos=qos, retain=retain
                )
                published_count += 1
            except Exception as e:
                logger.debug(f"Failed to publish QoS{qos} {full_topic}: {e}")
                error_count += 1

        # Update statistics
        self._stats['published'] += published_count
        self._stats['errors'] += error_count

        if error_count > 0:
            logger.warning(f"Batch published: {published_count} OK, {error_count} failed")
        else:
            logger.debug(f"Batch published: {published_count} messages")

    async def _flush_all(self):
        """Flush all remaining messages in queue."""
        batch = []
        async with self._lock:
            while self._queue:
                batch.append(self._queue.popleft())

        if batch:
            await self._publish_batch(batch)

    def get_stats(self) -> dict:
        """
        Get queue statistics.

        Returns:
            Dictionary with queue metrics
        """
        return {
            'queue_size': len(self._queue),
            'queue_max': self.max_size,
            'published_total': self._stats['published'],
            'dropped_total': self._stats['dropped'],
            'batches_total': self._stats['batches'],
            'queue_full_events': self._stats['queue_full_events'],
            'errors_total': self._stats['errors'],
            'running': self._running
        }


# Performance Testing
if __name__ == "__main__":
    import asyncio
    import json


    async def test_performance():
        """Test publish queue performance."""

        # Mock MQTT service
        class MockMQTT:
            def __init__(self):
                self.connected = True
                self.base_topic = "test"
                self.client = self

            async def publish(self, topic, payload, qos=0, retain=True):
                await asyncio.sleep(0.001)  # Simulate network delay

        mqtt = MockMQTT()
        queue = MQTTPublishQueue(mqtt, max_queue_size=100, batch_window_ms=10)

        await queue.start()

        # Test 1: Latency measurement
        print("\nTest 1: Single message latency")
        start = time.perf_counter()
        queue.publish_nowait("test/motion", json.dumps({"motion": True}))
        latency = (time.perf_counter() - start) * 1000000  # microseconds
        print(f"  publish_nowait() latency: {latency:.1f} µs")

        # Test 2: Burst handling
        print("\nTest 2: Burst handling (1000 messages)")
        start = time.perf_counter()
        for i in range(1000):
            queue.publish_nowait(f"test/sensor{i}", json.dumps({"value": i}))
        burst_time = (time.perf_counter() - start) * 1000
        print(f"  Queued 1000 messages in {burst_time:.2f} ms ({burst_time / 1000:.2f} ms/msg)")

        # Wait for queue to drain
        await asyncio.sleep(2)

        # Test 3: Statistics
        print("\nTest 3: Queue statistics")
        stats = queue.get_stats()
        for key, value in stats.items():
            print(f"  {key}: {value}")

        await queue.stop()
        print("\nPerformance test completed")


    asyncio.run(test_performance())