"""Filter unknown/invalid notifications from an upstream stdio MCP server.

Some MCP servers in the wild (e.g. ``@21st-dev/magic``) emit JSON-RPC
notifications with methods that are not part of the MCP ``ServerNotification``
union -- for example LSP-style ``window/logMessage``. When such a notification
reaches ``mcp.shared.session.BaseSession``'s receive loop, Pydantic raises a
``ValidationError`` while parsing it. Recent versions of the ``mcp`` SDK catch
that error inline, but older SDKs (and any future regression) let the exception
escape the anyio ``TaskGroup`` wrapping ``stdio_client`` and crash the proxy
process. The launchd/systemd unit then restart-loops the proxy.

This module wraps the stdio read stream and silently drops any notification
whose envelope cannot be validated as a ``ServerNotification``. A warning is
logged with the offending method so operators can identify misbehaving servers.
All other messages (requests, responses, errors, valid notifications) are
forwarded unchanged.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio
from mcp import types
from mcp.shared.message import SessionMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from anyio.streams.memory import (
        MemoryObjectReceiveStream,
        MemoryObjectSendStream,
    )

logger = logging.getLogger(__name__)


def is_known_server_notification(notification: types.JSONRPCNotification) -> bool:
    """Return ``True`` if ``notification`` parses as a known ``ServerNotification``."""
    try:
        types.ServerNotification.model_validate(
            notification.model_dump(by_alias=True, mode="json", exclude_none=True),
        )
    except Exception:  # noqa: BLE001 - any validation failure means "unknown"
        return False
    return True


async def _pump_filtered(
    source: MemoryObjectReceiveStream[SessionMessage | Exception],
    sink: MemoryObjectSendStream[SessionMessage | Exception],
) -> None:
    """Forward messages from ``source`` to ``sink``, dropping unknown notifications."""
    try:
        async with sink:
            async for item in source:
                if (
                    isinstance(item, SessionMessage)
                    and isinstance(item.message.root, types.JSONRPCNotification)
                    and not is_known_server_notification(item.message.root)
                ):
                    logger.warning(
                        "Dropping unknown notification from upstream server "
                        "(method=%r). The upstream server is emitting a "
                        "notification outside the MCP ServerNotification union; "
                        "this would otherwise crash the proxy via an "
                        "unhandled Pydantic ValidationError in the anyio "
                        "TaskGroup wrapping stdio_client.",
                        item.message.root.method,
                    )
                    continue
                await sink.send(item)
    except anyio.ClosedResourceError:  # pragma: no cover - normal shutdown
        return


@asynccontextmanager
async def filter_unknown_notifications(
    upstream_read: MemoryObjectReceiveStream[SessionMessage | Exception],
    upstream_write: MemoryObjectSendStream[SessionMessage],
) -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Wrap stdio streams so unknown server notifications are logged and dropped.

    Usage mirrors ``stdio_client``: the context manager yields a
    ``(read_stream, write_stream)`` pair that can be handed straight to
    :class:`mcp.client.session.ClientSession`. The write stream is passed
    through unchanged; only the read side is filtered.
    """
    downstream_write, downstream_read = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](0)
    async with anyio.create_task_group() as tg:
        tg.start_soon(_pump_filtered, upstream_read, downstream_write)
        try:
            yield downstream_read, upstream_write
        finally:
            await downstream_read.aclose()
            tg.cancel_scope.cancel()


__all__ = ["filter_unknown_notifications", "is_known_server_notification"]
