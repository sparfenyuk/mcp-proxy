"""Admin API routes for dynamic server management."""

import json
import logging
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .server_registry import ServerRegistry

logger = logging.getLogger(__name__)


def _check_admin_auth(request: Request, api_key: str | None) -> JSONResponse | None:
    """Check admin auth. Returns error response if unauthorized, None if OK."""
    if not api_key:
        return None  # No auth configured, allow all
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ")
    if not auth.startswith("Bearer ") or not secrets.compare_digest(token, api_key):
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


def create_admin_routes(
    registry: ServerRegistry,
    api_key: str | None = None,
    on_register: "callable | None" = None,  # type: ignore
    on_unregister: "callable | None" = None,  # type: ignore
) -> list[Route]:
    """Create admin API routes for server management.

    on_register: async callback(name, StdioServerParameters) called after registration
    on_unregister: async callback(name) called after unregistration
    """

    async def list_servers(request: Request) -> JSONResponse:
        if err := _check_admin_auth(request, api_key):
            return err
        return JSONResponse({"servers": registry.list_servers()})

    async def register_server(request: Request) -> JSONResponse:
        if err := _check_admin_auth(request, api_key):
            return err
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        name = body.get("name")
        if not name:
            return JSONResponse({"error": "Missing 'name'"}, status_code=400)

        config = {k: v for k, v in body.items() if k != "name"}
        try:
            params = await registry.register(name, config)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=409)

        if on_register and config.get("enabled", True):
            try:
                await on_register(name, params)  # type: ignore
            except Exception as e:
                # Rollback registration on failure
                registry.unregister(name)  # type: ignore
                logger.exception("Failed to start server '%s'", name)
                return JSONResponse(
                    {"error": f"Failed to start server: {e}"},
                    status_code=500,
                )

        return JSONResponse({"status": "registered", "name": name}, status_code=201)

    async def unregister_server(request: Request) -> JSONResponse:
        if err := _check_admin_auth(request, api_key):
            return err
        name = request.path_params["name"]

        # Check existence before cleanup
        if name not in registry.servers:
            return JSONResponse({"error": f"Server '{name}' not found"}, status_code=404)

        if on_unregister:
            try:
                await on_unregister(name)  # type: ignore
            except Exception:
                logger.exception("Error during cleanup for server '%s'", name)

        try:
            await registry.unregister(name)
        except KeyError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

        return JSONResponse({"status": "unregistered", "name": name})

    return [
        Route("/servers", endpoint=list_servers, methods=["GET"]),
        Route("/servers", endpoint=register_server, methods=["POST"]),
        Route("/servers/{name}", endpoint=unregister_server, methods=["DELETE"]),
    ]
