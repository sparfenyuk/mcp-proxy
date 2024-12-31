"""The entry point for the mcp-proxy application. It sets up the logging and runs the main function.

Two ways to run the application:
1. Run the application as a module `uv run -m mcp_proxy`
2. Run the application as a package `uv run mcp-proxy`

"""

import argparse
import asyncio
import logging
import os
import sys
import typing as t

from mcp.client.stdio import StdioServerParameters

from .sse_client import run_sse_client
from .sse_server import SseServerSettings, run_sse_server

logging.basicConfig(level=logging.DEBUG)
SSE_URL: t.Final[str | None] = os.getenv("SSE_URL", None)
API_ACCESS_TOKEN: t.Final[str | None] = os.getenv("API_ACCESS_TOKEN", None)


def main() -> None:
    """Start the client using asyncio."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command_or_url",
        help=(
            "Command or URL to connect to. When a URL, will run an SSE client "
            "to connect to the server, otherwise will run the command and "
            "connect as a stdio client. Can also be set as environment variable SSE_URL."
        ),
        nargs="?",  # Required below to allow for coming form env var
        default=SSE_URL,
    )

    sse_client_group = parser.add_argument_group("SSE client options")
    sse_client_group.add_argument(
        "--api-access-token",
        default=API_ACCESS_TOKEN,
        help=(
            "Access token Authorization header passed by the client to the SSE "
            "server. Can also be set as environment variable API_ACCESS_TOKEN."
        ),
    )

    stdio_client_options = parser.add_argument_group("stdio client options")
    stdio_client_options.add_argument(
        "args",
        nargs="*",
        help="Arguments to the command to run to spawn the server",
    )
    stdio_client_options.add_argument(
        "-e",
        "--env",
        nargs=2,
        action="append",
        metavar=("KEY", "VALUE"),
        help="Environment variables used when spawning the server. Can be used multiple times.",
        default=[],
    )

    sse_server_group = parser.add_argument_group("SSE server options")
    sse_server_group.add_argument(
        "--sse-port",
        type=int,
        default=None,
        help="Port to expose an SSE server on",
    )
    sse_server_group.add_argument(
        "--sse-host",
        default="127.0.0.1",
        help="Host to expose an SSE server on",
    )

    args = parser.parse_args()

    if not args.command_or_url:
        parser.print_help()
        sys.exit(1)

    if (
        SSE_URL
        or args.command_or_url.startswith("http://")
        or args.command_or_url.startswith("https://")
    ):
        # Start a client connected to the SSE server, and expose as a stdio server
        logging.debug("Starting SSE client and stdio server")
        asyncio.run(run_sse_client(args.command_or_url, api_access_token=API_ACCESS_TOKEN))
        return

    # Start a client connected to the given command, and expose as an SSE server
    logging.debug("Starting stdio client and SSE server")
    stdio_params = StdioServerParameters(
        command=args.command_or_url,
        args=args.args,
        env=dict(args.env),
    )
    sse_settings = SseServerSettings(
        bind_host=args.sse_host,
        port=args.sse_port,
    )
    asyncio.run(run_sse_server(stdio_params, sse_settings))


if __name__ == "__main__":
    main()
