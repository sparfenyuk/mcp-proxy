"""Tests for the sse server."""
# ruff: noqa: PLR2004

import asyncio
import contextlib
import typing as t
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import FastMCP, Server
from mcp.types import TextContent
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from mcp_proxy.mcp_server import MCPServerSettings, create_single_instance_routes, run_mcp_server


def create_starlette_app(
    mcp_server: Server[t.Any],
    allow_origins: list[str] | None = None,
    *,
    debug: bool = False,
    stateless: bool = False,
) -> Starlette:
    """Create a Starlette application for the MCP server.

    Args:
        mcp_server: The MCP server instance to wrap
        allow_origins: List of allowed CORS origins
        debug: Enable debug mode
        stateless: Whether to use stateless HTTP sessions

    Returns:
        Starlette application instance
    """
    routes, http_manager = create_single_instance_routes(mcp_server, stateless_instance=stateless)

    middleware: list[Middleware] = []
    if allow_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=allow_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> t.AsyncIterator[None]:
        async with http_manager.run():
            yield

    return Starlette(
        debug=debug,
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )


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


# Unit tests for run_mcp_server method


@pytest.fixture
def mock_settings() -> MCPServerSettings:
    """Create mock MCP server settings for testing."""
    return MCPServerSettings(
        bind_host="127.0.0.1",
        port=8080,
        stateless=False,
        allow_origins=["*"],
        log_level="INFO",
    )


@pytest.fixture
def mock_stdio_params() -> StdioServerParameters:
    """Create mock stdio server parameters for testing."""
    return StdioServerParameters(
        command="echo",
        args=["hello"],
        env={"TEST_VAR": "test_value"},
        cwd="/tmp",  # noqa: S108
    )


class AsyncContextManagerMock:  # noqa: D101
    def __init__(self, mock) -> None:  # noqa: ANN001, D107
        self.mock = mock

    async def __aenter__(self):  # noqa: ANN204, D105
        return self.mock

    async def __aexit__(self, *args):  # noqa: ANN002, ANN204, D105
        pass


def setup_async_context_mocks() -> tuple[
    AsyncContextManagerMock,
    AsyncContextManagerMock,
    AsyncMock,
    MagicMock,
    list[MagicMock],
]:
    """Helper function to set up async context manager mocks."""
    # Setup stdio client mock
    mock_streams = (AsyncMock(), AsyncMock())

    # Setup client session mock
    mock_session = AsyncMock()

    # Setup HTTP manager mock
    mock_http_manager = MagicMock()
    mock_http_manager.run.return_value = AsyncContextManagerMock(None)
    mock_routes = [MagicMock()]

    return (
        AsyncContextManagerMock(mock_streams),
        AsyncContextManagerMock(mock_session),
        mock_session,
        mock_http_manager,
        mock_routes,
    )


async def test_run_mcp_server_no_servers_configured(mock_settings: MCPServerSettings) -> None:
    """Test run_mcp_server when no servers are configured."""
    with patch("mcp_proxy.mcp_server.logger") as mock_logger:
        await run_mcp_server(mock_settings, None, {})
        mock_logger.error.assert_called_once_with("No servers configured to run.")


async def test_run_mcp_server_with_default_server(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with a default server configuration."""
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, {})

        # Verify calls
        mock_stdio_client.assert_called_once_with(mock_stdio_params)
        mock_create_proxy.assert_called_once_with(mock_session)
        mock_create_routes.assert_called_once_with(
            mock_proxy,
            stateless_instance=mock_settings.stateless,
        )
        mock_logger.info.assert_any_call(
            "Setting up default server: %s %s",
            mock_stdio_params.command,
            " ".join(mock_stdio_params.args),
        )
        mock_server_instance.serve.assert_called_once()


async def test_run_mcp_server_with_named_servers(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with named servers configuration."""
    named_servers = {
        "server1": mock_stdio_params,
        "server2": StdioServerParameters(
            command="python",
            args=["-m", "mcp_server"],
            env={"PYTHON_PATH": "/usr/bin/python"},
            cwd="/home/user",
        ),
    }

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, None, named_servers)

        # Verify calls
        assert mock_stdio_client.call_count == 2
        assert mock_create_proxy.call_count == 2
        assert mock_create_routes.call_count == 2

        # Check that named servers were logged
        mock_logger.info.assert_any_call(
            "Setting up named server '%s': %s %s",
            "server1",
            mock_stdio_params.command,
            " ".join(mock_stdio_params.args),
        )
        mock_logger.info.assert_any_call(
            "Setting up named server '%s': %s %s",
            "server2",
            "python",
            "-m mcp_server",
        )

        mock_server_instance.serve.assert_called_once()


async def test_run_mcp_server_with_cors_middleware(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server adds CORS middleware when allow_origins is set."""
    settings_with_cors = MCPServerSettings(
        bind_host="0.0.0.0",  # noqa: S104
        port=9090,
        allow_origins=["http://localhost:3000", "https://example.com"],
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("mcp_proxy.mcp_server.Starlette") as mock_starlette,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(settings_with_cors, mock_stdio_params, {})

        # Verify Starlette was called with middleware
        mock_starlette.assert_called_once()
        call_args = mock_starlette.call_args
        middleware = call_args.kwargs["middleware"]

        assert len(middleware) == 1
        assert middleware[0].cls == CORSMiddleware


async def test_run_mcp_server_debug_mode(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with debug mode enabled."""
    debug_settings = MCPServerSettings(
        bind_host="127.0.0.1",
        port=8080,
        log_level="DEBUG",
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("mcp_proxy.mcp_server.Starlette") as mock_starlette,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(debug_settings, mock_stdio_params, {})

        # Verify Starlette was called with debug=True
        mock_starlette.assert_called_once()
        call_args = mock_starlette.call_args
        assert call_args.kwargs["debug"] is True


async def test_run_mcp_server_stateless_mode(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with stateless mode enabled."""
    stateless_settings = MCPServerSettings(
        bind_host="127.0.0.1",
        port=8080,
        stateless=True,
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(stateless_settings, mock_stdio_params, {})

        # Verify create_single_instance_routes was called with stateless_instance=True
        mock_create_routes.assert_called_once_with(
            mock_proxy,
            stateless_instance=True,
        )


async def test_run_mcp_server_uvicorn_config(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server creates correct uvicorn configuration."""
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Config") as mock_uvicorn_config,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_config = MagicMock()
        mock_uvicorn_config.return_value = mock_config

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, {})

        # Verify uvicorn.Config was called with correct parameters
        mock_uvicorn_config.assert_called_once()
        call_args = mock_uvicorn_config.call_args

        assert call_args.kwargs["host"] == mock_settings.bind_host
        assert call_args.kwargs["port"] == mock_settings.port
        assert call_args.kwargs["log_level"] == mock_settings.log_level.lower()


async def test_run_mcp_server_global_status_updates(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server updates global status correctly."""
    from mcp_proxy.mcp_server import _global_status

    # Clear global status before test
    _global_status["server_instances"].clear()

    named_servers = {"test_server": mock_stdio_params}

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, named_servers)

        # Verify global status was updated
        assert "default" in _global_status["server_instances"]
        assert "test_server" in _global_status["server_instances"]
        assert _global_status["server_instances"]["default"] == "configured"
        assert _global_status["server_instances"]["test_server"] == "configured"


async def test_run_mcp_server_sse_url_logging(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server logs correct SSE URLs."""
    named_servers = {"test_server": mock_stdio_params}

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, named_servers)

        # Verify SSE URLs were logged
        expected_default_url = f"http://{mock_settings.bind_host}:{mock_settings.port}/sse"
        expected_named_url = (
            f"http://{mock_settings.bind_host}:{mock_settings.port}/servers/test_server/sse"
        )

        mock_logger.info.assert_any_call("Serving MCP Servers via SSE:")
        mock_logger.info.assert_any_call("  - %s", expected_default_url)
        mock_logger.info.assert_any_call("  - %s", expected_named_url)


async def test_run_mcp_server_exception_handling(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server handles exceptions properly."""
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession"),
    ):
        # Setup mocks to raise an exception
        mock_stdio_client.side_effect = Exception("Connection failed")

        # Should not raise, function should handle exceptions gracefully
        try:
            await run_mcp_server(mock_settings, mock_stdio_params, {})
        except Exception as e:  # noqa: BLE001
            # If an exception is raised, it should be the expected one
            assert "Connection failed" in str(e)  # noqa: PT017


async def test_run_mcp_server_both_default_and_named_servers(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with both default and named servers."""
    named_servers = {"named_server": mock_stdio_params}

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function with both default and named servers
        await run_mcp_server(mock_settings, mock_stdio_params, named_servers)

        # Verify both servers were set up
        assert mock_stdio_client.call_count == 2  # One for default, one for named
        assert mock_create_proxy.call_count == 2
        assert mock_create_routes.call_count == 2

        # Verify logging for both servers
        mock_logger.info.assert_any_call(
            "Setting up default server: %s %s",
            mock_stdio_params.command,
            " ".join(mock_stdio_params.args),
        )
        mock_logger.info.assert_any_call(
            "Setting up named server '%s': %s %s",
            "named_server",
            mock_stdio_params.command,
            " ".join(mock_stdio_params.args),
        )

        mock_server_instance.serve.assert_called_once()
