"""Transport-only liveness probes must not allocate MCP sessions."""

from http import HTTPStatus
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server import Server
from starlette.applications import Starlette
from starlette.routing import Mount

from mcp_proxy.mcp_server import create_single_instance_routes


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
@pytest.mark.parametrize("prefix", ["", "/servers/example"])
async def test_healthz_does_not_enter_session_manager(*, stateless: bool, prefix: str) -> None:
    """Both GET and HEAD answer without touching the MCP session manager."""
    routes, manager = create_single_instance_routes(
        Server("healthz-test"), stateless_instance=stateless
    )
    app = Starlette(routes=[Mount(prefix, routes=routes)] if prefix else routes)
    app.router.redirect_slashes = False
    with patch.object(manager, "handle_request", new_callable=AsyncMock) as handle:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for method in (client.get, client.head):
                response = await method(prefix + "/healthz")
                assert response.status_code == HTTPStatus.NO_CONTENT
                assert response.content == b""
        handle.assert_not_awaited()
