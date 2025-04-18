"""Create a local SSE server that proxies requests to a stdio MCP server."""

import logging
from dataclasses import dataclass
from typing import Literal

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.routing import Mount, Route

from .proxy_server import create_proxy_server
import os
from typing import Callable

logger = logging.getLogger(__name__)


class HeaderAuthMiddleware:
    """Middleware to authenticate requests based on Authorization header."""

    def __init__(
        self,
        app: Callable,
        auth_token: str,
        auth_scheme: str = "Bearer",
    ):
        """Initialize the middleware.

        Args:
            app: The ASGI application.
            auth_token: The token to validate against.
            auth_scheme: The authentication scheme (default: Bearer).
        """
        self.app = app
        self.auth_token = auth_token
        self.auth_scheme = auth_scheme

    async def __call__(self, scope, receive, send):
        """Process the request.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive function.
            send: The ASGI send function.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        
        expected = f"{self.auth_scheme} {self.auth_token}"
        
        if auth_header != expected:
            # Return 401 Unauthorized
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"text/plain")],
            })
            await send({
                "type": "http.response.body",
                "body": b"Unauthorized",
            })
            return
        
        await self.app(scope, receive, send)


@dataclass
class SseServerSettings:
    """Settings for the server."""

    bind_host: str
    port: int
    allow_origins: list[str] | None = None
    auth_token: str | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


def create_starlette_app(
    mcp_server: Server[object],
    *,
    allow_origins: list[str] | None = None,
    auth_token: str | None = None,
    debug: bool = False,
) -> Starlette:
    """Create a Starlette application with optional authentication."""
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    middleware: list[Middleware] = []
    if auth_token:
        middleware.append(
            Middleware(HeaderAuthMiddleware, auth_token=auth_token)
        )
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
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )


async def run_sse_server(
    stdio_params: StdioServerParameters,
    sse_settings: SseServerSettings,
) -> None:
    """Run the stdio client and expose an SSE server.

    Args:
        stdio_params: The parameters for the stdio client that spawns a stdio server.
        sse_settings: The settings for the SSE server that accepts incoming requests.

    """
    async with stdio_client(stdio_params) as streams, ClientSession(*streams) as session:
        logger.debug("Starting proxy server...")
        mcp_server = await create_proxy_server(session)

        # Bind SSE request handling to MCP server
        starlette_app = create_starlette_app(
            mcp_server,
            allow_origins=sse_settings.allow_origins,
            auth_token=sse_settings.auth_token,
            debug=(sse_settings.log_level == "DEBUG"),
        )

        # Configure HTTP server
        config = uvicorn.Config(
            starlette_app,
            host=sse_settings.bind_host,
            port=sse_settings.port,
            log_level=sse_settings.log_level.lower(),
        )
        http_server = uvicorn.Server(config)
        logger.debug(
            "Serving incoming requests on %s:%s",
            sse_settings.bind_host,
            sse_settings.port,
        )
        await http_server.serve()
