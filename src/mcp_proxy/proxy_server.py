"""Create an MCP server that proxies requests through an MCP client.

This server is created independent of any transport mechanism.
"""

import json
import logging
import sys
import typing as t

from mcp import server, types
from mcp.client.session import ClientSession

logger = logging.getLogger(__name__)


def create_roots_forwarding_callback(
    proxy_app: server.Server[object],
) -> t.Callable[..., t.Awaitable[types.ListRootsResult | types.ErrorData]]:
    """Create a list_roots callback that forwards roots/list requests to the upstream client.

    When a downstream server sends a roots/list request, this callback forwards it
    through the proxy's upstream session to the connected client.

    The proxy_app's request_context is only available during active request handling
    (tool calls, resource reads, etc.), which is when downstream servers typically
    request roots. If a roots/list request arrives outside of an active request
    context, an INVALID_REQUEST error is returned.
    """

    async def _forward_roots(_ctx: t.Any) -> types.ListRootsResult | types.ErrorData:  # noqa: ANN401
        try:
            return await proxy_app.request_context.session.list_roots()
        except LookupError:
            # request_context not set — no active upstream session
            logger.warning("roots/list requested but no active upstream session available")
            return types.ErrorData(
                code=types.INVALID_REQUEST,
                message="No active upstream session to forward roots/list request",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to forward roots/list to upstream client: %s", e)
            return types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"Failed to forward roots/list: {e}",
            )

    return _forward_roots


async def create_proxy_server(
    remote_app: ClientSession,
) -> server.Server[object]:
    """Create a server instance from a remote app.

    Roots/list requests from the downstream server are forwarded through the proxy
    server's upstream session to the connected client. The callback is injected into
    remote_app before initialize() so that the downstream server sees roots capability
    advertised during the handshake.
    """
    # Create the proxy server first — needed to build the roots forwarding callback
    # before we advertise capabilities to the downstream server via initialize().
    app: server.Server[object] = server.Server(name="mcp-proxy")

    # Wire roots forwarding: downstream server → proxy → upstream client.
    # Must happen before initialize() so the ClientSession advertises roots
    # capability to the downstream server during the handshake.
    callback = create_roots_forwarding_callback(app)
    remote_app._list_roots_callback = callback  # type: ignore[assignment]  # noqa: SLF001

    logger.debug("Sending initialization request to remote MCP server...")
    response = await remote_app.initialize()
    capabilities = response.capabilities

    # Update the server name now that we know it from the downstream server
    app.name = response.serverInfo.name
    logger.debug("Configuring proxied MCP server...")

    if capabilities.prompts:
        logger.debug("Capabilities: adding Prompts...")

        async def _list_prompts(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            result = await remote_app.list_prompts()
            return types.ServerResult(result)

        app.request_handlers[types.ListPromptsRequest] = _list_prompts

        async def _get_prompt(req: types.GetPromptRequest) -> types.ServerResult:
            result = await remote_app.get_prompt(req.params.name, req.params.arguments)
            return types.ServerResult(result)

        app.request_handlers[types.GetPromptRequest] = _get_prompt

    # DISABLED: kiro-cli 0.11.x (rmcp 0.17) fails to parse resource metadata
    # from downstream servers. Resources not used in our workflow.
    _resources_enabled = False  # rmcp 0.17 incompatible
    if _resources_enabled and capabilities.resources:
        logger.debug("Capabilities: adding Resources...")

        async def _list_resources(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            result = await remote_app.list_resources()
            return types.ServerResult(result)

        app.request_handlers[types.ListResourcesRequest] = _list_resources

        async def _list_resource_templates(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            result = await remote_app.list_resource_templates()
            return types.ServerResult(result)

        app.request_handlers[types.ListResourceTemplatesRequest] = _list_resource_templates

        async def _read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
            result = await remote_app.read_resource(req.params.uri)
            return types.ServerResult(result)

        app.request_handlers[types.ReadResourceRequest] = _read_resource

    if capabilities.logging:
        logger.debug("Capabilities: adding Logging...")

        async def _set_logging_level(req: types.SetLevelRequest) -> types.ServerResult:
            await remote_app.set_logging_level(req.params.level)
            return types.ServerResult(types.EmptyResult())

        app.request_handlers[types.SetLevelRequest] = _set_logging_level

    # DISABLED: same reason as above — resources incompatible with rmcp 0.17
    if _resources_enabled and capabilities.resources:
        logger.debug("Capabilities: adding Resources...")

        async def _subscribe_resource(req: types.SubscribeRequest) -> types.ServerResult:
            await remote_app.subscribe_resource(req.params.uri)
            return types.ServerResult(types.EmptyResult())

        app.request_handlers[types.SubscribeRequest] = _subscribe_resource

        async def _unsubscribe_resource(req: types.UnsubscribeRequest) -> types.ServerResult:
            await remote_app.unsubscribe_resource(req.params.uri)
            return types.ServerResult(types.EmptyResult())

        app.request_handlers[types.UnsubscribeRequest] = _unsubscribe_resource

    if capabilities.tools:
        logger.debug("Capabilities: adding Tools...")

        async def _list_tools(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            tools = await remote_app.list_tools()
            return types.ServerResult(tools)

        app.request_handlers[types.ListToolsRequest] = _list_tools

        async def _call_tool(req: types.CallToolRequest) -> types.ServerResult:
            try:
                # Get request context to access server session for progress forwarding
                from mcp.server.lowlevel.server import request_ctx  # noqa: PLC0415

                ctx = request_ctx.get()

                # Convert meta to dict if present (required for TypedDict compatibility)
                meta_dict = dict(req.params.meta) if req.params.meta else None

                # Create progress forwarder callback
                # Note: The callback receives individual parameters,
                # not a ProgressNotificationParams object
                # Capture sys in closure to avoid scoping issues
                _stderr = sys.stderr

                async def progress_forwarder(
                    progress: float,
                    total: float | None,
                    message: str | None,
                ) -> None:
                    # Extract progress token from meta
                    progress_token = meta_dict.get("progressToken") if meta_dict else None
                    if progress_token is not None:
                        # Forward progress notification back to parent via server session
                        await ctx.session.send_progress_notification(
                            progress_token=progress_token,
                            progress=progress,
                            total=total,
                            message=message,
                            related_request_id=str(ctx.request_id),
                        )
                    else:
                        print(
                            "[MCP-PROXY] WARNING: No progressToken in meta,"
                            " cannot forward progress notification",
                            file=_stderr,
                            flush=True,
                        )

                result = await remote_app.call_tool(
                    req.params.name,
                    (req.params.arguments or {}),
                    meta=meta_dict,
                    progress_callback=progress_forwarder,
                )
                # When the server returns structuredContent but no meaningful text,
                # add a JSON text fallback so stdio clients can display the result.
                content_items = result.content or []
                has_text = any(
                    isinstance(item, types.TextContent) and (item.text or "").strip()
                    for item in content_items
                )
                if not has_text and result.structuredContent is not None:
                    fallback_text = json.dumps(result.structuredContent, indent=2)
                    new_content = list(content_items)
                    new_content.append(
                        types.TextContent(type="text", text=fallback_text),
                    )
                    result = result.model_copy(update={"content": new_content})
                return types.ServerResult(result)
            except Exception as e:  # noqa: BLE001
                return types.ServerResult(
                    types.CallToolResult(
                        content=[types.TextContent(type="text", text=str(e))],
                        isError=True,
                    ),
                )

        app.request_handlers[types.CallToolRequest] = _call_tool

    async def _send_progress_notification(req: types.ProgressNotification) -> None:
        await remote_app.send_progress_notification(
            req.params.progressToken,
            req.params.progress,
            req.params.total,
        )

    app.notification_handlers[types.ProgressNotification] = _send_progress_notification

    async def _complete(req: types.CompleteRequest) -> types.ServerResult:
        result = await remote_app.complete(
            req.params.ref,
            req.params.argument.model_dump(),
        )
        return types.ServerResult(result)

    app.request_handlers[types.CompleteRequest] = _complete

    return app
