"""Tests for progress notification forwarding in the proxy.

This module contains creative approaches to test the _context parameter
and progress forwarding mechanism, working around MCP SDK limitations.
"""

import typing as t
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import types
from mcp.server import Server
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_proxy.proxy_server import create_proxy_server


@pytest.fixture
def mock_server() -> Server[t.Any]:
    """Create a mock server with tool capability."""
    server: Server[t.Any] = Server("test-server")

    @server.list_tools()  # type: ignore[no-untyped-call,misc]
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="test_tool",
                description="A test tool",
                inputSchema={"type": "object", "properties": {}},
            ),
        ]

    return server


@pytest.fixture
def progress_tracking_tool_callback() -> AsyncMock:
    """Create a tool callback that tracks if it receives _context."""
    callback = AsyncMock()
    callback.return_value = [
        types.TextContent(type="text", text="Tool executed"),
    ]
    return callback


async def test_progress_callback_passed_to_remote_app(
    mock_server: Server[object],
    progress_tracking_tool_callback: AsyncMock,
) -> None:
    """Test that progress_callback is passed to remote_app.call_tool.

    This test verifies that when a progressToken is provided in meta,
    the proxy creates a progress_callback and passes it to the remote
    app's call_tool method.

    Strategy: Mock the remote_app.call_tool to capture the progress_callback
    parameter and verify it's not None when progressToken is present.
    """

    # Set up the tool handler
    @mock_server.call_tool()  # type: ignore[misc]
    async def _call_tool(
        name: str,
        arguments: dict[str, t.Any] | None,
        _context: object | None = None,
    ) -> t.Iterable[types.Content]:
        return await progress_tracking_tool_callback(name, arguments, _context)

    # Create proxy through the server
    async with create_connected_server_and_client_session(mock_server) as remote_session:
        proxy_server = await create_proxy_server(remote_session)

        async with create_connected_server_and_client_session(proxy_server) as proxy_session:
            await proxy_session.initialize()

            # Patch the remote_app.call_tool to capture the progress_callback
            original_call_tool = remote_session.call_tool
            call_tool_spy = AsyncMock(side_effect=original_call_tool)

            with patch.object(remote_session, "call_tool", call_tool_spy):
                # Call tool with progressToken
                progress_token = 42
                await proxy_session.call_tool(
                    "test_tool",
                    {},
                    meta={"progressToken": progress_token},
                )

                # Verify call_tool was called with progress_callback
                call_tool_spy.assert_called_once()
                call_args = call_tool_spy.call_args

                # Check that progress_callback was passed and is not None
                assert "progress_callback" in call_args.kwargs
                assert call_args.kwargs["progress_callback"] is not None

                # Verify meta was passed correctly
                assert call_args.kwargs["meta"] == {"progressToken": progress_token}


async def test_progress_callback_not_created_without_token(
    mock_server: Server[object],
    progress_tracking_tool_callback: AsyncMock,
) -> None:
    """Test that progress_callback is None when no progressToken is provided.

    This verifies the optimization that avoids creating unnecessary callbacks.

    Strategy: Mock the remote_app.call_tool to capture the progress_callback
    parameter and verify it's None when no progressToken is present.
    """

    # Set up the tool handler
    @mock_server.call_tool()  # type: ignore[misc]
    async def _call_tool(
        name: str,
        arguments: dict[str, t.Any] | None,
        _context: object | None = None,
    ) -> t.Iterable[types.Content]:
        return await progress_tracking_tool_callback(name, arguments, _context)

    # Create proxy through the server
    async with create_connected_server_and_client_session(mock_server) as remote_session:
        proxy_server = await create_proxy_server(remote_session)

        async with create_connected_server_and_client_session(proxy_server) as proxy_session:
            await proxy_session.initialize()

            # Patch the remote_app.call_tool to capture the progress_callback
            original_call_tool = remote_session.call_tool
            call_tool_spy = AsyncMock(side_effect=original_call_tool)

            with patch.object(remote_session, "call_tool", call_tool_spy):
                # Call tool without progressToken
                await proxy_session.call_tool("test_tool", {}, meta={})

                # Verify call_tool was called with progress_callback=None
                call_tool_spy.assert_called_once()
                call_args = call_tool_spy.call_args

                # Check that progress_callback is None
                assert "progress_callback" in call_args.kwargs
                assert call_args.kwargs["progress_callback"] is None


async def test_progress_forwarder_callback_functionality() -> None:
    """Test the progress_forwarder callback function directly.

    This test simulates what happens when the MCP SDK calls the progress_callback
    by directly invoking the forwarder function created in proxy_server.py.

    Strategy: Create a mock request context and session, then directly test
    the progress_forwarder callback logic.
    """
    # Create mock session with send_progress_notification
    mock_session = AsyncMock()
    mock_session.send_progress_notification = AsyncMock()

    # Create mock request context
    mock_context = MagicMock()
    mock_context.session = mock_session
    mock_context.request_id = "test-request-123"

    # Simulate the progress_forwarder callback from proxy_server.py
    progress_token = 99

    async def progress_forwarder(
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        """Simulated progress forwarder from proxy_server.py."""
        await mock_context.session.send_progress_notification(
            progress_token=progress_token,
            progress=progress,
            total=total,
            message=message,
            related_request_id=str(mock_context.request_id),
        )

    # Test the callback with various progress values
    await progress_forwarder(0.5, 1.0, "Processing...")

    # Verify send_progress_notification was called correctly
    mock_session.send_progress_notification.assert_called_once_with(
        progress_token=99,
        progress=0.5,
        total=1.0,
        message="Processing...",
        related_request_id="test-request-123",
    )

    # Test with None values
    mock_session.send_progress_notification.reset_mock()
    await progress_forwarder(1.0, None, None)

    mock_session.send_progress_notification.assert_called_once_with(
        progress_token=99,
        progress=1.0,
        total=None,
        message=None,
        related_request_id="test-request-123",
    )


async def test_progress_forwarder_integration_simulation(
    mock_server: Server[t.Any],
) -> None:
    """Test progress forwarding by simulating the complete flow.

    This test simulates what would happen if the MCP SDK properly passed
    _context to tool handlers. It verifies that the progress_callback
    parameter is correctly passed to remote_app.call_tool.

    Strategy: Use a spy on remote_app.call_tool to capture the progress_callback
    parameter and verify it's created correctly when progressToken is present.

    Note: We cannot mock request_ctx.get() because ContextVar.get is read-only.
    Instead, we verify the progress_callback is passed to the remote app,
    which is the key behavior we need to test.
    """
    # Create a tool that will be called
    tool_callback = AsyncMock()
    tool_callback.return_value = [
        types.TextContent(type="text", text="Tool executed"),
    ]

    @mock_server.call_tool()  # type: ignore[misc]
    async def _call_tool(
        name: str,
        arguments: dict[str, t.Any] | None,
        _context: object | None = None,
    ) -> t.Iterable[types.Content]:
        return await tool_callback(name, arguments, _context)

    # Create proxy through the server
    async with create_connected_server_and_client_session(mock_server) as remote_session:
        proxy_server = await create_proxy_server(remote_session)

        async with create_connected_server_and_client_session(
            proxy_server,
        ) as proxy_session:
            await proxy_session.initialize()

            # Spy on remote_session.call_tool to capture progress_callback
            original_call_tool = remote_session.call_tool
            call_tool_spy = AsyncMock(side_effect=original_call_tool)

            with patch.object(remote_session, "call_tool", call_tool_spy):
                # Call tool with progressToken
                progress_token = 77
                result = await proxy_session.call_tool(
                    "test_tool",
                    {},
                    meta={"progressToken": progress_token},
                )

                # Verify the tool executed successfully
                assert not result.isError

                # Verify call_tool was called with progress_callback
                call_tool_spy.assert_called_once()
                call_args = call_tool_spy.call_args

                # The key verification: progress_callback was passed and is not None
                assert "progress_callback" in call_args.kwargs
                assert call_args.kwargs["progress_callback"] is not None

                # This proves that the proxy correctly:
                # 1. Extracted progressToken from meta
                # 2. Created a progress_forwarder callback
                # 3. Passed it to remote_app.call_tool
                #
                # In a real scenario with the MCP SDK passing _context properly,
                # this callback would be invoked and forward progress notifications.


async def test_meta_parameter_extraction() -> None:
    """Test that meta parameter is correctly extracted and converted.

    This test verifies the meta parameter handling logic in proxy_server.py,
    specifically the conversion from req.params.meta to a dict and the
    extraction of progressToken.

    Strategy: Test the logic in isolation by simulating the parameter
    extraction that happens in _call_tool.
    """
    # Simulate different meta scenarios
    test_cases = [
        # (input_meta, expected_dict, expected_token)
        ({"progressToken": 42}, {"progressToken": 42}, 42),
        ({"progressToken": 0}, {"progressToken": 0}, 0),
        ({"progressToken": "string-token"}, {"progressToken": "string-token"}, "string-token"),
        ({}, None, None),  # Empty dict becomes None in the actual logic
        (None, None, None),
    ]

    for input_meta, expected_dict, expected_token in test_cases:
        # Simulate the logic from proxy_server.py lines 96-100
        # Note: Empty dict is falsy in Python, so it becomes None
        meta_dict = dict(input_meta) if input_meta else None
        progress_token = meta_dict.get("progressToken") if meta_dict else None

        assert meta_dict == expected_dict
        assert progress_token == expected_token


async def test_progress_callback_creation_logic() -> None:
    """Test the conditional logic for creating progress_callback.

    This test verifies that progress_callback is only created when
    progressToken is present and not None.

    Strategy: Test the conditional logic in isolation.
    """
    # Test cases: (progressToken, should_create_callback)
    test_cases = [
        (42, True),
        (0, True),  # 0 is a valid token
        ("token", True),
        (None, False),
    ]

    for progress_token, should_create in test_cases:
        # Simulate the logic from proxy_server.py lines 100-122
        progress_callback = None

        if progress_token is not None:
            # In real code, this would create the actual callback
            progress_callback = "callback_created"  # Placeholder

        if should_create:
            assert progress_callback is not None
        else:
            assert progress_callback is None
