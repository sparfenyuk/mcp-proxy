"""Create a local server that proxies requests to a remote server over HTTP."""

from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from .proxy_server import create_proxy_server


async def run_http_client(url: str, headers: dict[str, Any] | None = None) -> None:
    """Run the HTTP client.

    Args:
        url: The URL to connect to.
        headers: Headers for connecting to MCP server.

    """
    async with streamablehttp_client(url=url, headers=headers) as streams, ClientSession(*streams) as session:
        app = await create_proxy_server(session)
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )