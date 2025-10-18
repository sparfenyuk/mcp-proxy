"""HTTP request logging patch for MCP proxy.

This module patches the create_mcp_http_client function to add comprehensive
request and response logging capabilities.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def custom_httpx_client(  # noqa: C901
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    verify_ssl: bool | str | None = None,
) -> httpx.AsyncClient:
    """Create a standardized httpx AsyncClient with MCP defaults and logging.

    This is a replacement for the original create_mcp_http_client that adds
    comprehensive request/response logging capabilities.

    Args:
        headers: Optional headers to include with all requests.
        timeout: Request timeout as httpx.Timeout object.
        auth: Optional authentication handler.
        verify_ssl: Control SSL verification. Use False to disable
            or a path to a certificate bundle.

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

    if verify_ssl is not None:
        normalized_verify: bool | str
        if isinstance(verify_ssl, str):
            lowered = verify_ssl.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                normalized_verify = True
            elif lowered in {"0", "false", "no", "off"}:
                normalized_verify = False
            else:
                normalized_verify = verify_ssl
        else:
            normalized_verify = verify_ssl

        kwargs["verify"] = normalized_verify

        if isinstance(normalized_verify, bool):
            logger.debug(
                "Configured httpx.AsyncClient verify=%s (SSL verification %s).",
                normalized_verify,
                "enabled" if normalized_verify else "disabled",
            )
        else:
            logger.debug(
                "Configured httpx.AsyncClient using certificate bundle at %s.",
                normalized_verify,
            )

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
