"""Create a local SSE server that proxies requests to a stdio MCP server."""

import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Literal

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

from mcp import types as mcp_types

from .config_loader import ServerConfig
from .proxy_server import create_proxy_server
from .rate_limiter import ServerRateLimiter, create_rate_limited_call_tool

logger = logging.getLogger(__name__)

# Paths that bypass API key authentication
_PUBLIC_PATHS: Final[frozenset[str]] = frozenset({"/health", "/status"})


class APIKeyMiddleware:
    """Starlette middleware that validates Bearer token authentication.

    Skips validation for health/status endpoints and CORS preflight requests.
    When no api_key is configured, all requests pass through (backward compatible).
    """

    def __init__(self, app: Any, *, api_key: str) -> None:  # noqa: ANN401
        """Initialize middleware with ASGI app and API key."""
        self._app = app
        self._api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Validate API key on HTTP requests."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")

        # Skip auth for public paths and CORS preflight
        if path in _PUBLIC_PATHS or method == "OPTIONS":
            await self._app(scope, receive, send)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers", []))
        auth_value = headers.get(b"authorization", b"").decode()

        if auth_value == f"Bearer {self._api_key}":
            await self._app(scope, receive, send)
            return

        # Reject
        response = JSONResponse(
            {"error": "Unauthorized", "message": "Invalid or missing API key"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


DEFAULT_EXPOSE_HEADERS: Final[tuple[str, ...]] = ("mcp-session-id",)


def _default_expose_headers() -> list[str]:
    return list(DEFAULT_EXPOSE_HEADERS)


@dataclass
class MCPServerSettings:
    """Settings for the MCP server."""

    bind_host: str
    port: int
    stateless: bool = False
    allow_origins: list[str] | None = None
    expose_headers: list[str] = field(default_factory=_default_expose_headers)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_key: str | None = None


# To store last activity for multiple servers if needed, though status endpoint is global for now.
_global_status: dict[str, Any] = {
    "api_last_activity": datetime.now(timezone.utc).isoformat(),
    "server_instances": {},  # Could be used to store per-instance status later
}


def _update_global_activity() -> None:
    _global_status["api_last_activity"] = datetime.now(timezone.utc).isoformat()


class _ASGIEndpointAdapter:
    """Wrap a coroutine function into an ASGI application."""

    def __init__(self, endpoint: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self._endpoint = endpoint

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._endpoint(scope, receive, send)


HTTP_METHODS = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"]


async def _handle_status(_: Request) -> Response:
    """Global health check and service usage monitoring endpoint."""
    healthy_count = sum(
        1
        for s in _global_status["server_instances"].values()
        if isinstance(s, dict) and s.get("status") == "running"
    )
    total_count = len(_global_status["server_instances"])
    is_healthy = healthy_count > 0 or total_count == 0
    status_code = 200 if is_healthy else 503
    return JSONResponse(
        {
            **_global_status,
            "healthy": is_healthy,
            "servers_running": healthy_count,
            "servers_total": total_count,
        },
        status_code=status_code,
    )


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
        updated_scope = scope
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path and path.rstrip("/") == "/mcp" and not path.endswith("/"):
                updated_scope = dict(scope)
                normalized_path = path + "/"
                logger.debug(
                    "Normalized request path from '%s' to '%s' without redirect",
                    path,
                    normalized_path,
                )
                updated_scope["path"] = normalized_path

                raw_path = scope.get("raw_path")
                if raw_path:
                    if b"?" in raw_path:
                        path_part, query_part = raw_path.split(b"?", 1)
                        updated_scope["raw_path"] = path_part.rstrip(b"/") + b"/?" + query_part
                    else:
                        updated_scope["raw_path"] = raw_path.rstrip(b"/") + b"/"

        await http_session_manager.handle_request(updated_scope, receive, send)

    routes = [
        Route(
            "/mcp",
            endpoint=_ASGIEndpointAdapter(handle_streamable_http_instance),
            methods=HTTP_METHODS,
            include_in_schema=False,
        ),
        Mount("/mcp", app=handle_streamable_http_instance),
        Route("/sse", endpoint=handle_sse_instance),
        Mount("/messages/", app=sse_transport.handle_post_message),
    ]
    return routes, http_session_manager


async def run_mcp_server(
    mcp_settings: MCPServerSettings,
    default_server_params: StdioServerParameters | None = None,
    named_server_params: dict[str, StdioServerParameters] | None = None,
    named_server_configs: dict[str, ServerConfig] | None = None,
) -> None:
    """Run stdio client(s) and expose an MCP server with multiple possible backends."""
    # Support both old-style named_server_params and new ServerConfig
    # Use a local copy to avoid modifying the caller's dictionary
    effective_configs: dict[str, ServerConfig] = (named_server_configs or {}).copy()
    if named_server_params:
        for name, params in named_server_params.items():
            if name not in effective_configs:
                effective_configs[name] = ServerConfig(stdio_params=params)

    all_routes: list[BaseRoute] = [
        Route("/status", endpoint=_handle_status),  # Global status endpoint
        Route("/health", endpoint=_handle_status),  # Health check alias
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
            _global_status["server_instances"]["default"] = {
                "status": "running",
                "command": default_server_params.command,
            }

        # Setup named servers
        failed_servers: list[str] = []
        for name, server_config in effective_configs.items():
            params = server_config.stdio_params
            try:
                logger.info(
                    "Setting up named server '%s': %s %s",
                    name,
                    params.command,
                    " ".join(params.args),
                )
                stdio_streams_named = await stack.enter_async_context(stdio_client(params))
                session_named = await stack.enter_async_context(ClientSession(*stdio_streams_named))
                proxy_named = await create_proxy_server(session_named)

                # Apply rate limiting if configured
                if mcp_types.CallToolRequest in proxy_named.request_handlers:
                    rate_limiter = ServerRateLimiter(
                        max_concurrent=server_config.max_concurrent,
                        max_wait_seconds=server_config.max_wait_seconds,
                    )
                    original_handler = proxy_named.request_handlers[mcp_types.CallToolRequest]
                    proxy_named.request_handlers[mcp_types.CallToolRequest] = (
                        create_rate_limited_call_tool(
                            original_handler,
                            rate_limiter,
                            name,
                        )
                    )
                    logger.info(
                        "Rate limiting enabled for '%s': max_concurrent=%d, max_wait=%.1fs",
                        name,
                        server_config.max_concurrent,
                        server_config.max_wait_seconds,
                    )

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
                _global_status["server_instances"][name] = {
                    "status": "running",
                    "command": params.command,
                }
            except Exception:
                logger.exception(
                    "Failed to start named server '%s'. Skipping this server.",
                    name,
                )
                _global_status["server_instances"][name] = {
                    "status": "failed",
                    "command": params.command,
                }
                failed_servers.append(name)

        if failed_servers:
            logger.warning(
                "The following named servers failed to start and were skipped: %s",
                ", ".join(failed_servers),
            )

        if not default_server_params and not effective_configs:
            logger.error("No servers configured to run.")
            return

        # Check if all named servers failed and there's no default server
        has_running_named = len(effective_configs) > len(failed_servers)
        if not default_server_params and not has_running_named:
            logger.error(
                "No servers are running. All named servers failed to start.",
            )
            return

        middleware: list[Middleware] = []
        if mcp_settings.allow_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=mcp_settings.allow_origins,
                    allow_methods=["*"],
                    allow_headers=["*"],
                    expose_headers=mcp_settings.expose_headers,
                ),
            )

        if mcp_settings.api_key:
            middleware.append(
                Middleware(APIKeyMiddleware, api_key=mcp_settings.api_key),
            )
            logger.info("API key authentication enabled")

        starlette_app = Starlette(
            debug=(mcp_settings.log_level == "DEBUG"),
            routes=all_routes,
            middleware=middleware,
            lifespan=combined_lifespan,
        )

        starlette_app.router.redirect_slashes = False

        config = uvicorn.Config(
            starlette_app,
            host=mcp_settings.bind_host,
            port=mcp_settings.port,
            log_level=mcp_settings.log_level.lower(),
        )
        http_server = uvicorn.Server(config)

        # Print out the SSE URLs for all configured servers
        base_url = f"http://{mcp_settings.bind_host}:{mcp_settings.port}"
        sse_urls = []

        # Add default server if configured
        if default_server_params:
            sse_urls.append(f"{base_url}/sse")

        # Add named servers
        sse_urls.extend([f"{base_url}/servers/{name}/sse" for name in effective_configs])

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
