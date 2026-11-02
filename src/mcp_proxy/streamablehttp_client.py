# Copyright (c) 2024 Sergey Parfenyuk

"""Expose a remote Streamable HTTP server over local MCP stdio."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, cast

import anyio
import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .httpx_client import custom_httpx_client
from .proxy_server import create_proxy_server
from .reconnecting_session import ReconnectingClientSession, SessionObserver
from .stdio import stdio_client_lifespan

logger = logging.getLogger(__name__)

# Only session termination receives this bound. Normal tool calls and
# long-lived GET streams retain the SDK's configured request/read timeouts.
SESSION_TERMINATION_TIMEOUT_SECONDS = 5.0
_SESSION_TERMINATION_DEADLINE_EXTENSION = "mcp_proxy.session_termination_deadline"


class _SessionTerminationAsyncClient(httpx.AsyncClient):
    """Bound the reverse-client session DELETE through response-body reading."""

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        if request.method.upper() != "DELETE":
            return await super().send(request, **kwargs)

        deadline = request.extensions.get(_SESSION_TERMINATION_DEADLINE_EXTENSION)
        if deadline is None:
            deadline = anyio.current_time() + SESSION_TERMINATION_TIMEOUT_SECONDS
            request.extensions[_SESSION_TERMINATION_DEADLINE_EXTENSION] = deadline
        response: httpx.Response | None = None
        try:
            with anyio.fail_after(max(0.0, deadline - anyio.current_time())):
                response = await super().send(request, **kwargs)
                await response.aread()
        except BaseException:
            if response is not None:
                cleanup_grace_seconds = 1
                try:
                    with anyio.move_on_after(
                        cleanup_grace_seconds,
                        shield=True,
                    ) as cleanup_scope:
                        await response.aclose()
                # Cleanup errors must not replace the operation error.
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Response cleanup failed after reverse-client DELETE cancellation"
                    )
                else:
                    if cleanup_scope.cancel_called:
                        logger.warning("Response cleanup exceeded the reverse-client DELETE grace")
            raise
        return response


def _termination_bounded_httpx_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    verify_ssl: bool | str | None = None,
) -> httpx.AsyncClient:
    """Use the shared client configuration with reverse-only DELETE behavior."""
    return custom_httpx_client(
        headers=headers,
        timeout=timeout,
        auth=auth,
        verify_ssl=verify_ssl,
        client_class=_SessionTerminationAsyncClient,
    )


def _session_httpx_client_factory(
    observer: SessionObserver,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    verify_ssl: bool | str | None = None,
) -> httpx.AsyncClient:
    """Add one generation observer while retaining the shared client setup."""
    client = _termination_bounded_httpx_client(
        headers=headers,
        timeout=timeout,
        auth=auth,
        verify_ssl=verify_ssl,
    )
    client.event_hooks.setdefault("response", []).append(observer.observe)
    return client


async def run_streamablehttp_client(
    url: str,
    headers: dict[str, Any] | None = None,
    auth: httpx.Auth | None = None,
    verify_ssl: bool | str | None = None,
) -> None:
    """Run the StreamableHTTP client.

    Args:
        url: The URL to connect to.
        headers: Headers for connecting to MCP server.
        auth: Optional authentication for the HTTP client.
        verify_ssl: Control SSL verification. Use False to disable
            or a path to a certificate bundle.
    """

    @asynccontextmanager
    async def connect(observer: SessionObserver) -> AsyncIterator[ClientSession]:
        async with (
            streamablehttp_client(
                url=url,
                headers=headers,
                auth=auth,
                httpx_client_factory=partial(
                    _session_httpx_client_factory,
                    observer,
                    verify_ssl=verify_ssl,
                ),
            ) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            yield session

    async with (
        stdio_client_lifespan() as (read_stream, write_stream),
        ReconnectingClientSession(connect) as session,
    ):
        app = await create_proxy_server(cast("ClientSession", session))
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )
