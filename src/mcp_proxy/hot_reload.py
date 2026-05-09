"""Config hot-reload via SIGHUP signal or POST /reload endpoint."""

import asyncio
import json
import logging
import secrets
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)


class ConfigReloader:
    """Watches config file and applies changes on reload signal."""

    def __init__(
        self,
        config_path: str | Path,
        on_reload: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        self._config_path = Path(config_path)
        self._on_reload = on_reload
        self._last_config: dict[str, Any] = {}
        self._reload_lock = asyncio.Lock()

    def _load_current_sync(self) -> dict[str, Any]:
        """Load and return current config from file (blocking I/O)."""
        with self._config_path.open() as f:
            return json.load(f)  # type: ignore

    async def load_current(self) -> dict[str, Any]:
        """Load and return current config from file (non-blocking)."""
        return await asyncio.to_thread(self._load_current_sync)

    async def reload(self) -> dict[str, Any]:
        """Reload config and apply changes. Returns diff summary."""
        async with self._reload_lock:
            try:
                new_config = await self.load_current()
                if not isinstance(new_config, dict):
                    return {"error": "Config file must contain a JSON object"}
            except (FileNotFoundError, json.JSONDecodeError) as e:
                logger.exception("Failed to reload config: %s", e)
                return {"error": str(e)}

            new_servers = new_config.get("mcpServers", {})
            old_servers = self._last_config.get("mcpServers", {})

            added = set(new_servers.keys()) - set(old_servers.keys())
            removed = set(old_servers.keys()) - set(new_servers.keys())
            updated = {
                name
                for name in set(new_servers.keys()) & set(old_servers.keys())
                if new_servers[name] != old_servers[name]
            }

            try:
                await self._on_reload(
                    {
                        "added": {name: new_servers[name] for name in added},
                        "removed": list(removed),
                        "updated": {name: new_servers[name] for name in updated},
                        "full_config": new_config,
                    }
                )
            except Exception as e:
                logger.exception("Error in reload callback: %s", e)
                return {"error": f"Reload callback failed: {e}"}

            self._last_config = new_config
            return {
                "status": "reloaded",
                "added": list(added),
                "removed": list(removed),
                "updated": list(updated),
            }

    def install_signal_handler(self, loop: asyncio.AbstractEventLoop) -> None:
        """Install SIGHUP handler (Unix only)."""
        if sys.platform == "win32":
            logger.warning("SIGHUP not supported on Windows")
            return

        def _handler() -> None:
            logger.info("Received SIGHUP, reloading config...")
            loop.create_task(self.reload())

        loop.add_signal_handler(signal.SIGHUP, _handler)
        logger.info("SIGHUP handler installed for config reload")

    def set_last_config(self, config: dict[str, Any]) -> None:
        """Set the baseline config for diff calculation."""
        self._last_config = config


def create_reload_route(
    reloader: ConfigReloader,
    api_key: str | None = None,
) -> list[Route]:
    """Create POST /reload route."""

    async def handle_reload(request: Request) -> JSONResponse:
        if api_key:
            auth = request.headers.get("authorization", "")
            token = auth.removeprefix("Bearer ")
            if not auth.startswith("Bearer ") or not secrets.compare_digest(token, api_key):
                return JSONResponse(
                    {"error": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        result = await reloader.reload()
        status = 200 if "error" not in result else 500
        return JSONResponse(result, status_code=status)

    return [Route("/reload", endpoint=handle_reload, methods=["POST"])]
