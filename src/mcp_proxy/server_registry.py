"""Dynamic server registry for runtime registration/unregistration of MCP servers."""

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from mcp.client.stdio import StdioServerParameters

logger = logging.getLogger(__name__)


class ServerRegistry:
    """Manages dynamic server registration with config persistence."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._servers: dict[str, dict[str, Any]] = {}
        self._config_path = Path(config_path) if config_path else None
        self._lock = asyncio.Lock()

    @property
    def servers(self) -> dict[str, dict[str, Any]]:
        return self._servers.copy()

    async def register(self, name: str, config: dict[str, Any]) -> StdioServerParameters:
        """Register a new server. Returns StdioServerParameters for immediate use."""
        if name in self._servers:
            msg = f"Server '{name}' already registered"
            raise ValueError(msg)
        if not config.get("command"):
            msg = "Server config must include 'command'"
            raise ValueError(msg)

        self._servers[name] = config
        async with self._lock:
            await asyncio.to_thread(self._persist)
        logger.info("Registered server '%s': %s", name, config.get("command"))
        return self._to_stdio_params(name, config)

    async def unregister(self, name: str) -> None:
        """Unregister a server by name."""
        if name not in self._servers:
            msg = f"Server '{name}' not found"
            raise KeyError(msg)
        del self._servers[name]
        async with self._lock:
            await asyncio.to_thread(self._persist)
        logger.info("Unregistered server '%s'", name)

    def list_servers(self) -> list[dict[str, Any]]:
        """List all registered servers."""
        return [{"name": name, **config} for name, config in self._servers.items()]

    def _to_stdio_params(self, name: str, config: dict[str, Any]) -> StdioServerParameters:
        return StdioServerParameters(
            command=config["command"],
            args=config.get("args", []),
            env=config.get("env", {}),
            cwd=None,
        )

    def _persist(self) -> None:
        """Persist current state to config file (atomic write, preserves disabled servers)."""
        if not self._config_path:
            return
        try:
            # Load existing config to preserve disabled servers
            if self._config_path.exists():
                with self._config_path.open() as f:
                    data = json.load(f)
            else:
                data = {}

            # Merge: keep disabled servers from existing config
            existing_servers = data.get("mcpServers", {})
            merged: dict[str, Any] = {}
            for name, config in existing_servers.items():
                if not config.get("enabled", True):
                    merged[name] = config  # Preserve disabled servers
            # Add/update active servers from registry
            for name, config in self._servers.items():
                merged[name] = {k: v for k, v in config.items() if k != "name"}

            data["mcpServers"] = merged

            # Atomic write: write to temp file then rename
            tmp_fd = tempfile.NamedTemporaryFile(
                mode="w",
                dir=self._config_path.parent,
                suffix=".tmp",
                delete=False,
            )
            try:
                json.dump(data, tmp_fd, indent=2)
                tmp_fd.close()
                Path(tmp_fd.name).replace(self._config_path)
            except Exception:
                Path(tmp_fd.name).unlink(missing_ok=True)
                raise

            logger.debug("Persisted config to %s", self._config_path)
        except Exception:
            logger.exception("Failed to persist config to %s", self._config_path)

    def load_from_config(self) -> None:
        """Load servers from config file (for initial startup)."""
        if not self._config_path or not self._config_path.exists():
            return
        try:
            with self._config_path.open() as f:
                data = json.load(f)
            for name, config in data.get("mcpServers", {}).items():
                if config.get("enabled", True):
                    self._servers[name] = config
        except Exception:
            logger.exception("Failed to load config from %s", self._config_path)
