"""Per-server rate limiting using asyncio.Semaphore."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from mcp import types

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 10
DEFAULT_MAX_WAIT_SECONDS = 30.0


class ServerRateLimiter:
    """Limits concurrent requests to a server using asyncio.Semaphore."""

    def __init__(
        self,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be > 0")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_wait_seconds = max_wait_seconds
        self._max_concurrent = max_concurrent

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    async def acquire(self) -> bool:
        """Acquire the semaphore with timeout. Returns True if acquired, False if timed out."""
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._max_wait_seconds)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self) -> None:
        """Release the semaphore."""
        self._semaphore.release()


def create_rate_limited_call_tool(
    original_handler: Callable[[types.CallToolRequest], Awaitable[types.ServerResult]],
    rate_limiter: ServerRateLimiter,
    server_name: str,
) -> Callable[[types.CallToolRequest], Awaitable[types.ServerResult]]:
    """Wrap a call_tool handler with rate limiting."""

    async def _rate_limited_call_tool(req: types.CallToolRequest) -> types.ServerResult:
        acquired = await rate_limiter.acquire()
        if not acquired:
            logger.warning(
                "Rate limit exceeded for server '%s' (max_concurrent=%d)",
                server_name,
                rate_limiter.max_concurrent,
            )
            return types.ServerResult(
                types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=f"Rate limit exceeded: server '{server_name}' has too many concurrent requests. Try again later.",
                        )
                    ],
                    isError=True,
                ),
            )
        try:
            return await original_handler(req)  # type: ignore
        finally:
            rate_limiter.release()

    return _rate_limited_call_tool
