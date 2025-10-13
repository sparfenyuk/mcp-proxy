"""Simplified tests for authentication middleware."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_proxy.auth import AuthMiddleware

# HTTP status codes
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401


async def dummy_endpoint(_request: Request) -> JSONResponse:
    """Simple endpoint for testing."""
    return JSONResponse({"message": "success"})


async def status_endpoint(_request: Request) -> JSONResponse:
    """Status endpoint."""
    return JSONResponse({"status": "ok"})


def create_app_without_auth() -> Starlette:
    """Create app without authentication."""
    routes = [
        Route("/sse", dummy_endpoint),
        Route("/mcp/test", dummy_endpoint),
        Route("/messages/test", dummy_endpoint),
        Route("/status", status_endpoint),
        Route("/other", dummy_endpoint),
    ]
    return Starlette(routes=routes)


def create_app_with_auth() -> Starlette:
    """Create app with authentication."""
    routes = [
        Route("/sse", dummy_endpoint),
        Route("/mcp/test", dummy_endpoint),
        Route("/messages/test", dummy_endpoint),
        Route("/status", status_endpoint),
        Route("/other", dummy_endpoint),
        Route("/servers/test/sse", dummy_endpoint),
        Route("/servers/test/mcp", dummy_endpoint),
    ]
    middleware = [Middleware(AuthMiddleware, api_key="test-api-key")]
    return Starlette(routes=routes, middleware=middleware)


def test_no_auth_allows_all() -> None:
    """Test that all requests work without authentication configured."""
    app = create_app_without_auth()
    with TestClient(app) as client:
        assert client.get("/sse").status_code == HTTP_OK
        assert client.get("/mcp/test").status_code == HTTP_OK
        assert client.get("/status").status_code == HTTP_OK


def test_auth_blocks_protected_endpoints() -> None:
    """Test that protected endpoints are blocked without API key."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        response = client.get("/sse")
        assert response.status_code == HTTP_UNAUTHORIZED
        assert response.json() == {"error": "Unauthorized"}

        response = client.get("/mcp/test")
        assert response.status_code == HTTP_UNAUTHORIZED

        response = client.get("/messages/test")
        assert response.status_code == HTTP_UNAUTHORIZED


def test_auth_allows_with_key() -> None:
    """Test that requests work with correct API key."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        headers = {"x-api-key": "test-api-key"}

        response = client.get("/sse", headers=headers)
        assert response.status_code == HTTP_OK
        assert response.json() == {"message": "success"}

        response = client.get("/mcp/test", headers=headers)
        assert response.status_code == HTTP_OK


def test_auth_blocks_wrong_key() -> None:
    """Test that requests are blocked with wrong API key."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        headers = {"x-api-key": "wrong-key"}

        response = client.get("/sse", headers=headers)
        assert response.status_code == HTTP_UNAUTHORIZED


def test_status_not_protected() -> None:
    """Test that /status endpoint is not protected."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        response = client.get("/status")
        assert response.status_code == HTTP_OK
        assert response.json() == {"status": "ok"}


def test_other_endpoints_not_protected() -> None:
    """Test that non-SSE/MCP endpoints are not protected."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        response = client.get("/other")
        assert response.status_code == HTTP_OK


def test_options_allowed() -> None:
    """Test that OPTIONS requests are allowed without auth."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        response = client.options("/sse")
        assert response.status_code != HTTP_UNAUTHORIZED


def test_case_insensitive_header() -> None:
    """Test that API key header is case-insensitive."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        # Different case variations
        headers = {"X-API-KEY": "test-api-key"}
        response = client.get("/sse", headers=headers)
        assert response.status_code == HTTP_OK

        headers = {"X-Api-Key": "test-api-key"}
        response = client.get("/sse", headers=headers)
        assert response.status_code == HTTP_OK


def test_named_servers_protected() -> None:
    """Test that named server endpoints are protected."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        # Without auth
        response = client.get("/servers/test/sse")
        assert response.status_code == HTTP_UNAUTHORIZED

        response = client.get("/servers/test/mcp")
        assert response.status_code == HTTP_UNAUTHORIZED

        # With auth
        headers = {"x-api-key": "test-api-key"}
        response = client.get("/servers/test/sse", headers=headers)
        assert response.status_code == HTTP_OK

        response = client.get("/servers/test/mcp", headers=headers)
        assert response.status_code == HTTP_OK
