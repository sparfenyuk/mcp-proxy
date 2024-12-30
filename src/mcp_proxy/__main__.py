"""The entry point for the mcp-proxy application. It sets up the logging and runs the main function.

Two ways to run the application:
1. Run the application as a module `uv run -m mcp_proxy`
2. Run the application as a package `uv run mcp-proxy`

"""

import asyncio
import logging
import os
import typing as t

from .sse_client import run_sse_client

logging.basicConfig(level=logging.DEBUG)
SSE_URL: t.Final[str] = os.getenv("SSE_URL", "")
API_ACCESS_TOKEN: t.Final[str | None] = os.getenv("API_ACCESS_TOKEN", None)

if not SSE_URL:
    raise ValueError("SSE_URL environment variable is not set")


def main() -> None:
    """Start the client using asyncio."""
    asyncio.run(run_sse_client(SSE_URL, api_access_token=API_ACCESS_TOKEN))


if __name__ == "__main__":
    main()
