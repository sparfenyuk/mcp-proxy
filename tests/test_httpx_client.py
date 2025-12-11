"""Tests for custom httpx client retryable status handling."""

from __future__ import annotations

import pytest
import httpx

from mcp_proxy.httpx_client import custom_httpx_client


@pytest.mark.asyncio
async def test_retryable_status_raises_http_status_error() -> None:
    """Retryable HTTP status codes should raise to trigger reconnect logic."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request, json={"error": "not found"})

    transport = httpx.MockTransport(handler)

    client = custom_httpx_client()
    client._transport = transport  # inject mock transport

    with pytest.raises(httpx.HTTPStatusError):
        await client.get("http://localhost/mcp")

    await client.aclose()


@pytest.mark.asyncio
async def test_non_retryable_status_passes() -> None:
    """200 responses should not raise."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"ok": True})

    transport = httpx.MockTransport(handler)

    client = custom_httpx_client()
    client._transport = transport

    resp = await client.get("http://localhost/mcp")
    assert resp.status_code == 200
    await client.aclose()
