"""Tests for CLI argument handling."""

from __future__ import annotations

import json
import logging
import sys
import typing as t
from http import HTTPStatus
from unittest.mock import Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from httpx_auth import OAuth2ClientCredentials
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware

from mcp_proxy import __main__ as cli
from mcp_proxy.__main__ import (
    _create_mcp_settings,
    _handle_sse_client_mode,
    _normalize_verify_ssl,
    _setup_argument_parser,
)
from mcp_proxy.httpx_client import custom_httpx_client
from mcp_proxy.mcp_server import DEFAULT_EXPOSE_HEADERS

if t.TYPE_CHECKING:
    from argparse import ArgumentParser
    from pathlib import Path

    from mcp.client.stdio import StdioServerParameters

    from mcp_proxy.mcp_server import MCPServerSettings


_INBOUND_ENV_VALUE = "inbound-secret"
_CLI_CHILD_ENV_VALUE = "explicit-cli-token"
_CONFIG_CHILD_ENV_VALUE = "explicit-config-token"


@pytest.fixture
def parser() -> ArgumentParser:
    """Return a fresh argument parser for each test."""
    return _setup_argument_parser()


@pytest.mark.parametrize(
    "origin_flags",
    [
        ["--allow-origin", "https://first.example", "https://second.example"],
        ["--allow-origin", "https://first.example", "--allow-origin", "https://second.example"],
    ],
)
async def test_allow_origin_flags_preserve_all_origins(
    parser: ArgumentParser, origin_flags: list[str]
) -> None:
    """Every origin supplied in one or multiple flags passes CORS preflight."""
    args = parser.parse_args(["uvx", "mcp-server-fetch", *origin_flags])
    settings = _create_mcp_settings(args)
    origins = ["https://first.example", "https://second.example"]
    assert settings.allow_origins == origins

    app = CORSMiddleware(Starlette(), allow_origins=settings.allow_origins, allow_methods=["GET"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for origin in origins:
            response = await client.options(
                "/sse",
                headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
            )
            assert response.status_code == HTTPStatus.OK
            assert response.headers["Access-Control-Allow-Origin"] == origin


def test_verify_ssl_cli_false(parser: ArgumentParser) -> None:
    """Calling --verify-ssl false disables verification."""
    args = parser.parse_args(["--verify-ssl", "false", "https://example.com"])
    assert _normalize_verify_ssl(args.verify_ssl) is False


def test_verify_ssl_cli_true(parser: ArgumentParser) -> None:
    """Passing --verify-ssl true enforces verification."""
    args = parser.parse_args(["--verify-ssl", "true", "https://example.com"])
    assert _normalize_verify_ssl(args.verify_ssl) is True


def test_verify_ssl_cli_cert_path(parser: ArgumentParser) -> None:
    """Passing a certificate path keeps the string value."""
    args = parser.parse_args(["--verify-ssl", "certs.pem", "https://example.com"])
    assert _normalize_verify_ssl(args.verify_ssl) == "certs.pem"


def test_verify_ssl_cli_no_verify_alias(parser: ArgumentParser) -> None:
    """The --no-verify-ssl alias sets the value to False."""
    args = parser.parse_args(["--no-verify-ssl", "https://example.com"])
    assert args.verify_ssl is False
    assert _normalize_verify_ssl(args.verify_ssl) is False


def test_expose_header_argument(parser: ArgumentParser) -> None:
    """--expose-header collects header names."""
    args = parser.parse_args(["--expose-header", "Custom-Header", "uvx", "mcp-server-fetch"])
    assert args.expose_headers == ["Custom-Header"]


def test_expose_header_multiple(parser: ArgumentParser) -> None:
    """Multiple --expose-header flags accumulate headers."""
    args = parser.parse_args(
        [
            "--expose-header",
            "Header-One",
            "--expose-header",
            "Header-Two",
            "uvx",
            "mcp-server-fetch",
        ],
    )
    assert args.expose_headers == ["Header-One", "Header-Two"]


def test_create_mcp_settings_default_expose_headers(parser: ArgumentParser) -> None:
    """_create_mcp_settings uses default expose headers when not provided."""
    args = parser.parse_args(["uvx", "mcp-server-fetch", "--port", "8080"])
    settings = _create_mcp_settings(args)
    assert settings.expose_headers == list(DEFAULT_EXPOSE_HEADERS)


def test_create_mcp_settings_custom_expose_headers(parser: ArgumentParser) -> None:
    """_create_mcp_settings uses custom expose headers when provided."""
    args = parser.parse_args(
        [
            "--expose-header",
            "Custom-Header",
            "--expose-header",
            "Another-Header",
            "uvx",
            "mcp-server-fetch",
            "--port",
            "8080",
        ],
    )
    settings = _create_mcp_settings(args)
    assert settings.expose_headers == ["Custom-Header", "Another-Header"]


@pytest.mark.parametrize(
    ("cli_token", "env_token", "expected"),
    [
        (None, None, None),
        (None, "environment-token", "environment-token"),
        ("cli-token", "environment-token", "cli-token"),
        ("", "environment-token", ""),
        ("cli-token", "", "cli-token"),
    ],
)
def test_inbound_bearer_token_uses_explicit_cli_precedence(
    parser: ArgumentParser,
    monkeypatch: pytest.MonkeyPatch,
    cli_token: str | None,
    env_token: str | None,
    expected: str | None,
) -> None:
    """CLI presence, including an empty value, takes precedence over the inbound environment."""
    if env_token is None:
        monkeypatch.delenv("MCP_PROXY_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MCP_PROXY_AUTH_TOKEN", env_token)
    options = [] if cli_token is None else ["--auth-bearer-token", cli_token]
    args = parser.parse_args([*options, "stdio-server"])
    assert _create_mcp_settings(args).auth_bearer_token == expected


def test_inbound_bearer_token_help_does_not_show_configured_value(
    parser: ArgumentParser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Help documents the safer environment input without rendering its current value."""
    monkeypatch.setenv("MCP_PROXY_AUTH_TOKEN", "help-output-secret")
    help_text = parser.format_help()
    assert "MCP_PROXY_AUTH_TOKEN" in help_text
    assert "help-output-secret" not in help_text


def test_reverse_client_mode_does_not_read_inbound_auth_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inbound token setting is only constructed after the reverse-client early return."""
    monkeypatch.setenv("MCP_PROXY_AUTH_TOKEN", "inbound-secret")
    monkeypatch.setattr(sys, "argv", ["mcp-proxy", "https://upstream.example/sse"])
    captured: list[str] = []
    monkeypatch.setattr(
        cli,
        "_handle_sse_client_mode",
        lambda *args, **_: captured.append(args[0].command_or_url),
    )
    monkeypatch.setattr(
        cli,
        "_create_mcp_settings",
        lambda _args: pytest.fail("server settings built"),
    )
    cli.main()
    assert captured == ["https://upstream.example/sse"]


def test_pass_environment_filters_only_inbound_auth_and_preserves_explicit_overlays(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bulk child inheritance drops the gate secret before default/config overlays."""
    config_path = tmp_path / "servers.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "named": {
                        "command": "python",
                        "args": ["-c", "pass"],
                        "env": {"MCP_PROXY_AUTH_TOKEN": _CONFIG_CHILD_ENV_VALUE},
                    },
                },
            },
        ),
    )
    monkeypatch.setenv("MCP_PROXY_AUTH_TOKEN", _INBOUND_ENV_VALUE)
    monkeypatch.setenv("MCP_PROXY_KEEP", "keep-value")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcp-proxy",
            "--pass-environment",
            "--named-server-config",
            str(config_path),
            "--env",
            "MCP_PROXY_AUTH_TOKEN",
            _CLI_CHILD_ENV_VALUE,
            "python",
        ],
    )

    async def capture_run_mcp_server(
        *,
        default_server_params: StdioServerParameters | None,
        named_server_params: dict[str, StdioServerParameters],
        mcp_settings: MCPServerSettings,
    ) -> None:
        assert default_server_params is not None
        assert default_server_params.env["MCP_PROXY_AUTH_TOKEN"] == _CLI_CHILD_ENV_VALUE
        assert default_server_params.env["MCP_PROXY_KEEP"] == "keep-value"
        assert named_server_params["named"].env["MCP_PROXY_AUTH_TOKEN"] == _CONFIG_CHILD_ENV_VALUE
        assert named_server_params["named"].env["MCP_PROXY_KEEP"] == "keep-value"
        assert mcp_settings.auth_bearer_token == _INBOUND_ENV_VALUE

    monkeypatch.setattr(cli, "run_mcp_server", capture_run_mcp_server)
    cli.main()

    assert _INBOUND_ENV_VALUE not in caplog.text
    assert _CLI_CHILD_ENV_VALUE not in caplog.text
    assert _CONFIG_CHILD_ENV_VALUE not in caplog.text


def test_allowed_host_argument_accumulates_trusted_authorities(parser: ArgumentParser) -> None:
    """Repeated --allowed-host flags are preserved for server settings."""
    args = parser.parse_args(
        [
            "--allowed-host",
            "mcp.example.com",
            "--allowed-host",
            "mcp.example.com:8443",
            "uvx",
            "mcp-server-fetch",
        ],
    )
    assert _create_mcp_settings(args).allowed_hosts == [
        "mcp.example.com",
        "mcp.example.com:8443",
    ]


@patch("mcp_proxy.httpx_client.httpx.AsyncClient")
def test_custom_httpx_client_disable_ssl(mock_async_client: Mock) -> None:
    """custom_httpx_client passes verify=False to httpx when disabled."""
    custom_httpx_client(verify_ssl=False)
    kwargs = mock_async_client.call_args.kwargs
    assert kwargs["verify"] is False


@patch("mcp_proxy.httpx_client.httpx.AsyncClient")
def test_custom_httpx_client_cert_path(mock_async_client: Mock) -> None:
    """custom_httpx_client forwards certificate bundle paths."""
    custom_httpx_client(verify_ssl="/tmp/cert.pem")  # noqa: S108
    kwargs = mock_async_client.call_args.kwargs
    assert kwargs["verify"] == "/tmp/cert.pem"  # noqa: S108


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "invalid"])
def test_initialization_timeout_rejects_invalid_values(parser: ArgumentParser, value: str) -> None:
    """Initialization deadlines must be finite and positive."""
    with pytest.raises(SystemExit):
        parser.parse_args([f"--initialization-timeout={value}"])


@pytest.mark.parametrize("value", [None, "2.5"])
def test_initialization_timeout_settings(parser: ArgumentParser, value: str | None) -> None:
    """Propagate the deadline while preserving the unlimited default."""
    args = parser.parse_args([] if value is None else ["--initialization-timeout", value])
    assert _create_mcp_settings(args).initialization_timeout == (
        None if value is None else float(value)
    )


@pytest.mark.parametrize(
    "oauth_flags",
    [
        ["--client-id", "cid"],
        ["--client-secret", "secret"],
        ["--token-url", "https://example.com/token"],
        ["--client-id", "cid", "--token-url", "https://example.com/token"],
        ["--client-id", "cid", "--client-secret", "secret"],
        ["--client-secret", "secret", "--token-url", "https://example.com/token"],
    ],
)
def test_partial_oauth2_flags_exit_without_connecting(
    parser: ArgumentParser,
    oauth_flags: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial OAuth2 trio must fail fast instead of connecting without auth."""

    async def must_not_connect(*_args: t.Any, **_kwargs: t.Any) -> None:  # noqa: ANN401
        pytest.fail("partial OAuth2 flags must not start a client connection")

    args = parser.parse_args([*oauth_flags, "https://example.com/sse"])
    monkeypatch.setattr(cli, "run_sse_client", must_not_connect)
    with pytest.raises(SystemExit):
        _handle_sse_client_mode(args, logging.getLogger(__name__))


def test_complete_oauth2_flags_build_client_credentials_auth(
    parser: ArgumentParser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three OAuth2 flags together build an OAuth2 client-credentials auth handler."""
    args = parser.parse_args(
        [
            "--client-id",
            "cid",
            "--client-secret",
            "secret",
            "--token-url",
            "https://example.com/token",
            "https://example.com/sse",
        ],
    )
    captured: dict[str, t.Any] = {}

    async def capture_run(
        _url: str,
        _headers: t.Any = None,  # noqa: ANN401
        auth: t.Any = None,  # noqa: ANN401
        **_kwargs: t.Any,  # noqa: ANN401
    ) -> None:
        captured["auth"] = auth

    monkeypatch.setattr(cli, "run_sse_client", capture_run)
    _handle_sse_client_mode(args, logging.getLogger(__name__))
    assert isinstance(captured["auth"], OAuth2ClientCredentials)
