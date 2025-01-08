"""Create a local server that proxies requests to a remote server over SSE."""

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.server.stdio import stdio_server

from .proxy_server import create_proxy_server


async def run_sse_client(url: str, api_access_token: str | None = None) -> None:
    """Run the SSE client.

    Args:
        url: The URL to connect to.
        api_access_token: The API access token to use for authentication.

    """
    headers = {}
    if api_access_token is not None:
        headers["Authorization"] = f"Bearer {api_access_token}"

    async with sse_client(url=url, headers=headers) as streams, ClientSession(*streams) as session:
        app = await create_proxy_server(session)
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
