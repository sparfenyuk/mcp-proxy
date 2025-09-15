"""Tests for the HTTP client."""

import pytest
from unittest.mock import AsyncMock, patch

from mcp_proxy.http_client import run_http_client


@pytest.mark.asyncio
async def test_run_http_client():
    """Test HTTP client run function."""
    url = "http://example.com/"
    headers = {"Authorization": "Bearer token"}

    with patch("mcp_proxy.http_client.streamablehttp_client") as mock_http_client, \
         patch("mcp_proxy.http_client.ClientSession") as mock_session, \
         patch("mcp_proxy.http_client.create_proxy_server") as mock_create_proxy, \
         patch("mcp_proxy.http_client.stdio_server") as mock_stdio:

        # Setup mocks
        mock_streams = (AsyncMock(), AsyncMock())
        mock_http_client.return_value.__aenter__.return_value = mock_streams
        mock_session.return_value.__aenter__.return_value = AsyncMock()
        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_stdio.return_value.__aenter__.return_value = (AsyncMock(), AsyncMock())

        # Run the client
        await run_http_client(url, headers)

        # Verify calls
        mock_http_client.assert_called_once_with(url=url, headers=headers)
        mock_create_proxy.assert_called_once()
        mock_proxy.run.assert_called_once()


@pytest.mark.asyncio
async def test_run_http_client_no_headers():
    """Test HTTP client run function without headers."""
    url = "http://example.com/"

    with patch("mcp_proxy.http_client.streamablehttp_client") as mock_http_client, \
         patch("mcp_proxy.http_client.ClientSession") as mock_session, \
         patch("mcp_proxy.http_client.create_proxy_server") as mock_create_proxy, \
         patch("mcp_proxy.http_client.stdio_server") as mock_stdio:

        # Setup mocks
        mock_streams = (AsyncMock(), AsyncMock())
        mock_http_client.return_value.__aenter__.return_value = mock_streams
        mock_session.return_value.__aenter__.return_value = AsyncMock()
        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_stdio.return_value.__aenter__.return_value = (AsyncMock(), AsyncMock())

        # Run the client
        await run_http_client(url)

        # Verify calls
        mock_http_client.assert_called_once_with(url=url, headers=None)
        mock_create_proxy.assert_called_once()
        mock_proxy.run.assert_called_once()