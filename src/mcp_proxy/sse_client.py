"""Create a local server that proxies requests to a remote server over SSE."""

import logging
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.server.stdio import stdio_server

from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)


async def run_sse_client(url: str, headers: dict[str, Any] | None = None) -> None:
    """Run the SSE client.

    Args:
        url: The URL to connect to.
        headers: Headers for connecting to MCP server.

    """
    logger.info(f"Starting SSE client connection to: {url}")
    
    if headers:
        # Log headers with sensitive values masked
        masked_headers = {}
        for key, value in headers.items():
            if key.lower() in ['authorization', 'x-api-key', 'api-key']:
                masked_headers[key] = f"{value[:10]}***" if len(value) > 10 else "***"
            else:
                masked_headers[key] = value
        logger.debug(f"SSE client headers (sensitive values masked): {masked_headers}")
    else:
        logger.debug("SSE client connecting without custom headers")
    
    try:
        logger.debug("Creating SSE client connection...")
        async with sse_client(url=url, headers=headers) as streams, ClientSession(*streams) as session:
            logger.info("SSE client connected successfully")
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
        logger.error(f"SSE client failed: {e}")
        logger.debug("SSE client exception details:", exc_info=True)
        raise
