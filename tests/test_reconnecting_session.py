# Copyright (c) 2024 Sergey Parfenyuk

"""Tests for the reconnecting client session wrapper.

The wrapper is exercised against a scriptable fake upstream for the unit cases, and once
end-to-end through a real ``create_proxy_server`` so that the anyio task ownership rules are
verified against real task groups.
"""

import asyncio
import json
import logging
import os
import sys
import typing as t
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import anyio
import anyio.lowlevel
import httpx
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError

import mcp_proxy.reconnecting_session as reconnecting_session_module
from mcp_proxy.reconnecting_session import (
    ReconnectingClientSession,
    SessionFactory,
    SessionObserver,
)
from mcp_proxy.streamablehttp_client import _SessionTerminationAsyncClient

_RECOVERY_SOURCE_FILE = Path(reconnecting_session_module.__file__).resolve()
_RECOVERY_SOURCE_ROOT = _RECOVERY_SOURCE_FILE.parents[1]
_RECOVERY_PACKAGE_ROOT = _RECOVERY_SOURCE_ROOT.parent
_TEST_FILE = Path(__file__).resolve()

OK = "ok"
PROGRESS_ID = "progress-1"

ONE_CONNECT = 1
TWO_CONNECTS = 2
TWO_EFFECTS = 2
TWO_ACTIVE_CALLS = 2
FOUR_TOOL_CALLS = 4
THREE_CONNECTS = 3
CONCURRENT_CALLERS = 5
SYSTEM_EXIT_CODE = 73

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
        types.ErrorData(code=32600, message="Session terminated"),
    )


@dataclass(frozen=True)
class Http404:
    """A started request whose actual transport response is HTTP 404."""

    error: McpError


@dataclass
class Gate:
    """Hold one fake upstream stage until the test releases it."""

    started: anyio.Event
    release: anyio.Event


def gate() -> Gate:
    """Create a fresh async gate inside the current test backend."""
    return Gate(anyio.Event(), anyio.Event())


class FakeSession:
    """Stand-in for ``ClientSession`` whose method results are scripted per connection."""

    def __init__(
        self,
        upstream: "FakeUpstream",
        script: dict[str, list[object]],
        observer: SessionObserver,
        session_id: str,
    ) -> None:
        """Bind the session to its upstream and to the script for this connection."""
        self.upstream = upstream
        self.script = script
        self.observer = observer
        self.session_id = session_id
        self.generation = int(session_id[1:]) - 1
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def initialize(self) -> types.InitializeResult:
        """Record a handshake and return a fixed initialization result."""
        self.upstream.handshakes += 1
        await self.observer.observe(
            httpx.Response(
                200,
                headers={"Mcp-Session-Id": self.session_id},
                request=httpx.Request("POST", "http://upstream.test/mcp"),
            ),
        )
        init_gate = self.upstream.initialize_gates.get(self.generation)
        if init_gate is not None:
            init_gate.started.set()
            await init_gate.release.wait()
        error = self.upstream.initialize_errors.get(self.generation)
        if error is not None:
            raise error
        if self.generation < len(self.upstream.initialize_results):
            return self.upstream.initialize_results[self.generation]
        return INITIALIZE_RESULT

    def __getattr__(self, name: str) -> Callable[..., Awaitable[object]]:
        """Return a coroutine function serving the next scripted outcome for ``name``."""
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args: object, **kwargs: object) -> object:
            self.calls.append((name, args, kwargs))
            if name == "call_tool":
                self.upstream.effects += 1
            outcomes = self.script.get(name)
            if outcomes:
                outcome = outcomes.pop(0)
                if isinstance(outcome, Http404):
                    await self.observer.observe(
                        httpx.Response(
                            404,
                            request=httpx.Request(
                                "POST",
                                "http://upstream.test/mcp",
                                headers={"Mcp-Session-Id": self.session_id},
                            ),
                        ),
                    )
                    raise outcome.error
                if isinstance(outcome, BaseException):
                    raise outcome
                if callable(outcome):
                    return await outcome(*args, **kwargs)
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
        self.initialize_errors: dict[int, BaseException] = {}
        self.initialize_gates: dict[int, Gate] = {}
        self.initialize_results: list[types.InitializeResult] = []
        self.exit_gates: dict[int, Gate] = {}
        self.connects = 0
        self.exits = 0
        self.handshakes = 0
        self.effects = 0
        self.owner_entries: list[int] = []
        self.owner_exits: list[int] = []
        self.sessions: list[FakeSession] = []
        self.closed: list[anyio.Event] = []

    @asynccontextmanager
    async def connect(self, observer: SessionObserver) -> AsyncIterator[ClientSession]:
        """Open one scripted connection and count its entry and its exit."""
        index = self.connects
        self.connects += 1
        error = self.connect_errors.get(index)
        if error is not None:
            raise error
        script = self.scripts[index] if index < len(self.scripts) else {}
        session = FakeSession(self, script, observer, f"s{index + 1}")
        self.owner_entries.append(id(asyncio.current_task()))
        self.sessions.append(session)
        closed = anyio.Event()
        self.closed.append(closed)
        try:
            yield t.cast("ClientSession", session)
        finally:
            self.exits += 1
            self.owner_exits.append(id(asyncio.current_task()))
            closed.set()
            exit_gate = self.exit_gates.get(index)
            if exit_gate is not None:
                exit_gate.started.set()
                await exit_gate.release.wait()
            exit_error = self.exit_errors.get(index)
            if exit_error is not None:
                raise exit_error


@asynccontextmanager
async def open_wrapper(connect: SessionFactory) -> AsyncIterator[ReconnectingClientSession]:
    """Open a wrapper under a deadline so a leaked connection fails instead of hanging."""
    with anyio.fail_after(LIFECYCLE_TIMEOUT):
        async with ReconnectingClientSession(connect) as wrapper:
            yield wrapper


def bounded_delete_factory(
    upstream: FakeUpstream,
    started: anyio.Event,
    release: anyio.Event,
    completed: anyio.Event,
) -> SessionFactory:
    """Add the existing bounded HTTPX DELETE to one fake connection's cleanup."""

    async def handle_delete(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        started.set()
        await release.wait()
        return httpx.Response(204, request=request)

    @asynccontextmanager
    async def connect(observer: SessionObserver) -> AsyncIterator[ClientSession]:
        async with upstream.connect(observer) as session:
            try:
                yield session
            finally:
                async with _SessionTerminationAsyncClient(
                    transport=httpx.MockTransport(handle_delete),
                ) as client:
                    response = await client.delete("http://upstream.test/mcp")
                    assert response.status_code == httpx.codes.NO_CONTENT
                    completed.set()

    return connect


async def test_happy_path_uses_a_single_connection() -> None:
    """A call that succeeds must not open a second connection."""
    upstream = FakeUpstream()

    async with open_wrapper(upstream.connect) as wrapper:
        assert await wrapper.list_tools() == OK

    assert upstream.connects == ONE_CONNECT


async def test_observer_marks_only_an_established_session_post_404() -> None:
    """HTTP status, method and the server-assigned session id gate loss marking."""
    observer = SessionObserver()

    def response(
        method: str,
        status: int,
        request_session_id: str | None = None,
        response_session_id: str | None = None,
    ) -> httpx.Response:
        request_headers = {"Mcp-Session-Id": request_session_id} if request_session_id else {}
        headers = {"Mcp-Session-Id": response_session_id} if response_session_id else {}
        return httpx.Response(
            status,
            headers=headers,
            request=httpx.Request(method, "http://upstream.test/mcp", headers=request_headers),
        )

    await observer.observe(response("POST", 200, "user-header", "s1"))
    await observer.observe(response("POST", 404, "s1"))
    assert not observer.lost.is_set()
    observer.establish()

    for method, status, session_id in (
        ("GET", 404, "s1"),
        ("DELETE", 404, "s1"),
        ("POST", 200, "s1"),
        ("POST", 401, "s1"),
        ("POST", 403, "s1"),
        ("POST", 503, "s1"),
        ("POST", 404, "user-header"),
        ("POST", 404, "s2"),
    ):
        await observer.observe(response(method, status, session_id))
        assert not observer.lost.is_set(), (method, status, session_id)

    await observer.observe(response("POST", 404, "s1"))
    assert observer.lost.is_set()

    non_success = SessionObserver()
    await non_success.observe(response("POST", 300, response_session_id="s1"))
    non_success.establish()
    await non_success.observe(response("POST", 404, "s1"))
    assert not non_success.lost.is_set()

    initial = SessionObserver()
    await initial.observe(response("POST", 404))
    assert not initial.lost.is_set()


async def test_http_404_fails_current_operation_then_recovers_on_next() -> None:
    """The operation that sees 404 is preserved; only the next request reconnects."""
    error = session_lost()
    upstream = FakeUpstream(scripts=[{"list_tools": [Http404(error)]}])

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError) as exc_info:
            await wrapper.list_tools()
        assert exc_info.value is error
        assert upstream.connects == ONE_CONNECT
        assert await wrapper.list_tools() == OK

    assert upstream.connects == TWO_CONNECTS
    assert [name for name, _, _ in upstream.sessions[0].calls] == ["list_tools"]
    assert [name for name, _, _ in upstream.sessions[1].calls] == ["list_tools"]
    assert upstream.owner_entries == upstream.owner_exits


async def test_application_invalid_request_32600_does_not_rebuild() -> None:
    """An application error with the SDK's old session-loss code is ordinary."""
    error = session_lost()
    upstream = FakeUpstream(scripts=[{"list_tools": [error]}])

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError) as exc_info:
            await wrapper.list_tools()
        assert exc_info.value is error
        assert upstream.connects == ONE_CONNECT
        assert await wrapper.list_tools() == OK

    assert upstream.connects == ONE_CONNECT


async def test_post_effect_schema_404_does_not_replay_tool_call() -> None:
    """A schema-read 404 after a tool effect must not replay the public call."""
    error = session_lost()
    upstream = FakeUpstream(scripts=[{"call_tool": [Http404(error)]}])

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError) as exc_info:
            await wrapper.call_tool("effect", {})
        assert exc_info.value is error
        assert upstream.effects == 1
        assert upstream.connects == ONE_CONNECT
        assert await wrapper.list_tools() == OK
        assert upstream.connects == TWO_CONNECTS
        assert await wrapper.call_tool("effect", {}) == OK

    assert upstream.effects == TWO_EFFECTS


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
    """Each explicit call sees one original error and can trigger only one rebuild."""
    first_error = session_lost()
    second_error = session_lost()
    upstream = FakeUpstream(
        scripts=[
            {"list_tools": [Http404(first_error)]},
            {"list_tools": [Http404(second_error)]},
        ],
    )

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError) as first:
            await wrapper.list_tools()
        assert first.value is first_error
        with pytest.raises(McpError) as second:
            await wrapper.list_tools()
        assert second.value is second_error
        assert upstream.connects == TWO_CONNECTS
        assert await wrapper.list_tools() == OK

    assert upstream.connects == THREE_CONNECTS


async def test_initialize_returns_the_cached_handshake() -> None:
    """``initialize`` must reuse the handshake performed while opening the connection."""
    upstream = FakeUpstream()

    async with open_wrapper(upstream.connect) as wrapper:
        first = await wrapper.initialize()
        second = await wrapper.initialize()

    assert first is INITIALIZE_RESULT
    assert second is INITIALIZE_RESULT
    assert upstream.handshakes == ONE_CONNECT


async def test_lost_generation_drains_before_shared_replacement() -> None:
    """Old leases finish before one replacement serves later callers."""
    progress_started = anyio.Event()
    release_progress = anyio.Event()
    cleanup = gate()
    loss_error = session_lost()
    upstream = FakeUpstream(
        scripts=[
            {
                "send_progress_notification": [
                    lambda *_args, **_kwargs: hold_progress(),
                ],
                "get_prompt": [Http404(loss_error)],
            },
        ],
    )
    upstream.exit_gates[0] = cleanup
    results: list[object] = []
    losses: list[McpError] = []
    loss_seen = anyio.Event()
    queued = 3

    async def hold_progress() -> str:
        progress_started.set()
        await release_progress.wait()
        return OK

    async with (
        open_wrapper(upstream.connect) as wrapper,
        anyio.create_task_group() as task_group,
    ):

        async def notify() -> None:
            results.append(
                await wrapper.send_progress_notification(
                    progress_token=PROGRESS_ID,
                    progress=1.0,
                ),
            )

        async def lose() -> None:
            try:
                await wrapper.get_prompt("prompt", {})
            except McpError as exc:
                losses.append(exc)
            finally:
                loss_seen.set()

        async def request() -> None:
            results.append(await wrapper.list_tools())

        task_group.start_soon(notify)
        await progress_started.wait()
        task_group.start_soon(lose)
        await loss_seen.wait()

        with pytest.raises(RuntimeError, match="progress notifications"):
            await wrapper.send_progress_notification(
                progress_token=PROGRESS_ID,
                progress=2.0,
            )
        for _ in range(queued):
            task_group.start_soon(request)
        await anyio.lowlevel.checkpoint()
        assert upstream.connects == ONE_CONNECT
        assert not upstream.closed[0].is_set()
        assert {name for name, _, _ in upstream.sessions[0].calls} == {
            "send_progress_notification",
            "get_prompt",
        }

        release_progress.set()
        await cleanup.started.wait()
        assert upstream.connects == ONE_CONNECT
        cleanup.release.set()

    assert losses == [loss_error]
    assert results == [OK] * (queued + 1)
    assert upstream.connects == TWO_CONNECTS
    assert [name for name, _, _ in upstream.sessions[1].calls] == ["list_tools"] * queued
    assert upstream.exits == upstream.connects
    assert upstream.owner_entries == upstream.owner_exits


async def test_healthy_operations_remain_concurrent() -> None:
    """The admission lock is not held while healthy upstream calls run."""
    both_started = anyio.Event()
    release = anyio.Event()
    active = 0
    peak = 0

    async def hold(*_args: object, **_kwargs: object) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == TWO_ACTIVE_CALLS:
            both_started.set()
        await release.wait()
        active -= 1
        return OK

    upstream = FakeUpstream(scripts=[{"list_tools": [hold, hold]}])
    results: list[object] = []
    async with open_wrapper(upstream.connect) as wrapper:

        async def run_and_store() -> None:
            results.append(await wrapper.list_tools())

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_and_store)
            task_group.start_soon(run_and_store)
            await both_started.wait()
            assert peak == TWO_ACTIVE_CALLS
            release.set()
    assert results == [OK, OK]
    assert upstream.connects == ONE_CONNECT


async def test_failed_replacement_fans_out_one_error_then_later_call_retries() -> None:
    """Waiters share one failed initialize; only a later operation tries again."""
    initialize_gate = gate()
    loss_error = session_lost()
    initialize_error = RuntimeError("upstream unavailable")
    upstream = FakeUpstream(scripts=[{"list_tools": [Http404(loss_error)]}])
    upstream.initialize_gates[1] = initialize_gate
    upstream.initialize_errors[1] = initialize_error
    all_started = anyio.Event()
    errors: list[BaseException] = []
    callers_started = 0

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError) as exc_info:
            await wrapper.list_tools()
        assert exc_info.value is loss_error

        async def call() -> None:
            nonlocal callers_started
            callers_started += 1
            if callers_started == CONCURRENT_CALLERS:
                all_started.set()
            try:
                await wrapper.list_tools()
            except RuntimeError as exc:
                errors.append(exc)

        async with anyio.create_task_group() as task_group:
            for _ in range(CONCURRENT_CALLERS):
                task_group.start_soon(call)
            await all_started.wait()
            await initialize_gate.started.wait()
            await anyio.lowlevel.checkpoint()
            initialize_gate.release.set()

        assert len(errors) == CONCURRENT_CALLERS
        assert all(error is initialize_error for error in errors)
        assert upstream.connects == TWO_CONNECTS
        assert upstream.exits == TWO_CONNECTS
        assert await wrapper.list_tools() == OK
        assert upstream.connects == THREE_CONNECTS

    assert upstream.exits == upstream.connects
    assert upstream.owner_entries == upstream.owner_exits


def changed_handshake(field: str) -> types.InitializeResult:
    """Return one public handshake variant for exact-equality rejection tests."""
    data = INITIALIZE_RESULT.model_dump(mode="python")
    if field == "protocolVersion":
        data[field] = "other-protocol"
    elif field == "capabilities":
        data[field] = types.ServerCapabilities(tools=types.ToolsCapability(listChanged=True))
    elif field == "serverInfo":
        data[field] = types.Implementation(name="other-server", version="1.0.0")
    elif field == "instructions":
        data[field] = "changed instructions"
    else:
        data["vendorExtension"] = {"changed": True}
    return types.InitializeResult.model_validate(data)


@pytest.mark.parametrize(
    "field",
    ["protocolVersion", "capabilities", "serverInfo", "instructions", "extra"],
)
async def test_replacement_handshake_mismatch_refuses_permanently(field: str) -> None:
    """Every public handshake field is pinned to the first connection."""
    upstream = FakeUpstream(scripts=[{"list_tools": [Http404(session_lost())]}])
    upstream.initialize_results = [INITIALIZE_RESULT, changed_handshake(field)]

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError):
            await wrapper.list_tools()
        with pytest.raises(RuntimeError, match="handshake changed; restart") as mismatch:
            await wrapper.list_tools()
        assert str(mismatch.value) == "Upstream handshake changed; restart the MCP client"
        with pytest.raises(RuntimeError, match="handshake changed; restart"):
            await wrapper.list_tools()
        assert upstream.connects == TWO_CONNECTS
        assert upstream.exits == TWO_CONNECTS


@pytest.mark.parametrize(
    "method",
    ["subscribe_resource", "unsubscribe_resource", "set_logging_level"],
)
async def test_failed_stateful_method_latches_state_loss_refusal(method: str) -> None:
    """Stateful method admission is remembered even when that method fails."""
    state_error = ValueError("state operation failed")
    upstream = FakeUpstream(
        scripts=[
            {
                method: [state_error],
                "list_tools": [Http404(session_lost())],
            },
        ],
    )

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(ValueError, match="state operation failed") as exc_info:
            await getattr(wrapper, method)("state")
        assert exc_info.value is state_error
        with pytest.raises(McpError):
            await wrapper.list_tools()
        with pytest.raises(RuntimeError, match="session state was lost; restart"):
            await wrapper.list_tools()
        assert upstream.connects == ONE_CONNECT


async def test_subscribe_then_unsubscribe_still_latches_state_loss() -> None:
    """Removing a subscription does not make its session-local history restorable."""
    upstream = FakeUpstream(scripts=[{"list_tools": [Http404(session_lost())]}])
    async with open_wrapper(upstream.connect) as wrapper:
        await wrapper.subscribe_resource("file:///resource")
        await wrapper.unsubscribe_resource("file:///resource")
        with pytest.raises(McpError):
            await wrapper.list_tools()
        with pytest.raises(RuntimeError, match="session state was lost; restart"):
            await wrapper.list_tools()
    assert upstream.connects == ONE_CONNECT


async def test_progress_forwards_once_when_healthy_and_never_recovers() -> None:
    """Progress notifications stay on the current generation and never join recovery."""
    upstream = FakeUpstream(
        scripts=[
            {
                "send_progress_notification": [None],
                "list_tools": [Http404(session_lost())],
            },
        ],
    )
    async with open_wrapper(upstream.connect) as wrapper:
        assert await wrapper.send_progress_notification(PROGRESS_ID, 1.0) is None
        with pytest.raises(McpError):
            await wrapper.list_tools()
        with pytest.raises(RuntimeError, match="progress notifications"):
            await wrapper.send_progress_notification(PROGRESS_ID, 2.0)
        assert upstream.connects == ONE_CONNECT
        assert await wrapper.list_tools() == OK
    assert [name for name, _, _ in upstream.sessions[0].calls] == [
        "send_progress_notification",
        "list_tools",
    ]
    assert [name for name, _, _ in upstream.sessions[1].calls] == ["list_tools"]


async def test_progress_does_not_join_an_in_flight_replacement() -> None:
    """Progress is rejected while another explicit call is rebuilding the session."""
    initialize_gate = gate()
    upstream = FakeUpstream(scripts=[{"list_tools": [Http404(session_lost())]}])
    upstream.initialize_gates[1] = initialize_gate
    recovered: list[object] = []

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError):
            await wrapper.list_tools()

        async def recover() -> None:
            recovered.append(await wrapper.list_tools())

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(recover)
            await initialize_gate.started.wait()
            try:
                with anyio.fail_after(LIFECYCLE_TIMEOUT):
                    with pytest.raises(RuntimeError, match="progress notifications"):
                        await wrapper.send_progress_notification(PROGRESS_ID, 1.0)
            finally:
                initialize_gate.release.set()

        assert recovered == [OK]
        assert upstream.connects == TWO_CONNECTS
        assert [name for session in upstream.sessions for name, _, _ in session.calls] == [
            "list_tools",
            "list_tools",
        ]


async def test_initial_connect_failure_propagates() -> None:
    """A first connection that cannot be opened fails adapter entry."""
    error = RuntimeError("upstream unreachable")
    upstream = FakeUpstream(connect_errors={0: error})

    with pytest.raises(RuntimeError) as exc_info:
        async with open_wrapper(upstream.connect):
            pass  # pragma: no cover - the context manager never opens

    assert exc_info.value is error
    assert upstream.connects == ONE_CONNECT
    assert upstream.exits == 0


async def test_initial_initialize_failure_closes_entered_owner() -> None:
    """A failed initial SDK initialize unwinds the context that entered it."""
    error = RuntimeError("initialize failed")
    upstream = FakeUpstream()
    upstream.initialize_errors[0] = error
    with pytest.raises(RuntimeError) as exc_info:
        async with open_wrapper(upstream.connect):
            pass  # pragma: no cover - the context manager never opens
    assert exc_info.value is error
    assert upstream.connects == ONE_CONNECT
    assert upstream.exits == ONE_CONNECT
    assert upstream.owner_entries == upstream.owner_exits


async def test_cancelled_initial_initialize_closes_entered_owner() -> None:
    """EOF-style cancellation unwinds an initialize still owned by its task."""
    initialize_gate = gate()
    upstream = FakeUpstream()
    upstream.initialize_gates[0] = initialize_gate
    scope = anyio.CancelScope()

    async def open_until_cancelled() -> None:
        with scope:
            async with ReconnectingClientSession(upstream.connect):
                pytest.fail("cancelled initialize unexpectedly returned")

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(open_until_cancelled)
        await initialize_gate.started.wait()
        scope.cancel()

    assert upstream.exits == ONE_CONNECT
    assert upstream.owner_entries == upstream.owner_exits


async def test_cancelled_replacement_initialize_closes_candidate_owner() -> None:
    """EOF-style cancellation during replacement unwinds its unpublished candidate."""
    initialize_gate = gate()
    upstream = FakeUpstream(scripts=[{"list_tools": [Http404(session_lost())]}])
    upstream.initialize_gates[1] = initialize_gate
    wrapper = ReconnectingClientSession(upstream.connect)
    await wrapper.__aenter__()
    try:
        with pytest.raises(McpError):
            await wrapper.list_tools()

        cancel_waiter = anyio.CancelScope()

        async def recover() -> None:
            with cancel_waiter:
                await wrapper.list_tools()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(recover)
            await initialize_gate.started.wait()
            cancel_waiter.cancel()

    finally:
        await wrapper.__aexit__(None, None, None)

    assert upstream.connects == TWO_CONNECTS
    assert upstream.exits == TWO_CONNECTS
    assert upstream.owner_entries == upstream.owner_exits


async def test_cancelled_operation_releases_its_generation_lease() -> None:
    """Cancelling an admitted operation does not strand later work or teardown."""
    call_gate = gate()

    async def hold(*_args: object, **_kwargs: object) -> str:
        call_gate.started.set()
        await call_gate.release.wait()
        return OK

    upstream = FakeUpstream(scripts=[{"list_tools": [hold]}])
    scope = anyio.CancelScope()

    async with open_wrapper(upstream.connect) as wrapper:

        async def cancelable_call() -> None:
            with scope:
                await wrapper.list_tools()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(cancelable_call)
            await call_gate.started.wait()
            scope.cancel()
        assert await wrapper.list_tools() == OK

    assert upstream.exits == upstream.connects
    assert upstream.owner_entries == upstream.owner_exits


async def test_clean_shutdown_closes_every_connection() -> None:
    """Leaving the wrapper closes its one current owner."""
    upstream = FakeUpstream()

    async with open_wrapper(upstream.connect) as wrapper:
        assert await wrapper.list_tools() == OK

    assert upstream.connects == ONE_CONNECT
    assert upstream.exits == upstream.connects
    assert upstream.owner_entries == upstream.owner_exits


async def test_clean_shutdown_completes_bounded_delete() -> None:
    """A clean wrapper exit lets its real bounded DELETE finish."""
    upstream = FakeUpstream()
    delete_started = anyio.Event()
    release_delete = anyio.Event()
    delete_completed = anyio.Event()
    connect = bounded_delete_factory(upstream, delete_started, release_delete, delete_completed)

    async def release_after_delete_starts() -> None:
        await delete_started.wait()
        await anyio.lowlevel.checkpoint()
        release_delete.set()

    with anyio.fail_after(LIFECYCLE_TIMEOUT):
        async with anyio.create_task_group() as controllers:
            controllers.start_soon(release_after_delete_starts)
            async with open_wrapper(connect) as wrapper:
                assert await wrapper.list_tools() == OK

    assert delete_started.is_set()
    assert delete_completed.is_set()
    assert upstream.connects == upstream.exits == ONE_CONNECT
    assert upstream.owner_entries == upstream.owner_exits


async def test_external_cancellation_during_exit_completes_bounded_delete() -> None:
    """External cancellation waits for the active bounded DELETE to complete."""
    upstream = FakeUpstream()
    delete_started = anyio.Event()
    release_delete = anyio.Event()
    delete_completed = anyio.Event()
    connect = bounded_delete_factory(upstream, delete_started, release_delete, delete_completed)
    outer = anyio.CancelScope()

    async def cancel_and_release() -> None:
        await delete_started.wait()
        outer.cancel()
        await anyio.lowlevel.checkpoint()
        release_delete.set()

    with anyio.fail_after(LIFECYCLE_TIMEOUT):
        async with anyio.create_task_group() as controllers:
            controllers.start_soon(cancel_and_release)
            try:
                with outer:
                    async with open_wrapper(connect) as wrapper:
                        assert await wrapper.list_tools() == OK
                    await anyio.lowlevel.checkpoint()
            finally:
                release_delete.set()

    assert outer.cancel_called
    assert outer.cancelled_caught
    assert delete_started.is_set()
    assert delete_completed.is_set()
    assert upstream.connects == upstream.exits == ONE_CONNECT
    assert upstream.owner_entries == upstream.owner_exits


async def test_finished_transport_owner_rejects_later_operation() -> None:
    """A finished owner with a retained session cannot admit another call."""
    crash_transport = anyio.Event()

    async def trigger_failure(*_args: object, **_kwargs: object) -> str:
        crash_transport.set()
        return OK

    upstream = FakeUpstream(scripts=[{"list_tools": [trigger_failure]}])

    @asynccontextmanager
    async def connect(observer: SessionObserver) -> AsyncIterator[ClientSession]:
        async with (
            upstream.connect(observer) as session,
            anyio.create_task_group() as transport_tasks,
        ):

            async def fail_transport() -> None:
                await crash_transport.wait()
                raise RuntimeError("controlled transport owner failure")

            transport_tasks.start_soon(fail_transport)
            yield session

    async with open_wrapper(connect) as wrapper:
        assert await wrapper.list_tools() == OK
        connection = wrapper._connection  # noqa: SLF001 - observe a real finished owner
        assert connection is not None
        await connection.finished.wait()
        assert connection.session is not None
        assert not connection.observer.lost.is_set()

        with pytest.raises(RuntimeError, match=r"^Upstream session is not connected$"):
            await wrapper.list_tools()
        assert [name for name, _, _ in upstream.sessions[0].calls] == ["list_tools"]

    assert upstream.connects == upstream.exits == ONE_CONNECT
    assert upstream.owner_entries == upstream.owner_exits


async def test_cancelled_sole_lost_lease_shutdown_resolves_later_waiters() -> None:  # noqa: PLR0915
    """The sole lost lease drains before retirement and shutdown resolves both waiters."""
    loss_observed = anyio.Event()
    hold_operation = anyio.Event()
    old_operation_done = anyio.Event()
    initialize_gate = gate()
    old_operation_scope = anyio.CancelScope()
    waiter_errors: list[RuntimeError] = []
    waiters_done = anyio.Event()
    upstream = FakeUpstream()

    async def observe_loss_then_hold(*_args: object, **_kwargs: object) -> str:
        session = upstream.sessions[0]
        await session.observer.observe(
            httpx.Response(
                404,
                request=httpx.Request(
                    "POST",
                    "http://upstream.test/mcp",
                    headers={"Mcp-Session-Id": session.session_id},
                ),
            ),
        )
        loss_observed.set()
        await hold_operation.wait()
        return OK

    upstream.scripts = [{"list_tools": [observe_loss_then_hold]}]
    upstream.initialize_gates[1] = initialize_gate
    wrapper = ReconnectingClientSession(upstream.connect)

    async def old_operation() -> None:
        try:
            with old_operation_scope:
                await wrapper.list_tools()
        finally:
            old_operation_done.set()

    async def later_request() -> None:
        try:
            await wrapper.list_tools()
        except RuntimeError as exc:
            waiter_errors.append(exc)
            if len(waiter_errors) == TWO_CONNECTS:
                waiters_done.set()

    with anyio.fail_after(LIFECYCLE_TIMEOUT):
        async with anyio.create_task_group() as external_callers:
            await wrapper.__aenter__()
            try:
                external_callers.start_soon(old_operation)
                await loss_observed.wait()
                external_callers.start_soon(later_request)
                external_callers.start_soon(later_request)
                await anyio.wait_all_tasks_blocked()

                assert upstream.connects == ONE_CONNECT
                assert not upstream.closed[0].is_set()

                old_operation_scope.cancel()
                await old_operation_done.wait()
                await initialize_gate.started.wait()
                attempt = wrapper._attempt  # noqa: SLF001 - observe the shared attempt
                assert attempt is not None
                assert upstream.exits == ONE_CONNECT
                assert upstream.connects == TWO_CONNECTS

                await wrapper.__aexit__(None, None, None)
                await waiters_done.wait()
                assert attempt.done.is_set()
                assert len(waiter_errors) == TWO_CONNECTS
                assert waiter_errors[0] is waiter_errors[1]
                assert str(waiter_errors[0]) == "Upstream session is not connected"
            finally:
                old_operation_scope.cancel()
                hold_operation.set()
                initialize_gate.release.set()
                if wrapper._task_group is not None:  # noqa: SLF001 - avoid double exit
                    await wrapper.__aexit__(None, None, None)

    assert upstream.connects == upstream.exits == TWO_CONNECTS
    assert upstream.owner_entries == upstream.owner_exits


async def test_closing_during_publish_retires_unpublished_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown at publication closes the ready candidate without installing it."""
    publish_entered = anyio.Event()
    publish_completed = anyio.Event()
    waiter_done = anyio.Event()
    errors: list[RuntimeError] = []
    candidates: list[t.Any] = []
    upstream = FakeUpstream(scripts=[{"list_tools": [Http404(session_lost())]}])
    wrapper = ReconnectingClientSession(upstream.connect)
    original_publish = ReconnectingClientSession._publish_replacement  # noqa: SLF001

    async def observe_publish(
        session: ReconnectingClientSession,
        candidate: t.Any,  # noqa: ANN401 - private runtime object observed by the test
        attempt: t.Any,  # noqa: ANN401 - private runtime object observed by the test
    ) -> bool:
        candidates.append(candidate)
        assert candidate.ready.is_set()
        publish_entered.set()
        result = await original_publish(session, candidate, attempt)
        publish_completed.set()
        return result

    monkeypatch.setattr(ReconnectingClientSession, "_publish_replacement", observe_publish)

    async def later_request() -> None:
        try:
            await wrapper.list_tools()
        except RuntimeError as exc:
            errors.append(exc)
        finally:
            waiter_done.set()

    with anyio.fail_after(LIFECYCLE_TIMEOUT):
        async with anyio.create_task_group() as external_callers:
            await wrapper.__aenter__()
            try:
                with pytest.raises(McpError):
                    await wrapper.list_tools()
                external_callers.start_soon(later_request)
                await publish_entered.wait()
                candidate = candidates[0]
                assert not candidate.finished.is_set()

                await wrapper.__aexit__(None, None, None)
                await waiter_done.wait()
                assert publish_completed.is_set()

                assert candidate.finished.is_set()
                assert candidate.retire.is_set()
                assert wrapper._connection is not candidate  # noqa: SLF001 - publication observation
                assert len(errors) == ONE_CONNECT
                assert str(errors[0]) == "Upstream session is not connected"
                assert upstream.closed[1].is_set()
            finally:
                if wrapper._task_group is not None:  # noqa: SLF001 - avoid double exit
                    await wrapper.__aexit__(None, None, None)

    assert upstream.connects == upstream.exits == TWO_CONNECTS
    assert upstream.owner_entries == upstream.owner_exits


async def test_system_exit_in_factory_cleanup_propagates_in_finite_child() -> None:
    """A cleanup SystemExit escapes its bounded child without ending pytest."""
    child_env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(
            path for path in (str(_RECOVERY_SOURCE_ROOT), os.environ.get("PYTHONPATH", "")) if path
        ),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        str(_TEST_FILE),
        "--system-exit-child",
        cwd=str(_RECOVERY_PACKAGE_ROOT),
        env=child_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=10,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        pytest.fail("SystemExit cleanup child exceeded its 10-second bound")

    stdout = stdout_bytes.decode()
    stderr = stderr_bytes.decode()
    assert process.returncode == SYSTEM_EXIT_CODE, f"stdout={stdout!r}; stderr={stderr!r}"
    assert f"RECOVERY_SOURCE={_RECOVERY_SOURCE_FILE}" in stdout
    assert f"SYSTEM_EXIT_PROPAGATED:{SYSTEM_EXIT_CODE}" in stdout
    assert "SYSTEM_EXIT_SWALLOWED" not in stdout


async def test_shutdown_closes_admission_before_transport_exit() -> None:
    """No new operation is admitted while the owning transport is closing."""
    exit_gate = gate()
    upstream = FakeUpstream()
    upstream.exit_gates[0] = exit_gate

    async with (
        anyio.create_task_group() as task_group,
        open_wrapper(
            upstream.connect,
        ) as wrapper,
    ):
        outcomes: list[object] = []

        async def call_during_close() -> None:
            await exit_gate.started.wait()
            try:
                outcomes.append(await wrapper.list_tools())
            except RuntimeError as exc:
                outcomes.append(exc)
            finally:
                exit_gate.release.set()

        task_group.start_soon(call_during_close)
        await wrapper.__aexit__(None, None, None)

    assert len(outcomes) == 1
    assert isinstance(outcomes[0], RuntimeError)
    assert str(outcomes[0]) == "Upstream session is not connected"
    assert upstream.exits == upstream.connects
    assert upstream.owner_entries == upstream.owner_exits


async def test_keyword_arguments_are_forwarded_once_on_the_later_call() -> None:
    """A next explicit call keeps caller arguments without replaying the failed one."""
    error = session_lost()
    upstream = FakeUpstream(scripts=[{"call_tool": [Http404(error)]}])

    async def progress_callback(progress: float, total: float | None, message: str | None) -> None:
        """Ignore progress updates; only their callback identity matters."""

    async with open_wrapper(upstream.connect) as wrapper:
        with pytest.raises(McpError) as exc_info:
            await wrapper.call_tool(
                "tool",
                {"input1": "value"},
                meta={"progressToken": "token"},
                progress_callback=progress_callback,
            )
        assert exc_info.value is error
        assert upstream.effects == 1
        assert (
            await wrapper.call_tool(
                "tool",
                {"input1": "value"},
                meta={"progressToken": "token"},
                progress_callback=progress_callback,
            )
            == OK
        )

    assert upstream.effects == TWO_EFFECTS
    for session in upstream.sessions:
        name, args, kwargs = session.calls[0]
        assert name == "call_tool"
        assert args == ("tool", {"input1": "value"})
        assert kwargs == {
            "meta": {"progressToken": "token"},
            "progress_callback": progress_callback,
        }


async def test_methods_before_entering_raise() -> None:
    """Using the wrapper before entry fails with a clear connection error."""
    wrapper = ReconnectingClientSession(FakeUpstream().connect)

    with pytest.raises(RuntimeError, match="not connected"):
        await wrapper.initialize()
    with pytest.raises(RuntimeError, match="not connected"):
        await wrapper.list_tools()


async def test_teardown_failures_do_not_kill_the_proxy(caplog: pytest.LogCaptureFixture) -> None:
    """A cleanup error after readiness stays logged and does not mask completion."""
    caplog.set_level(logging.DEBUG, logger="mcp_proxy.reconnecting_session")
    upstream = FakeUpstream(exit_errors={0: RuntimeError("socket already gone")})

    async with open_wrapper(upstream.connect) as wrapper:
        assert await wrapper.list_tools() == OK

    assert upstream.connects == upstream.exits == ONE_CONNECT
    assert "socket already gone" in caplog.text


def test_private_attributes_are_not_forwarded() -> None:
    """Dunder and private lookups must fail normally so introspection keeps working."""
    wrapper = ReconnectingClientSession(FakeUpstream().connect)

    with pytest.raises(AttributeError):
        getattr(wrapper, "_not_a_session_method")  # noqa: B009 - the literal is the point


def _cli_source_paths() -> tuple[Path, Path]:
    """Return the source file and checkout root used by the live CLI test."""
    source_file = Path(__file__).resolve().parents[1] / "src/mcp_proxy/streamablehttp_client.py"
    return source_file, source_file.parents[2]


@pytest.mark.asyncio
async def test_cli_session_loss_recovers_on_later_explicit_request_without_replay() -> None:  # noqa: C901, PLR0915
    """A real CLI preserves the failed request, then recovers on the next request."""
    source_file, package_root = _cli_source_paths()
    source_root = source_file.parents[1]
    sessions: list[str] = []
    invalidated_sessions: set[str] = set()
    effects: list[str] = []
    requests: list[dict[str, object]] = []
    failed_lists: list[str | None] = []
    responses: dict[int, dict[str, object]] = {}
    response_available = asyncio.Event()
    handlers: set[asyncio.Task[object]] = set()

    async def respond(
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, object] | None = None,
        extra_headers: str = "",
    ) -> None:
        body = json.dumps(payload).encode() if payload is not None else b""
        reason = {
            200: "OK",
            202: "Accepted",
            204: "No Content",
            404: "Not Found",
            405: "Method Not Allowed",
        }[status]
        content_type = "Content-Type: application/json\r\n" if payload is not None else ""
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"{content_type}"
            f"{extra_headers}Connection: close\r\n\r\n"
        )
        writer.write(head.encode() + body)
        await writer.drain()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:  # noqa: C901, PLR0912
        task = asyncio.current_task()
        if task is not None:
            handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method, _, _ = lines[0].split()
            headers = {
                name.lower(): value
                for line in lines[1:]
                if ": " in line
                for name, value in [line.split(": ", 1)]
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            message = json.loads(body) if body else {}
            rpc_method = message.get("method")
            session_id = headers.get("mcp-session-id")
            requests.append(
                {
                    "method": method,
                    "rpc_method": rpc_method,
                    "request_id": message.get("id"),
                    "session_id": session_id,
                },
            )

            if method == "GET":
                await respond(writer, 405)
            elif method == "DELETE":
                await respond(writer, 204)
            elif rpc_method == "initialize":
                session_id = f"s{len(sessions) + 1}"
                sessions.append(session_id)
                protocol_version = message["params"]["protocolVersion"]
                result = {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "recovery-test", "version": "1"},
                    "instructions": "stable handshake",
                }
                await respond(
                    writer,
                    200,
                    {"jsonrpc": "2.0", "id": message["id"], "result": result},
                    f"Mcp-Session-Id: {session_id}\r\nMcp-Protocol-Version: {protocol_version}\r\n",
                )
            elif rpc_method == "notifications/initialized":
                await respond(writer, 202)
            elif session_id in invalidated_sessions:
                if rpc_method == "tools/list":
                    failed_lists.append(session_id)
                await respond(writer, 404)
            elif rpc_method == "tools/list":
                result = {
                    "tools": [
                        {"name": "other", "inputSchema": {"type": "object", "properties": {}}},
                    ],
                }
                await respond(
                    writer,
                    200,
                    {"jsonrpc": "2.0", "id": message["id"], "result": result},
                    "Mcp-Protocol-Version: 2025-11-25\r\n",
                )
            elif rpc_method == "tools/call":
                effects.append(session_id or "sessionless")
                result = {"content": [{"type": "text", "text": f"effect-{len(effects)}"}]}
                await respond(
                    writer,
                    200,
                    {"jsonrpc": "2.0", "id": message["id"], "result": result},
                    "Mcp-Protocol-Version: 2025-11-25\r\n",
                )
                arguments = message["params"].get("arguments", {})
                if arguments.get("invalidate_after_effect"):
                    invalidated_sessions.add(session_id or "")
            else:
                await respond(writer, 404)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            if task is not None:
                handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    env = {
        **os.environ,
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
        "PYTHONPATH": os.pathsep.join(
            path for path in (str(source_root), os.environ.get("PYTHONPATH", "")) if path
        ),
    }
    child_launcher = (
        "import runpy, sys; "
        "import mcp_proxy.streamablehttp_client as reverse; "
        "print('MCP_PROXY_STREAMABLE_SOURCE=' + reverse.__file__, file=sys.stderr); "
        "runpy.run_module('mcp_proxy', run_name='__main__')"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_launcher,
        "--transport",
        "streamablehttp",
        f"http://127.0.0.1:{port}/mcp",
        cwd=package_root,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    retained_stdin = os.dup(process.stdin.transport.get_extra_info("pipe").fileno())

    async def read_responses() -> None:
        async for line in process.stdout:
            message = json.loads(line)
            if "id" in message:
                responses[message["id"]] = message
                response_available.set()

    async def send(message: dict[str, object]) -> None:
        process.stdin.write(json.dumps(message).encode() + b"\n")
        await process.stdin.drain()

    async def wait_response(request_id: int) -> dict[str, object]:
        async def wait_until_available() -> None:
            while request_id not in responses:
                response_available.clear()
                if request_id in responses:
                    break
                await response_available.wait()

        try:
            await asyncio.wait_for(wait_until_available(), timeout=10)
        except TimeoutError as exc:
            child_stderr = stderr_task.result().decode() if stderr_task.done() else "still open"
            timeout_message = (
                f"CLI response {request_id} timed out; upstream={requests!r}; "
                f"returncode={process.returncode}; stderr={child_stderr!r}"
            )
            raise AssertionError(timeout_message) from exc
        return responses[request_id]

    stdout_task = asyncio.create_task(read_responses())
    stderr_task = asyncio.create_task(process.stderr.read())
    stderr = ""
    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "recovery-test", "version": "1"},
                },
            },
        )
        assert "result" in await wait_response(1)
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert "result" in await wait_response(2)
        await send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "effect", "arguments": {}},
            },
        )
        assert "result" in await wait_response(3)
        await send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "effect",
                    "arguments": {"invalidate_after_effect": True},
                },
            },
        )
        failed_operation = await wait_response(4)
        failed_result = failed_operation.get("result")
        assert isinstance(failed_result, dict)
        assert failed_result.get("isError") is True, failed_operation

        await send(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "effect", "arguments": {}},
            },
        )
        recovered = await wait_response(5)
        recovered_result = recovered.get("result")
        assert isinstance(recovered_result, dict)
        assert not recovered_result.get("isError", False), (
            f"later explicit call did not recover: {recovered!r}; "
            f"sessions={sessions!r}; requests={requests!r}"
        )

        await send(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "effect", "arguments": {}},
            },
        )
        assert "result" in await wait_response(6)
        assert sessions == ["s1", "s2"]
        tool_calls = [item for item in requests if item["rpc_method"] == "tools/call"]
        assert [item["session_id"] for item in tool_calls] == ["s1", "s1", "s2", "s2"]
        assert (
            len({(item["session_id"], item["request_id"]) for item in tool_calls})
            == FOUR_TOOL_CALLS
        )
        assert effects == ["s1", "s1", "s2", "s2"]
        assert failed_lists == ["s1"]
        effect_index = next(
            index
            for index, item in enumerate(requests)
            if item["rpc_method"] == "tools/call" and item["session_id"] == "s1"
        )
        failed_list_index = next(
            index
            for index, item in enumerate(requests)
            if item["rpc_method"] == "tools/list"
            and item["session_id"] == "s1"
            and index > effect_index
        )
        assert failed_list_index > effect_index
        assert process.returncode is None
        os.fstat(retained_stdin)
    finally:
        process.stdin.close()
        os.close(retained_stdin)
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=7)
            except TimeoutError:
                process.kill()
                await process.wait()
        await stdout_task
        stderr = (await stderr_task).decode()
        assert f"MCP_PROXY_STREAMABLE_SOURCE={source_file}" in stderr
        server.close()
        await server.wait_closed()
        if handlers:
            _done, pending = await asyncio.wait(handlers, timeout=2)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    assert not handlers


async def _run_system_exit_child() -> None:
    """Exercise fatal factory cleanup only in the test's finite child process."""
    sys.stdout.write(f"RECOVERY_SOURCE={_RECOVERY_SOURCE_FILE}\n")
    sys.stdout.flush()
    upstream = FakeUpstream(exit_errors={0: SystemExit(SYSTEM_EXIT_CODE)})
    async with ReconnectingClientSession(upstream.connect) as wrapper:
        assert await wrapper.list_tools() == OK
    sys.stdout.write("SYSTEM_EXIT_SWALLOWED\n")
    sys.stdout.flush()


if __name__ == "__main__" and "--system-exit-child" in sys.argv:
    try:
        asyncio.run(_run_system_exit_child())
    except SystemExit as exc:
        sys.stdout.write(f"SYSTEM_EXIT_PROPAGATED:{exc.code}\n")
        sys.stdout.flush()
        raise
