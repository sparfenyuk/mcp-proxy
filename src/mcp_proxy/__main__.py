"""The entry point for the mcp-proxy application. It sets up the logging and runs the main function.

Two ways to run the application:
1. Run the application as a module `uv run -m mcp_proxy`
2. Run the application as a package `uv run mcp-proxy`

"""

import argparse
import asyncio
import json
import logging
import os
import shlex
import sys
import typing as t

from mcp.client.stdio import StdioServerParameters

from .config_loader import load_named_server_configs_from_file
from .mcp_server import MCPServerSettings, run_mcp_server
from .sse_client import run_sse_client
from .streamablehttp_client import run_streamablehttp_client

# Deprecated env var. Here for backwards compatibility.
SSE_URL: t.Final[str | None] = os.getenv(
    "SSE_URL",
    None,
)


def _setup_argument_parser() -> argparse.ArgumentParser:
    """Set up and return the argument parser for the MCP proxy."""
    parser = argparse.ArgumentParser(
        description=("Start the MCP proxy in one of two possible modes: as a client or a server."),
        epilog=(
            "Examples:\n"
            "  mcp-proxy http://localhost:8080/sse\n"
            "  mcp-proxy --transport streamablehttp http://localhost:8080/mcp\n"
            "  mcp-proxy --headers Authorization 'Bearer YOUR_TOKEN' http://localhost:8080/sse\n"
            "  mcp-proxy --port 8080 -- your-command --arg1 value1 --arg2 value2\n"
            "  mcp-proxy --named-server fetch 'uvx mcp-server-fetch' --port 8080\n"
            "  mcp-proxy your-command --port 8080 -e KEY VALUE -e ANOTHER_KEY ANOTHER_VALUE\n"
            "  mcp-proxy your-command --port 8080 --allow-origin='*'\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    _add_arguments_to_parser(parser)
    return parser


def _add_arguments_to_parser(parser: argparse.ArgumentParser) -> None:
    """Add all arguments to the argument parser."""
    parser.add_argument(
        "command_or_url",
        help=(
            "Command or URL to connect to. When a URL, will run an SSE/StreamableHTTP client. "
            "Otherwise, if --named-server is not used, this will be the command "
            "for the default stdio client. If --named-server is used, this argument "
            "is ignored for stdio mode unless no default server is desired. "
            "See corresponding options for more details."
        ),
        nargs="?",
        default=SSE_URL,
    )

    client_group = parser.add_argument_group("SSE/StreamableHTTP client options")
    client_group.add_argument(
        "-H",
        "--headers",
        nargs=2,
        action="append",
        metavar=("KEY", "VALUE"),
        help="Headers to pass to the SSE server. Can be used multiple times.",
        default=[],
    )
    client_group.add_argument(
        "--transport",
        choices=["sse", "streamablehttp"],
        default="sse",  # For backwards compatibility
        help="The transport to use for the client. Default is SSE.",
    )

    stdio_client_options = parser.add_argument_group("stdio client options")
    stdio_client_options.add_argument(
        "args",
        nargs="*",
        help=(
            "Any extra arguments to the command to spawn the default server. "
            "Ignored if only named servers are defined."
        ),
    )
    stdio_client_options.add_argument(
        "-e",
        "--env",
        nargs=2,
        action="append",
        metavar=("KEY", "VALUE"),
        help=(
            "Environment variables used when spawning the default server. Can be "
            "used multiple times. For named servers, environment is inherited or "
            "passed via --pass-environment."
        ),
        default=[],
    )
    stdio_client_options.add_argument(
        "--cwd",
        default=None,
        help=(
            "The working directory to use when spawning the default server process. "
            "Named servers inherit the proxy's CWD."
        ),
    )
    stdio_client_options.add_argument(
        "--pass-environment",
        action=argparse.BooleanOptionalAction,
        help="Pass through all environment variables when spawning all server processes.",
        default=False,
    )
    stdio_client_options.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        help="Enable debug mode with detailed logging output.",
        default=False,
    )
    stdio_client_options.add_argument(
        "--named-server",
        action="append",
        nargs=2,
        metavar=("NAME", "COMMAND_STRING"),
        help=(
            "Define a named stdio server. NAME is for the URL path /servers/NAME/. "
            "COMMAND_STRING is a single string with the command and its arguments "
            "(e.g., 'uvx mcp-server-fetch --timeout 10'). "
            "These servers inherit the proxy's CWD and environment from --pass-environment."
        ),
        default=[],
        dest="named_server_definitions",
    )
    stdio_client_options.add_argument(
        "--named-server-config",
        type=str,
        default=None,
        metavar="FILE_PATH",
        help=(
            "Path to a JSON configuration file for named stdio servers. "
            "If provided, this will be the exclusive source for named server definitions, "
            "and any --named-server CLI arguments will be ignored."
        ),
    )

    mcp_server_group = parser.add_argument_group("SSE server options")
    mcp_server_group.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to expose an SSE server on. Default is a random port",
    )
    mcp_server_group.add_argument(
        "--host",
        default=None,
        help="Host to expose an SSE server on. Default is 127.0.0.1",
    )
    mcp_server_group.add_argument(
        "--stateless",
        action=argparse.BooleanOptionalAction,
        help="Enable stateless mode for streamable http transports. Default is False",
        default=False,
    )
    mcp_server_group.add_argument(
        "--sse-port",
        type=int,
        default=0,
        help="(deprecated) Same as --port",
    )
    mcp_server_group.add_argument(
        "--sse-host",
        default="127.0.0.1",
        help="(deprecated) Same as --host",
    )
    mcp_server_group.add_argument(
        "--allow-origin",
        nargs="+",
        default=[],
        help=(
            "Allowed origins for the SSE server. Can be used multiple times. "
            "Default is no CORS allowed."
        ),
    )


def _setup_logging(*, debug: bool) -> logging.Logger:
    """Set up logging configuration and return the logger."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="[%(levelname)1.1s %(asctime)s.%(msecs).03d %(name)s] %(message)s",
    )
    return logging.getLogger(__name__)


def _handle_sse_client_mode(
    args_parsed: argparse.Namespace,
    logger: logging.Logger,
) -> None:
    """Handle SSE/StreamableHTTP client mode operation."""
    if args_parsed.named_server_definitions:
        logger.warning(
            "--named-server arguments are ignored when command_or_url is an HTTP/HTTPS URL "
            "(SSE/StreamableHTTP client mode).",
        )
    # Start a client connected to the SSE server, and expose as a stdio server
    logger.debug("Starting SSE/StreamableHTTP client and stdio server")
    headers = dict(args_parsed.headers)
    if api_access_token := os.getenv("API_ACCESS_TOKEN", None):
        headers["Authorization"] = f"Bearer {api_access_token}"

    if args_parsed.transport == "streamablehttp":
        asyncio.run(run_streamablehttp_client(args_parsed.command_or_url, headers=headers))
    else:
        asyncio.run(run_sse_client(args_parsed.command_or_url, headers=headers))


def _configure_default_server(
    args_parsed: argparse.Namespace,
    base_env: dict[str, str],
    logger: logging.Logger,
) -> StdioServerParameters | None:
    """Configure the default server if applicable."""
    if not (
        args_parsed.command_or_url
        and not args_parsed.command_or_url.startswith(("http://", "https://"))
    ):
        return None

    default_server_env = base_env.copy()
    default_server_env.update(dict(args_parsed.env))  # Specific env vars for default server

    default_stdio_params = StdioServerParameters(
        command=args_parsed.command_or_url,
        args=args_parsed.args,
        env=default_server_env,
        cwd=args_parsed.cwd if args_parsed.cwd else None,
    )
    logger.info(
        "Configured default server: %s %s",
        args_parsed.command_or_url,
        " ".join(args_parsed.args),
    )
    return default_stdio_params


def _load_named_servers_from_config(
    config_path: str,
    base_env: dict[str, str],
    logger: logging.Logger,
) -> dict[str, StdioServerParameters]:
    """Load named server configurations from a file."""
    try:
        return load_named_server_configs_from_file(config_path, base_env)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        # Specific errors are already logged by the loader function
        # We log a generic message here before exiting
        logger.exception(
            "Failed to load server configurations from %s. Exiting.",
            config_path,
        )
        sys.exit(1)
    except Exception:  # Catch any other unexpected errors from loader
        logger.exception(
            "An unexpected error occurred while loading server configurations from %s. Exiting.",
            config_path,
        )
        sys.exit(1)


def _configure_named_servers_from_cli(
    named_server_definitions: list[tuple[str, str]],
    base_env: dict[str, str],
    logger: logging.Logger,
) -> dict[str, StdioServerParameters]:
    """Configure named servers from CLI arguments."""
    named_stdio_params: dict[str, StdioServerParameters] = {}

    for name, command_string in named_server_definitions:
        try:
            command_parts = shlex.split(command_string)
            if not command_parts:  # Handle empty command_string
                logger.error("Empty COMMAND_STRING for named server '%s'. Skipping.", name)
                continue

            command = command_parts[0]
            command_args = command_parts[1:]
            # Named servers inherit base_env (which includes passed-through env)
            # and use the proxy's CWD.
            named_stdio_params[name] = StdioServerParameters(
                command=command,
                args=command_args,
                env=base_env.copy(),  # Each named server gets a copy of the base env
                cwd=None,  # Named servers run in the proxy's CWD
            )
            logger.info("Configured named server '%s': %s", name, command_string)
        except IndexError:  # Should be caught by the check for empty command_parts
            logger.exception(
                "Invalid COMMAND_STRING for named server '%s': '%s'. Must include a command.",
                name,
                command_string,
            )
            sys.exit(1)
        except Exception:
            logger.exception("Error parsing COMMAND_STRING for named server '%s'", name)
            sys.exit(1)

    return named_stdio_params


def _create_mcp_settings(args_parsed: argparse.Namespace) -> MCPServerSettings:
    """Create MCP server settings from parsed arguments."""
    return MCPServerSettings(
        bind_host=args_parsed.host if args_parsed.host is not None else args_parsed.sse_host,
        port=args_parsed.port if args_parsed.port is not None else args_parsed.sse_port,
        stateless=args_parsed.stateless,
        allow_origins=args_parsed.allow_origin if len(args_parsed.allow_origin) > 0 else None,
        log_level="DEBUG" if args_parsed.debug else "INFO",
    )


def main() -> None:
    """Start the client using asyncio."""
    parser = _setup_argument_parser()
    args_parsed = parser.parse_args()
    logger = _setup_logging(debug=args_parsed.debug)

    # Validate required arguments
    if (
        not args_parsed.command_or_url
        and not args_parsed.named_server_definitions
        and not args_parsed.named_server_config
    ):
        parser.print_help()
        logger.error(
            "Either a command_or_url for a default server or at least one --named-server "
            "(or --named-server-config) must be provided for stdio mode.",
        )
        sys.exit(1)

    # Handle SSE client mode if URL is provided
    if args_parsed.command_or_url and args_parsed.command_or_url.startswith(
        ("http://", "https://"),
    ):
        _handle_sse_client_mode(args_parsed, logger)
        return

    # Start stdio client(s) and expose as an SSE server
    logger.debug("Configuring stdio client(s) and SSE server")

    # Base environment for all spawned processes
    base_env: dict[str, str] = {}
    if args_parsed.pass_environment:
        base_env.update(os.environ)

    # Configure default server
    default_stdio_params = _configure_default_server(args_parsed, base_env, logger)

    # Configure named servers
    named_stdio_params: dict[str, StdioServerParameters] = {}
    if args_parsed.named_server_config:
        if args_parsed.named_server_definitions:
            logger.warning(
                "--named-server CLI arguments are ignored when --named-server-config is provided.",
            )
        named_stdio_params = _load_named_servers_from_config(
            args_parsed.named_server_config,
            base_env,
            logger,
        )
    elif args_parsed.named_server_definitions:
        named_stdio_params = _configure_named_servers_from_cli(
            args_parsed.named_server_definitions,
            base_env,
            logger,
        )

    # Ensure at least one server is configured
    if not default_stdio_params and not named_stdio_params:
        parser.print_help()
        logger.error(
            "No stdio servers configured. Provide a default command or use --named-server.",
        )
        sys.exit(1)

    # Create MCP server settings and run the server
    mcp_settings = _create_mcp_settings(args_parsed)
    asyncio.run(
        run_mcp_server(
            default_server_params=default_stdio_params,
            named_server_params=named_stdio_params,
            mcp_settings=mcp_settings,
        ),
    )


if __name__ == "__main__":
    main()
