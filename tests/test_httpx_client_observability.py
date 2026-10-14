"""Privacy and rate observability tests for the shared HTTP client."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import Mock, patch

import httpx
import pytest

from mcp_proxy import httpx_client as httpx_client_module
from mcp_proxy.httpx_client import _sanitize_retry_after, custom_httpx_client


@pytest.fixture(autouse=True)
def _reset_request_rate_window() -> None:
    """Isolate the module-level rolling request counter between tests."""
    httpx_client_module._request_timestamps.clear()  # noqa: SLF001


def _event_hooks(mock_async_client: Mock) -> dict[str, list[Any]]:
    custom_httpx_client()
    return mock_async_client.call_args.kwargs["event_hooks"]


def _run_hook(
    hook: Callable[[httpx.Request | httpx.Response], Coroutine[object, object, None]],
    value: httpx.Request | httpx.Response,
) -> None:
    asyncio.run(hook(value))


def test_retry_after_never_logs_arbitrary_header_values() -> None:
    """Only bounded numeric delay seconds are safe to include in logs."""
    assert _sanitize_retry_after(" 12 ") == "12"
    assert _sanitize_retry_after("Bearer private-token") == "-"
    assert _sanitize_retry_after("Wed, 14 Oct 2026 09:00:00 GMT") == "-"
    assert _sanitize_retry_after("99999999999") == "-"
    assert _sanitize_retry_after(None) == "-"


@patch("mcp_proxy.httpx_client.httpx.AsyncClient")
def test_custom_certificate_bundle_path_is_not_logged(
    mock_async_client: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    """Avoid revealing local filesystem paths in debug logs."""
    caplog.set_level(logging.DEBUG, logger="mcp_proxy.httpx_client")
    custom_httpx_client(verify_ssl="C:/private/credentials/custom-ca.pem")
    assert mock_async_client.called
    assert "custom-ca.pem" not in caplog.text
    assert "private/credentials" not in caplog.text


@patch("mcp_proxy.httpx_client.httpx.AsyncClient")
def test_request_observability_logs_only_safe_fields(
    mock_async_client: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    """Emit only approved metadata and never request secrets or routing details."""
    hooks = _event_hooks(mock_async_client)
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:3020/mcp?secret=query",
        headers={"Authorization": "Bearer top-secret", "Cookie": "sid=private"},
    )
    response = httpx.Response(429, request=request, headers={"Retry-After": "12"})

    caplog.set_level(logging.INFO, logger="mcp_proxy.httpx_client")
    _run_hook(hooks["request"][0], request)
    _run_hook(hooks["response"][0], response)

    messages = [record.getMessage() for record in caplog.records]
    joined = "\n".join(messages)
    assert (
        "destination_class=loopback request_type=POST status=429 "
        "retry_after=12 requests_per_minute=1"
    ) in joined
    for forbidden in (
        "127.0.0.1",
        "/mcp",
        "secret=query",
        "Authorization",
        "top-secret",
        "Cookie",
        "sid=private",
    ):
        assert forbidden not in joined


@patch("mcp_proxy.httpx_client.httpx.AsyncClient")
def test_request_observability_tracks_per_process_requests_per_minute(
    mock_async_client: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep the rolling request count across clients within one connector process."""
    hooks = _event_hooks(mock_async_client)
    caplog.set_level(logging.INFO, logger="mcp_proxy.httpx_client")

    for method in ("GET", "POST"):
        request = httpx.Request(method, "http://192.168.1.10:3020/mcp")
        _run_hook(hooks["request"][0], request)
        _run_hook(hooks["response"][0], httpx.Response(200, request=request))

    messages = [record.getMessage() for record in caplog.records]
    safe = [message for message in messages if "destination_class=" in message]
    assert len(safe) == len(("GET", "POST"))
    assert "destination_class=private-network request_type=GET status=200" in safe[0]
    assert "requests_per_minute=1" in safe[0]
    assert "destination_class=private-network request_type=POST status=200" in safe[1]
    assert "requests_per_minute=2" in safe[1]
