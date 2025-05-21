"""Tests for the configuration loader module."""

import json
import shutil
import tempfile
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.client.stdio import StdioServerParameters

from mcp_proxy.config_loader import load_named_server_configs_from_file


@pytest.fixture
def create_temp_config_file() -> Generator[Callable[[dict], str], None, None]:
    """Creates a temporary JSON config file and returns its path."""
    temp_files: list[str] = []

    def _create_temp_config_file(config_content: dict) -> str:
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            suffix=".json",
        ) as tmp_config:
            json.dump(config_content, tmp_config)
            temp_files.append(tmp_config.name)
            return tmp_config.name

    yield _create_temp_config_file

    for f_path in temp_files:
        path = Path(f_path)
        if path.exists():
            path.unlink()


def test_load_valid_config(create_temp_config_file: Callable[[dict], str]) -> None:
    """Test loading a valid configuration file."""
    config_content = {
        "mcpServers": {
            "server1": {
                "command": "echo",
                "args": ["hello"],
                "enabled": True,
            },
            "server2": {
                "command": "cat",
                "args": ["file.txt"],
            },
        },
    }
    tmp_config_path = create_temp_config_file(config_content)
    base_env = {"PASSED": "env_value"}

    loaded_params = load_named_server_configs_from_file(tmp_config_path, base_env)

    assert "server1" in loaded_params
    assert loaded_params["server1"].command == "echo"
    assert loaded_params["server1"].args == ["hello"]
    assert (
        loaded_params["server1"].env == base_env
    )  # Env is a copy, check if it contains base_env items

    assert "server2" in loaded_params
    assert loaded_params["server2"].command == "cat"
    assert loaded_params["server2"].args == ["file.txt"]
    assert loaded_params["server2"].env == base_env


def test_load_config_with_not_enabled_server(
    create_temp_config_file: Callable[[dict], str],
) -> None:
    """Test loading a configuration with disabled servers."""
    config_content = {
        "mcpServers": {
            "explicitly_enabled_server": {"command": "true_command", "enabled": True},
            # No 'enabled' flag, defaults to True
            "implicitly_enabled_server": {"command": "another_true_command"},
            "not_enabled_server": {"command": "false_command", "enabled": False},
        },
    }
    tmp_config_path = create_temp_config_file(config_content)
    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})

    assert "explicitly_enabled_server" in loaded_params
    assert loaded_params["explicitly_enabled_server"].command == "true_command"
    assert "implicitly_enabled_server" in loaded_params
    assert loaded_params["implicitly_enabled_server"].command == "another_true_command"
    assert "not_enabled_server" not in loaded_params


def test_file_not_found() -> None:
    """Test handling of non-existent configuration files."""
    with pytest.raises(FileNotFoundError):
        load_named_server_configs_from_file("non_existent_file.json", {})


def test_json_decode_error() -> None:
    """Test handling of invalid JSON in configuration files."""
    # Create a file with invalid JSON content
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        suffix=".json",
    ) as tmp_config:
        tmp_config.write("this is not json {")
        tmp_config_path = tmp_config.name

    # Use try/finally to ensure cleanup
    try:
        with pytest.raises(json.JSONDecodeError):
            load_named_server_configs_from_file(tmp_config_path, {})
    finally:
        path = Path(tmp_config_path)
        if path.exists():
            path.unlink()


def test_load_example_fetch_config_if_uvx_exists() -> None:
    """Test loading the example fetch configuration if uvx is available."""
    if not shutil.which("uvx"):
        pytest.skip("uvx command not found in PATH, skipping test for example config.")

    # Assuming the test is run from the root of the repository
    example_config_path = Path(__file__).parent.parent / "config_example.json"

    if not example_config_path.exists():
        pytest.fail(
            f"Example config file not found at expected path: {example_config_path}",
        )

    base_env = {"EXAMPLE_ENV": "true"}
    loaded_params = load_named_server_configs_from_file(example_config_path, base_env)

    assert "fetch" in loaded_params
    fetch_param = loaded_params["fetch"]
    assert isinstance(fetch_param, StdioServerParameters)
    assert fetch_param.command == "uvx"
    assert fetch_param.args == ["mcp-server-fetch"]
    assert fetch_param.env == base_env
    # The 'timeout' and 'transportType' fields from the config are currently ignored by the loader,
    # so no need to assert them on StdioServerParameters.


def test_invalid_config_format_missing_mcpservers(
    create_temp_config_file: Callable[[dict], str],
) -> None:
    """Test handling of configuration files missing the mcpServers key."""
    config_content = {"some_other_key": "value"}
    tmp_config_path = create_temp_config_file(config_content)

    with pytest.raises(ValueError, match="Missing 'mcpServers' key"):
        load_named_server_configs_from_file(tmp_config_path, {})


@patch("mcp_proxy.config_loader.logger")
def test_invalid_server_entry_not_dict(
    mock_logger: object,
    create_temp_config_file: Callable[[dict], str],
) -> None:
    """Test handling of server entries that are not dictionaries."""
    config_content = {"mcpServers": {"server1": "not_a_dict"}}
    tmp_config_path = create_temp_config_file(config_content)

    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})
    assert len(loaded_params) == 0  # No servers should be loaded
    mock_logger.warning.assert_called_with(
        "Skipping invalid server config for '%s' in %s. Entry is not a dictionary.",
        "server1",
        tmp_config_path,
    )


@patch("mcp_proxy.config_loader.logger")
def test_server_entry_missing_command(
    mock_logger: object,
    create_temp_config_file: Callable[[dict], str],
) -> None:
    """Test handling of server entries missing the command field."""
    config_content = {"mcpServers": {"server_no_command": {"args": ["arg1"]}}}
    tmp_config_path = create_temp_config_file(config_content)
    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})
    assert "server_no_command" not in loaded_params
    mock_logger.warning.assert_called_with(
        "Named server '%s' from config is missing 'command'. Skipping.",
        "server_no_command",
    )


@patch("mcp_proxy.config_loader.logger")
def test_server_entry_invalid_args_type(
    mock_logger: object,
    create_temp_config_file: Callable[[dict], str],
) -> None:
    """Test handling of server entries with invalid args type."""
    config_content = {
        "mcpServers": {
            "server_invalid_args": {"command": "mycmd", "args": "not_a_list"},
        },
    }
    tmp_config_path = create_temp_config_file(config_content)
    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})
    assert "server_invalid_args" not in loaded_params
    mock_logger.warning.assert_called_with(
        "Named server '%s' from config has invalid 'args' (must be a list). Skipping.",
        "server_invalid_args",
    )


def test_empty_mcpservers_dict(create_temp_config_file: Callable[[dict], str]) -> None:
    """Test handling of configuration files with empty mcpServers dictionary."""
    config_content = {"mcpServers": {}}
    tmp_config_path = create_temp_config_file(config_content)
    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})
    assert len(loaded_params) == 0


def test_config_file_is_empty_json_object(create_temp_config_file: Callable[[dict], str]) -> None:
    """Test handling of configuration files with empty JSON objects."""
    config_content = {}  # Empty JSON object
    tmp_config_path = create_temp_config_file(config_content)
    with pytest.raises(ValueError, match="Missing 'mcpServers' key"):
        load_named_server_configs_from_file(tmp_config_path, {})


def test_config_file_is_empty_string() -> None:
    """Test handling of configuration files with empty content."""
    # Create a file with an empty string
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        suffix=".json",
    ) as tmp_config:
        tmp_config.write("")  # Empty content
        tmp_config_path = tmp_config.name
    try:
        with pytest.raises(json.JSONDecodeError):
            load_named_server_configs_from_file(tmp_config_path, {})
    finally:
        path = Path(tmp_config_path)
        if path.exists():
            path.unlink()
