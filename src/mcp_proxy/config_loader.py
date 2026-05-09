"""Configuration loader for MCP proxy.

This module provides functionality to load named server configurations from JSON files.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from mcp.client.stdio import StdioServerParameters

from .rate_limiter import DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_WAIT_SECONDS

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Extended server configuration with rate limiting parameters."""

    stdio_params: StdioServerParameters
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS


def load_named_server_configs_from_file(
    config_file_path: str | Path,
    base_env: dict[str, str],
) -> dict[str, ServerConfig]:
    """Loads named server configurations from a JSON file.

    Args:
        config_file_path: Path to the JSON configuration file.
        base_env: The base environment dictionary to be inherited by servers.

    Returns:
        A dictionary of named server configurations.

    Raises:
        FileNotFoundError: If the config file is not found.
        json.JSONDecodeError: If the config file contains invalid JSON.
        ValueError: If the config file format is invalid.
    """
    named_configs: dict[str, ServerConfig] = {}
    logger.info("Loading named server configurations from: %s", config_file_path)

    try:
        with Path(config_file_path).open() as f:
            config_data = json.load(f)
    except FileNotFoundError:
        logger.exception("Configuration file not found: %s", config_file_path)
        raise
    except json.JSONDecodeError:
        logger.exception("Error decoding JSON from configuration file: %s", config_file_path)
        raise
    except Exception as e:
        logger.exception(
            "Unexpected error opening or reading configuration file %s",
            config_file_path,
        )
        error_message = f"Could not read configuration file: {e}"
        raise ValueError(error_message) from e

    if not isinstance(config_data, dict) or "mcpServers" not in config_data:
        msg = f"Invalid config file format in {config_file_path}. Missing 'mcpServers' key."
        logger.error(msg)
        raise ValueError(msg)

    for name, server_config in config_data.get("mcpServers", {}).items():
        if not isinstance(server_config, dict):
            logger.warning(
                "Skipping invalid server config for '%s' in %s. Entry is not a dictionary.",
                name,
                config_file_path,
            )
            continue
        if not server_config.get("enabled", True):  # Default to True if 'enabled' is not present
            logger.info("Named server '%s' from config is not enabled. Skipping.", name)
            continue

        command = server_config.get("command")
        command_args = server_config.get("args", [])
        env = server_config.get("env", {})

        if not command:
            logger.warning(
                "Named server '%s' from config is missing 'command'. Skipping.",
                name,
            )
            continue
        if not isinstance(command_args, list):
            logger.warning(
                "Named server '%s' from config has invalid 'args' (must be a list). Skipping.",
                name,
            )
            continue

        new_env = base_env.copy()
        new_env.update(
            {k: os.path.expandvars(str(Path(v).expanduser())) for k, v in env.items()},
        )

        max_concurrent = server_config.get("max_concurrent", DEFAULT_MAX_CONCURRENT)
        max_wait_seconds = server_config.get("max_wait_seconds", DEFAULT_MAX_WAIT_SECONDS)
        if not isinstance(max_concurrent, int) or max_concurrent < 1:
            logger.warning(
                "Named server '%s': invalid 'max_concurrent' (must be int >= 1). Using default.",
                name,
            )
            max_concurrent = DEFAULT_MAX_CONCURRENT
        if not isinstance(max_wait_seconds, (int, float)) or max_wait_seconds <= 0:
            logger.warning(
                "Named server '%s': invalid 'max_wait_seconds' (must be > 0). Using default.",
                name,
            )
            max_wait_seconds = DEFAULT_MAX_WAIT_SECONDS

        named_configs[name] = ServerConfig(
            stdio_params=StdioServerParameters(
                command=command,
                args=command_args,
                env=new_env,
                cwd=None,
            ),
            max_concurrent=max_concurrent,
            max_wait_seconds=float(max_wait_seconds),
        )
        logger.info(
            "Configured named server '%s' from config: %s %s",
            name,
            command,
            " ".join(command_args),
        )

    return named_configs
