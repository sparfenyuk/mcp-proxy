"""Tests for installed console entry points."""

from importlib.metadata import distribution

import pytest

from mcp_proxy.__main__ import main


@pytest.mark.parametrize("name", ["mcp-proxy", "mcp-reverse-proxy"])
def test_console_entry_point_loads_main(name: str) -> None:
    """Both installed commands resolve to the shared CLI entry point."""
    entry_point = next(
        entry
        for entry in distribution("mcp-proxy").entry_points
        if entry.group == "console_scripts" and entry.name == name
    )
    assert entry_point.load() is main
