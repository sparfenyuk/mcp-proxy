"""Tests for custom httpx client retryable status handling."""

from __future__ import annotations

import pytest
import httpx

from mcp_proxy.httpx_client import custom_httpx_client


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 429, 503])
async def test_retryable_status_raises_http_status_error(status: int) -> None:
    """Any 4xx and 503 should raise to trigger reconnect logic."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, json={"error": "boom"})

    transport = httpx.MockTransport(handler)

    client = custom_httpx_client()
    client._transport = transport  # inject mock transport

    with pytest.raises(httpx.HTTPStatusError):
        await client.get("http://localhost/mcp")

    await client.aclose()


@pytest.mark.asyncio
async def test_non_retryable_status_passes() -> None:
    """Non-retryable responses should not raise."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"ok": True})

    transport = httpx.MockTransport(handler)

    client = custom_httpx_client()
    client._transport = transport

    resp = await client.get("http://localhost/mcp")
    assert resp.status_code == 200
    await client.aclose()


@pytest.mark.asyncio
async def test_500_is_not_retryable() -> None:
    """500 should not be treated as retryable (only 4xx and 503)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request, json={"error": "server error"})

    transport = httpx.MockTransport(handler)

    client = custom_httpx_client()
    client._transport = transport

    resp = await client.get("http://localhost/mcp")
    assert resp.status_code == 500
    await client.aclose()
