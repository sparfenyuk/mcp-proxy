"""Simplified tests for authentication middleware."""

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_proxy.auth import AuthMiddleware


async def dummy_endpoint(request):
    """Simple endpoint for testing."""
    return JSONResponse({"message": "success"})


async def status_endpoint(request):
    """Status endpoint."""
    return JSONResponse({"status": "ok"})


def create_app_without_auth():
    """Create app without authentication."""
    routes = [
        Route("/sse", dummy_endpoint),
        Route("/mcp/test", dummy_endpoint),
        Route("/messages/test", dummy_endpoint),
        Route("/status", status_endpoint),
        Route("/other", dummy_endpoint),
    ]
    return Starlette(routes=routes)


def create_app_with_auth():
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


def test_no_auth_allows_all():
    """Test that all requests work without authentication configured."""
    app = create_app_without_auth()
    with TestClient(app) as client:
        assert client.get("/sse").status_code == 200
        assert client.get("/mcp/test").status_code == 200
        assert client.get("/status").status_code == 200


def test_auth_blocks_protected_endpoints():
    """Test that protected endpoints are blocked without API key."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        response = client.get("/sse")
        assert response.status_code == 401
        assert response.json() == {"error": "Unauthorized"}

        response = client.get("/mcp/test")
        assert response.status_code == 401

        response = client.get("/messages/test")
        assert response.status_code == 401


def test_auth_allows_with_key():
    """Test that requests work with correct API key."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        headers = {"x-api-key": "test-api-key"}

        response = client.get("/sse", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"message": "success"}

        response = client.get("/mcp/test", headers=headers)
        assert response.status_code == 200


def test_auth_blocks_wrong_key():
    """Test that requests are blocked with wrong API key."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        headers = {"x-api-key": "wrong-key"}

        response = client.get("/sse", headers=headers)
        assert response.status_code == 401


def test_status_not_protected():
    """Test that /status endpoint is not protected."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        response = client.get("/status")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_other_endpoints_not_protected():
    """Test that non-SSE/MCP endpoints are not protected."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        response = client.get("/other")
        assert response.status_code == 200


def test_options_allowed():
    """Test that OPTIONS requests are allowed without auth."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        response = client.options("/sse")
        assert response.status_code != 401


def test_case_insensitive_header():
    """Test that API key header is case-insensitive."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        # Different case variations
        headers = {"X-API-KEY": "test-api-key"}
        response = client.get("/sse", headers=headers)
        assert response.status_code == 200

        headers = {"X-Api-Key": "test-api-key"}
        response = client.get("/sse", headers=headers)
        assert response.status_code == 200


def test_named_servers_protected():
    """Test that named server endpoints are protected."""
    app = create_app_with_auth()
    with TestClient(app) as client:
        # Without auth
        response = client.get("/servers/test/sse")
        assert response.status_code == 401

        response = client.get("/servers/test/mcp")
        assert response.status_code == 401

        # With auth
        headers = {"x-api-key": "test-api-key"}
        response = client.get("/servers/test/sse", headers=headers)
        assert response.status_code == 200

        response = client.get("/servers/test/mcp", headers=headers)
        assert response.status_code == 200

