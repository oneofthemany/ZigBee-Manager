"""
Robust Error Handler
====================
Comprehensive error handling for Zigbee commands with automatic retries
and graceful degradation.

Based on ZHA patterns:
- Exponential backoff
- Context-aware retries
- Delivery failure handling
- Proper logging
"""
import asyncio
import logging
import functools
from typing import Callable, Any, Optional, Dict
from bellows.ash import NcpFailure
import bellows.types as t

logger = logging.getLogger("error_handler")


class DeliveryError(Exception):
    """Raised when message delivery fails."""
    pass


class RetryExhausted(Exception):
    """Raised when max retries exceeded."""
    pass


class ErrorHandler:
    """
    Centralized error handling for Zigbee operations.

    Features:
    - Automatic retries with exponential backoff
    - Error classification (transient vs permanent)
    - Statistics tracking
    - Context preservation
    """

    # Transient errors that should be retried
    TRANSIENT_ERRORS = {
        'DELIVERY_FAILED',
        'MAC_NO_ACK',
        'MAC_CHANNEL_ACCESS_FAILURE',
        'ERROR_EXCEEDED_MAXIMUM_ACK_TIMEOUT_COUNT',
        'EZSP_ERROR_NO_BUFFERS',
        'NETWORK_BUSY',
    }

    # Permanent errors that shouldn't be retried
    PERMANENT_ERRORS = {
        'NOT_FOUND',
        'INVALID_PARAMETER',
        'INVALID_CALL',
        'TABLE_FULL',
    }

    def __init__(self):
        self.stats = {
            'total_attempts': 0,
            'total_retries': 0,
            'total_successes': 0,
            'total_failures': 0,
            'errors_by_type': {},
        }

    def is_transient(self, error: Exception) -> bool:
        """
        Determine if an error is transient (should be retried).

        Args:
            error: The exception

        Returns:
            True if error is transient
        """
        error_str = str(error).upper()

        # Check for known transient patterns
        for pattern in self.TRANSIENT_ERRORS:
            if pattern in error_str:
                return True

        # Check for permanent patterns
        for pattern in self.PERMANENT_ERRORS:
            if pattern in error_str:
                return False

        # NcpFailure is usually transient
        if isinstance(error, NcpFailure):
            return True

        # asyncio.TimeoutError is transient
        if isinstance(error, asyncio.TimeoutError):
            return True

        # Default to non-transient to avoid infinite retries
        return False

    def record_error(self, error: Exception):
        """Record error in statistics."""
        error_type = type(error).__name__
        self.stats['errors_by_type'][error_type] = \
            self.stats['errors_by_type'].get(error_type, 0) + 1

    async def retry_operation(
            self,
            operation: Callable,
            *args,
            max_retries: int = 3,
            backoff_base: float = 1.0,
            backoff_max: float = 30.0,
            timeout: Optional[float] = None,
            context: Optional[str] = None,
            **kwargs
    ) -> Any:
        """
        Execute operation with automatic retries.

        Args:
            operation: The async function to execute
            *args: Positional arguments for operation
            max_retries: Maximum number of retry attempts
            backoff_base: Base delay for exponential backoff (seconds)
            backoff_max: Maximum backoff delay (seconds)
            timeout: Optional timeout for each attempt (seconds)
            context: Optional context string for logging
            **kwargs: Keyword arguments for operation

        Returns:
            Result from operation

        Raises:
            RetryExhausted: If max retries exceeded
            Exception: If error is permanent (non-retryable)
        """
        self.stats['total_attempts'] += 1

        last_error = None

        for attempt in range(max_retries + 1):
            try:
                # Calculate backoff for this attempt
                if attempt > 0:
                    backoff = min(backoff_base * (2 ** (attempt - 1)), backoff_max)
                    logger.debug(f"Retry #{attempt} after {backoff:.1f}s" +
                                 (f" ({context})" if context else ""))
                    await asyncio.sleep(backoff)
                    self.stats['total_retries'] += 1

                # Execute operation with optional timeout
                if timeout:
                    result = await asyncio.wait_for(
                        operation(*args, **kwargs),
                        timeout=timeout
                    )
                else:
                    result = await operation(*args, **kwargs)

                # Success!
                if attempt > 0:
                    logger.info(f"Operation succeeded after {attempt} retries" +
                                (f" ({context})" if context else ""))

                self.stats['total_successes'] += 1
                return result

            except Exception as e:
                last_error = e
                self.record_error(e)

                # Log the error
                if attempt == 0:
                    logger.warning(f"Operation failed: {e}" +
                                   (f" ({context})" if context else ""))
                else:
                    logger.warning(f"Retry #{attempt} failed: {e}" +
                                   (f" ({context})" if context else ""))

                # Check if we should retry
                if not self.is_transient(e):
                    logger.error(f"Permanent error, not retrying: {e}")
                    self.stats['total_failures'] += 1
                    raise

                # If this was the last attempt, raise
                if attempt >= max_retries:
                    logger.error(f"Max retries ({max_retries}) exceeded" +
                                 (f" ({context})" if context else ""))
                    self.stats['total_failures'] += 1
                    raise RetryExhausted(
                        f"Operation failed after {max_retries} retries: {last_error}"
                    ) from last_error

        # Should never reach here
        raise RetryExhausted(f"Unexpected retry exhaustion: {last_error}")

    def get_stats(self) -> Dict[str, Any]:
        """Get error handling statistics."""
        total = self.stats['total_attempts']
        if total > 0:
            success_rate = (self.stats['total_successes'] / total) * 100
            retry_rate = (self.stats['total_retries'] / total) * 100
        else:
            success_rate = 0
            retry_rate = 0

        return {
            **self.stats,
            'success_rate': success_rate,
            'retry_rate': retry_rate,
        }


# Global error handler instance
_error_handler = ErrorHandler()


def with_retries(
        max_retries: int = 3,
        backoff_base: float = 1.0,
        timeout: Optional[float] = None
):
    """
    Decorator to add automatic retries to async functions.

    Usage:
        @with_retries(max_retries=3, backoff_base=2.0, timeout=10.0)
        async def send_command(device, command):
            ...

    Args:
        max_retries: Maximum retry attempts
        backoff_base: Base delay for exponential backoff
        timeout: Timeout for each attempt
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract context from function name and first argument
            context = f"{func.__name__}"
            if args and hasattr(args[0], '__class__'):
                obj = args[0]
                if hasattr(obj, 'ieee'):
                    context += f"({obj.ieee})"

            return await _error_handler.retry_operation(
                func,
                *args,
                max_retries=max_retries,
                backoff_base=backoff_base,
                timeout=timeout,
                context=context,
                **kwargs
            )

        return wrapper

    return decorator


def with_resilience(
        max_retries: int = 2,
        timeout: float = 10.0,
        suppress_errors: bool = False
):
    """
    Decorator for device commands that need extra resilience.

    Features:
    - Shorter timeout (10s default)
    - Fewer retries (2 default)
    - Optional error suppression for non-critical operations

    Usage:
        @with_resilience(suppress_errors=True)
        async def poll_sensor(device):
            ...
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            context = f"{func.__name__}"

            try:
                return await _error_handler.retry_operation(
                    func,
                    *args,
                    max_retries=max_retries,
                    backoff_base=1.0,
                    timeout=timeout,
                    context=context,
                    **kwargs
                )
            except Exception as e:
                if suppress_errors:
                    logger.debug(f"Suppressed error in {context}: {e}")
                    return None
                raise

        return wrapper

    return decorator


def get_error_stats() -> Dict[str, Any]:
    """Get global error handling statistics."""
    return _error_handler.get_stats()


class CommandWrapper:
    """
    Wraps device commands with proper error handling.

    Usage:
        wrapper = CommandWrapper(device)
        await wrapper.send_command('on')
    """

    def __init__(self, device, error_handler: Optional[ErrorHandler] = None):
        """
        Initialize command wrapper.

        Args:
            device: The ZHA device to wrap
            error_handler: Optional error handler (uses global if None)
        """
        self.device = device
        self.error_handler = error_handler or _error_handler

    async def send_command(
            self,
            command: str,
            value: Any = None,
            endpoint_id: Optional[int] = None,
            max_retries: int = 3,
            timeout: float = 10.0
    ) -> Dict[str, Any]:
        """
        Send command with automatic retry.

        Args:
            command: Command name ('on', 'off', 'level', etc.)
            value: Optional value for command
            endpoint_id: Optional endpoint ID
            max_retries: Maximum retry attempts
            timeout: Timeout per attempt

        Returns:
            Result dictionary
        """
        context = f"send_command({self.device.ieee}, {command})"

    async def execute(
            self,
            operation: Callable,
            max_retries: int = 2,
            timeout: float = 10.0,
            context_suffix: Optional[str] = None
    ) -> Any:
        """
        Execute an arbitrary async operation (e.g., handler.poll) with automatic retry.

        Args:
            operation: The async function to execute.
            max_retries: Maximum retry attempts.
            timeout: Timeout per attempt.
            context_suffix: Optional string to append to the context for logging.

        Returns:
            Result from operation.
        """
        # Base context derived from the device and the function being executed
        context = f"execute({self.device.ieee}, {operation.__name__})"
        if context_suffix:
            context += f" ({context_suffix})"

        # The core logic is delegated to the centralized error handler
        return await self.error_handler.retry_operation(
            operation,
            max_retries=max_retries,
            timeout=timeout,
            context=context
        )

    async def poll(self, max_retries: int = 2, timeout: float = 10.0) -> Dict[str, Any]:
        """
        Poll device with automatic retry.

        Args:
            max_retries: Maximum retry attempts
            timeout: Timeout per attempt

        Returns:
            Poll result
        """
        context = f"poll({self.device.ieee})"

        async def _execute():
            return await self.device.poll()

        return await self.error_handler.retry_operation(
            _execute,
            max_retries=max_retries,
            timeout=timeout,
            context=context
        )


class BatchCommandExecutor:
    """
    Execute commands on multiple devices with proper error handling.

    Features:
    - Concurrent execution with rate limiting
    - Per-device error handling
    - Progress tracking
    """

    def __init__(self, max_concurrent: int = 5):
        """
        Initialize batch executor.

        Args:
            max_concurrent: Maximum concurrent operations
        """
        self.max_concurrent = max_concurrent
        self.error_handler = ErrorHandler()

    async def execute_batch(
            self,
            devices: list,
            operation: Callable,
            *args,
            **kwargs
    ) -> Dict[str, Any]:
        """
        Execute operation on multiple devices.

        Args:
            devices: List of devices
            operation: Operation to execute (async function)
            *args: Arguments for operation
            **kwargs: Keyword arguments for operation

        Returns:
            Results dictionary with successes and failures
        """
        results = {
            'successful': [],
            'failed': [],
            'total': len(devices)
        }

        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def _execute_single(device):
            async with semaphore:
                try:
                    result = await self.error_handler.retry_operation(
                        operation,
                        device,
                        *args,
                        max_retries=2,
                        context=f"batch({device.ieee})",
                        **kwargs
                    )
                    results['successful'].append({
                        'ieee': device.ieee,
                        'result': result
                    })
                except Exception as e:
                    logger.warning(f"Batch operation failed for {device.ieee}: {e}")
                    results['failed'].append({
                        'ieee': device.ieee,
                        'error': str(e)
                    })

        # Execute all operations concurrently
        await asyncio.gather(*[_execute_single(dev) for dev in devices])

        return results


if __name__ == "__main__":
    # Demo/testing
    logging.basicConfig(level=logging.INFO)


    async def test_retry():
        """Test the retry mechanism."""

        # Simulate operation that fails twice then succeeds
        call_count = 0

        async def flaky_operation():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise DeliveryError("Simulated delivery failure")
            return "Success!"

        handler = ErrorHandler()

        try:
            result = await handler.retry_operation(
                flaky_operation,
                max_retries=3,
                context="test"
            )
            print(f"Result: {result}")
            print(f"Stats: {handler.get_stats()}")
        except Exception as e:
            print(f"Failed: {e}")


    asyncio.run(test_retry())