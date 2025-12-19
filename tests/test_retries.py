"""Retry behavior tests for SSE and StreamableHTTP clients."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest
from unittest.mock import AsyncMock, patch

from mcp_proxy.sse_client import run_sse_client
from mcp_proxy.streamablehttp_client import run_streamablehttp_client


class DummyApp:
    """Minimal proxy app stub."""

    async def run(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None

    def create_initialization_options(self) -> None:  # noqa: D401
        return None


@asynccontextmanager
async def dummy_stdio_server() -> AsyncIterator[tuple[Any, Any]]:
    """Yield placeholder stdio streams."""
    yield ("r", "w")


@asynccontextmanager
async def dummy_client_session(*_: Any, **__: Any) -> AsyncIterator[object]:
    """Yield a placeholder client session with mutable attributes."""
    class _Session:
        pass
    yield _Session()


def make_fail_then_success_cm():
    """Return a context manager that fails once then yields streams."""
    state = {"calls": 0}

    @asynccontextmanager
    async def _cm(*_: Any, **__: Any) -> AsyncIterator[tuple[Any, Any, Any]]:
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("boom")
        yield ("r", "w", None)

    return _cm


def make_always_fail_cm():
    """Return a context manager that always raises."""

    @asynccontextmanager
    async def _cm(*_: Any, **__: Any) -> AsyncIterator[tuple[Any, Any, Any]]:
        raise RuntimeError("still broken")
        yield  # pragma: no cover

    return _cm


@pytest.mark.asyncio
async def test_streamable_exits_cleanly_when_stdio_already_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If stdin is already closed, StreamableHTTP client should exit without retries."""
    class _ClosedStdin:
        closed = True

    monkeypatch.setattr("mcp_proxy.streamablehttp_client.sys.stdin", _ClosedStdin())

    called = False

    @asynccontextmanager
    async def _cm(*_: Any, **__: Any) -> AsyncIterator[tuple[Any, Any, Any]]:
        nonlocal called
        called = True
        yield ("r", "w", None)

    monkeypatch.setattr("mcp_proxy.streamablehttp_client.streamablehttp_client", _cm)
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.asyncio.sleep", AsyncMock())

    await run_streamablehttp_client(
        url="http://example/mcp",
        headers=None,
        auth=None,
        verify_ssl=None,
        retry_attempts=6,
    )

    assert called is False


@pytest.mark.asyncio
async def test_streamable_exits_cleanly_when_stdio_server_raises_closed_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`I/O operation on closed file` should be treated as clean shutdown, not a retryable failure."""
    @asynccontextmanager
    async def _ok_cm(*_: Any, **__: Any) -> AsyncIterator[tuple[Any, Any, Any]]:
        yield ("r", "w", None)

    monkeypatch.setattr("mcp_proxy.streamablehttp_client.streamablehttp_client", _ok_cm)
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.create_proxy_server", AsyncMock(return_value=DummyApp()))
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.ClientSession", dummy_client_session)

    @asynccontextmanager
    async def _raising_stdio_server() -> AsyncIterator[tuple[Any, Any]]:
        raise ValueError("I/O operation on closed file")
        yield  # pragma: no cover

    monkeypatch.setattr("mcp_proxy.streamablehttp_client.stdio_server", _raising_stdio_server)
    sleep = AsyncMock()
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.asyncio.sleep", sleep)

    await run_streamablehttp_client(
        url="http://example/mcp",
        headers=None,
        auth=None,
        verify_ssl=None,
        retry_attempts=6,
    )

    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_streamable_retry_succeeds_after_one_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Should retry and succeed on second attempt when enabled."""
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.streamablehttp_client", make_fail_then_success_cm())
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.stdio_server", dummy_stdio_server)
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.create_proxy_server", AsyncMock(return_value=DummyApp()))
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.ClientSession", dummy_client_session)
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.asyncio.sleep", AsyncMock())

    await run_streamablehttp_client(
        url="http://example/mcp",
        headers=None,
        auth=None,
        verify_ssl=None,
        retry_attempts=1,
    )


@pytest.mark.asyncio
async def test_streamable_retry_respects_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stops after configured retries and raises."""
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.streamablehttp_client", make_always_fail_cm())
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.stdio_server", dummy_stdio_server)
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.create_proxy_server", AsyncMock(return_value=DummyApp()))
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.ClientSession", dummy_client_session)
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.asyncio.sleep", AsyncMock())

    with pytest.raises(RuntimeError):
        await run_streamablehttp_client(
            url="http://example/mcp",
            headers=None,
            auth=None,
            verify_ssl=None,
            retry_attempts=2,
        )


@pytest.mark.asyncio
async def test_sse_retry_succeeds_after_one_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE retry succeeds on second attempt."""
    monkeypatch.setattr("mcp_proxy.sse_client.sse_client", make_fail_then_success_cm())
    monkeypatch.setattr("mcp_proxy.sse_client.stdio_server", dummy_stdio_server)
    monkeypatch.setattr("mcp_proxy.sse_client.create_proxy_server", AsyncMock(return_value=DummyApp()))
    monkeypatch.setattr("mcp_proxy.sse_client.ClientSession", dummy_client_session)
    monkeypatch.setattr("mcp_proxy.sse_client.asyncio.sleep", AsyncMock())

    await run_sse_client(
        url="http://example/sse",
        headers=None,
        auth=None,
        verify_ssl=None,
        retry_attempts=1,
    )


@pytest.mark.asyncio
async def test_sse_retry_respects_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE retry stops after configured attempts."""
    monkeypatch.setattr("mcp_proxy.sse_client.sse_client", make_always_fail_cm())
    monkeypatch.setattr("mcp_proxy.sse_client.stdio_server", dummy_stdio_server)
    monkeypatch.setattr("mcp_proxy.sse_client.create_proxy_server", AsyncMock(return_value=DummyApp()))
    monkeypatch.setattr("mcp_proxy.sse_client.ClientSession", dummy_client_session)
    monkeypatch.setattr("mcp_proxy.sse_client.asyncio.sleep", AsyncMock())

    with pytest.raises(RuntimeError):
        await run_sse_client(
            url="http://example/sse",
            headers=None,
            auth=None,
            verify_ssl=None,
            retry_attempts=2,
        )
