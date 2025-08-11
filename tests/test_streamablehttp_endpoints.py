"""Tests for StreamableHTTP endpoints in the MCP server."""

import pytest
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from mcp_proxy.mcp_server import create_single_instance_routes, _handle_status


class TestStreamableHTTPEndpoints:
    """Test class for StreamableHTTP endpoints."""

    def test_create_single_instance_routes_includes_mcp_endpoint(self) -> None:
        """Test that create_single_instance_routes includes the /mcp endpoint."""
        # Create a mock MCP server instance
        mock_mcp_server = AsyncMock()
        
        # Call the function under test
        routes, http_session_manager = create_single_instance_routes(
            mock_mcp_server,
            stateless_instance=False
        )
        
        # Verify that the routes include the expected endpoints
        route_paths = []
        for route in routes:
            if isinstance(route, Mount):
                route_paths.append(route.path)
            elif isinstance(route, Route):
                route_paths.append(route.path)
        
        # Check that all expected endpoints are present
        assert "/mcp" in route_paths, "Missing /mcp StreamableHTTP endpoint"
        assert "/sse" in route_paths, "Missing /sse endpoint"
        assert "/messages" in route_paths, "Missing /messages endpoint"

    def test_status_endpoint_returns_json(self) -> None:
        """Test that the status endpoint returns JSON response."""
        # Create a simple Starlette app with just the status route
        app = Starlette(routes=[
            Route("/status", endpoint=_handle_status)
        ])
        
        # Create test client and make request
        with TestClient(app) as client:
            response = client.get("/status")
        
        # Verify response
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        
        # Verify JSON content structure
        json_data = response.json()
        assert "api_last_activity" in json_data
        assert "server_instances" in json_data

    def test_mcp_endpoint_path_structure(self) -> None:
        """Test that the MCP endpoint is properly mounted."""
        mock_mcp_server = AsyncMock()
        
        routes, _ = create_single_instance_routes(
            mock_mcp_server,
            stateless_instance=True  # Test with stateless mode
        )
        
        # Find the /mcp mount
        mcp_mount = None
        for route in routes:
            if isinstance(route, Mount) and route.path == "/mcp":
                mcp_mount = route
                break
        
        assert mcp_mount is not None, "StreamableHTTP /mcp endpoint not found"
        assert mcp_mount.path == "/mcp"

    def test_stateless_mode_configuration(self) -> None:
        """Test that stateless mode is properly configured."""
        mock_mcp_server = AsyncMock()
        
        # Test with stateless enabled
        routes_stateless, manager_stateless = create_single_instance_routes(
            mock_mcp_server,
            stateless_instance=True
        )
        
        # Test with stateless disabled
        routes_stateful, manager_stateful = create_single_instance_routes(
            mock_mcp_server,
            stateless_instance=False
        )
        
        # Both should have the same route structure
        assert len(routes_stateless) == len(routes_stateful)
        
        # The HTTP session managers should be different instances
        assert manager_stateless is not manager_stateful


@pytest.fixture
def mock_mcp_server():
    """Create a mock MCP server for testing."""
    server = AsyncMock()
    server.run = AsyncMock()
    return server


def test_endpoint_integration():
    """Integration test to verify all endpoints work together."""
    # This is a placeholder for more comprehensive integration tests
    # that would require actual MCP server setup
    pass