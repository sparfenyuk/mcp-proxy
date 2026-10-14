"""Expose a remote Streamable HTTP server over local MCP stdio."""

from functools import partial
from typing import Any

import anyio
import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from .httpx_client import custom_httpx_client
from .proxy_server import create_proxy_server

# Only session termination receives this bound. Normal tool calls and
# long-lived GET streams retain the SDK's configured request/read timeouts.
SESSION_TERMINATION_TIMEOUT_SECONDS = 5.0


def _termination_bounded_httpx_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    verify_ssl: bool | str | None = None,
) -> httpx.AsyncClient:
    """Apply a deadline to MCP DELETE, including the SDK's direct-send path.

    SDK 1.30 calls ``AsyncClient.send`` via ``request_within_origin`` when
    terminating a Streamable HTTP session. Wrapping ``client.delete`` alone
    misses that path; intercepting send covers older client.delete calls too.
    """
    client = custom_httpx_client(headers=headers, timeout=timeout, auth=auth, verify_ssl=verify_ssl)
    send = client.send

    async def send_with_bounded_termination(
        request: httpx.Request,
        **send_kwargs: Any,  # noqa: ANN401
    ) -> httpx.Response:
        if request.method.upper() == "DELETE":
            with anyio.fail_after(SESSION_TERMINATION_TIMEOUT_SECONDS):
                return await send(request, **send_kwargs)
        return await send(request, **send_kwargs)

    client.send = send_with_bounded_termination  # type: ignore[method-assign]
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
    async with (
        streamablehttp_client(
            url=url,
            headers=headers,
            auth=auth,
            httpx_client_factory=partial(_termination_bounded_httpx_client, verify_ssl=verify_ssl),
        ) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        app = await create_proxy_server(session)
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
