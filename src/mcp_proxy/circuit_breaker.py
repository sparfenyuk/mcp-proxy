"""Circuit Breaker pattern for MCP server resilience."""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass
class CircuitBreakerConfig:
    """Configuration for a circuit breaker."""

    failure_threshold: int = 3
    recovery_timeout: float = 30.0


@dataclass
class CircuitBreaker:
    """Circuit breaker for a single server."""

    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)

    @property
    def state(self) -> CircuitState:
        """Get current state (read-only, no side effects)."""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.config.recovery_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    def check_state(self) -> CircuitState:
        """Check and transition state if recovery timeout has elapsed."""
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.config.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        """Record a successful call."""
        self._failure_count = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Record a failed call."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.config.failure_threshold:
            self._state = CircuitState.OPEN

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        current = self.check_state()  # triggers open->half-open transition
        if current == CircuitState.CLOSED:
            return True
        return current == CircuitState.HALF_OPEN


# Global registry of circuit breakers per server name
_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(server_name: str) -> CircuitBreaker | None:
    """Get circuit breaker for a server, or None if not configured."""
    return _circuit_breakers.get(server_name)


def register_circuit_breaker(server_name: str, config: dict[str, Any]) -> None:
    """Register a circuit breaker for a server from config dict."""
    cb_config = CircuitBreakerConfig(
        failure_threshold=config.get("failure_threshold", 3),
        recovery_timeout=config.get("recovery_timeout", 30.0),
    )
    _circuit_breakers[server_name] = CircuitBreaker(config=cb_config)


def clear_circuit_breakers() -> None:
    """Clear all circuit breakers (for testing)."""
    _circuit_breakers.clear()
