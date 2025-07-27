#
# MCP Foxxy Bridge - MCP Server
#
# Copyright (C) 2024 Billy Bryant
# Portions copyright (C) 2024 Sergey Parfenyuk (original MIT-licensed author)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# MIT License attribution: Portions of this file were originally licensed under the MIT License by Sergey Parfenyuk (2024).
#
"""Create a local SSE server that proxies requests to a stdio MCP server."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server as MCPServerSDK  # Renamed to avoid conflict
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import Receive, Scope, Send

from .proxy_server import create_proxy_server
from .bridge_server import create_bridge_server, shutdown_bridge_server
from .config_loader import BridgeConfiguration

logger = logging.getLogger(__name__)


@dataclass
class MCPServerSettings:
    """Settings for the MCP server."""

    bind_host: str
    port: int
    stateless: bool = False
    allow_origins: list[str] | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


# To store last activity for multiple servers if needed, though status endpoint is global for now.
_global_status: dict[str, Any] = {
    "api_last_activity": datetime.now(timezone.utc).isoformat(),
    "server_instances": {},  # Could be used to store per-instance status later
}


def _update_global_activity() -> None:
    _global_status["api_last_activity"] = datetime.now(timezone.utc).isoformat()


def _find_available_port(host: str, requested_port: int) -> int:
    """Find an available port starting from the requested port."""
    import socket
    
    actual_port = requested_port
    max_attempts = 100  # Try up to 100 ports
    
    for attempt in range(max_attempts):
        try:
            with socket.socket() as s:
                s.bind((host, actual_port))
                # Port is available, break out of loop
                if actual_port != requested_port:
                    logger.info("Port %d was in use, using port %d instead", requested_port, actual_port)
                return actual_port
        except OSError:
            # Port is in use, try the next one
            actual_port += 1
    
    # If we exhausted all attempts, fall back to system-assigned port
    with socket.socket() as s:
        s.bind(('', 0))
        actual_port = s.getsockname()[1]
    logger.warning("Could not find available port in range %d-%d, using system-assigned port %d", 
                  requested_port, requested_port + max_attempts - 1, actual_port)
    return actual_port


async def _handle_status(_: Request) -> Response:
    """Global health check and service usage monitoring endpoint."""
    return JSONResponse(_global_status)


def create_single_instance_routes(
    mcp_server_instance: MCPServerSDK[object],
    *,
    stateless_instance: bool,
) -> tuple[list[BaseRoute], StreamableHTTPSessionManager]:  # Return the manager itself
    """Create Starlette routes and the HTTP session manager for a single MCP server instance."""
    logger.debug(
        "Creating routes for a single MCP server instance (stateless: %s)",
        stateless_instance,
    )

    sse_transport = SseServerTransport("/messages/")
    http_session_manager = StreamableHTTPSessionManager(
        app=mcp_server_instance,
        event_store=None,
        json_response=True,
        stateless=stateless_instance,
    )

    async def handle_sse_instance(request: Request) -> Response:
        async with sse_transport.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            _update_global_activity()
            await mcp_server_instance.run(
                read_stream,
                write_stream,
                mcp_server_instance.create_initialization_options(),
            )
        return Response()

    async def handle_streamable_http_instance(scope: Scope, receive: Receive, send: Send) -> None:
        _update_global_activity()
        await http_session_manager.handle_request(scope, receive, send)

    routes = [
        Mount("/mcp", app=handle_streamable_http_instance),
        Route("/sse", endpoint=handle_sse_instance),
        Mount("/messages/", app=sse_transport.handle_post_message),
    ]
    return routes, http_session_manager


async def run_mcp_server(
    mcp_settings: MCPServerSettings,
    default_server_params: StdioServerParameters | None = None,
    named_server_params: dict[str, StdioServerParameters] | None = None,
) -> None:
    """Run stdio client(s) and expose an MCP server with multiple possible backends."""
    if named_server_params is None:
        named_server_params = {}

    all_routes: list[BaseRoute] = [
        Route("/status", endpoint=_handle_status),  # Global status endpoint
    ]
    # Use AsyncExitStack to manage lifecycles of multiple components
    async with contextlib.AsyncExitStack() as stack:
        # Manage lifespans of all StreamableHTTPSessionManagers
        @contextlib.asynccontextmanager
        async def combined_lifespan(_app: Starlette) -> AsyncIterator[None]:
            logger.info("Main application lifespan starting...")
            # All http_session_managers' .run() are already entered into the stack
            yield
            logger.info("Main application lifespan shutting down...")

        # Setup default server if configured
        if default_server_params:
            logger.info(
                "Setting up default server: %s %s",
                default_server_params.command,
                " ".join(default_server_params.args),
            )
            stdio_streams = await stack.enter_async_context(stdio_client(default_server_params))
            session = await stack.enter_async_context(ClientSession(*stdio_streams))
            proxy = await create_proxy_server(session)

            instance_routes, http_manager = create_single_instance_routes(
                proxy,
                stateless_instance=mcp_settings.stateless,
            )
            await stack.enter_async_context(http_manager.run())  # Manage lifespan by calling run()
            all_routes.extend(instance_routes)
            _global_status["server_instances"]["default"] = "configured"

        # Setup named servers
        for name, params in named_server_params.items():
            logger.info(
                "Setting up named server '%s': %s %s",
                name,
                params.command,
                " ".join(params.args),
            )
            stdio_streams_named = await stack.enter_async_context(stdio_client(params))
            session_named = await stack.enter_async_context(ClientSession(*stdio_streams_named))
            proxy_named = await create_proxy_server(session_named)

            instance_routes_named, http_manager_named = create_single_instance_routes(
                proxy_named,
                stateless_instance=mcp_settings.stateless,
            )
            await stack.enter_async_context(
                http_manager_named.run(),
            )  # Manage lifespan by calling run()

            # Mount these routes under /servers/<name>/
            server_mount = Mount(f"/servers/{name}", routes=instance_routes_named)
            all_routes.append(server_mount)
            _global_status["server_instances"][name] = "configured"

        if not default_server_params and not named_server_params:
            logger.error("No servers configured to run.")
            return

        middleware: list[Middleware] = []
        if mcp_settings.allow_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=mcp_settings.allow_origins,
                    allow_methods=["*"],
                    allow_headers=["*"],
                ),
            )

        starlette_app = Starlette(
            debug=(mcp_settings.log_level == "DEBUG"),
            routes=all_routes,
            middleware=middleware,
            lifespan=combined_lifespan,
        )

        starlette_app.router.redirect_slashes = False

        # Find an available port
        actual_port = _find_available_port(mcp_settings.bind_host, mcp_settings.port)
        
        config = uvicorn.Config(
            starlette_app,
            host=mcp_settings.bind_host,
            port=actual_port,
            log_level=mcp_settings.log_level.lower(),
        )
        http_server = uvicorn.Server(config)

        # Print out the SSE URLs for all configured servers
        base_url = f"http://{mcp_settings.bind_host}:{actual_port}"
        sse_urls = []

        # Add default server if configured
        if default_server_params:
            sse_urls.append(f"{base_url}/sse")

        # Add named servers
        sse_urls.extend([f"{base_url}/servers/{name}/sse" for name in named_server_params])

        # Display the SSE URLs prominently
        if sse_urls:
            # Using print directly for user visibility, with noqa to ignore linter warnings
            logger.info("Serving MCP Servers via SSE:")
            for url in sse_urls:
                logger.info("  - %s", url)

        logger.debug(
            "Serving incoming MCP requests on %s:%s",
            mcp_settings.bind_host,
            mcp_settings.port,
        )
        await http_server.serve()


async def run_bridge_server(
    mcp_settings: MCPServerSettings,
    bridge_config: BridgeConfiguration,
) -> None:
    """Run the bridge server that aggregates multiple MCP servers.
    
    Args:
        mcp_settings: Server settings for the bridge.
        bridge_config: Configuration for the bridge and all MCP servers.
    """
    logger.info("Starting MCP Foxxy Bridge server...")
    
    # Global status for bridge server
    _global_status["server_instances"] = {}
    for name, server_config in bridge_config.servers.items():
        _global_status["server_instances"][name] = {
            "enabled": server_config.enabled,
            "command": server_config.command,
            "status": "configuring"
        }
    
    all_routes: list[BaseRoute] = [
        Route("/status", endpoint=_handle_status),
    ]
    
    # Use AsyncExitStack to manage bridge server lifecycle
    async with contextlib.AsyncExitStack() as stack:
        @contextlib.asynccontextmanager
        async def bridge_lifespan(_app: Starlette) -> AsyncIterator[None]:
            logger.info("Bridge application lifespan starting...")
            try:
                yield
            finally:
                logger.info("Bridge application lifespan shutting down...")
                # Brief cleanup delay
                await asyncio.sleep(0.05)
        
        # Create and configure the bridge server
        bridge_server = await create_bridge_server(bridge_config)
        
        # Register cleanup on exit
        stack.callback(lambda: asyncio.create_task(shutdown_bridge_server(bridge_server)))
        
        # Create routes for the bridge server
        instance_routes, http_manager = create_single_instance_routes(
            bridge_server,
            stateless_instance=mcp_settings.stateless,
        )
        await stack.enter_async_context(http_manager.run())
        all_routes.extend(instance_routes)
        
        # Update server status
        server_manager = getattr(bridge_server, '_server_manager', None)
        if server_manager:
            server_statuses = server_manager.get_server_status()
            for name, status in server_statuses.items():
                _global_status["server_instances"][name]["status"] = status["status"]
        
        # Setup middleware
        middleware: list[Middleware] = []
        if mcp_settings.allow_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=mcp_settings.allow_origins,
                    allow_methods=["*"],
                    allow_headers=["*"],
                ),
            )
        
        # Create Starlette app
        starlette_app = Starlette(
            debug=(mcp_settings.log_level == "DEBUG"),
            routes=all_routes,
            middleware=middleware,
            lifespan=bridge_lifespan,
        )
        
        starlette_app.router.redirect_slashes = False
        
        # Find an available port
        actual_port = _find_available_port(mcp_settings.bind_host, mcp_settings.port)
        
        # Configure uvicorn server with the available port
        config = uvicorn.Config(
            starlette_app,
            host=mcp_settings.bind_host,
            port=actual_port,
            log_level=mcp_settings.log_level.lower(),
        )
        http_server = uvicorn.Server(config)
        
        # Display connection information
        base_url = f"http://{mcp_settings.bind_host}:{actual_port}"
        logger.info("MCP Foxxy Bridge server is ready!")
        logger.info("SSE endpoint: %s/sse", base_url)
        logger.info("Status endpoint: %s/status", base_url)
        logger.info("Bridging %d configured servers", len(bridge_config.servers))
        
        # Setup graceful shutdown
        shutdown_event = asyncio.Event()
        
        def signal_handler(signum, frame):
            logger.info("Received signal %d, initiating graceful shutdown...", signum)
            shutdown_event.set()
        
        # Install signal handlers (but don't let them propagate to child processes)
        old_sigint_handler = signal.signal(signal.SIGINT, signal_handler)
        old_sigterm_handler = signal.signal(signal.SIGTERM, signal_handler)
        
        try:
            # Start server in a task so we can handle shutdown
            server_task = asyncio.create_task(http_server.serve())
            shutdown_task = asyncio.create_task(shutdown_event.wait())
            
            # Wait for either server completion or shutdown signal
            done, pending = await asyncio.wait(
                [server_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # If shutdown was triggered, cancel the server
            if shutdown_task in done:
                logger.info("Shutdown requested, stopping server...")
                server_task.cancel()
                try:
                    await asyncio.wait_for(server_task, timeout=0.5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            
            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                    
        except Exception as e:
            logger.error("Server error: %s", str(e))
        finally:
            # Restore original signal handlers
            signal.signal(signal.SIGINT, old_sigint_handler)
            signal.signal(signal.SIGTERM, old_sigterm_handler)
            
            # Force close any remaining HTTP connections
            try:
                await http_server.shutdown()
            except Exception:
                pass
            logger.info("Bridge server shutdown complete")
