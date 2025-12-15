"""Retry/re-init behavior for streamable HTTP and SSE clients on HTTP status errors."""

from __future__ import annotations

import httpx
import pytest
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp_proxy.streamablehttp_client import run_streamablehttp_client
from mcp_proxy.sse_client import run_sse_client


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


def make_http_status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://example/mcp")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"status {status}", request=req, response=resp)


def make_fail_then_pass_cm(status: int):
    state = {"called": False}

    @asynccontextmanager
    async def _cm(*_: Any, **__: Any) -> AsyncIterator[tuple[Any, Any, Any]]:
        if not state["called"]:
            state["called"] = True
            raise make_http_status_error(status)
        yield ("r", "w", None)

    return _cm


def make_always_fail_cm(status: int):

    @asynccontextmanager
    async def _cm(*_: Any, **__: Any) -> AsyncIterator[tuple[Any, Any, Any]]:
        raise make_http_status_error(status)
        yield ("r", "w", None)  # pragma: no cover

    return _cm


@pytest.mark.asyncio
async def test_streamable_reinit_on_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """StreamableHTTP client should retry/re-init on retryable HTTP status."""
    monkeypatch.setattr(
        "mcp_proxy.streamablehttp_client.streamablehttp_client",
        make_fail_then_pass_cm(404),
    )
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.stdio_server", dummy_stdio_server)
    async def _create_proxy(_s: Any) -> DummyApp:
        return DummyApp()
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.create_proxy_server", _create_proxy)
    @asynccontextmanager
    async def _client_session(*_: Any) -> AsyncIterator[Any]:
        yield DummyApp()
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.ClientSession", _client_session)
    async def _sleep(_: float) -> None:  # noqa: D401
        return None
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.asyncio.sleep", _sleep)

    await run_streamablehttp_client(
        url="http://example/mcp",
        headers=None,
        auth=None,
        verify_ssl=None,
        retry_attempts=1,
    )


@pytest.mark.asyncio
async def test_streamable_omits_reconnect_attempts_when_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StreamableHTTP client should not pass reconnect_attempts if SDK doesn't support it."""

    @asynccontextmanager
    async def _no_reconnect_arg_cm(
        url: str,
        headers: dict[str, Any] | None = None,
        auth: Any | None = None,
        httpx_client_factory: Any | None = None,
    ) -> AsyncIterator[tuple[Any, Any, Any]]:
        _ = (url, headers, auth, httpx_client_factory)
        yield ("r", "w", None)

    monkeypatch.setattr(
        "mcp_proxy.streamablehttp_client.streamablehttp_client",
        _no_reconnect_arg_cm,
    )
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.stdio_server", dummy_stdio_server)

    async def _create_proxy(_s: Any) -> DummyApp:
        return DummyApp()

    monkeypatch.setattr("mcp_proxy.streamablehttp_client.create_proxy_server", _create_proxy)

    @asynccontextmanager
    async def _client_session(*_: Any) -> AsyncIterator[Any]:
        yield DummyApp()

    monkeypatch.setattr("mcp_proxy.streamablehttp_client.ClientSession", _client_session)

    async def _sleep(_: float) -> None:  # noqa: D401
        return None

    monkeypatch.setattr("mcp_proxy.streamablehttp_client.asyncio.sleep", _sleep)

    await run_streamablehttp_client(
        url="http://example/mcp",
        headers=None,
        auth=None,
        verify_ssl=None,
        retry_attempts=1,
    )


@pytest.mark.asyncio
async def test_streamable_respects_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """StreamableHTTP stops after exceeding retry budget."""
    monkeypatch.setattr(
        "mcp_proxy.streamablehttp_client.streamablehttp_client",
        make_always_fail_cm(503),
    )
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.stdio_server", dummy_stdio_server)
    async def _create_proxy(_s: Any) -> DummyApp:
        return DummyApp()
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.create_proxy_server", _create_proxy)
    @asynccontextmanager
    async def _client_session(*_: Any) -> AsyncIterator[Any]:
        yield DummyApp()
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.ClientSession", _client_session)
    async def _sleep(_: float) -> None:  # noqa: D401
        return None
    monkeypatch.setattr("mcp_proxy.streamablehttp_client.asyncio.sleep", _sleep)

    with pytest.raises(httpx.HTTPStatusError):
        await run_streamablehttp_client(
            url="http://example/mcp",
            headers=None,
            auth=None,
            verify_ssl=None,
            retry_attempts=1,
        )


@pytest.mark.asyncio
async def test_sse_reinit_on_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE client should retry/re-init on retryable HTTP status."""
    monkeypatch.setattr(
        "mcp_proxy.sse_client.sse_client",
        make_fail_then_pass_cm(404),
    )
    monkeypatch.setattr("mcp_proxy.sse_client.stdio_server", dummy_stdio_server)
    async def _create_proxy(_s: Any) -> DummyApp:
        return DummyApp()
    monkeypatch.setattr("mcp_proxy.sse_client.create_proxy_server", _create_proxy)
    @asynccontextmanager
    async def _client_session(*_: Any) -> AsyncIterator[Any]:
        yield DummyApp()
    monkeypatch.setattr("mcp_proxy.sse_client.ClientSession", _client_session)
    async def _sleep(_: float) -> None:  # noqa: D401
        return None
    monkeypatch.setattr("mcp_proxy.sse_client.asyncio.sleep", _sleep)

    await run_sse_client(
        url="http://example/sse",
        headers=None,
        auth=None,
        verify_ssl=None,
        retry_attempts=1,
    )
