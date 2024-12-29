"""Tests for the mcp-proxy module.

Tests are running in two modes:
- One where the server is exercised directly though an in memory client, just to
  set a baseline for the expected behavior.
- Another where the server is exercised through a proxy server, which forwards
  the requests to the original server.

The same test code is run on both to ensure parity.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.server import Server
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

from mcp_proxy import create_proxy_server

TOOL_INPUT_SCHEMA = {"type": "object", "properties": {"input1": {"type": "string"}}}

SessionContextManager = Callable[[Server], AbstractAsyncContextManager[ClientSession]]

# Direct server connection
in_memory: SessionContextManager = create_connected_server_and_client_session


@asynccontextmanager
async def proxy(server: Server) -> AsyncGenerator[ClientSession, None]:
    """Create a connection to the server through the proxy server."""
    async with in_memory(server) as session:
        wrapped_server = await create_proxy_server(session)
        async with in_memory(wrapped_server) as wrapped_session:
            yield wrapped_session


@pytest.fixture(params=["server", "proxy"])
def session_generator(request: pytest.FixtureRequest) -> SessionContextManager:
    """Fixture that returns a client creation strategy either direct or using the proxy."""
    if request.param == "server":
        return in_memory
    return proxy


@pytest.fixture
def server() -> Server:
    """Return a server instance."""
    return Server("test-server")


@pytest.fixture
def server_can_list_prompts(server: Server, prompt: types.Prompt) -> Server:
    """Return a server instance with prompts."""

    @server.list_prompts()
    async def _() -> list[types.Prompt]:
        return [prompt]

    return server


@pytest.fixture
def server_can_get_prompt(
    server_can_list_prompts: Server,
    prompt_callback: Callable[[str, dict[str, str] | None], Awaitable[types.GetPromptResult]],
) -> Server:
    """Return a server instance with prompts."""
    server_can_list_prompts.get_prompt()(prompt_callback)

    return server_can_list_prompts


@pytest.fixture
def server_can_list_tools(server: Server, tool: types.Tool) -> Server:
    """Return a server instance with tools."""

    @server.list_tools()
    async def _() -> list[types.Tool]:
        return [tool]

    return server


@pytest.fixture
def server_can_call_tool(server_can_list_tools: Server, tool: Callable[..., ...]) -> Server:
    """Return a server instance with tools."""
    server_can_list_tools.call_tool()(tool)

    return server_can_list_tools


@pytest.fixture
def server_can_list_resources(server: Server, resource: types.Resource) -> Server:
    """Return a server instance with resources."""

    @server.list_resources()
    async def _() -> list[types.Resource]:
        return [resource]

    return server


@pytest.fixture
def server_can_subscribe_resource(
    server_can_list_resources: Server,
    subscribe_callback: Callable[[AnyUrl], Awaitable[None]],
) -> Server:
    """Return a server instance with resource templates."""
    server_can_list_resources.subscribe_resource()(subscribe_callback)

    return server_can_list_resources


@pytest.fixture
def server_can_unsubscribe_resource(
    server_can_list_resources: Server,
    unsubscribe_callback: Callable[[AnyUrl], Awaitable[None]],
) -> Server:
    """Return a server instance with resource templates."""
    server_can_list_resources.unsubscribe_resource()(unsubscribe_callback)

    return server_can_list_resources


@pytest.fixture
def server_can_read_resource(
    server_can_list_resources: Server,
    resource_callback: Callable[[AnyUrl], Awaitable[str | bytes]],
) -> Server:
    """Return a server instance with resources."""
    server_can_list_resources.read_resource()(resource_callback)

    return server_can_list_resources


@pytest.fixture
def server_can_set_logging_level(
    server: Server,
    logging_level_callback: Callable[[types.LoggingLevel], Awaitable[None]],
) -> Server:
    """Return a server instance with logging capabilities."""
    server.set_logging_level()(logging_level_callback)

    return server


@pytest.fixture
def server_can_send_progress_notification(
    server: Server,
) -> Server:
    """Return a server instance with logging capabilities."""
    return server


@pytest.fixture
def server_can_complete(
    server: Server,
    complete_callback: Callable[
        [types.PromptReference | types.ResourceReference, types.CompletionArgument],
        Awaitable[types.Completion | None],
    ],
) -> Server:
    """Return a server instance with logging capabilities."""
    server.completion()(complete_callback)
    return server


@pytest.mark.parametrize("prompt", [types.Prompt(name="prompt1")])
async def test_list_prompts(
    session_generator: SessionContextManager,
    server_can_list_prompts: Server,
    prompt: types.Prompt,
) -> None:
    """Test list_prompts."""
    async with session_generator(server_can_list_prompts) as session:
        result = await session.initialize()
        assert result.capabilities
        assert result.capabilities.prompts
        assert not result.capabilities.tools
        assert not result.capabilities.resources
        assert not result.capabilities.logging

        list_prompts_result = await session.list_prompts()
        assert list_prompts_result.prompts == [prompt]

        with pytest.raises(McpError, match="Method not found"):
            await session.list_tools()


@pytest.mark.parametrize(
    "tool",
    [
        types.Tool(
            name="tool-name",
            description="tool-description",
            inputSchema=TOOL_INPUT_SCHEMA,
        ),
    ],
)
async def test_list_tools(
    session_generator: SessionContextManager,
    server_can_list_tools: Server,
    tool: types.Tool,
) -> None:
    """Test list_tools."""
    async with session_generator(server_can_list_tools) as session:
        result = await session.initialize()
        assert result.capabilities
        assert result.capabilities.tools
        assert not result.capabilities.prompts
        assert not result.capabilities.resources
        assert not result.capabilities.logging

        list_tools_result = await session.list_tools()
        assert list_tools_result.tools == [tool]

        with pytest.raises(McpError, match="Method not found"):
            await session.list_prompts()


@pytest.mark.parametrize("logging_level_callback", [AsyncMock()])
@pytest.mark.parametrize(
    "log_level",
    ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"],
)
async def test_set_logging_error(
    session_generator: SessionContextManager,
    server_can_set_logging_level: Server,
    logging_level_callback: AsyncMock,
    log_level: types.LoggingLevel,
) -> None:
    """Test set_logging_level."""
    async with session_generator(server_can_set_logging_level) as session:
        result = await session.initialize()
        assert result.capabilities
        assert result.capabilities.logging
        assert not result.capabilities.prompts
        assert not result.capabilities.resources
        assert not result.capabilities.tools

        logging_level_callback.return_value = None
        await session.set_logging_level(log_level)
        logging_level_callback.assert_called_once_with(log_level)
        logging_level_callback.reset_mock()  # Reset the mock for the next test case


@pytest.mark.parametrize("tool", [AsyncMock()])
async def test_call_tool(
    session_generator: SessionContextManager,
    server_can_call_tool: Server,
    tool: AsyncMock,
) -> None:
    """Test call_tool."""
    async with session_generator(server_can_call_tool) as session:
        result = await session.initialize()
        assert result.capabilities
        assert result.capabilities
        assert result.capabilities.tools
        assert not result.capabilities.prompts
        assert not result.capabilities.resources
        assert not result.capabilities.logging

        tool.return_value = []
        call_tool_result = await session.call_tool("tool", {})
        assert call_tool_result.content == []
        assert not call_tool_result.isError

        tool.assert_called_once_with("tool", {})
        tool.reset_mock()


@pytest.mark.parametrize(
    "resource",
    [
        types.Resource(
            uri=AnyUrl("scheme://resource-uri"),
            name="resource-name",
            description="resource-description",
        ),
    ],
)
async def test_list_resources(
    session_generator: SessionContextManager,
    server_can_list_resources: Server,
    resource: types.Resource,
) -> None:
    """Test get_resource."""
    async with session_generator(server_can_list_resources) as session:
        result = await session.initialize()
        assert result.capabilities
        assert result.capabilities.resources
        assert not result.capabilities.prompts
        assert not result.capabilities.tools
        assert not result.capabilities.logging

        list_resources_result = await session.list_resources()
        assert list_resources_result.resources == [resource]


@pytest.mark.parametrize("prompt_callback", [AsyncMock()])
@pytest.mark.parametrize("prompt", [types.Prompt(name="prompt1")])
async def test_get_prompt(
    session_generator: SessionContextManager,
    server_can_get_prompt: Server,
    prompt_callback: AsyncMock,
) -> None:
    """Test get_prompt."""
    async with session_generator(server_can_get_prompt) as session:
        await session.initialize()

        prompt_callback.return_value = types.GetPromptResult(messages=[])

        await session.get_prompt("prompt", {})
        prompt_callback.assert_called_once_with("prompt", {})
        prompt_callback.reset_mock()


@pytest.mark.parametrize("resource_callback", [AsyncMock()])
@pytest.mark.parametrize(
    "resource",
    [
        types.Resource(
            uri=AnyUrl("scheme://resource-uri"),
            name="resource-name",
            description="resource-description",
        ),
    ],
)
async def test_read_resource(
    session_generator: SessionContextManager,
    server_can_read_resource: Server,
    resource_callback: AsyncMock,
    resource: types.Resource,
) -> None:
    """Test read_resource."""
    async with session_generator(server_can_read_resource) as session:
        await session.initialize()

        resource_callback.return_value = "resource-content"
        await session.read_resource(resource.uri)
        resource_callback.assert_called_once_with(resource.uri)
        resource_callback.reset_mock()


@pytest.mark.parametrize("subscribe_callback", [AsyncMock()])
@pytest.mark.parametrize(
    "resource",
    [
        types.Resource(
            uri=AnyUrl("scheme://resource-uri"),
            name="resource-name",
            description="resource-description",
        ),
    ],
)
async def test_subscribe_resource(
    session_generator: SessionContextManager,
    server_can_subscribe_resource: Server,
    subscribe_callback: AsyncMock,
    resource: types.Resource,
) -> None:
    """Test subscribe_resource."""
    async with session_generator(server_can_subscribe_resource) as session:
        await session.initialize()

        subscribe_callback.return_value = None
        await session.subscribe_resource(resource.uri)
        subscribe_callback.assert_called_once_with(resource.uri)
        subscribe_callback.reset_mock()


@pytest.mark.parametrize("unsubscribe_callback", [AsyncMock()])
@pytest.mark.parametrize(
    "resource",
    [
        types.Resource(
            uri=AnyUrl("scheme://resource-uri"),
            name="resource-name",
            description="resource-description",
        ),
    ],
)
async def test_unsubscribe_resource(
    session_generator: SessionContextManager,
    server_can_unsubscribe_resource: Server,
    unsubscribe_callback: AsyncMock,
    resource: types.Resource,
) -> None:
    """Test subscribe_resource."""
    async with session_generator(server_can_unsubscribe_resource) as session:
        await session.initialize()

        unsubscribe_callback.return_value = None
        await session.unsubscribe_resource(resource.uri)
        unsubscribe_callback.assert_called_once_with(resource.uri)
        unsubscribe_callback.reset_mock()


async def test_send_progress_notification(
    session_generator: SessionContextManager,
    server_can_send_progress_notification: Server,
) -> None:
    """Test send_progress_notification."""
    async with session_generator(server_can_send_progress_notification) as session:
        await session.initialize()

        await session.send_progress_notification(
            progress_token=1,
            progress=0.5,
            total=1,
        )


@pytest.mark.parametrize("complete_callback", [AsyncMock()])
async def test_complete(
    session_generator: SessionContextManager,
    server_can_complete: Server,
    complete_callback: AsyncMock,
) -> None:
    """Test complete."""
    async with session_generator(server_can_complete) as session:
        await session.initialize()

        complete_callback.return_value = None
        result = await session.complete(
            types.PromptReference(type="ref/prompt", name="name"),
            argument={"name": "name", "value": "value"},
        )

        assert result.completion.values == []

        complete_callback.assert_called_with(
            types.PromptReference(type="ref/prompt", name="name"),
            types.CompletionArgument(name="name", value="value"),
        )
        complete_callback.reset_mock()


@pytest.mark.parametrize("tool", [AsyncMock()])
async def test_call_tool_with_error(
    session_generator: SessionContextManager,
    server_can_call_tool: Server,
    tool: AsyncMock,
) -> None:
    """Test call_tool."""
    async with session_generator(server_can_call_tool) as session:
        result = await session.initialize()
        assert result.capabilities
        assert result.capabilities
        assert result.capabilities.tools
        assert not result.capabilities.prompts
        assert not result.capabilities.resources
        assert not result.capabilities.logging

        tool.side_effect = Exception("Error")

        call_tool_result = await session.call_tool("tool", {})
        assert call_tool_result.isError
