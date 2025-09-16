"""Simple authentication middleware for MCP proxy."""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """Simple API key authentication middleware."""

    def __init__(self, app, api_key: str | None = None) -> None:
        """Initialize middleware with optional API key."""
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next) -> Response:
        """Check API key for protected endpoints."""
        # Skip auth if no API key configured
        if not self.api_key:
            return await call_next(request)

        # Allow OPTIONS (CORS preflight) and /status endpoint
        if request.method == "OPTIONS" or request.url.path == "/status":
            return await call_next(request)

        # Check if path needs protection (/sse, /mcp, /messages, /servers/*/sse, /servers/*/mcp)
        path = request.url.path
        needs_auth = (
            path.startswith("/sse") or
            path.startswith("/mcp") or
            path.startswith("/messages") or
            "/sse" in path or
            "/mcp" in path
        )

        if not needs_auth:
            return await call_next(request)

        # Check for API key in headers (case-insensitive)
        api_key = request.headers.get("x-api-key", "")

        if api_key != self.api_key:
            logger.warning("Auth failed for %s %s", request.method, path)
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401
            )

        return await call_next(request)

