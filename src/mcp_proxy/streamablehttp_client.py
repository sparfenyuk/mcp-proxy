"""Create a local server that proxies requests to a remote server over SSE."""

import asyncio
import inspect
import logging
import os
import re
import sys
import time
from contextlib import AsyncExitStack
from functools import partial
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from .httpx_client import custom_httpx_client
from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)
_STREAM_LOGGER_NAME = "mcp.client.streamable_http"
_JSONRPC_ID_RE = re.compile(r"id=(\d+)")
_JSONRPC_METHOD_RE = re.compile(r"JSONRPCRequest\(method='([^']+)'")
_RECONNECT_ATTEMPTS_RE = re.compile(r"max reconnection attempts \\((\\d+)\\)")

try:
    _BaseExceptionGroup: type[BaseException] | tuple[type[BaseException], ...] = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:  # pragma: no cover (Python < 3.11)
    _BaseExceptionGroup = ()


def _stdin_is_closed() -> bool:
    try:
        return sys.stdin is None or sys.stdin.closed
    except Exception:  # noqa: BLE001
        return False


def _is_closed_stdio_error(exc: BaseException) -> bool:
    # If Codex terminates a tool call, it may close the stdio pipes immediately.
    # The MCP stdio server can raise `ValueError: I/O operation on closed file` on startup.
    # Treat that as a clean shutdown signal rather than a retryable remote failure.
    stack: list[BaseException] = [exc]
    seen: set[int] = set()

    while stack:
        current = stack.pop()
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)

        if isinstance(current, ValueError) and "I/O operation on closed file" in str(current):
            return True

        # Python 3.11+ ExceptionGroup/BaseExceptionGroup
        if _BaseExceptionGroup and isinstance(current, _BaseExceptionGroup):  # type: ignore[arg-type]
            try:
                stack.extend(list(getattr(current, "exceptions")))  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

        for next_exc in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if isinstance(next_exc, BaseException):
                stack.append(next_exc)

    return False


def _parse_call_timeout_s() -> float | None:
    raw = os.getenv("MCP_PROXY_CALL_TIMEOUT_S")
    if raw is None:
        return 15.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid MCP_PROXY_CALL_TIMEOUT_S=%s; using default 20s", raw)
        return 20.0
    if value <= 0:
        return None
    return value


def _parse_reinit_timeout_s() -> float | None:
    raw = os.getenv("MCP_PROXY_REINIT_TIMEOUT_S")
    if raw is None:
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid MCP_PROXY_REINIT_TIMEOUT_S=%s; using default 5s", raw)
        return 5.0
    if value <= 0:
        return None
    return value


def _parse_reconnect_timeout_s() -> float:
    raw = os.getenv("MCP_PROXY_RECONNECT_TIMEOUT_S")
    if raw is None:
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid MCP_PROXY_RECONNECT_TIMEOUT_S=%s; using default 5s", raw)
        return 5.0
    if value <= 0:
        return 5.0
    return value


class _StreamableHttpTelemetryHandler(logging.Handler):
    def __init__(self, request_state: dict[str, Any]) -> None:
        super().__init__()
        self._request_state = request_state

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return

        now = time.monotonic()

        if "Sending client message:" in message and "JSONRPCRequest" in message:
            method_match = _JSONRPC_METHOD_RE.search(message)
            id_match = _JSONRPC_ID_RE.search(message)
            if method_match:
                self._request_state["last_sent_method"] = method_match.group(1)
            if id_match:
                self._request_state["last_sent_id"] = int(id_match.group(1))
            self._request_state["last_sent_ts"] = now
            return

        if "SSE message:" in message:
            id_match = _JSONRPC_ID_RE.search(message)
            if id_match:
                self._request_state["last_sse_id"] = int(id_match.group(1))
            self._request_state["last_sse_ts"] = now
            return

        if "GET SSE connection established" in message:
            self._request_state["last_sse_connect_ts"] = now
            return

        if "GET stream disconnected" in message:
            self._request_state["last_sse_disconnect_ts"] = now
            self._request_state["last_sse_disconnect_msg"] = message
            self._request_state["sse_disconnect_count"] = (
                self._request_state.get("sse_disconnect_count", 0) + 1
            )
            return

        if "GET stream error:" in message:
            self._request_state["last_sse_error_ts"] = now
            self._request_state["last_sse_error_msg"] = message
            if "max reconnection attempts" in message:
                match = _RECONNECT_ATTEMPTS_RE.search(message)
                if match:
                    self._request_state["last_sse_reconnect_max"] = int(match.group(1))
                self._request_state["last_sse_reconnect_exhausted_ts"] = now


class _ReconnectableSession:
    def __init__(
        self,
        *,
        stream_kwargs: dict[str, Any],
        reconnect_timeout_s: float,
    ) -> None:
        self._stream_kwargs = stream_kwargs
        self._reconnect_timeout_s = reconnect_timeout_s
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def _acquire_lock(self, action: str) -> None:
        timeout_s = getattr(self, "_reconnect_timeout_s", None)
        if self._lock.locked():
            logger.warning("streamablehttp %s waiting for lock", action)
        if timeout_s:
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=timeout_s)
            except asyncio.TimeoutError:
                logger.warning(
                    "streamablehttp %s timed out waiting for lock after %.1fs",
                    action,
                    timeout_s,
                )
                raise
        else:
            await self._lock.acquire()

    async def _open_session(self) -> ClientSession:
        start = time.monotonic()
        logger.info("streamablehttp rebuild: opening new transport")
        stack = AsyncExitStack()
        logger.info("streamablehttp rebuild: entering streamablehttp_client")
        read, write, _ = await asyncio.wait_for(
            stack.enter_async_context(streamablehttp_client(**self._stream_kwargs)),
            timeout=self._reconnect_timeout_s,
        )
        logger.info("streamablehttp rebuild: streamablehttp_client entered (%.0fms)", (time.monotonic() - start) * 1000)
        logger.info("streamablehttp rebuild: entering ClientSession")
        session = await asyncio.wait_for(
            stack.enter_async_context(ClientSession(read, write)),
            timeout=self._reconnect_timeout_s,
        )
        logger.info("streamablehttp rebuild: ClientSession entered (%.0fms)", (time.monotonic() - start) * 1000)
        self._stack = stack
        self._session = session
        return session

    async def open(self) -> ClientSession:
        logger.info("streamablehttp rebuild: open requested")
        await self._acquire_lock("open")
        try:
            logger.info("streamablehttp rebuild: open lock acquired")
            if self._session is None:
                return await self._open_session()
            return self._session
        finally:
            self._lock.release()

    async def rebuild(self) -> ClientSession:
        logger.info("streamablehttp rebuild: rebuild requested")
        await self._acquire_lock("rebuild")
        try:
            logger.info("streamablehttp rebuild: rebuild lock acquired")
            await self._close_locked()
            logger.info("streamablehttp rebuild: opening new transport (rebuild)")
            timeout_s = getattr(self, "_reconnect_timeout_s", None)
            if timeout_s:
                try:
                    return await asyncio.wait_for(self._open_session(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    logger.warning(
                        "streamablehttp rebuild: opening new transport timed out after %.1fs",
                        timeout_s,
                    )
                    raise
            return await self._open_session()
        finally:
            self._lock.release()

    async def close(self) -> None:
        await self._acquire_lock("close")
        try:
            await self._close_locked()
        finally:
            self._lock.release()

    async def _close_locked(self) -> None:
        if self._stack is not None:
            logger.info("streamablehttp rebuild: closing existing transport")
            close_start = time.monotonic()
            try:
                await self._stack.aclose()
            finally:
                elapsed_ms = (time.monotonic() - close_start) * 1000
                logger.info("streamablehttp rebuild: existing transport close returned (%.0fms)", elapsed_ms)
        self._stack = None
        self._session = None

    def __getattr__(self, name: str) -> Any:
        if self._session is None:
            raise AttributeError(name)
        return getattr(self._session, name)


async def run_streamablehttp_client(
    url: str,
    headers: dict[str, Any] | None = None,
    auth: httpx.Auth | None = None,
    verify_ssl: bool | str | None = None,
    retry_attempts: int = 0,
) -> None:
    """Run the StreamableHTTP client.

    Args:
        url: The URL to connect to.
        headers: Headers for connecting to MCP server.
        auth: Optional authentication for the HTTP client.
        verify_ssl: Control SSL verification. Use False to disable
            or a path to a certificate bundle.
        retry_attempts: Number of retries for the remote MCP connection on failure.
    """
    attempts = 0
    max_attempts = 1 + max(0, retry_attempts)
    error_queue: asyncio.Queue[httpx.HTTPStatusError] = asyncio.Queue(maxsize=32)
    call_timeout_s = _parse_call_timeout_s()
    reinit_timeout_s = _parse_reinit_timeout_s()
    reconnect_timeout_s = _parse_reconnect_timeout_s()
    if call_timeout_s is None:
        logger.info("Per-call timeout disabled (MCP_PROXY_CALL_TIMEOUT_S<=0); retries depend on other errors")
    else:
        logger.info(
            "Per-call timeout set to %.1fs (MCP_PROXY_CALL_TIMEOUT_S); retry attempts=%s; url=%s",
            call_timeout_s,
            retry_attempts,
            url,
        )
    if reinit_timeout_s is None:
        logger.info("Re-init timeout disabled (MCP_PROXY_REINIT_TIMEOUT_S<=0); using per-call timeout")
    else:
        logger.info(
            "Re-init timeout set to %.1fs (MCP_PROXY_REINIT_TIMEOUT_S); url=%s",
            reinit_timeout_s,
            url,
        )
    logger.info(
        "Reconnect timeout set to %.1fs (MCP_PROXY_RECONNECT_TIMEOUT_S)",
        reconnect_timeout_s,
    )

    while attempts < max_attempts:
        # If our stdio is already closed, there's nothing useful we can do.
        # Exiting quietly avoids noisy ExceptionGroup tracebacks.
        if _stdin_is_closed():
            logger.info("stdio already closed; exiting (caller likely cancelled/timeout)")
            return

        try:
            request_state: dict[str, Any] = {}
            stream_logger = logging.getLogger(_STREAM_LOGGER_NAME)
            telemetry_handler = _StreamableHttpTelemetryHandler(request_state)
            stream_logger.addHandler(telemetry_handler)
            stream_kwargs: dict[str, Any] = {
                "url": url,
                "headers": headers,
                "auth": auth,
                "httpx_client_factory": partial(
                    custom_httpx_client,
                    verify_ssl=verify_ssl,
                    error_queue=error_queue,
                    request_state=request_state,
                ),
                # Don't terminate the whole proxy if the server->client GET stream drops.
                # QuickMemory (and some other servers) can intermittently fail the GET stream
                # while still accepting request/response POSTs. Exiting here causes Codex to see
                # `tools/call failed: Transport closed`.
                "terminate_on_close": False,
            }

            # Newer MCP Python SDKs accept reconnect_attempts; older ones don't.
            # Keep compatibility across versions by only passing supported kwargs.
            try:
                params = inspect.signature(streamablehttp_client).parameters
                supports_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                if "reconnect_attempts" in params or supports_kwargs:
                    # align SDK reconnect attempts with our retry budget (+1 for initial)
                    stream_kwargs["reconnect_attempts"] = max_attempts
            except (TypeError, ValueError):
                # If the callable is not introspectable, don't pass optional kwargs.
                pass

            reconnectable = _ReconnectableSession(
                stream_kwargs=stream_kwargs,
                reconnect_timeout_s=reconnect_timeout_s,
            )
            # Ensure the initial transport/session is created so retries behave as expected.
            await reconnectable.open()
            reconnectable._reconnect_timeout_s = reconnect_timeout_s  # type: ignore[attr-defined]
            # propagate retry budget to downstream handlers (used in CallTool wrapper)
            reconnectable._retry_attempts = retry_attempts  # type: ignore[attr-defined]
            reconnectable._http_error_queue = error_queue  # type: ignore[attr-defined]
            reconnectable._proxy_call_timeout_s = call_timeout_s  # type: ignore[attr-defined]
            reconnectable._proxy_reinit_timeout_s = reinit_timeout_s  # type: ignore[attr-defined]
            reconnectable._http_request_state = request_state  # type: ignore[attr-defined]
            try:
                app = await create_proxy_server(reconnectable)
                async with stdio_server() as (read_stream, write_stream):
                    await app.run(
                        read_stream,
                        write_stream,
                        app.create_initialization_options(),
                    )
            except ValueError as exc:
                if _is_closed_stdio_error(exc) or _stdin_is_closed():
                    logger.info("stdio closed during startup; exiting (caller likely cancelled/timeout)")
                    return
                raise
            finally:
                stream_logger.removeHandler(telemetry_handler)
                await reconnectable.close()
            return
        except asyncio.CancelledError as exc:
            # Some cancellations occur while stdio is still alive (e.g., sibling task cancels
            # a connect attempt). If stdio is open, treat this as retryable.
            if _stdin_is_closed():
                raise
            attempts += 1
            if attempts >= max_attempts:
                raise
            logger.warning(
                "StreamableHTTP cancelled while stdio open; retrying (%s/%s); error=%s",
                attempts,
                max_attempts - 1,
                exc,
            )
            await asyncio.sleep(0.5)
            continue
        except httpx.HTTPStatusError as exc:
            attempts += 1
            if attempts >= max_attempts:
                raise
            logger.warning(
                "Remote StreamableHTTP HTTP status %s; forcing re-init (%s/%s); url=%s",
                exc.response.status_code if exc.response else "unknown",
                attempts,
                max_attempts - 1,
                url,
            )
            await asyncio.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            if _is_closed_stdio_error(exc) or _stdin_is_closed():
                logger.info("stdio closed; exiting (caller likely cancelled/timeout)")
                return
            attempts += 1
            if attempts >= max_attempts:
                raise
            logger.warning(
                "Remote StreamableHTTP failure; attempt %s/%s; url=%s; error=%s",
                attempts,
                max_attempts - 1,
                url,
                exc,
            )
            await asyncio.sleep(0.5)
