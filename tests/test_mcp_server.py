"""Tests for the sse server."""

import asyncio
import contextlib
import typing as t

import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import FastMCP
from mcp.types import TextContent

from mcp_proxy.mcp_server import create_starlette_app


class BackgroundServer(uvicorn.Server):
    """A test server that runs in a background thread."""

    def install_signal_handlers(self) -> None:
        """Do not install signal handlers."""

    @contextlib.asynccontextmanager
    async def run_in_background(self) -> t.AsyncIterator[None]:
        """Run the server in a background thread."""
        task = asyncio.create_task(self.serve())
        try:
            while not self.started:  # noqa: ASYNC110
                await asyncio.sleep(1e-3)
            yield
        finally:
            self.should_exit = self.force_exit = True
            await task

    @property
    def url(self) -> str:
        """Return the url of the started server."""
        hostport = next(
            iter([socket.getsockname() for server in self.servers for socket in server.sockets]),
        )
        return f"http://{hostport[0]}:{hostport[1]}"


def make_background_server(**kwargs) -> BackgroundServer:  # noqa: ANN003
    """Create a BackgroundServer instance with specified parameters."""
    mcp = FastMCP("TestServer")

    @mcp.prompt(name="prompt1")
    async def list_prompts() -> str:
        return "hello world"

    @mcp.tool(name="echo")
    async def call_tool(message: str) -> str:
        return f"Echo: {message}"

    app = create_starlette_app(
        mcp._mcp_server,  # noqa: SLF001
        allow_origins=["*"],
        **kwargs,
    )

    config = uvicorn.Config(app, port=0, log_level="info")
    return BackgroundServer(config)


@pytest.mark.asyncio
async def test_sse_transport() -> None:
    """Test basic glue code for the SSE transport and a fake MCP server."""
    server = make_background_server(debug=True)
    async with server.run_in_background():
        sse_url = f"{server.url}/sse"
        async with sse_client(url=sse_url) as streams, ClientSession(*streams) as session:
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"


@pytest.mark.asyncio
async def test_http_transport() -> None:
    """Test HTTP transport layer functionality."""
    server = make_background_server(debug=True)
    async with server.run_in_background():
        http_url = f"{server.url}/mcp/"
        async with (
            streamablehttp_client(url=http_url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"

            for i in range(3):
                tool_result = await session.call_tool("echo", {"message": f"test_{i}"})
                assert len(tool_result.content) == 1
                assert isinstance(tool_result.content[0], TextContent)
                assert tool_result.content[0].text == f"Echo: test_{i}"


async def test_stateless_http_transport() -> None:
    """Test stateless HTTP transport functionality."""
    server = make_background_server(debug=True, stateless=True)
    async with server.run_in_background():
        http_url = f"{server.url}/mcp/"
        async with (
            streamablehttp_client(url=http_url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"

            for i in range(3):
                tool_result = await session.call_tool("echo", {"message": f"test_{i}"})
                assert len(tool_result.content) == 1
                assert isinstance(tool_result.content[0], TextContent)
                assert tool_result.content[0].text == f"Echo: test_{i}"
