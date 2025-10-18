"""HTTP request logging patch for MCP proxy.

This module patches the create_mcp_http_client function to add comprehensive
request and response logging capabilities.
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _create_mcp_http_client_with_logging(  # noqa: C901
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    """Create a standardized httpx AsyncClient with MCP defaults and logging.

    This is a replacement for the original create_mcp_http_client that adds
    comprehensive request/response logging capabilities.

    Args:
        headers: Optional headers to include with all requests.
        timeout: Request timeout as httpx.Timeout object.
        auth: Optional authentication handler.

    Returns:
        Configured httpx.AsyncClient instance with MCP defaults and logging.
    """
    # Set MCP defaults (copied from original implementation)
    kwargs: dict[str, Any] = {
        "follow_redirects": True,
    }

    # Handle timeout
    if timeout is None:
        kwargs["timeout"] = httpx.Timeout(30.0)
    else:
        kwargs["timeout"] = timeout

    # Handle headers
    if headers is not None:
        kwargs["headers"] = headers

    # Handle authentication
    if auth is not None:
        kwargs["auth"] = auth

    if (verify := os.getenv("MCP_PROXY_VERIFY_SSL")) is not None:
        enable_ssl_verification = str(verify).lower() in ("yes", "1", "true")
        logger.debug(
            "Setting httpx.AsyncClient(...,verify=%s,...) since MCP_PROXY_VERIFY_SSL=%s",
            enable_ssl_verification,
            verify,
        )
        kwargs["verify"] = enable_ssl_verification

    # Add logging event hooks
    async def log_request(request: httpx.Request) -> None:
        """Log HTTP request details."""
        logger.info(
            "HTTP Request: %s %s",
            request.method,
            request.url,
        )

        # Log headers (be careful with sensitive data)
        if logger.isEnabledFor(logging.DEBUG) or True:
            safe_headers = {}
            for key, value in request.headers.items():
                # Mask sensitive headers
                if key.lower() in ("authorization", "x-api-key", "cookie"):
                    safe_headers[key] = "***MASKED***"
                else:
                    safe_headers[key] = value
            logger.info("Request Headers: %s", safe_headers)

    async def log_response(response: httpx.Response) -> None:
        """Log HTTP response details."""
        logger.debug(
            "HTTP Response: %s %s - %d %s",
            response.request.method,
            response.request.url,
            response.status_code,
            response.reason_phrase,
        )

        # Log response headers
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Response Headers: %s", dict(response.headers))

    # Add event hooks
    kwargs["event_hooks"] = {
        "request": [log_request],
        "response": [log_response],
    }

    return httpx.AsyncClient(**kwargs)


def patch_mcp_http_client() -> None:
    """Patch the create_mcp_http_client function to add logging capabilities."""
    # Import the module we need to patch
    try:
        from mcp.client import sse

        # Replace with our logging version
        sse.create_mcp_http_client = _create_mcp_http_client_with_logging  # type: ignore[attr-defined]

        logger.warning(
            "Successfully patched create_mcp_http_client to add request/response logging",
        )

    except ImportError:
        logger.warning("Cannot patch create_mcp_http_client. Extra logging cannot be activated")
