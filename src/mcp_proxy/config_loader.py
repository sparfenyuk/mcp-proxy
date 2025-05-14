import json
import logging

from mcp.client.stdio import StdioServerParameters

logger = logging.getLogger(__name__)


def load_named_server_configs_from_file(
    config_file_path: str, base_env: dict[str, str],
) -> dict[str, StdioServerParameters]:
    """Loads named server configurations from a JSON file.

    Args:
        config_file_path: Path to the JSON configuration file.
        base_env: The base environment dictionary to be inherited by servers.

    Returns:
        A dictionary of named server parameters.

    Raises:
        FileNotFoundError: If the config file is not found.
        json.JSONDecodeError: If the config file contains invalid JSON.
        ValueError: If the config file format is invalid.
    """
    named_stdio_params: dict[str, StdioServerParameters] = {}
    logger.info(f"Loading named server configurations from: {config_file_path}")

    try:
        with open(config_file_path) as f:
            config_data = json.load(f)
    except FileNotFoundError:
        logger.error(f"Configuration file not found: {config_file_path}")
        raise
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON from configuration file: {config_file_path}")
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error opening or reading configuration file {config_file_path}: {e}",
        )
        raise ValueError(f"Could not read configuration file: {e}")

    if not isinstance(config_data, dict) or "mcpServers" not in config_data:
        msg = f"Invalid config file format in {config_file_path}. Missing 'mcpServers' key."
        logger.error(msg)
        raise ValueError(msg)

    for name, server_config in config_data.get("mcpServers", {}).items():
        if not isinstance(server_config, dict):
            logger.warning(
                f"Skipping invalid server config for '{name}' in {config_file_path}. Entry is not a dictionary.",
            )
            continue
        if not server_config.get("enabled", True):  # Default to True if 'enabled' is not present
            logger.info(f"Named server '{name}' from config is not enabled. Skipping.")
            continue

        command = server_config.get("command")
        command_args = server_config.get("args", [])

        if not command:
            logger.warning(
                f"Named server '{name}' from config is missing 'command'. Skipping.",
            )
            continue
        if not isinstance(command_args, list):
            logger.warning(
                f"Named server '{name}' from config has invalid 'args' (must be a list). Skipping.",
            )
            continue

        named_stdio_params[name] = StdioServerParameters(
            command=command,
            args=command_args,
            env=base_env.copy(),
            cwd=None,
        )
        logger.info(
            f"Configured named server '{name}' from config: {command} {' '.join(command_args)}",
        )

    return named_stdio_params
