"""Tests for the sse server."""

import asyncio
from collections.abc import Generator
from contextlib import asynccontextmanager

import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.server import Server
from starlette.applications import Starlette

from mcp_proxy.sse_server import create_starlette_app


@asynccontextmanager
async def start_http_server(app: Starlette) -> Generator[str]:
    """Start an http server in the background and return a url to connect to it.

    This sets port=0 to pick a free available port to avoid collisions. We don't
    have an explicit way to be notified when the server is ready so poll until
    we can grab the port number.
    """
    config = uvicorn.Config(app, port=0, log_level="info")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    hostport = next(
        iter([socket.getsockname() for server in server.servers for socket in server.sockets]),
    )
    yield f"http://{hostport[0]}:{hostport[1]}"
    task.cancel()
    server.shutdown()


async def test_create_starlette_app() -> None:
    """Test basic glue code for the SSE transport and a fake MCP server."""
    server = Server("prompt-server")

    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return [types.Prompt(name="prompt1")]

    app = create_starlette_app(server)

    async with start_http_server(app) as url:
        mcp_url = f"{url}/sse"
        async with sse_client(url=mcp_url) as streams, ClientSession(*streams) as session:
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"
