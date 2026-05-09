"""Tests for config hot-reload."""

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_proxy.hot_reload import ConfigReloader, create_reload_route


@pytest.fixture
def config_file():
    """Create a temp config file."""
    data = {
        "mcpServers": {
            "server1": {"command": "cmd1", "args": ["--flag"]},
            "server2": {"command": "cmd2"},
        }
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


class TestConfigReloader:
    @pytest.mark.asyncio
    async def test_reload_detects_added(self, config_file) -> None:
        on_reload = AsyncMock(return_value={})
        reloader = ConfigReloader(config_file, on_reload)
        reloader.set_last_config(
            {"mcpServers": {"server1": {"command": "cmd1", "args": ["--flag"]}}}
        )

        result = await reloader.reload()
        assert "server2" in result["added"]
        assert result["removed"] == []
        on_reload.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_detects_removed(self, config_file) -> None:
        on_reload = AsyncMock(return_value={})
        reloader = ConfigReloader(config_file, on_reload)
        reloader.set_last_config(
            {
                "mcpServers": {
                    "server1": {"command": "cmd1", "args": ["--flag"]},
                    "server2": {"command": "cmd2"},
                    "server3": {"command": "cmd3"},
                }
            }
        )

        result = await reloader.reload()
        assert "server3" in result["removed"]
        assert result["added"] == []

    @pytest.mark.asyncio
    async def test_reload_detects_updated(self, config_file) -> None:
        on_reload = AsyncMock(return_value={})
        reloader = ConfigReloader(config_file, on_reload)
        reloader.set_last_config(
            {
                "mcpServers": {
                    "server1": {"command": "cmd1", "args": ["--old"]},
                    "server2": {"command": "cmd2"},
                }
            }
        )

        result = await reloader.reload()
        assert "server1" in result["updated"]

    @pytest.mark.asyncio
    async def test_reload_no_changes(self, config_file) -> None:
        on_reload = AsyncMock(return_value={})
        reloader = ConfigReloader(config_file, on_reload)
        reloader.set_last_config(
            {
                "mcpServers": {
                    "server1": {"command": "cmd1", "args": ["--flag"]},
                    "server2": {"command": "cmd2"},
                }
            }
        )

        result = await reloader.reload()
        assert result["added"] == []
        assert result["removed"] == []
        assert result["updated"] == []

    @pytest.mark.asyncio
    async def test_reload_invalid_file(self) -> None:
        on_reload = AsyncMock(return_value={})
        reloader = ConfigReloader("/nonexistent/file.json", on_reload)
        result = await reloader.reload()
        assert "error" in result
        on_reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_invalid_json(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json{{{")
            path = f.name

        on_reload = AsyncMock(return_value={})
        reloader = ConfigReloader(path, on_reload)
        result = await reloader.reload()
        assert "error" in result
        Path(path).unlink()

    async def test_load_current(self, config_file) -> None:
        reloader = ConfigReloader(config_file, AsyncMock())
        config = await reloader.load_current()
        assert "mcpServers" in config
        assert "server1" in config["mcpServers"]


class TestReloadRoute:
    def test_reload_endpoint_no_auth(self, config_file) -> None:
        on_reload = AsyncMock(return_value={})
        reloader = ConfigReloader(config_file, on_reload)
        reloader.set_last_config({"mcpServers": {}})

        routes = create_reload_route(reloader)
        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post("/reload")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "reloaded"
        assert "server1" in data["added"]
        assert "server2" in data["added"]

    def test_reload_endpoint_with_auth(self, config_file) -> None:
        on_reload = AsyncMock(return_value={})
        reloader = ConfigReloader(config_file, on_reload)
        reloader.set_last_config({"mcpServers": {}})

        routes = create_reload_route(reloader, api_key="admin-key")
        app = Starlette(routes=routes)
        client = TestClient(app)

        # Without auth
        resp = client.post("/reload")
        assert resp.status_code == 401

        # With auth
        resp = client.post("/reload", headers={"Authorization": "Bearer admin-key"})
        assert resp.status_code == 200

    def test_reload_endpoint_error(self) -> None:
        on_reload = AsyncMock(return_value={})
        reloader = ConfigReloader("/nonexistent.json", on_reload)

        routes = create_reload_route(reloader)
        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.post("/reload")
        assert resp.status_code == 500
        assert "error" in resp.json()
