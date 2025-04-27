"""Create a local SSE server that proxies requests to a stdio MCP server."""

import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal
from uuid import uuid4

import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http import StreamableHTTPServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)
# Global task group that will be initialized in the lifespan
task_group = None

MCP_SESSION_ID_HEADER = "mcp-session-id"


@contextlib.asynccontextmanager
async def lifespan(_: Starlette) -> AsyncIterator[None]:
    """Application lifespan context manager for managing task group."""
    global task_group  # noqa: PLW0603

    async with anyio.create_task_group() as tg:
        task_group = tg
        logger.info("Application started, task group initialized!")
        try:
            yield
        finally:
            logger.info("Application shutting down, cleaning up resources...")
            if task_group:
                tg.cancel_scope.cancel()
                task_group = None
            logger.info("Resources cleaned up successfully.")


@dataclass
class MCPServerSettings:
    """Settings for the MCP server."""

    bind_host: str
    port: int
    allow_origins: list[str] | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


def create_starlette_app(
    mcp_server: Server[object],
    *,
    allow_origins: list[str] | None = None,
    debug: bool = False,
) -> Starlette:
    """Create a Starlette application that can server the provied mcp server with SSE."""
    sse = SseServerTransport("/messages/")

    # We need to store the server instances between requests
    server_instances = {}
    # Lock to prevent race conditions when creating new sessions
    session_creation_lock = anyio.Lock()

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

    # Refer: https://github.com/modelcontextprotocol/python-sdk/blob/5d8eaf77be00dbd9b33a7fe1e38cb0da77e49401/examples/servers/simple-streamablehttp/mcp_simple_streamablehttp/server.py
    async def handle_streamable_http(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        request_mcp_session_id = request.headers.get(MCP_SESSION_ID_HEADER)
        if request_mcp_session_id is not None and request_mcp_session_id in server_instances:
            transport = server_instances[request_mcp_session_id]
            logger.debug("Session already exists, handling request directly")
            await transport.handle_request(scope, receive, send)
        elif request_mcp_session_id is None:
            # try to establish new session
            logger.debug("Creating new transport")
            # Use lock to prevent race conditions when creating new sessions
            async with session_creation_lock:
                new_session_id = uuid4().hex
                http_transport = StreamableHTTPServerTransport(
                    mcp_session_id=new_session_id,
                    is_json_response_enabled=True,
                )
                server_instances[http_transport.mcp_session_id] = http_transport
                logger.info("Created new transport with session ID: %s", new_session_id)

                async def run_server(task_status=None) -> None:  # noqa: ANN001
                    async with http_transport.connect() as streams:
                        read_stream, write_stream = streams
                        if task_status:
                            task_status.started()
                        await mcp_server.run(
                            read_stream,
                            write_stream,
                            mcp_server.create_initialization_options(),
                        )

                if not task_group:
                    raise RuntimeError("Task group is not initialized")

                await task_group.start(run_server)

                # Handle the HTTP request and return the response
                await http_transport.handle_request(scope, receive, send)
        else:
            response = Response(
                "Bad Request: No valid session ID provided",
                status_code=HTTPStatus.BAD_REQUEST,
            )
            await response(scope, receive, send)

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
