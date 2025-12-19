"""Tests for the mcp-proxy module.

Tests are running in two modes:
- One where the server is exercised directly though an in memory client, just to
  set a baseline for the expected behavior.
- Another where the server is exercised through a proxy server, which forwards
  the requests to the original server.

The same test code is run on both to ensure parity.
"""

import asyncio
import typing as t
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
import httpx

from mcp_proxy.proxy_server import create_proxy_server

TOOL_INPUT_SCHEMA = {"type": "object", "properties": {"input1": {"type": "string"}}}

SessionContextManager = Callable[[Server[object]], AbstractAsyncContextManager[ClientSession]]

# Direct server connection
in_memory: SessionContextManager = create_connected_server_and_client_session


@pytest.fixture
def tool() -> types.Tool:
    """Provide a default tool definition for tests that do not override it."""
    return types.Tool(
        name="tool",
        description="tool-description",
        inputSchema=TOOL_INPUT_SCHEMA,
    )


@asynccontextmanager
async def proxy(server: Server[object]) -> AsyncGenerator[ClientSession, None]:
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
def server() -> Server[object]:
    """Return a server instance."""
    return Server("test-server")


@pytest.fixture
def server_can_list_prompts(server: Server[object], prompt: types.Prompt) -> Server[object]:
    """Return a server instance with prompts."""

    @server.list_prompts()  # type: ignore[no-untyped-call,misc]
    async def _() -> list[types.Prompt]:
        return [prompt]

    return server


@pytest.fixture
def server_can_get_prompt(
    server_can_list_prompts: Server[object],
    prompt_callback: Callable[[str, dict[str, str] | None], Awaitable[types.GetPromptResult]],
) -> Server[object]:
    """Return a server instance with prompts."""
    server_can_list_prompts.get_prompt()(prompt_callback)  # type: ignore[no-untyped-call]

    return server_can_list_prompts


@pytest.fixture
def server_can_list_tools(server: Server[object], tool: types.Tool) -> Server[object]:
    """Return a server instance with tools."""

    @server.list_tools()  # type: ignore[no-untyped-call,misc]
    async def _() -> list[types.Tool]:
        return [tool]

    return server


@pytest.fixture
def server_can_call_tool(
    server_can_list_tools: Server[object],
    tool_callback: Callable[..., t.Awaitable[t.Iterable[types.Content]]],
) -> Server[object]:
    """Return a server instance with tools."""

    @server_can_list_tools.call_tool()  # type: ignore[misc]
    async def _wrapped_call_tool(
        name: str,
        arguments: dict[str, t.Any] | None,
    ) -> t.Iterable[types.Content]:
        return await tool_callback(name, arguments or {})

    return server_can_list_tools


class _RetryRemoteApp:
    """Stub remote client that fails once then succeeds, honoring retry budget."""

    def __init__(self, first_error: Exception, *, retry_attempts: int = 1):
        self._retry_attempts = retry_attempts
        self.first_error = first_error
        self.call_count = 0
        self.init_count = 0

    async def initialize(self):
        self.init_count += 1
        return types.InitializeResult(
            protocolVersion="2025-06-18",
            serverInfo=types.Implementation(name="stub", version="1.0.0"),
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(listChanged=True),
                logging=None,
                prompts=None,
                resources=None,
            ),
            instructions=None,
        )

    async def list_tools(self):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="tool",
                    description="stub tool",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ],
        )

    async def call_tool(self, name: str, arguments: dict[str, t.Any]):
        self.call_count += 1
        if self.call_count == 1:
            raise self.first_error
        return types.CallToolResult(content=[], isError=False)


class _AlwaysFailRemoteApp:
    """Stub remote client that always fails, to validate retry budgets."""

    def __init__(self, error: Exception, *, retry_attempts: int):
        self._retry_attempts = retry_attempts
        self.error = error
        self.call_count = 0
        self.init_count = 0

    async def initialize(self):
        self.init_count += 1
        return types.InitializeResult(
            protocolVersion="2025-06-18",
            serverInfo=types.Implementation(name="stub", version="1.0.0"),
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(listChanged=True),
                logging=None,
                prompts=None,
                resources=None,
            ),
            instructions=None,
        )

    async def list_tools(self):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="tool",
                    description="stub tool",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ],
        )

    async def call_tool(self, name: str, arguments: dict[str, t.Any]):
        self.call_count += 1
        raise self.error


class _ResultThenSuccessRemoteApp:
    """Stub remote client that returns an error result once, then succeeds."""

    def __init__(self, first_result: types.CallToolResult, *, retry_attempts: int = 1):
        self._retry_attempts = retry_attempts
        self.first_result = first_result
        self.call_count = 0
        self.init_count = 0

    async def initialize(self):
        self.init_count += 1
        return types.InitializeResult(
            protocolVersion="2025-06-18",
            serverInfo=types.Implementation(name="stub", version="1.0.0"),
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(listChanged=True),
                logging=None,
                prompts=None,
                resources=None,
            ),
            instructions=None,
        )

    async def list_tools(self):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="tool",
                    description="stub tool",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ],
        )

    async def call_tool(self, name: str, arguments: dict[str, t.Any]):
        self.call_count += 1
        if self.call_count == 1:
            return self.first_result
        return types.CallToolResult(content=[], isError=False)


class _RetryRemoteResourcesApp:
    """Stub remote client exposing resources, failing once then succeeding."""

    def __init__(self, first_error: Exception, *, retry_attempts: int = 1):
        self._retry_attempts = retry_attempts
        self.first_error = first_error
        self.read_count = 0
        self.init_count = 0

    async def initialize(self):
        self.init_count += 1
        return types.InitializeResult(
            protocolVersion="2025-06-18",
            serverInfo=types.Implementation(name="stub", version="1.0.0"),
            capabilities=types.ServerCapabilities(
                tools=None,
                logging=None,
                prompts=None,
                resources=types.ResourcesCapability(listChanged=True),
            ),
            instructions=None,
        )

    async def list_resources(self):
        return types.ListResourcesResult(resources=[])

    async def list_resource_templates(self):
        return types.ListResourceTemplatesResult(resourceTemplates=[])

    async def read_resource(self, uri: str):
        self.read_count += 1
        if self.read_count == 1:
            raise self.first_error
        return types.ReadResourceResult(contents=[])


class _QueueErrorRemoteApp:
    """Stub remote client that waits on first call; error queue should trigger retry."""

    def __init__(self, error_queue: asyncio.Queue[httpx.HTTPStatusError], *, retry_attempts: int = 1):
        self._retry_attempts = retry_attempts
        self._http_error_queue = error_queue
        self._proxy_call_timeout_s = 5.0
        self.call_count = 0
        self.init_count = 0

    async def initialize(self):
        self.init_count += 1
        return types.InitializeResult(
            protocolVersion="2025-06-18",
            serverInfo=types.Implementation(name="stub", version="1.0.0"),
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(listChanged=True),
                logging=None,
                prompts=None,
                resources=None,
            ),
            instructions=None,
        )

    async def list_tools(self):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="tool",
                    description="stub tool",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ],
        )

    async def call_tool(self, name: str, arguments: dict[str, t.Any]):
        self.call_count += 1
        if self.call_count == 1:
            await asyncio.Event().wait()
        return types.CallToolResult(content=[], isError=False)


class _TimeoutRemoteApp:
    """Stub remote client that times out once then succeeds."""

    def __init__(self, *, retry_attempts: int = 1):
        self._retry_attempts = retry_attempts
        self._proxy_call_timeout_s = 0.01
        self.call_count = 0
        self.init_count = 0

    async def initialize(self):
        self.init_count += 1
        return types.InitializeResult(
            protocolVersion="2025-06-18",
            serverInfo=types.Implementation(name="stub", version="1.0.0"),
            capabilities=types.ServerCapabilities(
                tools=types.ToolsCapability(listChanged=True),
                logging=None,
                prompts=None,
                resources=None,
            ),
            instructions=None,
        )

    async def list_tools(self):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="tool",
                    description="stub tool",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ],
        )

    async def call_tool(self, name: str, arguments: dict[str, t.Any]):
        self.call_count += 1
        if self.call_count == 1:
            await asyncio.sleep(1)
        return types.CallToolResult(content=[], isError=False)


@pytest.mark.asyncio
async def test_proxy_read_resource_retries_on_timeout() -> None:
    """Proxy should retry/re-init non-tool handlers on timeout/network errors."""

    remote = _RetryRemoteResourcesApp(first_error=httpx.ReadTimeout("rt"), retry_attempts=1)
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.read_resource("resource://x")
        assert res.contents == []

    # initialize called once at startup and once during retry
    assert remote.init_count == 2
    assert remote.read_count == 2


@pytest.mark.asyncio
async def test_call_tool_retries_when_http_error_queue_fires() -> None:
    """CallTool should retry when an HTTP error is observed on the send path."""
    error_queue: asyncio.Queue[httpx.HTTPStatusError] = asyncio.Queue()
    remote = _QueueErrorRemoteApp(error_queue, retry_attempts=1)
    app = await create_proxy_server(remote)

    async def _enqueue_error() -> None:
        await asyncio.sleep(0)
        request = httpx.Request("POST", "http://example/mcp")
        response = httpx.Response(404, request=request)
        await error_queue.put(
            httpx.HTTPStatusError("Retryable HTTP status: 404", request=request, response=response),
        )

    async with create_connected_server_and_client_session(app) as session:
        enqueue_task = asyncio.create_task(_enqueue_error())
        result = await session.call_tool("tool", {})
        await enqueue_task

    assert not result.isError
    assert remote.call_count == 2
    assert remote.init_count == 2


@pytest.mark.asyncio
async def test_call_tool_retries_on_call_timeout() -> None:
    """CallTool should retry when the call hangs past the per-call timeout."""
    remote = _TimeoutRemoteApp(retry_attempts=1)
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        result = await session.call_tool("tool", {})

    assert not result.isError
    assert remote.call_count == 2
    assert remote.init_count == 2

@pytest.fixture
def server_can_list_resources(server: Server[object], resource: types.Resource) -> Server[object]:
    """Return a server instance with resources."""

    @server.list_resources()  # type: ignore[no-untyped-call,misc]
    async def _() -> list[types.Resource]:
        return [resource]

    return server


@pytest.fixture
def server_can_list_resource_templates(
    server_can_list_resources: Server[object],
    resource_template: types.ResourceTemplate,
) -> Server[object]:
    """Return a server instance with resources."""

    @server_can_list_resources.list_resource_templates()  # type: ignore[no-untyped-call,misc]
    async def _() -> list[types.ResourceTemplate]:
        return [resource_template]

    return server_can_list_resources


@pytest.fixture
def server_can_subscribe_resource(
    server_can_list_resources: Server[object],
    subscribe_callback: Callable[[AnyUrl], Awaitable[None]],
) -> Server[object]:
    """Return a server instance with resource templates."""
    server_can_list_resources.subscribe_resource()(subscribe_callback)  # type: ignore[no-untyped-call]

    return server_can_list_resources


@pytest.fixture
def server_can_unsubscribe_resource(
    server_can_list_resources: Server[object],
    unsubscribe_callback: Callable[[AnyUrl], Awaitable[None]],
) -> Server[object]:
    """Return a server instance with resource templates."""
    server_can_list_resources.unsubscribe_resource()(unsubscribe_callback)  # type: ignore[no-untyped-call]

    return server_can_list_resources


@pytest.fixture
def server_can_read_resource(
    server_can_list_resources: Server[object],
    resource_callback: Callable[[AnyUrl], Awaitable[str | bytes]],
) -> Server[object]:
    """Return a server instance with resources."""
    server_can_list_resources.read_resource()(resource_callback)  # type: ignore[no-untyped-call]

    return server_can_list_resources


@pytest.fixture
def server_can_set_logging_level(
    server: Server[object],
    logging_level_callback: Callable[[types.LoggingLevel], Awaitable[None]],
) -> Server[object]:
    """Return a server instance with logging capabilities."""
    server.set_logging_level()(logging_level_callback)  # type: ignore[no-untyped-call]

    return server


@pytest.fixture
def server_can_send_progress_notification(
    server: Server[object],
) -> Server[object]:
    """Return a server instance with logging capabilities."""
    return server


@pytest.fixture
def server_can_complete(
    server: Server[object],
    complete_callback: Callable[
        [types.PromptReference | types.ResourceReference, types.CompletionArgument],
        Awaitable[types.Completion | None],
    ],
) -> Server[object]:
    """Return a server instance with logging capabilities."""

    @server.completion()  # type: ignore[no-untyped-call,misc]
    async def _completion(
        reference: types.PromptReference | types.ResourceReference,
        argument: types.CompletionArgument,
        _context: object | None = None,
    ) -> types.Completion | None:
        return await complete_callback(reference, argument)

    return server


@pytest.mark.parametrize("prompt", [types.Prompt(name="prompt1")])
async def test_list_prompts(
    session_generator: SessionContextManager,
    server_can_list_prompts: Server[object],
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
    server_can_list_tools: Server[object],
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
    server_can_set_logging_level: Server[object],
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


@pytest.mark.parametrize("tool_callback", [AsyncMock()])
async def test_call_tool(
    session_generator: SessionContextManager,
    server_can_call_tool: Server[object],
    tool_callback: AsyncMock,
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

        tool_callback.return_value = []
        call_tool_result = await session.call_tool("tool", {})
        assert call_tool_result.content == []
        assert not call_tool_result.isError

        tool_callback.assert_called_once_with("tool", {})
        tool_callback.reset_mock()


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
    server_can_list_resources: Server[object],
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
@pytest.mark.parametrize(
    "resource_template",
    [
        types.ResourceTemplate(
            uriTemplate="scheme://resource-uri/{resource}",
            name="resource-name",
            description="resource-description",
        ),
    ],
)
async def test_list_resource_templates(
    session_generator: SessionContextManager,
    server_can_list_resource_templates: Server[object],
    resource_template: types.ResourceTemplate,
) -> None:
    """Test get_resource."""
    async with session_generator(server_can_list_resource_templates) as session:
        await session.initialize()

        list_resources_result = await session.list_resource_templates()
        assert list_resources_result.resourceTemplates == [resource_template]


@pytest.mark.parametrize("prompt_callback", [AsyncMock()])
@pytest.mark.parametrize("prompt", [types.Prompt(name="prompt1")])
async def test_get_prompt(
    session_generator: SessionContextManager,
    server_can_get_prompt: Server[object],
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
    server_can_read_resource: Server[object],
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
    server_can_subscribe_resource: Server[object],
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
    server_can_unsubscribe_resource: Server[object],
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
    server_can_send_progress_notification: Server[object],
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
    server_can_complete: Server[object],
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


@pytest.mark.parametrize("tool_callback", [AsyncMock()])
async def test_call_tool_with_error(
    session_generator: SessionContextManager,
    server_can_call_tool: Server[object],
    tool_callback: AsyncMock,
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

        tool_callback.side_effect = Exception("Error")

        call_tool_result = await session.call_tool("tool", {})
        assert call_tool_result.isError


@pytest.mark.asyncio
async def test_proxy_call_tool_retries_on_http_status() -> None:
    """Proxy should re-init and replay when call_tool raises HTTPStatusError."""

    err = httpx.HTTPStatusError(
        "status 404",
        request=httpx.Request("POST", "http://example/mcp"),
        response=httpx.Response(404, request=httpx.Request("POST", "http://example/mcp")),
    )
    remote = _RetryRemoteApp(first_error=err)
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        # proxy returns EmptyResult on success path
        assert not res.isError
        assert res.content == []

    # initialize called once at startup and once during retry
    assert remote.init_count == 2
    assert remote.call_count == 2


@pytest.mark.asyncio
async def test_proxy_call_tool_retries_on_session_not_found() -> None:
    """Proxy should re-init and replay when server returns session-not-found error payload."""

    remote = _RetryRemoteApp(first_error=Exception("Session not found (-32001)"))
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        assert not res.isError
        assert res.content == []

    # initialize called once at startup and once during retry
    assert remote.init_count == 2
    assert remote.call_count == 2


@pytest.mark.asyncio
async def test_proxy_call_tool_retries_on_session_not_found_mcp_error() -> None:
    """Proxy should re-init and replay when server returns McpError code -32001."""

    remote = _RetryRemoteApp(
        first_error=McpError(types.ErrorData(code=-32001, message="Session not found")),
    )
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        assert not res.isError
        assert res.content == []

    assert remote.init_count == 2
    assert remote.call_count == 2


@pytest.mark.asyncio
async def test_proxy_call_tool_retries_on_session_terminated_mcp_error() -> None:
    """Proxy should re-init and replay when server returns McpError 'Session terminated'."""

    remote = _RetryRemoteApp(
        first_error=McpError(types.ErrorData(code=32600, message="Session terminated")),
    )
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        assert not res.isError
        assert res.content == []

    assert remote.init_count == 2
    assert remote.call_count == 2


@pytest.mark.asyncio
async def test_proxy_call_tool_retries_on_session_not_found_exception_group() -> None:
    """Proxy should re-init and replay when session-not-found is wrapped."""

    remote = _RetryRemoteApp(
        first_error=ExceptionGroup("wrapped", [Exception("Session not found (-32001)")]),
    )
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        assert not res.isError
        assert res.content == []

    assert remote.init_count == 2
    assert remote.call_count == 2


@pytest.mark.asyncio
async def test_proxy_call_tool_retries_on_http_status_exception_group() -> None:
    """Proxy should re-init and replay when HTTPStatusError is wrapped."""

    err = httpx.HTTPStatusError(
        "status 404",
        request=httpx.Request("POST", "http://example/mcp"),
        response=httpx.Response(404, request=httpx.Request("POST", "http://example/mcp")),
    )
    remote = _RetryRemoteApp(first_error=ExceptionGroup("wrapped", [err]))
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        assert not res.isError
        assert res.content == []

    assert remote.init_count == 2
    assert remote.call_count == 2


@pytest.mark.asyncio
async def test_proxy_call_tool_does_not_retry_when_budget_zero() -> None:
    """Proxy should not replay when retry budget is zero."""

    remote = _RetryRemoteApp(
        first_error=Exception("Session not found (-32001)"),
        retry_attempts=0,
    )
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        assert res.isError

    assert remote.init_count == 1
    assert remote.call_count == 1


@pytest.mark.asyncio
async def test_proxy_call_tool_honors_retry_budget_when_all_attempts_fail() -> None:
    """Proxy should stop after max attempts and not exceed init/call counts."""

    err = httpx.HTTPStatusError(
        "status 404",
        request=httpx.Request("POST", "http://example/mcp"),
        response=httpx.Response(404, request=httpx.Request("POST", "http://example/mcp")),
    )
    remote = _AlwaysFailRemoteApp(error=err, retry_attempts=2)
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        assert res.isError

    # retry_attempts=2 => max_attempts=3 total call attempts (initial + 2 retries)
    assert remote.call_count == 3
    # initialize once at startup + once per retry (2) => 3
    assert remote.init_count == 3


@pytest.mark.asyncio
async def test_proxy_call_tool_retries_on_session_terminated_error_result() -> None:
    """Proxy should re-init and replay when remote returns an error result indicating session termination."""

    remote = _ResultThenSuccessRemoteApp(
        types.CallToolResult(
            content=[types.TextContent(type="text", text="Mcp error: 32600: Session terminated")],
            isError=True,
        ),
        retry_attempts=1,
    )
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        assert not res.isError
        assert res.content == []

    assert remote.call_count == 2
    assert remote.init_count == 2


@pytest.mark.asyncio
async def test_proxy_call_tool_retries_on_session_not_found_error_result() -> None:
    """Proxy should re-init and replay when remote returns an error result indicating session loss."""

    remote = _ResultThenSuccessRemoteApp(
        types.CallToolResult(
            content=[types.TextContent(type="text", text="Session not found (-32001)")],
            isError=True,
        ),
        retry_attempts=1,
    )
    app = await create_proxy_server(remote)

    async with create_connected_server_and_client_session(app) as session:
        res = await session.call_tool("tool", {})
        assert not res.isError
        assert res.content == []

    assert remote.call_count == 2
    assert remote.init_count == 2
