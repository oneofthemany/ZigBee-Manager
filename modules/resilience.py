"""
===============================
Implements robust error handling, watchdog recovery, and network stability
based on patterns from Home Assistant's ZHA integration.

Key Features:
- Automatic NCP failure recovery
- Watchdog timeout handling
- Connection state management
- Graceful degradation
- Comprehensive logging
"""
import asyncio
import logging
import time
from typing import Optional, Callable, Dict, Any
from datetime import datetime, timedelta
from bellows.ash import NcpFailure
import bellows.types as t

logger = logging.getLogger("resilience")


class ConnectionState:
    """Track coordinator connection state"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECOVERING = "recovering"
    FAILED = "failed"


class ResilienceManager:
    """
    Manages network resilience and automatic recovery.

    1. Detect failures early via watchdog
    2. Attempt graceful recovery
    3. Track failure patterns
    4. Prevent cascading failures
    """

    def __init__(self, app, event_callback: Optional[Callable] = None):
        """
        Initialize resilience manager.

        Args:
            app: The ControllerApplication instance
            event_callback: Callback for status events
        """
        self.app = app
        self.event_callback = event_callback

        # Connection state
        self.state = ConnectionState.DISCONNECTED
        self.last_state_change = time.time()

        # Error tracking
        self.error_count = 0
        self.last_error_time = 0
        self.error_window = 300  # 5 minute window
        self.max_errors_per_window = 10

        # Watchdog tracking
        self.last_watchdog_feed = time.time()
        self.watchdog_failures = 0
        self.watchdog_timeout = 120  # 2 minutes

        # Recovery state
        self.recovery_in_progress = False
        self.recovery_attempts = 0
        self.max_recovery_attempts = 3
        self.recovery_backoff = 5  # seconds

        # Statistics
        self.stats = {
            'total_errors': 0,
            'ncp_failures': 0,
            'watchdog_failures': 0,
            'recoveries_attempted': 0,
            'recoveries_successful': 0,
            'uptime_start': time.time()
        }

    def get_state(self) -> str:
        """Get current connection state."""
        return self.state

    def is_connected(self) -> bool:
        """Check if we're in a healthy connected state."""
        return self.state == ConnectionState.CONNECTED

    def update_state(self, new_state: str, reason: Optional[str] = None):
        """Update connection state and notify if changed."""
        if new_state != self.state:
            old_state = self.state
            self.state = new_state
            self.last_state_change = time.time()

            logger.info(f"State transition: {old_state} -> {new_state}" +
                        (f" ({reason})" if reason else ""))

            # Notify via callback
            if self.event_callback:
                asyncio.create_task(self.event_callback('coordinator_state', {
                    'state': new_state,
                    'previous_state': old_state,
                    'reason': reason,
                    'timestamp': time.time()
                }))

    async def handle_ncp_failure(self, error: NcpFailure) -> bool:
        """
        Handle NCP failure with automatic recovery.

        - Log the failure
        - Track error patterns
        - Attempt automatic recovery
        - Prevent infinite loops

        Args:
            error: The NcpFailure exception

        Returns:
            True if recovery was successful, False otherwise
        """
        self.stats['ncp_failures'] += 1
        self.stats['total_errors'] += 1
        self.error_count += 1
        self.last_error_time = time.time()

        error_code = str(error)
        logger.error(f"NCP Failure detected: {error_code}")

        # Check if we're in an error storm
        if self._is_error_storm():
            logger.critical("Error storm detected - too many failures in short time")
            self.update_state(ConnectionState.FAILED, "error_storm")
            return False

        # Attempt recovery
        if not self.recovery_in_progress:
            return await self._attempt_recovery(f"NCP failure: {error_code}")
        else:
            logger.warning("Recovery already in progress, skipping")
            return False

    async def handle_watchdog_failure(self, error: Exception) -> bool:
        """
        Handle watchdog timeout failure.

        Watchdog failures indicate the coordinator isn't responding.
        This is usually due to:
        - Radio firmware issues
        - Serial communication problems
        - Network congestion
        - Event loop blocking

        Args:
            error: The watchdog exception

        Returns:
            True if recovery was successful, False otherwise
        """
        self.stats['watchdog_failures'] += 1
        self.stats['total_errors'] += 1
        self.watchdog_failures += 1

        logger.error(f"Watchdog failure: {error}")

        # Watchdog failures are serious - attempt recovery immediately
        if not self.recovery_in_progress:
            return await self._attempt_recovery(f"Watchdog timeout: {error}")

        return False

    def feed_watchdog(self):
        """Mark successful watchdog feed."""
        self.last_watchdog_feed = time.time()

        # Reset watchdog failure counter on successful feed
        if self.watchdog_failures > 0:
            logger.info("Watchdog recovered, resetting failure counter")
            self.watchdog_failures = 0

    def _is_error_storm(self) -> bool:
        """
        Detect if we're experiencing an error storm.

        An error storm is when we get too many errors in a short time,
        indicating a fundamental problem that won't be fixed by retries.
        """
        now = time.time()

        # Reset counter if outside window
        if now - self.last_error_time > self.error_window:
            self.error_count = 1
            return False

        return self.error_count > self.max_errors_per_window

    async def _attempt_recovery(self, reason: str) -> bool:
        """
        Attempt to recover from a failure.

        Recovery strategy (based on ZHA):
        1. Mark recovery in progress
        2. Log the attempt
        3. Wait for backoff period
        4. Let the application handle reconnection
        5. Monitor the result

        Args:
            reason: Reason for recovery

        Returns:
            True if recovery succeeded, False otherwise
        """
        if self.recovery_in_progress:
            return False

        self.recovery_attempts += 1
        self.stats['recoveries_attempted'] += 1

        if self.recovery_attempts > self.max_recovery_attempts:
            logger.critical(f"Max recovery attempts ({self.max_recovery_attempts}) exceeded")
            self.update_state(ConnectionState.FAILED, "max_recovery_attempts")
            self.recovery_in_progress = False
            return False

        self.recovery_in_progress = True
        self.update_state(ConnectionState.RECOVERING, reason)

        try:
            logger.info(f"Recovery attempt {self.recovery_attempts}/{self.max_recovery_attempts}: {reason}")

            # Exponential backoff
            backoff = self.recovery_backoff * (2 ** (self.recovery_attempts - 1))
            logger.info(f"Waiting {backoff}s before recovery attempt")
            await asyncio.sleep(backoff)

            # The actual recovery is handled by zigpy/bellows automatically
            # We just need to wait and monitor
            logger.info("Waiting for automatic reconnection...")

            # Give it time to reconnect
            await asyncio.sleep(10)

            # Check if we're back online
            if await self._verify_connection():
                logger.info("Recovery successful!")
                self.recovery_attempts = 0
                self.error_count = 0
                self.stats['recoveries_successful'] += 1
                self.update_state(ConnectionState.CONNECTED, "recovery_successful")
                self.recovery_in_progress = False
                return True
            else:
                logger.warning("Recovery attempt failed - connection not restored")
                self.recovery_in_progress = False

                # Try again after delay
                await asyncio.sleep(5)
                return False

        except Exception as e:
            logger.error(f"Error during recovery: {e}", exc_info=True)
            self.recovery_in_progress = False
            return False

    async def _verify_connection(self) -> bool:
        """
        Verify that the coordinator connection is healthy.

        Returns:
            True if connection is healthy, False otherwise
        """
        try:
            # Try a simple EZSP command to verify connection
            if hasattr(self.app, '_ezsp') and self.app._ezsp:
                # Try to get network info
                status = await asyncio.wait_for(
                    self.app._ezsp.networkState(),
                    timeout=5.0
                )
                return status[0] == t.EmberNetworkStatus.JOINED_NETWORK
        except Exception as e:
            logger.debug(f"Connection verification failed: {e}")
            return False

        return False

    def reset_recovery_state(self):
        """Reset recovery state after successful operation."""
        self.recovery_attempts = 0
        self.error_count = 0

    def get_stats(self) -> Dict[str, Any]:
        """Get resilience statistics."""
        uptime = time.time() - self.stats['uptime_start']

        return {
            **self.stats,
            'uptime_seconds': uptime,
            'uptime_hours': uptime / 3600,
            'current_state': self.state,
            'error_count': self.error_count,
            'recovery_attempts': self.recovery_attempts,
            'recovery_in_progress': self.recovery_in_progress,
            'last_watchdog_feed': self.last_watchdog_feed,
            'watchdog_age_seconds': time.time() - self.last_watchdog_feed
        }


class WatchdogMonitor:
    """
    Independent watchdog monitor that runs alongside the main watchdog.

    Provides:
    - Independent health checks
    - Anomaly detection
    - Early warning system
    """

    def __init__(self, resilience_manager: ResilienceManager, check_interval: int = 30):
        """
        Initialize watchdog monitor.

        Args:
            resilience_manager: The resilience manager to notify
            check_interval: How often to check (seconds)
        """
        self.resilience = resilience_manager
        self.check_interval = check_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self):
        """Start the watchdog monitor."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._monitor_loop())
            logger.info(f"Watchdog monitor started (check interval: {self.check_interval}s)")

    def stop(self):
        """Stop the watchdog monitor."""
        self._running = False
        if self._task:
            self._task.cancel()


    async def _monitor_loop(self):
        """Main monitoring loop."""
        _stale_count = 0

        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                age = time.time() - self.resilience.last_watchdog_feed

                if age > self.resilience.watchdog_timeout:
                    _stale_count += 1
                    logger.warning(
                        f"Watchdog stale: {age:.1f}s since last feed "
                        f"(timeout: {self.resilience.watchdog_timeout}s, count: {_stale_count})")

                    # After 3 consecutive stale checks, trigger recovery
                    if _stale_count >= 3 and not self.resilience.recovery_in_progress:
                        logger.error(f"Watchdog stale for {_stale_count} checks — triggering recovery")
                        await self.resilience.handle_watchdog_failure(
                            Exception(f"Watchdog stale for {age:.0f}s")
                        )
                else:
                    _stale_count = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in watchdog monitor: {e}", exc_info=True)
                await asyncio.sleep(5)

def wrap_with_resilience(app, event_callback: Optional[Callable] = None):
    """
    Wrap a ControllerApplication with resilience features.

    This modifies the application's error handling to use the resilience manager.

    Args:
        app: The ControllerApplication instance
        event_callback: Optional callback for events

    Returns:
        ResilienceManager instance
    """
    resilience = ResilienceManager(app, event_callback)

    # Wrap the watchdog feed method
    original_watchdog_feed = app.watchdog_feed

    async def wrapped_watchdog_feed():
        """Wrapped watchdog feed with error handling."""
        try:
            await original_watchdog_feed()
            resilience.feed_watchdog()
        except NcpFailure as e:
            logger.error(f"NCP Failure in watchdog: {e}")
            await resilience.handle_ncp_failure(e)
            raise
        except Exception as e:
            logger.error(f"Watchdog failure: {e}")
            await resilience.handle_watchdog_failure(e)
            raise

    # Replace the method
    app.watchdog_feed = wrapped_watchdog_feed

    # Start independent monitor
    monitor = WatchdogMonitor(resilience)
    monitor.start()

    # Store references
    app._resilience_manager = resilience
    app._watchdog_monitor = monitor

    logger.info("Resilience wrapper installed")

    return resilience