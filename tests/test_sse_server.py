"""Tests for the sse server."""

import asyncio
import contextlib

import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.server import Server

from mcp_proxy.sse_server import create_starlette_app


class BackgroundServer(uvicorn.Server):
    """A test server that runs in a background thread."""

    def install_signal_handlers(self) -> None:
        """Do not install signal handlers."""

    @contextlib.asynccontextmanager
    async def run_in_background(self) -> None:
        """Run the server in a background thread."""
        task = asyncio.create_task(self.serve())
        try:
            while not self.started:  # noqa: ASYNC110
                await asyncio.sleep(1e-3)
            yield
        finally:
            task.cancel()
            self.shutdown()

    @property
    def url(self) -> str:
        """Return the url of the started server."""
        hostport = next(
            iter([socket.getsockname() for server in self.servers for socket in server.sockets]),
        )
        return f"http://{hostport[0]}:{hostport[1]}"


async def test_create_starlette_app() -> None:
    """Test basic glue code for the SSE transport and a fake MCP server."""
    server = Server("prompt-server")

    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return [types.Prompt(name="prompt1")]

    app = create_starlette_app(server)

    config = uvicorn.Config(app, port=0, log_level="info")
    server = BackgroundServer(config)
    async with server.run_in_background():
        mcp_url = f"{server.url}/sse"
        async with sse_client(url=mcp_url) as streams, ClientSession(*streams) as session:
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"
