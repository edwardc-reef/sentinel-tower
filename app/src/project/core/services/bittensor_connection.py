import time
from collections.abc import Callable
from typing import Any


class ProviderReconnectBackoff:
    """Track one provider outage across RPC and reconnect failures."""

    def __init__(
        self,
        *,
        initial_delay_seconds: float,
        max_delay_seconds: float,
        alert_after_attempts: int,
    ) -> None:
        if initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be non-negative")
        if max_delay_seconds < initial_delay_seconds:
            raise ValueError("max_delay_seconds must not be less than initial_delay_seconds")
        if alert_after_attempts < 1:
            raise ValueError("alert_after_attempts must be at least 1")

        self._initial_delay_seconds = initial_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._alert_after_attempts = alert_after_attempts
        self._attempt = 0
        self._delay_seconds = initial_delay_seconds

    def record_failure(self, logger: Any, event: str, **context: Any) -> float:
        """Log a failure and return the delay to wait before the next attempt."""
        self._attempt += 1
        delay = self._delay_seconds
        log = logger.error if self._attempt == self._alert_after_attempts else logger.warning
        log(event, attempt=self._attempt, retry_in_s=delay, exc_info=True, **context)
        self._delay_seconds = min(delay * 2, self._max_delay_seconds)
        return delay

    def record_recovery(self, logger: Any, **context: Any) -> None:
        """Reset the outage only after a provider operation has succeeded."""
        if self._attempt:
            logger.info("Provider connection recovered", failed_attempts=self._attempt, **context)
        self._attempt = 0
        self._delay_seconds = self._initial_delay_seconds

    @staticmethod
    def wait(seconds: float, shutdown_requested: Callable[[], bool]) -> None:
        """Wait in short slices so a shutdown signal is honored promptly."""
        deadline = time.monotonic() + seconds
        while not shutdown_requested():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))
