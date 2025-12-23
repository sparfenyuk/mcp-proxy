"""HTTP request logging patch for MCP proxy.

This module patches the create_mcp_http_client function to add comprehensive
request and response logging capabilities.
"""

import asyncio
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def custom_httpx_client(  # noqa: C901
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    verify_ssl: bool | str | None = None,
    error_queue: asyncio.Queue[httpx.HTTPStatusError] | None = None,
    request_state: dict[str, Any] | None = None,
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
        if request_state is not None and request_state.get("force_clear_session_id"):
            if "mcp-session-id" in request.headers:
                logger.info(
                    "Clearing MCP session id on outbound request due to prior 404 (method=%s url=%s)",
                    request.method,
                    request.url,
                )
                request.headers.pop("mcp-session-id", None)
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
        if request_state is not None:
            request_state["last_request_ts"] = time.monotonic()
            request_state["last_request_method"] = request.method
            request_state["last_request_url"] = str(request.url)
            request_state["last_request_session_id"] = request.headers.get("mcp-session-id")

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

        if request_state is not None:
            now = time.monotonic()
            if response.request.method == "POST":
                request_state["last_post_ts"] = now
                request_state["last_post_status"] = response.status_code
                request_state["last_post_url"] = str(response.request.url)
                request_state["last_post_session_id"] = response.request.headers.get("mcp-session-id")
            elif response.request.method == "GET":
                request_state["last_get_ts"] = now
                request_state["last_get_status"] = response.status_code
                request_state["last_get_url"] = str(response.request.url)
                request_state["last_get_session_id"] = response.request.headers.get("mcp-session-id")
            session_id = response.headers.get("mcp-session-id")
            if session_id:
                prev_session_id = request_state.get("last_session_id")
                if prev_session_id and prev_session_id != session_id:
                    logger.info(
                        "MCP session id changed: %s -> %s (from %s %s)",
                        prev_session_id,
                        session_id,
                        response.request.method,
                        response.request.url,
                    )
                request_state["last_session_id"] = session_id
                if request_state.get("force_clear_session_id"):
                    logger.info("Cleared MCP session id suppression after receiving new session id")
                request_state["force_clear_session_id"] = False
            if response.status_code == 404:
                request_state["force_clear_session_id"] = True
                logger.info(
                    "HTTP 404 for %s %s; will clear MCP session id on subsequent requests",
                    response.request.method,
                    response.request.url,
                )

        # Treat any 4xx and 503 as retryable to trigger outer reconnect/re-init logic.
        status = response.status_code
        if 400 <= status < 500 or status == 503:
            logger.warning(
                "Retryable HTTP status %s for %s %s; raising to trigger reconnect",
                status,
                response.request.method,
                response.request.url,
            )
            exc = httpx.HTTPStatusError(
                f"Retryable HTTP status: {status}",
                request=response.request,
                response=response,
            )
            if error_queue is not None:
                try:
                    error_queue.put_nowait(exc)
                except asyncio.QueueFull:
                    logger.debug("HTTP error queue full; dropping status %s", status)
            raise exc

    # Add event hooks
    kwargs["event_hooks"] = {
        "request": [log_request],
        "response": [log_response],
    }

    return httpx.AsyncClient(**kwargs)
