"""Create a local SSE server that proxies requests to a stdio MCP server."""

import contextlib
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Literal
from urllib.parse import SplitResult, urlsplit

import anyio
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server as MCPServerSDK  # Renamed to avoid conflict
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import Receive, Scope, Send

from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)

DEFAULT_EXPOSE_HEADERS: Final[tuple[str, ...]] = ("mcp-session-id",)
_LOOPBACK_HOSTS: Final[tuple[str, ...]] = ("localhost", "127.0.0.1", "[::1]")
# These identify bind values that must not imply trusted Host authorities.
_WILDCARD_BIND_HOSTS: Final[frozenset[str]] = frozenset({"0.0.0.0", "::"})  # noqa: S104
_CONTROL_CHAR_START: Final[int] = 32
_DELETE_CHAR: Final[int] = 127
_MAX_PORT: Final[int] = 65535
_HTTP_DEFAULT_PORT: Final[int] = 80
_HOST_REJECTION_STATUS: Final[int] = 421
_ORIGIN_REJECTION_STATUS: Final[int] = 403


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
    allowed_hosts: list[str] | None = None


def _has_whitespace_or_control(value: str) -> bool:
    return any(
        character.isspace()
        or ord(character) < _CONTROL_CHAR_START
        or ord(character) == _DELETE_CHAR
        for character in value
    )


def _valid_wildcard_port(value: str, allow_wildcard_port: bool) -> bool:
    if "*" not in value:
        return True
    return allow_wildcard_port and value.endswith(":*") and value.count("*") == 1


def _valid_authority_host(authority: str, hostname: str | None, netloc: str) -> bool:
    return hostname is not None and netloc == authority


def _valid_authority_port(port: int | None) -> bool:
    return port is None or 1 <= port <= _MAX_PORT


def _valid_parsed_authority(authority: str) -> bool:
    try:
        parsed = urlsplit(f"//{authority}")
        port = parsed.port
    except ValueError:
        return False
    return (
        _valid_authority_host(authority, parsed.hostname, parsed.netloc)
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and _valid_authority_port(port)
    )


def _valid_authority(value: str, *, allow_wildcard_port: bool = False) -> bool:
    """Accept one host authority, with an optional terminal wildcard port for policy entries."""
    if not value or value != value.strip() or _has_whitespace_or_control(value):
        return False
    wildcard_port = value.endswith(":*")
    if not _valid_wildcard_port(value, allow_wildcard_port):
        return False
    authority = value[:-2] if wildcard_port else value
    if (
        not authority
        or authority.endswith(":")
        or any(character in authority for character in ",/@?#\\")
    ):
        return False
    return _valid_parsed_authority(authority)


def _valid_origin_components(parsed: SplitResult) -> bool:
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
        and _valid_authority(parsed.netloc)
    )


def _valid_origin(value: str) -> bool:
    """Accept a serialized HTTP Origin value or the opaque ``null`` origin."""
    if value == "null":
        return True
    if "?" in value or "#" in value or value != value.strip() or _has_whitespace_or_control(value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return _valid_origin_components(parsed)


def _bracket_host(host: str) -> str:
    """Format raw IPv6 bind addresses for Host and Origin authorities."""
    return f"[{host}]" if ":" in host else host


def _configured_bind_authorities(
    settings: MCPServerSettings,
) -> tuple[list[str], list[str]]:
    """Return Host and Origin entries implied by one concrete bind address."""
    bind_host = settings.bind_host.strip("[]")
    if bind_host in _WILDCARD_BIND_HOSTS:
        return [], []

    formatted_host = _bracket_host(bind_host)
    bind_port = ":*" if settings.port == 0 else f":{settings.port}"
    if not _valid_authority(formatted_host) or not _valid_authority(
        f"{formatted_host}{bind_port}",
        allow_wildcard_port=True,
    ):
        message = f"Invalid server bind host: {settings.bind_host!r}"
        raise ValueError(message)

    allowed_hosts = [formatted_host, f"{formatted_host}{bind_port}"]
    allowed_origins = [f"http://{formatted_host}{bind_port}"]
    if settings.port == _HTTP_DEFAULT_PORT:
        allowed_origins.append(f"http://{formatted_host}")
    return allowed_hosts, allowed_origins


def _configured_allowed_hosts(hosts: list[str] | None) -> list[str]:
    allowed_hosts = []
    for host in hosts or []:
        if not _valid_authority(host, allow_wildcard_port=True):
            message = f"Invalid allowed Host authority: {host!r}"
            raise ValueError(message)
        allowed_hosts.append(host)
    return allowed_hosts


def _configured_allowed_origins(
    origins: list[str] | None,
    allowed_origins: list[str],
) -> list[str]:
    for origin in origins or []:
        if origin not in {"*", "null"} and not _valid_origin(origin):
            message = f"Invalid allowed Origin: {origin!r}"
            raise ValueError(message)
        if origin not in allowed_origins:
            allowed_origins.append(origin)
    return allowed_origins


def _create_transport_security_settings(
    settings: MCPServerSettings,
) -> TransportSecuritySettings:
    """Build and validate the authority policy before any backend process starts."""
    if not 0 <= settings.port <= _MAX_PORT:
        raise ValueError("Server port must be between 0 and 65535")

    allowed_hosts = [authority for host in _LOOPBACK_HOSTS for authority in (host, f"{host}:*")]
    allowed_origins = [
        f"{scheme}://{host}{port}"
        for host in _LOOPBACK_HOSTS
        for scheme in ("http", "https")
        for port in ("", ":*")
    ]

    bind_hosts, bind_origins = _configured_bind_authorities(settings)
    allowed_hosts.extend(bind_hosts)
    allowed_origins.extend(bind_origins)
    allowed_hosts.extend(_configured_allowed_hosts(settings.allowed_hosts))
    _configured_allowed_origins(settings.allow_origins, allowed_origins)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _allows_any_origin(settings: MCPServerSettings) -> bool:
    return bool(settings.allow_origins and "*" in settings.allow_origins)


def _authority_headers(headers: list[tuple[bytes, bytes]]) -> tuple[list[str], list[str]]:
    hosts = [value.decode("latin-1") for name, value in headers if name.lower() == b"host"]
    origins = [value.decode("latin-1") for name, value in headers if name.lower() == b"origin"]
    return hosts, origins


def _authority_rejection_status(hosts: list[str], origins: list[str]) -> int | None:
    if len(hosts) != 1 or not _valid_authority(hosts[0]):
        return _HOST_REJECTION_STATUS
    if len(origins) > 1 or (origins and not _valid_origin(origins[0])):
        return _ORIGIN_REJECTION_STATUS
    return None


class _TransportSecurityAdapter:
    """Validate all HTTP authority headers before CORS, routing, or request reads."""

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        settings: TransportSecuritySettings,
        *,
        allow_any_origin: bool,
    ) -> None:
        self.app = app
        self.validator = TransportSecurityMiddleware(settings)
        self.allow_any_origin = allow_any_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return None

        headers = scope.get("headers", [])
        hosts, origins = _authority_headers(headers)
        rejection_status = _authority_rejection_status(hosts, origins)
        if rejection_status == _HOST_REJECTION_STATUS:
            rejection = Response("Invalid Host header", status_code=_HOST_REJECTION_STATUS)
            return await rejection(scope, receive, send)
        if rejection_status == _ORIGIN_REJECTION_STATUS:
            rejection = Response("Invalid Origin header", status_code=_ORIGIN_REJECTION_STATUS)
            return await rejection(scope, receive, send)

        validation_scope = scope
        if self.allow_any_origin and origins:
            validation_scope = dict(scope)
            validation_scope["headers"] = [
                (name, value) for name, value in headers if name.lower() != b"origin"
            ]

        is_post = False
        response = await self.validator.validate_request(
            HTTPConnection(validation_scope),
            is_post=is_post,
        )
        if response is not None:
            return await response(scope, receive, send)
        await self.app(scope, receive, send)
        return None


# To store last activity for multiple servers if needed, though status endpoint is global for now.
_global_status: dict[str, Any] = {
    "api_last_activity": datetime.now(timezone.utc).isoformat(),
    "active_sse_connections": 0,
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
    return JSONResponse(_global_status)


async def _handle_healthz(_: Request) -> Response:
    """Respond to a transport liveness probe without allocating an MCP session."""
    return Response(status_code=204)


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
        try:
            async with sse_transport.connect_sse(
                request.scope,
                request.receive,
                request._send,  # noqa: SLF001
            ) as (read_stream, write_stream):
                _update_global_activity()
                _global_status["active_sse_connections"] += 1
                try:
                    await mcp_server_instance.run(
                        read_stream,
                        write_stream,
                        mcp_server_instance.create_initialization_options(),
                        stateless=stateless_instance,
                    )
                finally:
                    _global_status["active_sse_connections"] -= 1
        except Exception:  # noqa: BLE001
            # SSE response already started — cannot send HTTP error response.
            # Log and let the connection close gracefully.
            logger.warning(
                "SSE connection for '%s' terminated with error (upstream disconnect?)",
                mcp_server_instance.name,
                exc_info=True,
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
        Route("/healthz", endpoint=_handle_healthz, methods=["GET", "HEAD"]),
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


class _StdioExitWatch:
    """Stop the proxy when a stdio server exits, so it exits non-zero and can be restarted."""

    def __init__(self) -> None:
        self.http_server: uvicorn.Server | None = None
        self.exited: list[str] = []

    @contextlib.asynccontextmanager
    async def client(
        self, params: StdioServerParameters, name: str
    ) -> AsyncIterator[tuple[Any, Any]]:
        """Like stdio_client, but notice when the server's stdout closes."""
        async with stdio_client(params) as (read_stream, write_stream):
            send, receive = anyio.create_memory_object_stream[Any](0)

            async def forward() -> None:
                async with send:
                    try:
                        async for message in read_stream:
                            await send.send(message)
                    except anyio.BrokenResourceError:
                        return  # the session stopped reading: we are shutting down
                self._on_exit(name)

            async with anyio.create_task_group() as tg:
                tg.start_soon(forward)
                yield receive, write_stream
                tg.cancel_scope.cancel()

    def _on_exit(self, name: str) -> None:
        if self.http_server is not None and self.http_server.should_exit:
            return  # already shutting down (e.g. Ctrl+C), which stops the stdio server too
        logger.error("Stdio server '%s' exited, shutting down", name)
        self.exited.append(name)
        if self.http_server is not None:
            self.http_server.should_exit = True
            # Don't let a client's open connection keep a proxy with a dead server running.
            self.http_server.config.timeout_graceful_shutdown = 5

    def attach(self, http_server: uvicorn.Server) -> None:
        """Stop this server when a stdio server exits, or right away if one already has."""
        self.http_server = http_server
        http_server.should_exit = bool(self.exited)

    def exit_if_exited(self) -> None:
        """Exit non-zero if a stdio server exited, so a supervisor restarts the proxy."""
        if self.exited:
            sys.exit(1)


async def run_mcp_server(
    mcp_settings: MCPServerSettings,
    default_server_params: StdioServerParameters | None = None,
    named_server_params: dict[str, StdioServerParameters] | None = None,
) -> None:
    """Run stdio client(s) and expose an MCP server with multiple possible backends."""
    security_settings = _create_transport_security_settings(mcp_settings)
    named_server_params = {} if named_server_params is None else named_server_params

    stdio_exit = _StdioExitWatch()
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
            stdio_streams = await stack.enter_async_context(
                stdio_exit.client(default_server_params, "default"),
            )
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
            stdio_streams_named = await stack.enter_async_context(stdio_exit.client(params, name))
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
                    expose_headers=mcp_settings.expose_headers,
                ),
            )

        starlette_app = Starlette(
            debug=(mcp_settings.log_level == "DEBUG"),
            routes=all_routes,
            middleware=middleware,
            lifespan=combined_lifespan,
        )

        starlette_app.router.redirect_slashes = False

        server_app = _TransportSecurityAdapter(
            starlette_app,
            security_settings,
            allow_any_origin=_allows_any_origin(mcp_settings),
        )

        config = uvicorn.Config(
            server_app,
            host=mcp_settings.bind_host,
            port=mcp_settings.port,
            log_level=mcp_settings.log_level.lower(),
            access_log=False,
            timeout_graceful_shutdown=5,
        )
        http_server = uvicorn.Server(config)
        stdio_exit.attach(http_server)

        # Print out the SSE URLs for all configured servers
        base_url = f"http://{mcp_settings.bind_host}:{mcp_settings.port}"
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

    stdio_exit.exit_if_exited()
