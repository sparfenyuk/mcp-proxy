"""Behavioral checks for concise MCP proxy timestamps."""

import logging
import time
from unittest.mock import patch

from mcp_proxy.__main__ import _setup_logging


def test_logging_timestamp_renders_milliseconds_once() -> None:
    """Render one normalized millisecond field using the CLI configuration."""
    with patch("logging.basicConfig") as configure:
        _setup_logging(level="INFO", debug=False)
    configure.assert_called_once()
    settings = configure.call_args.kwargs
    formatter = logging.Formatter(settings["format"], datefmt=settings["datefmt"])
    record = logging.LogRecord("mcp_proxy", logging.INFO, "", 0, "example", (), None)
    record.created = 1_700_000_000.5
    record.msecs = 500
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
    assert formatter.format(record) == f"[I {timestamp}.500 mcp_proxy] example"
