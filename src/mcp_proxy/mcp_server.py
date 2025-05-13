"""Create a local SSE server that proxies requests to a stdio MCP server."""

import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)


@dataclass
class MCPServerSettings:
    """Settings for the MCP server."""

    bind_host: str
    port: int
    stateless: bool = False
    allow_origins: list[str] | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


def create_starlette_app(
    mcp_server: Server[object],
    *,
    stateless: bool = False,
    allow_origins: list[str] | None = None,
    debug: bool = False,
) -> Starlette:
    """Create a Starlette application that can serve the mcp server with SSE or Streamable http."""
    logger.debug("Creating Starlette app with stateless: %s and debug: %s", stateless, debug)
    # record the last activity of api
    status = {
        "api_last_activity": datetime.now(timezone.utc).isoformat(),
    }

    def _update_mcp_activity() -> None:
        status.update(
            {
                "api_last_activity": datetime.now(timezone.utc).isoformat(),
            },
        )

    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            _update_mcp_activity()

            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    # Refer: https://github.com/modelcontextprotocol/python-sdk/blob/v1.8.0/examples/servers/simple-streamablehttp-stateless/mcp_simple_streamablehttp_stateless/server.py
    http = StreamableHTTPSessionManager(
        app=mcp_server,
        event_store=None,
        json_response=True,
        stateless=stateless,
    )

    async def handle_streamable_http(scope: Scope, receive: Receive, send: Send) -> None:
        _update_mcp_activity()
        await http.handle_request(scope, receive, send)

    async def handle_status(_: Request) -> Response:
        """Health check and service usage monitoring endpoint.

        Purpose of this handler:
        - Provides a dedicated API endpoint for external health checks.
        - Returns last API activity timestamp to monitor service usage patterns and uptime.
        - Serves as basic infrastructure for potential future service metrics expansion.
        """
        return JSONResponse(status)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        """Context manager for session manager."""
        async with http.run():
            logger.info("Application started with StreamableHTTP session manager!")
            try:
                yield
            finally:
                logger.info("Application shutting down...")

    middleware: list[Middleware] = []
    if allow_origins is not None:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=allow_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        )

    return Starlette(
        debug=debug,
        middleware=middleware,
        routes=[
            Route("/status", endpoint=handle_status),
            Mount("/mcp", app=handle_streamable_http),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        lifespan=lifespan,
    )


async def run_mcp_server(
    stdio_params: StdioServerParameters,
    mcp_settings: MCPServerSettings,
) -> None:
    """Run the stdio client and expose an MCP server.

    Args:
        stdio_params: The parameters for the stdio client that spawns a stdio server.
        mcp_settings: The settings for the MCP server that accepts incoming requests.

    """
    async with stdio_client(stdio_params) as streams, ClientSession(*streams) as session:
        logger.debug("Starting proxy server...")
        mcp_server = await create_proxy_server(session)

        # Bind request handling to MCP server
        starlette_app = create_starlette_app(
            mcp_server,
            stateless=mcp_settings.stateless,
            allow_origins=mcp_settings.allow_origins,
            debug=(mcp_settings.log_level == "DEBUG"),
        )

        # Configure HTTP server
        config = uvicorn.Config(
            starlette_app,
            host=mcp_settings.bind_host,
            port=mcp_settings.port,
            log_level=mcp_settings.log_level.lower(),
        )
        http_server = uvicorn.Server(config)
        logger.debug(
            "Serving incoming requests on %s:%s",
            mcp_settings.bind_host,
            mcp_settings.port,
        )
        await http_server.serve()
