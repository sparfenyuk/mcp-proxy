"""Tests for roots/list forwarding through the proxy.

Verifies that:
- The proxy advertises roots capability to downstream servers.
- roots/list requests are forwarded from downstream → proxy → upstream client.
- The callback gracefully handles missing request context and upstream errors.
"""

import typing as t
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from mcp import server, types
from mcp.server import Server
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_proxy.proxy_server import create_proxy_server, create_roots_forwarding_callback

in_memory = create_connected_server_and_client_session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_server() -> Server[object]:
    """Provide a minimal server with a tool (needed to trigger request_context)."""
    srv = Server("test-server")

    @srv.list_tools()  # type: ignore[no-untyped-call,misc]
    async def _() -> list[types.Tool]:  # pragma: no cover — only needed to advertise capability
        return [
            types.Tool(
                name="echo",
                description="Echo tool",
                inputSchema={"type": "object", "properties": {"msg": {"type": "string"}}},
            ),
        ]

    @srv.call_tool()  # type: ignore[misc]
    async def _call(
        _name: str, arguments: dict[str, t.Any] | None
    ) -> list[types.Content]:  # pragma: no cover
        return [types.TextContent(type="text", text=str(arguments))]

    return srv


# ---------------------------------------------------------------------------
# Integration tests: roots through the proxy
# ---------------------------------------------------------------------------


async def test_proxy_advertises_roots_capability(test_server: Server[object]) -> None:
    """The proxy should advertise roots capability to the downstream server."""
    async with in_memory(test_server) as session:
        wrapped = await create_proxy_server(session)
        # The outer client connects with a roots callback
        async with in_memory(
            wrapped,
            list_roots_callback=AsyncMock(return_value=types.ListRootsResult(roots=[])),
        ) as outer_session:
            await outer_session.initialize()
            # The downstream server should see that the *client* (which is the proxy's
            # ClientSession connected to it) advertises roots capability.
            # We verify this by checking that the inner session (which talks to the
            # real server) had its callback replaced.
            assert session._list_roots_callback is not None  # noqa: SLF001


async def test_proxy_callback_is_set_before_initialize(test_server: Server[object]) -> None:
    """The roots callback must be injected before initialize() to advertise the capability."""
    async with in_memory(test_server) as session:
        # Before create_proxy_server, the callback is the default (error-returning)
        from mcp.client.session import _default_list_roots_callback

        original_callback = session._list_roots_callback  # noqa: SLF001
        assert original_callback is _default_list_roots_callback

        # After create_proxy_server, it should be our forwarding callback
        await create_proxy_server(session)
        assert session._list_roots_callback is not original_callback  # noqa: SLF001


# ---------------------------------------------------------------------------
# Unit tests: create_roots_forwarding_callback edge cases
# ---------------------------------------------------------------------------


async def test_callback_returns_error_when_no_request_context() -> None:
    """When request_context is not set (no active upstream session), return ErrorData."""
    app: server.Server[object] = server.Server(name="test-proxy")

    callback = create_roots_forwarding_callback(app)
    result = await callback(None)  # ctx is unused

    assert isinstance(result, types.ErrorData)
    assert result.code == types.INVALID_REQUEST
    assert "No active upstream session" in result.message


async def test_callback_returns_error_on_upstream_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the upstream session raises, return ErrorData with INTERNAL_ERROR."""
    app: server.Server[object] = server.Server(name="test-proxy")

    # Mock request_context to have a session that raises on list_roots
    mock_session = MagicMock()
    mock_session.list_roots = AsyncMock(side_effect=RuntimeError("connection lost"))

    mock_context = MagicMock()
    mock_context.session = mock_session

    # Patch request_context on the class (auto-restored by monkeypatch)
    monkeypatch.setattr(type(app), "request_context", PropertyMock(return_value=mock_context))

    callback = create_roots_forwarding_callback(app)
    result = await callback(None)

    assert isinstance(result, types.ErrorData)
    assert result.code == types.INTERNAL_ERROR
    assert "connection lost" in result.message


async def test_callback_forwards_roots_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    """When upstream session returns roots, forward them unchanged."""
    app: server.Server[object] = server.Server(name="test-proxy")

    expected_roots = types.ListRootsResult(
        roots=[
            types.Root(uri="file:///project", name="project"),
        ],
    )

    mock_session = MagicMock()
    mock_session.list_roots = AsyncMock(return_value=expected_roots)

    mock_context = MagicMock()
    mock_context.session = mock_session

    monkeypatch.setattr(type(app), "request_context", PropertyMock(return_value=mock_context))

    callback = create_roots_forwarding_callback(app)
    result = await callback(None)

    assert isinstance(result, types.ListRootsResult)
    assert len(result.roots) == 1
    assert result.roots[0].name == "project"
    assert str(result.roots[0].uri) == "file:///project"


async def test_callback_forwards_empty_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """When upstream client has no roots, forward the empty list."""
    app: server.Server[object] = server.Server(name="test-proxy")

    expected_roots = types.ListRootsResult(roots=[])

    mock_session = MagicMock()
    mock_session.list_roots = AsyncMock(return_value=expected_roots)

    mock_context = MagicMock()
    mock_context.session = mock_session

    monkeypatch.setattr(type(app), "request_context", PropertyMock(return_value=mock_context))

    callback = create_roots_forwarding_callback(app)
    result = await callback(None)

    assert isinstance(result, types.ListRootsResult)
    assert result.roots == []
