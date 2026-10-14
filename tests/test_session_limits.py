"""Configurable StreamableHTTP session limits and backwards-compatible defaults."""
# ruff: noqa: PLR2004

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from mcp.server import Server

from mcp_proxy.__main__ import _create_mcp_settings, _setup_argument_parser
from mcp_proxy.mcp_server import MCPServerSettings, create_single_instance_routes


def _settings(*flags: str) -> MCPServerSettings:
    return _create_mcp_settings(_setup_argument_parser().parse_args([*flags, "uvx"]))


@pytest.mark.parametrize(
    ("flags", "expected_idle", "expected_max"),
    [
        ((), "sdk", "sdk"),
        (("--session-idle-timeout", "12.5", "--max-sessions", "7"), 12.5, 7),
        (("--session-idle-timeout", "0", "--max-sessions", "none"), None, None),
        (("--session-idle-timeout", "none"), None, "sdk"),
    ],
)
def test_cli_limits(flags: tuple[str, ...], expected_idle: object, expected_max: object) -> None:
    """Unset flags preserve SDK defaults; explicit 'none' disables limits."""
    settings = _settings(*flags)
    assert settings.session_idle_timeout == expected_idle
    assert settings.max_sessions == expected_max


@pytest.mark.parametrize(
    ("flag", "invalid"),
    [
        ("--session-idle-timeout", "-1"),
        ("--session-idle-timeout", "nan"),
        ("--session-idle-timeout", "inf"),
        ("--session-idle-timeout", "incorrect"),
        ("--max-sessions", "-1"),
        ("--max-sessions", "0"),
        ("--max-sessions", "1.5"),
    ],
)
def test_reject_invalid_cli_limits(flag: str, invalid: str) -> None:
    """Invalid limits are rejected during parsing, not silently ignored."""
    with pytest.raises(SystemExit):
        _setup_argument_parser().parse_args([flag, invalid, "uvx"])


@patch("mcp_proxy.mcp_server.StreamableHTTPSessionManager")
def test_manager_omits_unspecified_limits(manager: Mock) -> None:
    """Unconfigured settings defer to the installed SDK's native defaults."""
    create_single_instance_routes(Server("test"), stateless_instance=False)
    assert "session_idle_timeout" not in manager.call_args.kwargs
    assert "max_sessions" not in manager.call_args.kwargs


@patch("mcp_proxy.mcp_server.StreamableHTTPSessionManager")
def test_manager_forwards_explicit_limits(manager: Mock) -> None:
    """Explicit disabled/finite values are passed to the SDK."""
    create_single_instance_routes(
        Server("test"),
        stateless_instance=False,
        session_idle_timeout=None,
        max_sessions=8,
    )
    assert manager.call_args.kwargs["session_idle_timeout"] is None
    assert manager.call_args.kwargs["max_sessions"] == 8


@patch("mcp_proxy.mcp_server.StreamableHTTPSessionManager")
def test_stateless_mode_ignores_limits(manager: Mock, caplog: pytest.LogCaptureFixture) -> None:
    """Session limits do not apply to stateless HTTP."""
    create_single_instance_routes(
        Server("test"),
        stateless_instance=True,
        session_idle_timeout=15,
        max_sessions=3,
    )
    assert "session_idle_timeout" not in manager.call_args.kwargs
    assert "max_sessions" not in manager.call_args.kwargs
    assert "stateless mode" in caplog.text


def _config(tmp_path: Path, options: object) -> str:
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"mcpServers": {}, "mcpProxy": options}), encoding="utf-8")
    return str(path)


def test_named_config_and_cli_precedence(tmp_path: Path) -> None:
    """Global JSON options apply to all named routes and CLI flags win."""
    path = _config(tmp_path, {"session_idle_timeout": 0, "max_sessions": 42})
    settings = _settings("--named-server-config", path)
    assert settings.session_idle_timeout is None
    assert settings.max_sessions == 42
    overridden = _settings("--named-server-config", path, "--max-sessions", "10")
    assert overridden.max_sessions == 10
    assert overridden.session_idle_timeout is None


@pytest.mark.parametrize(
    "options",
    [
        {"max_sessions": True},
        {"session_idle_timeout": -2},
        {"max_sessions": "4.5"},
        {"unexpected": 3},
        ["not", "a", "mapping"],
    ],
)
def test_invalid_config_values(tmp_path: Path, options: object) -> None:
    """Malformed named-server transport settings are rejected."""
    with pytest.raises((ValueError, TypeError), match="mcpProxy"):
        _settings("--named-server-config", _config(tmp_path, options))
