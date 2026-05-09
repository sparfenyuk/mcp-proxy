"""Tests for circuit breaker pattern."""

import time
from unittest.mock import patch

import pytest

from mcp_proxy.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    clear_circuit_breakers,
    get_circuit_breaker,
    register_circuit_breaker,
)


@pytest.fixture(autouse=True)
def _cleanup():
    """Clean up circuit breakers between tests."""
    clear_circuit_breakers()
    yield
    clear_circuit_breakers()


class TestCircuitBreakerStates:
    """Test circuit breaker state transitions."""

    def test_initial_state_is_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_stays_closed_below_threshold(self) -> None:
        cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_opens_at_threshold(self) -> None:
        cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_open_blocks_requests(self) -> None:
        cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=2))
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_transitions_to_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout=1.0))
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Mock time to simulate timeout
        with patch("mcp_proxy.circuit_breaker.time.monotonic", return_value=time.monotonic() + 2.0):
            assert cb.state == CircuitState.HALF_OPEN
            assert cb.allow_request() is True

    def test_half_open_success_closes_circuit(self) -> None:
        cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout=1.0))
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Simulate timeout to get to half-open
        future = time.monotonic() + 2.0
        with patch("mcp_proxy.circuit_breaker.time.monotonic", return_value=future):
            assert cb.state == CircuitState.HALF_OPEN
            cb.record_success()

        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens_circuit(self) -> None:
        cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=1, recovery_timeout=10.0))
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Simulate timeout to get to half-open
        future = time.monotonic() + 11.0
        with patch("mcp_proxy.circuit_breaker.time.monotonic", return_value=future):
            assert cb.state == CircuitState.HALF_OPEN
            cb.record_failure()

        # After failure in half-open, circuit should be open again
        # (and recovery_timeout hasn't elapsed from the new failure time)
        assert cb._state == CircuitState.OPEN

    def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreaker(config=CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        # After success, counter resets — need 3 more failures to open
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_closed_allows_requests(self) -> None:
        cb = CircuitBreaker()
        assert cb.allow_request() is True


class TestCircuitBreakerRegistry:
    """Test circuit breaker registry functions."""

    def test_get_unregistered_returns_none(self) -> None:
        assert get_circuit_breaker("nonexistent") is None

    def test_register_and_get(self) -> None:
        register_circuit_breaker("test-server", {"failure_threshold": 5, "recovery_timeout": 60})
        cb = get_circuit_breaker("test-server")
        assert cb is not None
        assert cb.config.failure_threshold == 5
        assert cb.config.recovery_timeout == 60.0

    def test_register_with_defaults(self) -> None:
        register_circuit_breaker("test-server", {})
        cb = get_circuit_breaker("test-server")
        assert cb is not None
        assert cb.config.failure_threshold == 3
        assert cb.config.recovery_timeout == 30.0

    def test_clear_removes_all(self) -> None:
        register_circuit_breaker("s1", {})
        register_circuit_breaker("s2", {})
        clear_circuit_breakers()
        assert get_circuit_breaker("s1") is None
        assert get_circuit_breaker("s2") is None
