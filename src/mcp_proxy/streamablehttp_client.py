"""Create a local server that proxies requests to a remote server over SSE."""

from functools import partial
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from .httpx_client import custom_httpx_client
from .proxy_server import create_proxy_server


async def run_streamablehttp_client(
    url: str,
    headers: dict[str, Any] | None = None,
    verify_ssl: bool | str | None = None,
) -> None:
    """Run the SSE client.

    Args:
        url: The URL to connect to.
        headers: Headers for connecting to MCP server.
        verify_ssl: Control SSL verification. Use False to disable
            or a path to a certificate bundle.
    """
    async with (
        streamablehttp_client(
            url=url,
            headers=headers,
            httpx_client_factory=partial(custom_httpx_client, verify_ssl=verify_ssl),
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
