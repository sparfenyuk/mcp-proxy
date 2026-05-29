"""Tests for ``mcp_proxy.notification_filter``.

These tests cover the scenario where an upstream MCP server emits a JSON-RPC
notification with a ``method`` that is not part of the MCP
``ServerNotification`` union (for example LSP-style ``window/logMessage``).

Without the filter, the unknown notification reaches
``mcp.shared.session.BaseSession`` and Pydantic raises a ``ValidationError``
that, on older versions of the ``mcp`` SDK, escapes the anyio ``TaskGroup``
wrapping ``stdio_client`` and crashes the proxy process.

The filter must:

* Drop unknown notifications (and log a warning) so the proxy stays up.
* Forward known notifications untouched (e.g. ``ProgressNotification``).
* Forward requests / responses untouched.
* Tear down cleanly on context exit without leaking tasks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import anyio
import pytest
from mcp import types
from mcp.shared.message import SessionMessage

from mcp_proxy.notification_filter import (
    filter_unknown_notifications,
    is_known_server_notification,
)

if TYPE_CHECKING:
    from anyio.streams.memory import MemoryObjectReceiveStream


def _make_session_message(payload: dict[str, object]) -> SessionMessage:
    """Wrap ``payload`` as a JSON-RPC ``SessionMessage`` exactly as ``stdio_client`` would."""
    return SessionMessage(types.JSONRPCMessage.model_validate(payload))


def test_is_known_server_notification_accepts_progress() -> None:
    """A standard MCP ``ProgressNotification`` envelope is recognised as known."""
    notification = types.JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/progress",
        params={"progressToken": 1, "progress": 0.5, "total": 1.0},
    )
    assert is_known_server_notification(notification) is True


def test_is_known_server_notification_rejects_lsp_log() -> None:
    """An LSP-style ``window/logMessage`` notification is recognised as unknown."""
    notification = types.JSONRPCNotification(
        jsonrpc="2.0",
        method="window/logMessage",
        params={"type": 3, "message": "hello from a misbehaving server"},
    )
    assert is_known_server_notification(notification) is False


@pytest.mark.anyio
async def test_filter_drops_unknown_notification_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown notifications are dropped from the read stream and surface as a warning."""
    upstream_write_to_proxy, upstream_read_into_proxy = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](0)
    proxy_write_to_upstream, _proxy_read_from_upstream = anyio.create_memory_object_stream[
        SessionMessage
    ](0)

    bad = _make_session_message(
        {
            "jsonrpc": "2.0",
            "method": "window/logMessage",
            "params": {"type": 3, "message": "noise"},
        },
    )
    good = _make_session_message(
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": 7, "progress": 0.25, "total": 1.0},
        },
    )

    received: list[SessionMessage | Exception] = []

    async def producer() -> None:
        async with upstream_write_to_proxy:
            await upstream_write_to_proxy.send(bad)
            await upstream_write_to_proxy.send(good)

    async def consumer(
        stream: MemoryObjectReceiveStream[SessionMessage | Exception],
    ) -> None:
        received.extend([item async for item in stream])

    caplog.set_level(logging.WARNING, logger="mcp_proxy.notification_filter")

    async with anyio.create_task_group() as tg, filter_unknown_notifications(
        upstream_read_into_proxy,
        proxy_write_to_upstream,
    ) as (filtered_read, _filtered_write):
        tg.start_soon(producer)
        tg.start_soon(consumer, filtered_read)
        await anyio.sleep(0.05)
        await filtered_read.aclose()

    assert len(received) == 1, "the unknown notification must be dropped"
    assert isinstance(received[0], SessionMessage)
    assert isinstance(received[0].message.root, types.JSONRPCNotification)
    assert received[0].message.root.method == "notifications/progress"

    assert any(
        "window/logMessage" in record.getMessage() for record in caplog.records
    ), "filter must log the dropped notification's method"


@pytest.mark.anyio
async def test_filter_passes_requests_and_responses_through() -> None:
    """Non-notification frames (requests, responses, errors) pass through unchanged."""
    upstream_write_to_proxy, upstream_read_into_proxy = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](0)
    proxy_write_to_upstream, _ = anyio.create_memory_object_stream[SessionMessage](0)

    request = _make_session_message(
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None},
    )
    response = _make_session_message(
        {"jsonrpc": "2.0", "id": 1, "result": {}},
    )

    received: list[SessionMessage | Exception] = []

    async def producer() -> None:
        async with upstream_write_to_proxy:
            await upstream_write_to_proxy.send(request)
            await upstream_write_to_proxy.send(response)

    async def consumer(
        stream: MemoryObjectReceiveStream[SessionMessage | Exception],
    ) -> None:
        received.extend([item async for item in stream])

    async with anyio.create_task_group() as tg, filter_unknown_notifications(
        upstream_read_into_proxy,
        proxy_write_to_upstream,
    ) as (filtered_read, _filtered_write):
        tg.start_soon(producer)
        tg.start_soon(consumer, filtered_read)
        await anyio.sleep(0.05)
        await filtered_read.aclose()

    assert len(received) == 2  # noqa: PLR2004 - request + response
    assert all(isinstance(item, SessionMessage) for item in received)


@pytest.mark.anyio
async def test_filter_survives_burst_of_unknown_notifications() -> None:
    """A flood of unknown notifications must not crash the filter or its parent task group."""
    upstream_write_to_proxy, upstream_read_into_proxy = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](0)
    proxy_write_to_upstream, _ = anyio.create_memory_object_stream[SessionMessage](0)

    burst = [
        _make_session_message(
            {
                "jsonrpc": "2.0",
                "method": "window/logMessage",
                "params": {"type": 3, "message": f"noise #{i}"},
            },
        )
        for i in range(50)
    ]

    received: list[SessionMessage | Exception] = []

    async def producer() -> None:
        async with upstream_write_to_proxy:
            for item in burst:
                await upstream_write_to_proxy.send(item)

    async def consumer(
        stream: MemoryObjectReceiveStream[SessionMessage | Exception],
    ) -> None:
        received.extend([item async for item in stream])

    async with anyio.create_task_group() as tg, filter_unknown_notifications(
        upstream_read_into_proxy,
        proxy_write_to_upstream,
    ) as (filtered_read, _filtered_write):
        tg.start_soon(producer)
        tg.start_soon(consumer, filtered_read)
        await anyio.sleep(0.05)
        await filtered_read.aclose()

    assert received == []
