"""Configurable StreamableHTTP session limits and backwards-compatible defaults."""
# ruff: noqa: PLR2004

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette

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


@pytest.mark.parametrize("underflow", ["1e-10000", "-1e-10000"])
def test_positive_or_negative_timeout_underflow_does_not_disable_reaping(
    underflow: str,
) -> None:
    """Float underflow is not equivalent to an intentional numeric zero."""
    with pytest.raises(SystemExit):
        _setup_argument_parser().parse_args(["--session-idle-timeout", underflow, "uvx"])


@pytest.mark.parametrize("explicit_zero", ["0", "0.0", "0e1000", "none"])
def test_explicit_numeric_zero_or_none_disables_idle_timeout(explicit_zero: str) -> None:
    """Maintain support for explicitly disabling cleanup without allowing underflow."""
    settings = _settings("--session-idle-timeout", explicit_zero)
    assert settings.session_idle_timeout is None


def test_declared_mcp_sdk_minimum_supports_configurable_limits() -> None:
    """Packaging must not permit an SDK lacking the new manager constructor arguments."""
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp>=1.30.0,<2"' in pyproject


def test_real_sdk_manager_accepts_explicit_session_limits() -> None:
    """Do not mock the constructor: catch SDK keyword incompatibility."""
    _, manager = create_single_instance_routes(
        Server("sdk-compatibility"),
        stateless_instance=False,
        session_idle_timeout=2.5,
        max_sessions=3,
    )
    assert isinstance(manager, StreamableHTTPSessionManager)
    assert manager.session_idle_timeout == 2.5
    assert manager.max_sessions == 3


@pytest.mark.asyncio
async def test_real_sdk_enforces_cap_then_releases_slot_after_delete() -> None:
    """A real session manager returns 503 at capacity and reopens after DELETE."""
    routes, manager = create_single_instance_routes(
        Server("session-cap-integration"),
        stateless_instance=False,
        session_idle_timeout=20.0,
        max_sessions=1,
    )
    app = Starlette(routes=routes)
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": types.LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "session-limit-test", "version": "1.0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    async with (
        manager.run(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        first = await client.post("/mcp/", json=init, headers=headers)
        assert first.status_code == 200
        first_id = first.headers["mcp-session-id"]
        capped = await client.post("/mcp/", json=init, headers=headers)
        assert capped.status_code == 503

        deletion = await client.delete(
            "/mcp/",
            headers={
                **headers,
                "Mcp-Session-Id": first_id,
                "MCP-Protocol-Version": types.LATEST_PROTOCOL_VERSION,
            },
        )
        assert deletion.status_code in (200, 202)
        reopened = await client.post("/mcp/", json=init, headers=headers)
        assert reopened.status_code == 200
        assert reopened.headers["mcp-session-id"] != first_id


@pytest.mark.asyncio
async def test_real_sdk_idle_expiry_releases_session_capacity() -> None:
    """Idle expiry releases capacity for a new stateful HTTP client."""
    routes, manager = create_single_instance_routes(
        Server("session-idle-integration"),
        stateless_instance=False,
        session_idle_timeout=0.05,
        max_sessions=1,
    )
    app = Starlette(routes=routes)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": types.LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "idle-test", "version": "1.0"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    async with (
        manager.run(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        initial = await client.post("/mcp/", json=initialize, headers=headers)
        assert initial.status_code == 200
        old_session_id = initial.headers["mcp-session-id"]

        capped = await client.post("/mcp/", json=initialize, headers=headers)
        assert capped.status_code == 503

        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            new_session = await client.post("/mcp/", json=initialize, headers=headers)
            if new_session.status_code == 200:
                assert new_session.headers["mcp-session-id"] != old_session_id
                break
            assert new_session.status_code == 503
        else:
            pytest.fail("Idle timeout never released session capacity")


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


def test_named_config_rejects_underflow_instead_of_disabling_cleanup(tmp_path: Path) -> None:
    """The same timeout parser must fail safely for JSON-originated values."""
    with pytest.raises(ValueError, match=r"mcpProxy\.session_idle_timeout"):
        _settings(
            "--named-server-config",
            _config(tmp_path, {"session_idle_timeout": "1e-10000"}),
        )


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
