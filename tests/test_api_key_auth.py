"""Tests for API key authentication middleware."""
# ruff: noqa: PLR2004

import asyncio
import contextlib
import typing as t

import httpx
import pytest
import uvicorn
from mcp import types
from mcp.server import Server
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_proxy.mcp_server import APIKeyMiddleware, MCPServerSettings, create_single_instance_routes

API_KEY = "test-secret-key"


def _create_test_app(*, api_key: str | None = API_KEY) -> Starlette:
    """Create a minimal Starlette app with auth middleware for testing."""
    mcp_server: Server[object, t.Any] = Server("TestServer")

    @mcp_server.list_tools()  # type: ignore[misc,no-untyped-call]
    async def list_tools() -> list[types.Tool]:
        return []

    routes, http_manager = create_single_instance_routes(mcp_server, stateless_instance=False)

    # Add health/status routes like the real server
    async def handle_health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    routes = [
        Route("/health", endpoint=handle_health),
        Route("/status", endpoint=handle_health),
        *routes,
    ]

    middleware: list[Middleware] = []
    if api_key:
        middleware.append(Middleware(APIKeyMiddleware, api_key=api_key))

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> t.AsyncIterator[None]:
        async with http_manager.run():
            yield

    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.router.redirect_slashes = False
    return app


class _BackgroundServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        pass

    @contextlib.asynccontextmanager
    async def run_in_background(self) -> t.AsyncIterator[None]:
        task = asyncio.create_task(self.serve())
        try:
            while not self.started:
                await asyncio.sleep(1e-3)
            yield
        finally:
            self.should_exit = self.force_exit = True
            await task

    @property
    def url(self) -> str:
        hostport = next(
            iter([s.getsockname() for srv in self.servers for s in srv.sockets]),
        )
        return f"http://{hostport[0]}:{hostport[1]}"


@pytest.fixture
async def auth_server() -> t.AsyncIterator[str]:
    """Start a test server with API key auth enabled."""
    app = _create_test_app(api_key=API_KEY)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = _BackgroundServer(config)
    async with server.run_in_background():
        yield server.url


@pytest.fixture
async def noauth_server() -> t.AsyncIterator[str]:
    """Start a test server without API key auth."""
    app = _create_test_app(api_key=None)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = _BackgroundServer(config)
    async with server.run_in_background():
        yield server.url


async def test_health_bypasses_auth(auth_server: str) -> None:
    """Health endpoint should be accessible without auth."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{auth_server}/health")
        assert resp.status_code == 200


async def test_status_bypasses_auth(auth_server: str) -> None:
    """Status endpoint should be accessible without auth."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{auth_server}/status")
        assert resp.status_code == 200


async def test_sse_rejected_without_auth(auth_server: str) -> None:
    """SSE endpoint should reject requests without auth header."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{auth_server}/sse")
        assert resp.status_code == 401
        assert resp.json()["error"] == "Unauthorized"


async def test_sse_rejected_with_wrong_key(auth_server: str) -> None:
    """SSE endpoint should reject requests with wrong API key."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{auth_server}/sse",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401


async def test_sse_accepted_with_correct_key(auth_server: str) -> None:
    """SSE endpoint should accept requests with correct API key."""
    async with (
        httpx.AsyncClient() as client,
        client.stream(
            "GET",
            f"{auth_server}/sse",
            headers={"Authorization": f"Bearer {API_KEY}"},
        ) as resp,
    ):
        assert resp.status_code == 200


async def test_mcp_rejected_without_auth(auth_server: str) -> None:
    """MCP endpoint should reject requests without auth."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{auth_server}/mcp")
        assert resp.status_code == 401


async def test_mcp_accepted_with_correct_key(auth_server: str) -> None:
    """MCP endpoint should accept requests with correct API key."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{auth_server}/mcp",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        # Should get past auth (200 or protocol-level response, not 401)
        assert resp.status_code != 401


async def test_options_bypasses_auth(auth_server: str) -> None:
    """CORS preflight (OPTIONS) should bypass auth."""
    async with httpx.AsyncClient() as client:
        resp = await client.options(f"{auth_server}/sse")
        assert resp.status_code != 401


async def test_no_auth_configured_allows_all(noauth_server: str) -> None:
    """When no API key is configured, all requests should pass through."""
    async with (
        httpx.AsyncClient() as client,
        client.stream(
            "GET",
            f"{noauth_server}/sse",
        ) as resp,
    ):
        assert resp.status_code == 200


async def test_www_authenticate_header_on_reject(auth_server: str) -> None:
    """401 responses should include WWW-Authenticate header."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{auth_server}/sse")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"


def test_settings_api_key_field() -> None:
    """MCPServerSettings should accept api_key parameter."""
    settings = MCPServerSettings(
        bind_host="127.0.0.1",
        port=3100,
        api_key="my-secret",
    )
    assert settings.api_key == "my-secret"


def test_settings_api_key_default_none() -> None:
    """MCPServerSettings api_key should default to None."""
    settings = MCPServerSettings(bind_host="127.0.0.1", port=3100)
    assert settings.api_key is None
