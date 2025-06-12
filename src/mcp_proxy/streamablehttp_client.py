"""Create a local server that proxies requests to a remote server over StreamableHTTP."""

import logging
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)


async def run_streamablehttp_client(url: str, headers: dict[str, Any] | None = None) -> None:
    """Run the StreamableHTTP client.

    Args:
        url: The URL to connect to.
        headers: Headers for connecting to MCP server.

    """
    logger.info(f"Starting StreamableHTTP client connection to: {url}")
    
    if headers:
        # Log headers with sensitive values masked
        masked_headers = {}
        for key, value in headers.items():
            if key.lower() in ['authorization', 'x-api-key', 'api-key']:
                masked_headers[key] = f"{value[:10]}***" if len(value) > 10 else "***"
            else:
                masked_headers[key] = value
        logger.debug(f"StreamableHTTP client headers (sensitive values masked): {masked_headers}")
    else:
        logger.debug("StreamableHTTP client connecting without custom headers")
    
    # Warning about known issues with StreamableHTTP transport
    logger.warning(
        "Using StreamableHTTP transport. Note: There are known issues with this transport "
        "when using authentication headers. If you experience connection issues, "
        "try using SSE transport instead (remove --transport streamablehttp)."
    )
    
    try:
        logger.debug("Creating StreamableHTTP client connection...")
        async with (
            streamablehttp_client(url=url, headers=headers) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            logger.info("StreamableHTTP client connected successfully")
            app = await create_proxy_server(session)
            logger.debug("Starting stdio server...")
            async with stdio_server() as (read_stream, write_stream):
                logger.info("Stdio server started, beginning MCP proxy operation")
                await app.run(
                    read_stream,
                    write_stream,
                    app.create_initialization_options(),
                )
    except Exception as e:
        logger.error(f"StreamableHTTP client failed: {e}")
        logger.debug("StreamableHTTP client exception details:", exc_info=True)
        
        # Provide helpful error messages for known issues
        error_msg = str(e).lower()
        if "session terminated" in error_msg:
            logger.error(
                "Session terminated error detected. This is a known issue with StreamableHTTP transport "
                "when using authentication. Try using SSE transport instead."
            )
        elif "taskgroup" in error_msg or "unhandled errors" in error_msg:
            logger.error(
                "TaskGroup error detected. This may be due to timeout parameter type issues "
                "in the MCP SDK. Try using SSE transport instead."
            )
        elif "authorization" in error_msg or "auth" in error_msg:
            logger.error(
                "Authentication-related error detected. This may be a known issue with "
                "header handling in StreamableHTTP transport."
            )
        
        raise
