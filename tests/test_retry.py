"""Tests for retry with exponential backoff."""

import pytest

from mcp_proxy.retry import (
    RetryConfig,
    clear_retry_configs,
    compute_delay,
    get_retry_config,
    is_retryable_error,
    register_retry_config,
)


@pytest.fixture(autouse=True)
def _cleanup():
    """Clean up retry configs between tests."""
    clear_retry_configs()
    yield
    clear_retry_configs()


class TestComputeDelay:
    """Test exponential backoff delay computation."""

    def test_first_attempt_base_delay(self) -> None:
        cfg = RetryConfig(base_delay=1.0, max_delay=10.0)
        delay = compute_delay(0, cfg)
        # base_delay * 2^0 = 1.0, plus jitter [0, 0.5]
        assert 1.0 <= delay <= 1.5

    def test_second_attempt_doubles(self) -> None:
        cfg = RetryConfig(base_delay=1.0, max_delay=10.0)
        delay = compute_delay(1, cfg)
        # base_delay * 2^1 = 2.0, plus jitter [0, 0.5]
        assert 2.0 <= delay <= 2.5

    def test_third_attempt(self) -> None:
        cfg = RetryConfig(base_delay=1.0, max_delay=10.0)
        delay = compute_delay(2, cfg)
        # base_delay * 2^2 = 4.0, plus jitter [0, 0.5]
        assert 4.0 <= delay <= 4.5

    def test_capped_at_max_delay(self) -> None:
        cfg = RetryConfig(base_delay=1.0, max_delay=5.0)
        delay = compute_delay(10, cfg)
        # min(1.0 * 2^10, 5.0) = 5.0, plus jitter [0, 0.5]
        assert 5.0 <= delay <= 5.5

    def test_custom_base_delay(self) -> None:
        cfg = RetryConfig(base_delay=2.0, max_delay=20.0)
        delay = compute_delay(0, cfg)
        # 2.0 * 2^0 = 2.0, plus jitter [0, 1.0]
        assert 2.0 <= delay <= 3.0


class TestIsRetryableError:
    """Test error classification for retry decisions."""

    def test_connection_error_is_retryable(self) -> None:
        assert is_retryable_error(ConnectionError("refused")) is True

    def test_timeout_error_is_retryable(self) -> None:
        assert is_retryable_error(TimeoutError("timed out")) is True

    def test_os_error_is_retryable(self) -> None:
        assert is_retryable_error(OSError("broken pipe")) is True

    def test_connection_reset_is_retryable(self) -> None:
        assert is_retryable_error(ConnectionResetError("reset")) is True

    def test_generic_exception_with_connection_keyword(self) -> None:
        assert is_retryable_error(Exception("connection refused")) is True

    def test_generic_exception_with_timeout_keyword(self) -> None:
        assert is_retryable_error(Exception("request timeout")) is True

    def test_value_error_not_retryable(self) -> None:
        assert is_retryable_error(ValueError("invalid input")) is False

    def test_runtime_error_not_retryable(self) -> None:
        assert is_retryable_error(RuntimeError("something failed")) is False

    def test_key_error_not_retryable(self) -> None:
        assert is_retryable_error(KeyError("missing")) is False


class TestRetryRegistry:
    """Test retry config registry."""

    def test_get_unregistered_returns_none(self) -> None:
        assert get_retry_config("nonexistent") is None

    def test_register_and_get(self) -> None:
        register_retry_config(
            "test-server",
            {
                "max_attempts": 5,
                "base_delay": 2.0,
                "max_delay": 20.0,
            },
        )
        cfg = get_retry_config("test-server")
        assert cfg is not None
        assert cfg.max_attempts == 5
        assert cfg.base_delay == 2.0
        assert cfg.max_delay == 20.0

    def test_register_with_defaults(self) -> None:
        register_retry_config("test-server", {})
        cfg = get_retry_config("test-server")
        assert cfg is not None
        assert cfg.max_attempts == 3
        assert cfg.base_delay == 1.0
        assert cfg.max_delay == 10.0

    def test_clear_removes_all(self) -> None:
        register_retry_config("s1", {})
        register_retry_config("s2", {})
        clear_retry_configs()
        assert get_retry_config("s1") is None
        assert get_retry_config("s2") is None
