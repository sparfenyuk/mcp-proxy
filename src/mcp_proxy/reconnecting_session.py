# Copyright (c) 2024 Sergey Parfenyuk

"""Recover a lost upstream session only before a later explicit operation."""

import logging
import typing as t
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from http import HTTPStatus
from types import TracebackType

import anyio
import httpx
from mcp import types
from mcp.client.session import ClientSession

if t.TYPE_CHECKING:
    from anyio.abc import TaskGroup
    from typing_extensions import Self

logger = logging.getLogger(__name__)

_NOT_CONNECTED = "Upstream session is not connected"
_HANDSHAKE_CHANGED = "Upstream handshake changed; restart the MCP client"
_STATE_LOST = "Upstream session state was lost; restart the MCP client"
_PROGRESS_SESSION_LOST = "Upstream session is unavailable for progress notifications"
_LOST_BEFORE_ADMISSION = "Upstream session was lost before the request started"
_STATEFUL_METHODS = frozenset({"subscribe_resource", "unsubscribe_resource", "set_logging_level"})
_PROGRESS_METHOD = "send_progress_notification"


class SessionObserver:
    """Mark an established generation lost only after its own POST returns 404."""

    def __init__(self) -> None:
        """Create fresh response-hook state for one upstream generation."""
        self.lost = anyio.Event()
        self._candidate_session_id: str | None = None
        self._established_session_id: str | None = None

    async def observe(self, response: httpx.Response) -> None:
        """Observe status and headers without reading the response or request body."""
        request = response.request
        if request.method.upper() != "POST":
            return

        if self._established_session_id is None:
            if HTTPStatus.OK <= response.status_code < HTTPStatus.MULTIPLE_CHOICES:
                self._candidate_session_id = (
                    response.headers.get("mcp-session-id") or self._candidate_session_id
                )
            return

        if (
            response.status_code == HTTPStatus.NOT_FOUND
            and request.headers.get("mcp-session-id") == self._established_session_id
        ):
            self.lost.set()

    def establish(self) -> None:
        """Enable loss detection after the complete SDK initialization succeeds."""
        self._established_session_id = self._candidate_session_id


SessionFactory = t.Callable[[SessionObserver], AbstractAsyncContextManager[ClientSession]]


@dataclass
class _Connection:
    """One session context, entered and exited by its owner task."""

    observer: SessionObserver = field(default_factory=SessionObserver)
    ready: anyio.Event = field(default_factory=anyio.Event)
    finished: anyio.Event = field(default_factory=anyio.Event)
    retire: anyio.Event = field(default_factory=anyio.Event)
    drained: anyio.Event = field(default_factory=anyio.Event)
    session: ClientSession | None = None
    initialize_result: types.InitializeResult | None = None
    error: BaseException | None = None
    leases: int = 0
    retire_started: bool = False

    def __post_init__(self) -> None:
        """Start with no operation leases to drain."""
        self.drained.set()


@dataclass
class _Attempt:
    """Result shared by all callers that joined one replacement attempt."""

    done: anyio.Event = field(default_factory=anyio.Event)
    connection: _Connection | None = None
    error: BaseException | None = None
    cancel_scope: anyio.CancelScope | None = None


class _HandshakeChangedError(RuntimeError):
    """The replacement connection no longer matches the advertised contract."""


class ReconnectingClientSession:
    """Lease public operations and recover only when a later operation is admitted."""

    def __init__(self, connect: SessionFactory) -> None:
        """Store the per-generation connection factory."""
        self._connect = connect
        self._connection: _Connection | None = None
        self._task_group: TaskGroup | None = None
        self._lock: anyio.Lock | None = None
        self._attempt: _Attempt | None = None
        self._pending_connection: _Connection | None = None
        self._baseline_handshake: dict[str, t.Any] | None = None
        self._permanent_error: str | None = None
        self._stateful_operations_used = False
        self._closing = False

    async def __aenter__(self) -> "Self":
        """Start the owner group and publish the initial complete handshake."""
        self._lock = anyio.Lock()
        task_group = anyio.create_task_group()
        await task_group.__aenter__()
        self._task_group = task_group
        connection = _Connection()
        self._connection = connection
        task_group.start_soon(self._run_connection, connection)

        try:
            await connection.ready.wait()
            if connection.error is not None:
                await connection.finished.wait()
        except BaseException:
            self._closing = True
            connection.retire.set()
            task_group.cancel_scope.cancel()
            self._task_group = None
            await task_group.__aexit__(None, None, None)
            raise
        if connection.error is not None:
            error = connection.error
            self._closing = True
            connection.retire.set()
            task_group.cancel_scope.cancel()
            self._task_group = None
            await task_group.__aexit__(None, None, None)
            raise error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Cancel pending work and unwind every generation in its owner task."""
        self._closing = True
        attempt = self._attempt
        if attempt is not None and attempt.cancel_scope is not None:
            attempt.cancel_scope.cancel()
        task_group = self._task_group
        pending = self._pending_connection
        if task_group is not None and pending is not None and not pending.ready.is_set():
            task_group.cancel_scope.cancel()
        for connection in (self._connection, self._pending_connection):
            if connection is not None:
                connection.retire.set()
        task_group, self._task_group = self._task_group, None
        if task_group is None:
            return None
        task_group.cancel_scope.shield = True
        await task_group.__aexit__(None, None, None)
        return None

    async def initialize(self) -> types.InitializeResult:
        """Return the immutable initial handshake used by the downstream proxy."""
        connection = self._connection
        if connection is None or connection.initialize_result is None:
            raise RuntimeError(_NOT_CONNECTED)
        return connection.initialize_result

    def __getattr__(self, name: str) -> t.Callable[..., t.Awaitable[t.Any]]:
        """Return one-shot forwarding for a public session operation."""
        if name.startswith("_"):
            raise AttributeError(name)

        def invoke(*args: t.Any, **kwargs: t.Any) -> t.Awaitable[t.Any]:  # noqa: ANN401
            attempt = self._attempt
            if attempt is not None and attempt.done.is_set():
                if self._attempt is attempt:
                    self._attempt = None
                attempt = None
            if name == _PROGRESS_METHOD:
                attempt = None
            return self._invoke(name, attempt, args, kwargs)

        return invoke

    async def _invoke(
        self,
        name: str,
        attempt: _Attempt | None,
        args: tuple[t.Any, ...],
        kwargs: dict[str, t.Any],
    ) -> t.Any:  # noqa: ANN401
        """Run one captured public invocation without replaying it."""
        if attempt is None:
            connection, pending = await self._admit(name)
            if pending is not None:
                connection = await self._wait_for_attempt(pending, name)
        else:
            connection = await self._wait_for_attempt(attempt, name)

        if connection is None:
            raise RuntimeError(_NOT_CONNECTED)
        try:
            if connection.session is None:
                raise RuntimeError(_NOT_CONNECTED)
            return await getattr(connection.session, name)(*args, **kwargs)
        finally:
            with anyio.CancelScope(shield=True):
                await self._release(connection)

    async def _admit(self, name: str) -> tuple[_Connection | None, _Attempt | None]:
        """Lease the healthy generation or join the single shared replacement."""
        lock = self._lock
        if lock is None:
            raise RuntimeError(_NOT_CONNECTED)

        async with lock:
            if self._closing:
                raise RuntimeError(_NOT_CONNECTED)
            if self._permanent_error is not None:
                raise RuntimeError(self._permanent_error)

            connection = self._connection
            if connection is None:
                raise RuntimeError(_NOT_CONNECTED)
            if connection.observer.lost.is_set():
                if self._stateful_operations_used:
                    self._permanent_error = _STATE_LOST
                    self._retire_once(connection)
                    raise RuntimeError(_STATE_LOST)
                if name == _PROGRESS_METHOD:
                    raise RuntimeError(_PROGRESS_SESSION_LOST)

                return None, self._replacement_attempt_locked(connection)

            leased = self._lease_locked(connection, name)
            return leased, None

    def _replacement_attempt_locked(self, stale: _Connection) -> _Attempt:
        """Join or start the single attempt while admission is serialized."""
        attempt = self._attempt
        if attempt is None or attempt.done.is_set():
            attempt = _Attempt()
            self._attempt = attempt
            task_group = self._task_group
            if task_group is None:
                attempt.error = RuntimeError(_NOT_CONNECTED)
                attempt.done.set()
            else:
                task_group.start_soon(self._run_replacement, attempt, stale)
        return attempt

    async def _wait_for_attempt(self, attempt: _Attempt, name: str) -> _Connection:
        """Return the shared replacement or propagate this attempt's exact error."""
        await attempt.done.wait()
        if attempt.error is not None:
            raise attempt.error
        connection = attempt.connection
        if connection is None:
            raise RuntimeError(_NOT_CONNECTED)

        lock = self._lock
        if lock is None:
            raise RuntimeError(_NOT_CONNECTED)
        async with lock:
            if self._closing:
                raise RuntimeError(_NOT_CONNECTED)
            if self._permanent_error is not None:
                raise RuntimeError(self._permanent_error)
            return self._lease_locked(connection, name)

    def _lease_locked(self, connection: _Connection, name: str) -> _Connection:
        """Admit one operation without serializing its upstream execution."""
        if connection.session is None or connection.finished.is_set():
            raise RuntimeError(_NOT_CONNECTED)
        if connection.observer.lost.is_set():
            raise RuntimeError(_LOST_BEFORE_ADMISSION)
        if name in _STATEFUL_METHODS:
            self._stateful_operations_used = True
        if connection.leases == 0:
            connection.drained = anyio.Event()
        connection.leases += 1
        return connection

    async def _release(self, connection: _Connection) -> None:
        """Release one operation lease, waking a replacement only after the drain."""
        lock = self._lock
        if lock is None:
            return
        async with lock:
            connection.leases -= 1
            if connection.leases == 0:
                connection.drained.set()

    async def _run_replacement(self, attempt: _Attempt, stale: _Connection) -> None:
        """Drain and retire the stale owner before starting one replacement owner."""
        try:
            with anyio.CancelScope() as scope:
                attempt.cancel_scope = scope
                try:
                    await self._prepare_replacement(attempt, stale)
                except Exception as exc:  # noqa: BLE001 - resolve every non-cancellation attempt
                    attempt.error = exc
                finally:
                    attempt.cancel_scope = None
        finally:
            await self._finish_replacement(attempt)

    async def _prepare_replacement(self, attempt: _Attempt, stale: _Connection) -> None:
        """Drain the old owner and publish one initialized matching candidate."""
        candidate: _Connection | None = None
        try:
            await stale.drained.wait()
            if self._closing:
                return
            stale.retire.set()
            await stale.finished.wait()

            candidate = _Connection()
            if not await self._start_replacement(candidate):
                attempt.error = RuntimeError(_NOT_CONNECTED)
                return
            await candidate.ready.wait()
            if candidate.error is not None:
                await candidate.finished.wait()
                if isinstance(candidate.error, _HandshakeChangedError):
                    self._permanent_error = _HANDSHAKE_CHANGED
                attempt.error = candidate.error
            elif not await self._publish_replacement(candidate, attempt):
                attempt.error = RuntimeError(_NOT_CONNECTED)
        finally:
            if candidate is not None and self._pending_connection is candidate:
                self._pending_connection = None

    async def _finish_replacement(self, attempt: _Attempt) -> None:
        """Resolve every waiter after the attempt has succeeded, failed or stopped."""
        if attempt.error is None and attempt.connection is None:
            attempt.error = RuntimeError(_NOT_CONNECTED)
        lock = self._lock
        if lock is not None:
            with anyio.CancelScope(shield=True):
                async with lock:
                    attempt.done.set()

    async def _start_replacement(self, candidate: _Connection) -> bool:
        """Start the candidate owner unless shutdown has already begun."""
        lock = self._lock
        task_group = self._task_group
        if lock is None or task_group is None:
            return False
        async with lock:
            if self._closing:
                return False
            self._pending_connection = candidate
            task_group.start_soon(self._run_connection, candidate)
        return True

    async def _publish_replacement(self, candidate: _Connection, attempt: _Attempt) -> bool:
        """Publish only a fully initialized candidate before shutdown starts."""
        lock = self._lock
        if lock is None:
            return False
        async with lock:
            if self._closing:
                return False
            self._connection = candidate
            self._pending_connection = None
            attempt.connection = candidate
        return True

    async def _retire_connection(self, connection: _Connection) -> None:
        """Close a lost generation after admitted operations finish when recovery is refused."""
        await connection.drained.wait()
        connection.retire.set()
        await connection.finished.wait()

    def _retire_once(self, connection: _Connection) -> None:
        """Schedule one drain-and-close for a permanently refused generation."""
        if connection.retire_started:
            return
        connection.retire_started = True
        task_group = self._task_group
        if task_group is not None:
            task_group.start_soon(self._retire_connection, connection)

    async def _run_connection(self, connection: _Connection) -> None:
        """Own one transport and ClientSession from entry through exit."""
        if self._closing:
            connection.error = RuntimeError(_NOT_CONNECTED)
            connection.ready.set()
            connection.finished.set()
            return
        try:
            async with self._connect(connection.observer) as session:
                initialize_result = await session.initialize()
                connection.observer.establish()
                handshake = initialize_result.model_dump(mode="json")
                if self._baseline_handshake is None:
                    self._baseline_handshake = handshake
                elif handshake != self._baseline_handshake:
                    connection.error = _HandshakeChangedError(_HANDSHAKE_CHANGED)
                    connection.ready.set()
                    return

                connection.session = session
                connection.initialize_result = initialize_result
                connection.ready.set()
                await connection.retire.wait()
        except BaseException as exc:
            if not connection.ready.is_set():
                connection.error = exc
                connection.ready.set()
            elif isinstance(exc, Exception):
                logger.debug("Upstream connection ended: %s", exc)

            if isinstance(exc, anyio.get_cancelled_exc_class()) or not isinstance(exc, Exception):
                raise
        finally:
            connection.finished.set()
