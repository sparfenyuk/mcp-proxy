"""Tests for per-server rate limiting."""

import asyncio

import pytest
from mcp import types

from mcp_proxy.rate_limiter import ServerRateLimiter, create_rate_limited_call_tool


@pytest.mark.asyncio
async def test_rate_limiter_acquire_release():
    """Test basic acquire and release."""
    limiter = ServerRateLimiter(max_concurrent=2, max_wait_seconds=1.0)
    assert await limiter.acquire() is True
    assert await limiter.acquire() is True
    limiter.release()
    limiter.release()


@pytest.mark.asyncio
async def test_rate_limiter_timeout():
    """Test that acquire times out when semaphore is exhausted."""
    limiter = ServerRateLimiter(max_concurrent=1, max_wait_seconds=0.1)
    assert await limiter.acquire() is True
    # Second acquire should timeout
    assert await limiter.acquire() is False
    limiter.release()


@pytest.mark.asyncio
async def test_rate_limiter_waits_then_succeeds():
    """Test that acquire waits and succeeds when released in time."""
    limiter = ServerRateLimiter(max_concurrent=1, max_wait_seconds=2.0)
    assert await limiter.acquire() is True

    async def release_after_delay():
        await asyncio.sleep(0.1)
        limiter.release()

    asyncio.create_task(release_after_delay())
    # Should succeed because release happens before timeout
    assert await limiter.acquire() is True
    limiter.release()


@pytest.mark.asyncio
async def test_rate_limited_call_tool_passes_through():
    """Test that rate-limited handler passes through when under limit."""
    call_count = 0

    async def mock_handler(req):
        nonlocal call_count
        call_count += 1
        return types.ServerResult(
            types.CallToolResult(content=[types.TextContent(type="text", text="ok")])
        )

    limiter = ServerRateLimiter(max_concurrent=5, max_wait_seconds=1.0)
    wrapped = create_rate_limited_call_tool(mock_handler, limiter, "test-server")

    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="test_tool", arguments={}),
    )
    result = await wrapped(req)
    assert call_count == 1
    assert not result.root.isError


@pytest.mark.asyncio
async def test_rate_limited_call_tool_returns_error_on_timeout():
    """Test that rate-limited handler returns error when limit exceeded."""

    async def mock_handler(req):
        await asyncio.sleep(10)  # Never completes
        return types.ServerResult(
            types.CallToolResult(content=[types.TextContent(type="text", text="ok")])
        )

    limiter = ServerRateLimiter(max_concurrent=1, max_wait_seconds=0.1)
    wrapped = create_rate_limited_call_tool(mock_handler, limiter, "test-server")

    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="test_tool", arguments={}),
    )

    # First call acquires the semaphore
    task = asyncio.create_task(wrapped(req))
    await asyncio.sleep(0.05)  # Let it start

    # Second call should timeout
    result = await wrapped(req)
    assert result.root.isError
    assert "Rate limit exceeded" in result.root.content[0].text

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
