"""Regression tests for Streamable HTTP reverse-client DELETE teardown.

Cover both the SDK 1.30 direct-send session termination and ordinary tool
requests, which must not inherit the short termination deadline.
"""

# ruff: noqa: SLF001, PLR2004
# pylint: disable=protected-access  # Intentionally test the private HTTP factory.
from __future__ import annotations

import importlib
from typing import Any

import anyio
import httpx
import pytest
from mcp.client.streamable_http import streamablehttp_client

reverse_client = importlib.import_module("mcp_proxy.streamablehttp_client")


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_normal_requests_are_not_limited_by_termination_deadline(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """A short DELETE deadline must not terminate normal upstream traffic."""
    monkeypatch.setattr(reverse_client, "SESSION_TERMINATION_TIMEOUT_SECONDS", 0.02)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == method
        await anyio.sleep(0.05)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(reverse_client, "custom_httpx_client", lambda **_kwargs: real_client)

    async def scenario() -> None:
        async with reverse_client._termination_bounded_httpx_client() as client:
            response = await client.send(client.build_request(method, "http://unused/mcp"))
            assert response.status_code == 200
        assert real_client.is_closed

    anyio.run(scenario)


def test_direct_send_delete_cancels_stalled_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK's raw send(DELETE) path is cancellation-bounded."""
    monkeypatch.setattr(reverse_client, "SESSION_TERMINATION_TIMEOUT_SECONDS", 0.025)
    methods: list[str] = []
    cancelled = anyio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()
        return httpx.Response(204)

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(reverse_client, "custom_httpx_client", lambda **_kwargs: real_client)

    async def scenario() -> None:
        with anyio.fail_after(3):
            async with reverse_client._termination_bounded_httpx_client() as client:
                with pytest.raises(TimeoutError):
                    await client.send(client.build_request("DELETE", "http://unused/mcp"))
        assert methods == ["DELETE"]
        assert cancelled.is_set()
        assert real_client.is_closed

    anyio.run(scenario)


def test_mcp_sdk_session_teardown_uses_bounded_direct_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real SDK 1.30 session termination sequence on context exit."""
    monkeypatch.setattr(reverse_client, "SESSION_TERMINATION_TIMEOUT_SECONDS", 0.025)
    methods: list[str] = []
    cancelled = anyio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()
        return httpx.Response(204)

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(reverse_client, "custom_httpx_client", lambda **_kwargs: real_client)

    async def scenario() -> None:
        # The real deprecated factory delegates to the modern transport in SDK 1.30.
        # The transport's test session id simulates a successfully initialized
        # stateful remote server without making a real external connection.
        with anyio.fail_after(3):
            async with streamablehttp_client(
                "http://unused/mcp",
                httpx_client_factory=reverse_client._termination_bounded_httpx_client,
            ) as streams:
                transport: Any = streams[2].__self__
                transport.session_id = "test-session"
        assert methods == ["DELETE"]
        assert cancelled.is_set()
        assert real_client.is_closed

    anyio.run(scenario)


def test_reverse_client_factory_preserves_tls_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not lose the existing HTTP client options when installing the guard."""
    supplied: dict[str, Any] = {}
    real_client = httpx.AsyncClient()

    def factory(**kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401
        supplied.update(kwargs)
        return real_client

    monkeypatch.setattr(reverse_client, "custom_httpx_client", factory)
    client = reverse_client._termination_bounded_httpx_client(
        headers={"X-Test": "header"}, verify_ssl=False, auth=None
    )
    assert client is real_client
    assert supplied == {
        "headers": {"X-Test": "header"},
        "verify_ssl": False,
        "auth": None,
        "timeout": None,
    }
    anyio.run(client.aclose)
