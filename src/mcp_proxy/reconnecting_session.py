"""Client session wrapper that rebuilds the upstream session when the remote drops it.

When a streamable HTTP server restarts, it answers subsequent requests with HTTP 404. The MCP
SDK turns that into an ``McpError`` carrying ``ErrorData(code=32600, message="Session
terminated")`` and keeps the now stale ``mcp-session-id``, so every later call fails the same
way. This wrapper detects that error, rebuilds the upstream connection and retries the call once.
"""

import logging
import typing as t
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from types import TracebackType

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError

if t.TYPE_CHECKING:
    from anyio.abc import TaskGroup
    from typing_extensions import Self

logger = logging.getLogger(__name__)

SessionFactory = t.Callable[[], AbstractAsyncContextManager[ClientSession]]

SESSION_TERMINATED_ERROR_CODE = 32600
"""Code the SDK reports when the remote no longer knows the session (HTTP 404 mid-session)."""

_NOT_CONNECTED = "Upstream session is not connected"


def _is_session_lost(exc: BaseException) -> bool:
    """Report whether the exception means the upstream session no longer exists."""
    return isinstance(exc, McpError) and exc.error.code == SESSION_TERMINATED_ERROR_CODE


@dataclass
class _Connection:
    """State of a single upstream connection, owned by one supervisor task."""

    ready: anyio.Event
    closing: anyio.Event
    session: ClientSession | None = None
    initialize_result: types.InitializeResult | None = None
    error: BaseException | None = None


class ReconnectingClientSession:
    """Duck-typed ``ClientSession`` that re-initializes the upstream session when it is lost.

    Every attribute other than the ones defined here is forwarded to the live upstream session.
    A connection is entered and exited inside a dedicated supervisor task, because anyio cancel
    scopes are bound to the task that created them while the low-level server dispatches every
    request in its own task.
    """

    def __init__(self, connect: SessionFactory) -> None:
        """Store the factory used to open each upstream connection."""
        self._connect = connect
        self._connection: _Connection | None = None
        self._task_group: TaskGroup | None = None
        self._lock: anyio.Lock | None = None

    async def __aenter__(self) -> "Self":
        """Start the supervisor task group and open the first upstream connection."""
        self._lock = anyio.Lock()
        task_group = anyio.create_task_group()
        await task_group.__aenter__()
        self._task_group = task_group
        try:
            await self._reconnect(None)
        except BaseException:
            self._task_group = None
            await task_group.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Close the live connection, then wind down the supervisor task group."""
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.closing.set()
        task_group, self._task_group = self._task_group, None
        if task_group is None:
            return None
        return await task_group.__aexit__(exc_type, exc_value, traceback)

    async def initialize(self) -> types.InitializeResult:
        """Return the handshake result captured when the connection was opened.

        Defining this method shadows :meth:`__getattr__`, so ``create_proxy_server`` does not
        send a second ``initialize`` request over an already initialized session.
        """
        connection = self._connection
        if connection is None or connection.initialize_result is None:
            raise RuntimeError(_NOT_CONNECTED)
        return connection.initialize_result

    def __getattr__(self, name: str) -> t.Callable[..., t.Awaitable[t.Any]]:
        """Forward a session method, retrying once if the upstream session was lost."""
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args: t.Any, **kwargs: t.Any) -> t.Any:  # noqa: ANN401
            session = await self._acquire_session()
            try:
                return await getattr(session, name)(*args, **kwargs)
            except McpError as exc:
                if not _is_session_lost(exc):
                    raise
                logger.warning("Upstream session lost; re-initializing")
            await self._reconnect(session)
            retry_session = await self._acquire_session()
            return await getattr(retry_session, name)(*args, **kwargs)

        return call

    async def _acquire_session(self) -> ClientSession:
        """Return the live upstream session, opening one if an earlier attempt failed."""
        connection = self._connection
        if connection is not None and connection.session is not None:
            return connection.session
        await self._reconnect(None)
        connection = self._connection
        if connection is None or connection.session is None:
            raise RuntimeError(_NOT_CONNECTED)
        return connection.session

    async def _reconnect(self, stale_session: ClientSession | None) -> None:
        """Replace the connection owning ``stale_session``, at most once per outage."""
        if self._lock is None:
            raise RuntimeError(_NOT_CONNECTED)
        async with self._lock:
            current = self._connection
            if current is not None and current.session is not stale_session:
                return
            self._connection = None
            if current is not None:
                current.closing.set()
            if self._task_group is None:
                raise RuntimeError(_NOT_CONNECTED)
            connection = _Connection(ready=anyio.Event(), closing=anyio.Event())
            self._task_group.start_soon(self._run_connection, connection)
            await connection.ready.wait()
            if connection.error is not None:
                raise connection.error
            self._connection = connection

    async def _run_connection(self, connection: _Connection) -> None:
        """Own one upstream connection for its whole lifetime, inside a single task."""
        try:
            async with self._connect() as session:
                connection.initialize_result = await session.initialize()
                connection.session = session
                connection.ready.set()
                await connection.closing.wait()
        except Exception as exc:  # noqa: BLE001 - a dead connection must not kill the proxy
            if connection.ready.is_set():
                logger.debug("Upstream connection ended: %s", exc)
            else:
                connection.error = exc
                connection.ready.set()
