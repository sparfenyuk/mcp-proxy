"""Tests for CLI argument handling."""

from __future__ import annotations

import typing as t
from unittest.mock import Mock, patch

import pytest

from mcp_proxy.__main__ import _create_mcp_settings, _normalize_verify_ssl, _setup_argument_parser
from mcp_proxy.httpx_client import custom_httpx_client
from mcp_proxy.mcp_server import DEFAULT_EXPOSE_HEADERS

if t.TYPE_CHECKING:
    from argparse import ArgumentParser


@pytest.fixture
def parser() -> ArgumentParser:
    """Return a fresh argument parser for each test."""
    return _setup_argument_parser()


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
