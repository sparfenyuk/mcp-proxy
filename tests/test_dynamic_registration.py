"""Tests for dynamic server registration."""

import json
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_proxy.admin_api import create_admin_routes
from mcp_proxy.server_registry import ServerRegistry


class TestServerRegistry:
    async def test_register_server(self) -> None:
        registry = ServerRegistry()
        params = await registry.register("test", {"command": "echo", "args": ["hello"]})
        assert params.command == "echo"
        assert params.args == ["hello"]
        assert "test" in registry.servers

    async def test_register_duplicate_raises(self) -> None:
        registry = ServerRegistry()
        await registry.register("test", {"command": "echo"})
        with pytest.raises(ValueError, match="already registered"):
            await registry.register("test", {"command": "echo2"})

    async def test_register_no_command_raises(self) -> None:
        registry = ServerRegistry()
        with pytest.raises(ValueError, match="must include 'command'"):
            await registry.register("test", {"args": ["hello"]})

    async def test_unregister_server(self) -> None:
        registry = ServerRegistry()
        await registry.register("test", {"command": "echo"})
        await registry.unregister("test")
        assert "test" not in registry.servers

    async def test_unregister_not_found_raises(self) -> None:
        registry = ServerRegistry()
        with pytest.raises(KeyError, match="not found"):
            await registry.unregister("nonexistent")

    async def test_list_servers(self) -> None:
        registry = ServerRegistry()
        await registry.register("s1", {"command": "cmd1"})
        await registry.register("s2", {"command": "cmd2", "args": ["--flag"]})
        servers = registry.list_servers()
        assert len(servers) == 2
        assert servers[0]["name"] == "s1"
        assert servers[1]["command"] == "cmd2"

    async def test_persist_and_load(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            path = f.name

        registry = ServerRegistry(config_path=path)
        await registry.register("test", {"command": "echo", "args": ["hi"]})

        # Verify persisted
        with open(path) as f:
            data = json.load(f)
        assert "test" in data["mcpServers"]

        # Load in new registry
        registry2 = ServerRegistry(config_path=path)
        registry2.load_from_config()
        assert "test" in registry2.servers

        Path(path).unlink()

    def test_load_from_nonexistent_config(self) -> None:
        registry = ServerRegistry(config_path="/nonexistent/path.json")
        registry.load_from_config()  # Should not raise
        assert registry.servers == {}


class TestAdminAPI:
    def _make_app(self, api_key=None, on_register=None, on_unregister=None):
        registry = ServerRegistry()
        routes = create_admin_routes(
            registry,
            api_key=api_key,
            on_register=on_register,
            on_unregister=on_unregister,
        )
        app = Starlette(routes=routes)
        return app, registry

    async def test_list_servers_empty(self) -> None:
        app, _ = self._make_app()
        client = TestClient(app)
        resp = client.get("/servers")
        assert resp.status_code == 200
        assert resp.json() == {"servers": []}

    async def test_register_server_no_auth(self) -> None:
        app, registry = self._make_app()
        client = TestClient(app)
        resp = client.post("/servers", json={"name": "test", "command": "echo"})
        assert resp.status_code == 201
        assert resp.json()["status"] == "registered"
        assert "test" in registry.servers

    async def test_register_server_with_auth(self) -> None:
        app, _ = self._make_app(api_key="secret123")
        client = TestClient(app)
        # Without auth
        resp = client.post("/servers", json={"name": "test", "command": "echo"})
        assert resp.status_code == 401
        # With auth
        resp = client.post(
            "/servers",
            json={"name": "test", "command": "echo"},
            headers={"Authorization": "Bearer secret123"},
        )
        assert resp.status_code == 201

    def test_register_missing_name(self) -> None:
        app, _ = self._make_app()
        client = TestClient(app)
        resp = client.post("/servers", json={"command": "echo"})
        assert resp.status_code == 400

    def test_register_missing_command(self) -> None:
        app, _ = self._make_app()
        client = TestClient(app)
        resp = client.post("/servers", json={"name": "test"})
        assert resp.status_code == 409  # ValueError from registry

    def test_register_duplicate(self) -> None:
        app, _ = self._make_app()
        client = TestClient(app)
        client.post("/servers", json={"name": "test", "command": "echo"})
        resp = client.post("/servers", json={"name": "test", "command": "echo2"})
        assert resp.status_code == 409

    async def test_unregister_server(self) -> None:
        app, registry = self._make_app()
        client = TestClient(app)
        client.post("/servers", json={"name": "test", "command": "echo"})
        resp = client.delete("/servers/test")
        assert resp.status_code == 200
        assert "test" not in registry.servers

    def test_unregister_not_found(self) -> None:
        app, _ = self._make_app()
        client = TestClient(app)
        resp = client.delete("/servers/nonexistent")
        assert resp.status_code == 404

    def test_unregister_with_auth(self) -> None:
        app, _ = self._make_app(api_key="key")
        client = TestClient(app)
        # Register first
        client.post(
            "/servers",
            json={"name": "test", "command": "echo"},
            headers={"Authorization": "Bearer key"},
        )
        # Delete without auth
        resp = client.delete("/servers/test")
        assert resp.status_code == 401
        # Delete with auth
        resp = client.delete("/servers/test", headers={"Authorization": "Bearer key"})
        assert resp.status_code == 200

    def test_list_after_register(self) -> None:
        app, _ = self._make_app()
        client = TestClient(app)
        client.post("/servers", json={"name": "s1", "command": "cmd1"})
        client.post("/servers", json={"name": "s2", "command": "cmd2", "args": ["--x"]})
        resp = client.get("/servers")
        servers = resp.json()["servers"]
        assert len(servers) == 2
        names = [s["name"] for s in servers]
        assert "s1" in names
        assert "s2" in names
