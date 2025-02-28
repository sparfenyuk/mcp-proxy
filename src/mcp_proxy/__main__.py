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
SSE_URL: t.Final[str | None] = os.getenv(
    "SSE_URL",
    None,
)


def main() -> None:
    """Start the client using asyncio."""
    parser = argparse.ArgumentParser(
        description=(
            "Start the MCP proxy in one of two possible modes: as an SSE or stdio client."
        ),
        epilog=(
            "Examples:\n"
            "  mcp-proxy http://localhost:8080/sse\n"
            "  mcp-proxy --headers Authorization 'Bearer YOUR_TOKEN' http://localhost:8080/sse\n"
            "  mcp-proxy --sse-port 8080 -- your-command --arg1 value1 --arg2 value2\n"
            "  mcp-proxy your-command --sse-port 8080 -e KEY VALUE -e ANOTHER_KEY ANOTHER_VALUE\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "command_or_url",
        help=(
            "Command or URL to connect to. When a URL, will run an SSE client, "
            "otherwise will run the given command and connect as a stdio client. "
            "See corresponding options for more details."
        ),
        nargs="?",  # Required below to allow for coming form env var
        default=SSE_URL,
    )

    sse_client_group = parser.add_argument_group("SSE client options")
    sse_client_group.add_argument(
        "-H",
        "--headers",
        nargs=2,
        action="append",
        metavar=("KEY", "VALUE"),
        help="Headers to pass to the SSE server. Can be used multiple times.",
        default=[],
    )

    stdio_client_options = parser.add_argument_group("stdio client options")
    stdio_client_options.add_argument(
        "args",
        nargs="*",
        help="Any extra arguments to the command to spawn the server",
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
    stdio_client_options.add_argument(
        "--pass-environment",
        action=argparse.BooleanOptionalAction,
        help="Pass through all environment variables when spawning the server.",
        default=False,
    )

    sse_server_group = parser.add_argument_group("SSE server options")
    sse_server_group.add_argument(
        "--sse-port",
        type=int,
        default=0,
        help="Port to expose an SSE server on. Default is a random port",
    )
    sse_server_group.add_argument(
        "--sse-host",
        default="127.0.0.1",
        help="Host to expose an SSE server on. Default is 127.0.0.1",
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
        headers = dict(args.headers)
        if api_access_token := os.getenv("API_ACCESS_TOKEN", None):
            headers["Authorization"] = f"Bearer {api_access_token}"
        asyncio.run(run_sse_client(args.command_or_url, headers=headers))
        return

    # Start a client connected to the given command, and expose as an SSE server
    logging.debug("Starting stdio client and SSE server")

    # The environment variables passed to the server process
    env: dict[str, str] = {}
    # Pass through current environment variables if configured
    if args.pass_environment:
        env.update(os.environ)
    # Pass in and override any environment variables with those passed on the command line
    env.update(dict(args.env))

    stdio_params = StdioServerParameters(
        command=args.command_or_url,
        args=args.args,
        env=env,
    )
    sse_settings = SseServerSettings(
        bind_host=args.sse_host,
        port=args.sse_port,
    )
    asyncio.run(run_sse_server(stdio_params, sse_settings))


if __name__ == "__main__":
    main()
