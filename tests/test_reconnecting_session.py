"""Tests for the reconnecting client session wrapper.

The wrapper is exercised against a scriptable fake upstream for the unit cases, and once
end-to-end through a real ``create_proxy_server`` so that the anyio task ownership rules are
verified against real task groups.
"""

import typing as t
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.server import Server
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_proxy.proxy_server import create_proxy_server
from mcp_proxy.reconnecting_session import (
    SESSION_TERMINATED_ERROR_CODE,
    ReconnectingClientSession,
    SessionFactory,
)

TOOL_INPUT_SCHEMA = {"type": "object", "properties": {"input1": {"type": "string"}}}

OK = "ok"
PROGRESS_ID = "progress-1"

ONE_CONNECT = 1
TWO_CONNECTS = 2
THREE_CONNECTS = 3
CONCURRENT_CALLERS = 5

# A connection that is never closed deadlocks the wrapper's teardown instead of failing an
# assertion, so every test that opens a wrapper runs under a deadline.
LIFECYCLE_TIMEOUT = 5

INITIALIZE_RESULT = types.InitializeResult(
    protocolVersion=types.LATEST_PROTOCOL_VERSION,
    capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
    serverInfo=types.Implementation(name="fake-upstream", version="1.0.0"),
)


def session_lost() -> McpError:
    """Return the error the SDK raises once the remote has forgotten the session."""
    return McpError(
        types.ErrorData(code=SESSION_TERMINATED_ERROR_CODE, message="Session terminated"),
    )


class FakeSession:
    """Stand-in for ``ClientSession`` whose method results are scripted per connection."""

    def __init__(self, upstream: "FakeUpstream", script: dict[str, list[object]]) -> None:
        """Bind the session to its upstream and to the script for this connection."""
        self.upstream = upstream
        self.script = script
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def initialize(self) -> types.InitializeResult:
        """Record a handshake and return a fixed initialization result."""
        self.upstream.handshakes += 1
        return INITIALIZE_RESULT

    def __getattr__(self, name: str) -> Callable[..., Awaitable[object]]:
        """Return a coroutine function serving the next scripted outcome for ``name``."""
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args: object, **kwargs: object) -> object:
            self.calls.append((name, args, kwargs))
            outcomes = self.script.get(name)
            if outcomes:
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
            return OK

        return call


class FakeUpstream:
    """Session factory that records connection lifecycles and scripts each connection."""

    def __init__(
        self,
        scripts: list[dict[str, list[object]]] | None = None,
        connect_errors: dict[int, BaseException] | None = None,
        exit_errors: dict[int, BaseException] | None = None,
    ) -> None:
        """Configure the per-connection scripts and the connections that must fail."""
        self.scripts = scripts or []
        self.connect_errors = connect_errors or {}
        self.exit_errors = exit_errors or {}
        self.connects = 0
        self.exits = 0
        self.handshakes = 0
        self.sessions: list[FakeSession] = []
        self.closed: list[anyio.Event] = []

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[ClientSession]:
        """Open one scripted connection and count its entry and its exit."""
        index = self.connects
        self.connects += 1
        error = self.connect_errors.get(index)
        if error is not None:
            raise error
        script = self.scripts[index] if index < len(self.scripts) else {}
        session = FakeSession(self, script)
        self.sessions.append(session)
        closed = anyio.Event()
        self.closed.append(closed)
        try:
            yield t.cast("ClientSession", session)
        finally:
            self.exits += 1
            closed.set()
            exit_error = self.exit_errors.get(index)
            if exit_error is not None:
                raise exit_error


@asynccontextmanager
async def open_wrapper(connect: SessionFactory) -> AsyncIterator[ReconnectingClientSession]:
    """Open a wrapper under a deadline so a leaked connection fails instead of hanging."""
    with anyio.fail_after(LIFECYCLE_TIMEOUT):
        async with ReconnectingClientSession(connect) as wrapper:
            yield wrapper


async def test_happy_path_uses_a_single_connection() -> None:
    """A call that succeeds must not open a second connection."""
    upstream = FakeUpstream()

    async with open_wrapper(upstream.connect) as wrapper:
        assert await wrapper.list_tools() == OK

    assert upstream.connects == ONE_CONNECT


async def test_session_lost_rebuilds_and_retries() -> None:
    """A session-terminated error must rebuild the connection and retry the call once."""
    upstream = FakeUpstream(scripts=[{"list_tools": [session_lost()]}])

    async with open_wrapper(upstream.connect) as wrapper:
        assert await wrapper.list_tools() == OK

    assert upstream.connects == TWO_CONNECTS
    assert upstream.sessions[0] is not upstream.sessions[1]
    assert [name for name, _, _ in upstream.sessions[1].calls] == ["list_tools"]


async def test_other_mcp_errors_propagate_unchanged() -> None:
    """An ordinary protocol error must not trigger a rebuild."""
    error = McpError(types.ErrorData(code=-32602, message="Invalid params"))
    upstream = FakeUpstream(scripts=[{"list_tools": [error]}])

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError) as exc_info:
            await wrapper.list_tools()

    assert exc_info.value is error
    assert upstream.connects == ONE_CONNECT


async def test_unexpected_errors_propagate_unchanged() -> None:
    """A genuine tool failure must reach the caller instead of being retried."""
    error = ValueError("tool exploded")
    upstream = FakeUpstream(scripts=[{"call_tool": [error]}])

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(ValueError, match="tool exploded"):
            await wrapper.call_tool("tool", {})

    assert upstream.connects == ONE_CONNECT


async def test_two_consecutive_session_losses_propagate() -> None:
    """The wrapper retries once; a second failure must surface rather than spin."""
    upstream = FakeUpstream(
        scripts=[{"list_tools": [session_lost()]}, {"list_tools": [session_lost()]}],
    )

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError, match="Session terminated"):
            await wrapper.list_tools()

    assert upstream.connects == TWO_CONNECTS


async def test_initialize_returns_the_cached_handshake() -> None:
    """``initialize`` must reuse the handshake performed while opening the connection."""
    upstream = FakeUpstream()

    async with open_wrapper(upstream.connect) as wrapper:
        first = await wrapper.initialize()
        second = await wrapper.initialize()

    assert first is INITIALIZE_RESULT
    assert second is INITIALIZE_RESULT
    assert upstream.handshakes == ONE_CONNECT


async def test_concurrent_callers_rebuild_once() -> None:
    """Several handlers hitting the same outage must share one rebuild."""
    upstream = FakeUpstream(scripts=[{"list_tools": [session_lost()] * CONCURRENT_CALLERS}])
    results: list[object] = []

    async with open_wrapper(upstream.connect) as wrapper:

        async def call() -> None:
            results.append(await wrapper.list_tools())

        async with anyio.create_task_group() as task_group:
            for _ in range(CONCURRENT_CALLERS):
                task_group.start_soon(call)

    assert results == [OK] * CONCURRENT_CALLERS
    assert upstream.connects == TWO_CONNECTS


async def test_failed_rebuild_propagates_and_is_not_fatal() -> None:
    """A rebuild that cannot connect must raise, and must not poison later calls."""
    upstream = FakeUpstream(
        scripts=[{"list_tools": [session_lost()]}],
        connect_errors={1: RuntimeError("upstream unreachable")},
    )

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(RuntimeError, match="upstream unreachable"):
            await wrapper.list_tools()
        assert await wrapper.list_tools() == OK

    assert upstream.connects == THREE_CONNECTS


async def test_old_connection_is_closed_on_rebuild() -> None:
    """The connection that lost its session must be torn down, not leaked."""
    upstream = FakeUpstream(scripts=[{"list_tools": [session_lost()]}])

    async with open_wrapper(upstream.connect) as wrapper:
        await wrapper.list_tools()
        await upstream.closed[0].wait()
        assert upstream.exits == ONE_CONNECT

    assert upstream.exits == TWO_CONNECTS


async def test_clean_shutdown_closes_every_connection() -> None:
    """Leaving the wrapper must close as many connections as it opened."""
    upstream = FakeUpstream(scripts=[{"list_tools": [session_lost()]}])

    async with open_wrapper(upstream.connect) as wrapper:
        await wrapper.list_tools()

    assert upstream.exits == upstream.connects


async def test_keyword_arguments_survive_a_rebuild() -> None:
    """Retried calls must be replayed with their positional and keyword arguments."""
    upstream = FakeUpstream(scripts=[{"call_tool": [session_lost()]}])

    async def progress_callback(progress: float, total: float | None, message: str | None) -> None:
        """Ignore progress updates; only its identity matters for this test."""

    async with open_wrapper(upstream.connect) as wrapper:
        assert (
            await wrapper.call_tool(
                "tool",
                {"input1": "value"},
                meta={"progressToken": "token"},
                progress_callback=progress_callback,
            )
            == OK
        )

    name, args, kwargs = upstream.sessions[1].calls[0]
    assert name == "call_tool"
    assert args == ("tool", {"input1": "value"})
    assert kwargs == {"meta": {"progressToken": "token"}, "progress_callback": progress_callback}


async def test_notifications_forward_without_a_result() -> None:
    """Methods that return nothing must be forwarded like any other."""
    upstream = FakeUpstream(scripts=[{"send_progress_notification": [None]}])

    async with open_wrapper(upstream.connect) as wrapper:
        result = await wrapper.send_progress_notification(
            progress_token=PROGRESS_ID,
            progress=1.0,
        )

    assert result is None


async def test_initial_connect_failure_propagates() -> None:
    """A first connection that cannot be opened must fail the wrapper's own startup."""
    upstream = FakeUpstream(connect_errors={0: RuntimeError("upstream unreachable")})

    with pytest.raises(RuntimeError, match="upstream unreachable"):
        async with open_wrapper(upstream.connect):
            pass  # pragma: no cover - the context manager never opens


async def test_methods_before_entering_raise() -> None:
    """Using the wrapper before it is entered must fail with a clear error."""
    wrapper = ReconnectingClientSession(FakeUpstream().connect)

    with pytest.raises(RuntimeError, match="not connected"):
        await wrapper.initialize()
    with pytest.raises(RuntimeError, match="not connected"):
        await wrapper.list_tools()


async def test_teardown_failures_do_not_kill_the_proxy() -> None:
    """A connection that raises while closing must be logged, not propagated."""
    upstream = FakeUpstream(
        scripts=[{"list_tools": [session_lost()]}],
        exit_errors={0: RuntimeError("socket already gone")},
    )

    async with open_wrapper(upstream.connect) as wrapper:
        assert await wrapper.list_tools() == OK
        await upstream.closed[0].wait()

    assert upstream.connects == TWO_CONNECTS


def test_private_attributes_are_not_forwarded() -> None:
    """Dunder and private lookups must fail normally so introspection keeps working."""
    wrapper = ReconnectingClientSession(FakeUpstream().connect)

    with pytest.raises(AttributeError):
        getattr(wrapper, "_not_a_session_method")  # noqa: B009 - the literal is the point


async def test_rebuild_through_a_real_proxy_server() -> None:
    """A rebuild triggered from a request handler must work with real anyio task groups."""
    calls = 0
    server: Server[object] = Server("flaky-server")

    @server.list_tools()  # type: ignore[no-untyped-call,misc]
    async def _() -> list[types.Tool]:
        nonlocal calls
        calls += 1
        if calls == ONE_CONNECT:
            raise session_lost()
        return [
            types.Tool(
                name="tool",
                description="tool-description",
                inputSchema=TOOL_INPUT_SCHEMA,
            ),
        ]

    connects = 0

    @asynccontextmanager
    async def connect() -> AsyncIterator[ClientSession]:
        nonlocal connects
        connects += 1
        async with create_connected_server_and_client_session(server) as session:
            yield session

    async with open_wrapper(connect) as wrapper:
        app = await create_proxy_server(t.cast("ClientSession", wrapper))
        async with create_connected_server_and_client_session(app) as client:
            result = await client.list_tools()

    assert [tool.name for tool in result.tools] == ["tool"]
    assert connects == TWO_CONNECTS
