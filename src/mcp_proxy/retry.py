"""Retry with exponential backoff for transient failures."""

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 10.0


def compute_delay(attempt: int, config: RetryConfig) -> float:
    """Compute delay with exponential backoff + jitter."""
    delay = min(config.base_delay * (2**attempt), config.max_delay)
    jitter = random.uniform(0, 0.5 * config.base_delay)  # noqa: S311
    return delay + jitter  # type: ignore


def is_retryable_error(exc: Exception) -> bool:
    """Check if an exception is a transient connection/timeout error worth retrying."""
    retryable_types = (
        ConnectionError,
        TimeoutError,
        OSError,
        asyncio.TimeoutError,
    )
    if isinstance(exc, retryable_types):
        return True
    # For generic Exception, check message keywords only if not a specific subclass
    if type(exc) is Exception:
        msg = str(exc).lower()
        return any(keyword in msg for keyword in ("connection", "timeout", "broken pipe", "eof"))
    return False


# Global registry of retry configs per server name
_retry_configs: dict[str, RetryConfig] = {}


def get_retry_config(server_name: str) -> RetryConfig | None:
    """Get retry config for a server, or None if not configured."""
    return _retry_configs.get(server_name)


def register_retry_config(server_name: str, config: dict[str, Any]) -> None:
    """Register a retry config for a server from config dict."""
    _retry_configs[server_name] = RetryConfig(
        max_attempts=config.get("max_attempts", 3),
        base_delay=config.get("base_delay", 1.0),
        max_delay=config.get("max_delay", 10.0),
    )


def clear_retry_configs() -> None:
    """Clear all retry configs (for testing)."""
    _retry_configs.clear()
