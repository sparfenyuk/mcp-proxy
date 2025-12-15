"""Create a local server that proxies requests to a remote server over SSE."""

import asyncio
import inspect
import logging
from functools import partial
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from .httpx_client import custom_httpx_client
from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)


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

    while attempts < max_attempts:
        try:
            stream_kwargs: dict[str, Any] = {
                "url": url,
                "headers": headers,
                "auth": auth,
                "httpx_client_factory": partial(custom_httpx_client, verify_ssl=verify_ssl),
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
                app = await create_proxy_server(session)
                async with stdio_server() as (read_stream, write_stream):
                    await app.run(
                        read_stream,
                        write_stream,
                        app.create_initialization_options(),
                    )
                return
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
