"""HTTP request logging patch for MCP proxy.

This module patches the create_mcp_http_client function to add comprehensive
request and response logging capabilities.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def custom_httpx_client(
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
        "timeout": timeout if timeout is not None else httpx.Timeout(30.0),
        "headers": headers,
        "auth": auth,
    }
    kwargs["follow_redirects"] = False

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
            request.url.copy_with(username=None, password=None, query=None, fragment=None),
        )

    async def log_response(response: httpx.Response) -> None:
        """Log HTTP response details."""
        safe_url = response.request.url.copy_with(
            username=None,
            password=None,
            query=None,
            fragment=None,
        )
        logger.info(
            "HTTP Response: %s %s - %d %s",
            response.request.method,
            safe_url,
            response.status_code,
            response.reason_phrase,
        )

    # Add event hooks
    kwargs["event_hooks"] = {
        "request": [log_request],
        "response": [log_response],
    }

    return httpx.AsyncClient(**kwargs)
