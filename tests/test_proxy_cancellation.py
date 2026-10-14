"""Cancellation must cross the proxy's independent upstream SDK session."""

import anyio
import pytest
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from mcp_proxy import proxy_server
from mcp_proxy.proxy_server import create_proxy_server


@pytest.mark.parametrize("stall_forward", [False, True])
def test_downstream_cancel_targets_actual_upstream_request_and_releases_handler(
    monkeypatch: pytest.MonkeyPatch,
    stall_forward: bool,
) -> None:
    """Use two real SDK sessions and protocol notifications, without replaying work."""

    async def scenario() -> None:
        entered, cancelled, invocation_done = anyio.Event(), anyio.Event(), anyio.Event()
        forwarding_finished = anyio.Event()
        calls = []
        original = Server("cancellation-fixture")

        @original.list_tools()
        async def list_tools() -> list[types.Tool]:
            return [types.Tool(name="hang", inputSchema={"type": "object"})]

        @original.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
            assert name == "hang"
            assert arguments == {}
            calls.append(request_ctx.get().request_id)
            entered.set()
            try:
                await anyio.sleep_forever()
            finally:
                cancelled.set()

        with anyio.fail_after(3):
            async with create_connected_server_and_client_session(original) as upstream:
                proxied = await create_proxy_server(upstream)
                async with create_connected_server_and_client_session(proxied) as downstream:
                    upstream_id = upstream._request_id  # noqa: SLF001
                    downstream_id = downstream._request_id  # noqa: SLF001
                    if stall_forward:
                        monkeypatch.setattr(
                            proxy_server,
                            "CANCELLATION_FORWARD_TIMEOUT_SECONDS",
                            0.03,
                        )

                        async def stalled_notification(
                            _notification: types.ClientNotification,
                        ) -> None:
                            try:
                                await anyio.sleep_forever()
                            finally:
                                forwarding_finished.set()

                        monkeypatch.setattr(upstream, "send_notification", stalled_notification)

                    async def invoke() -> None:
                        with pytest.raises(McpError, match="Request cancelled"):
                            await downstream.call_tool("hang", {})
                        invocation_done.set()

                    async with anyio.create_task_group() as group:
                        group.start_soon(invoke)
                        await entered.wait()
                        await downstream.send_notification(
                            types.ClientNotification(
                                types.CancelledNotification(
                                    params=types.CancelledNotificationParams(
                                        requestId=downstream_id
                                    ),
                                ),
                            ),
                        )
                        if stall_forward:
                            await forwarding_finished.wait()
                            assert not cancelled.is_set()
                        else:
                            await cancelled.wait()
                        await invocation_done.wait()
                    assert calls == [upstream_id]
                    assert upstream_id != downstream_id

    anyio.run(scenario)
