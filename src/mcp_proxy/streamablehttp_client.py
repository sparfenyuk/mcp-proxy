"""Create a local server that proxies requests to a remote server over SSE."""

import asyncio
import inspect
import logging
import os
import sys
from functools import partial
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from .httpx_client import custom_httpx_client
from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)

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
        return 20.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid MCP_PROXY_CALL_TIMEOUT_S=%s; using default 20s", raw)
        return 20.0
    if value <= 0:
        return None
    return value


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

    while attempts < max_attempts:
        # If our stdio is already closed, there's nothing useful we can do.
        # Exiting quietly avoids noisy ExceptionGroup tracebacks.
        if _stdin_is_closed():
            logger.debug("stdio already closed; exiting StreamableHTTP client loop")
            return

        try:
            stream_kwargs: dict[str, Any] = {
                "url": url,
                "headers": headers,
                "auth": auth,
                "httpx_client_factory": partial(
                    custom_httpx_client,
                    verify_ssl=verify_ssl,
                    error_queue=error_queue,
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

            async with (
                streamablehttp_client(
                    **stream_kwargs,
                ) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                # propagate retry budget to downstream handlers (used in CallTool wrapper)
                session._retry_attempts = retry_attempts  # type: ignore[attr-defined]
                session._http_error_queue = error_queue  # type: ignore[attr-defined]
                session._proxy_call_timeout_s = call_timeout_s  # type: ignore[attr-defined]
                app = await create_proxy_server(session)
                try:
                    async with stdio_server() as (read_stream, write_stream):
                        await app.run(
                            read_stream,
                            write_stream,
                            app.create_initialization_options(),
                        )
                except ValueError as exc:
                    if _is_closed_stdio_error(exc) or _stdin_is_closed():
                        logger.debug("stdio closed during startup; exiting")
                        return
                    raise
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
                logger.debug("stdio closed; exiting")
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
