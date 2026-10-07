"""Retry and circuit breaker utilities."""

import time
import random


def retry(fn, attempts, base_delay, max_delay, jitter):
    """Retry a function with exponential backoff.

    Args:
        fn: Callable that may raise an exception
        attempts: Maximum number of attempts
        base_delay: Initial delay in seconds before first retry
        max_delay: Maximum delay cap
        jitter: Whether to add random jitter to delays

    Returns:
        Result from successful call to fn()

    Raises:
        Exception: The last exception from fn() if all attempts fail
    """
    last_exception = None

    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exception = e

        delay = base_delay * (2 ** (attempt + 1))
        delay = min(delay, max_delay)
        if jitter:
            delay += random.uniform(0, delay * 0.1)
        time.sleep(delay)

    raise last_exception


class CircuitBreaker:
    """A circuit breaker pattern implementation.

    This class manages the state of a circuit (open/closed/half-open)
    to prevent cascading failures.
    """

    def __init__(self, failure_threshold, timeout):
        """Initialize a circuit breaker.

        Args:
            failure_threshold: Number of failures before opening
            timeout: Time to wait before attempting to recover
        """
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half-open

    def call(self, fn, *args, **kwargs):
        """Call a function through the circuit breaker.

        Args:
            fn: Callable to execute
            *args: Positional arguments for fn
            **kwargs: Keyword arguments for fn

        Returns:
            Result from fn(*args, **kwargs)

        Raises:
            RuntimeError: If circuit is open
            Exception: Any exception from fn
        """
        if self.state == "open":
            if time.time() - self.last_failure_time > self.timeout:
                self.state = "half-open"
            else:
                raise RuntimeError("Circuit breaker is open")

        try:
            result = fn(*args, **kwargs)
            if self.state == "half-open":
                self.state = "closed"
                self.failure_count = 0
            return result
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
            raise e
