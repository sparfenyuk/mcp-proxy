#
# MCP Foxxy Bridge - Bridge Server
#
# Copyright (C) 2024 Billy Bryant
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Create an MCP server that bridges multiple MCP servers.

This server aggregates capabilities from multiple MCP servers and provides
a unified interface for AI tools to interact with all of them.
"""

import logging
import typing as t
from typing import Any, Dict, Optional

from mcp import server, types

from .server_manager import ServerManager
from .config_loader import BridgeConfiguration

logger = logging.getLogger(__name__)


async def create_bridge_server(bridge_config: BridgeConfiguration) -> server.Server[object]:
    """Create a bridge server that aggregates multiple MCP servers.
    
    Args:
        bridge_config: Configuration for the bridge and all MCP servers.
        
    Returns:
        A configured MCP server that bridges to multiple backends.
    """
    logger.info("Creating bridge server with %d configured servers", len(bridge_config.servers))
    
    # Create and start the server manager
    server_manager = ServerManager(bridge_config)
    await server_manager.start()
    
    # Create the bridge server
    bridge_name = "MCP Foxxy Bridge"
    app: server.Server[object] = server.Server(name=bridge_name)
    
    # Store server manager for cleanup
    app._server_manager = server_manager  # type: ignore
    
    # Configure capabilities based on aggregation settings
    if bridge_config.bridge.aggregation.prompts:
        logger.debug("Configuring prompts aggregation...")
        
        async def _list_prompts(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            try:
                prompts = server_manager.get_aggregated_prompts()
                result = types.ListPromptsResult(prompts=prompts)
                return types.ServerResult(result)
            except Exception as e:
                logger.error("Error listing prompts: %s", str(e))
                return types.ServerResult(types.ListPromptsResult(prompts=[]))
        
        app.request_handlers[types.ListPromptsRequest] = _list_prompts
        
        async def _get_prompt(req: types.GetPromptRequest) -> types.ServerResult:
            try:
                result = await server_manager.get_prompt(
                    req.params.name, 
                    req.params.arguments
                )
                return types.ServerResult(result)
            except Exception as e:
                logger.error("Error getting prompt '%s': %s", req.params.name, str(e))
                return types.ServerResult(
                    types.GetPromptResult(
                        description=f"Error: {str(e)}",
                        messages=[
                            types.PromptMessage(
                                role="user",
                                content=types.TextContent(
                                    type="text",
                                    text=f"Error retrieving prompt: {str(e)}"
                                )
                            )
                        ]
                    )
                )
        
        app.request_handlers[types.GetPromptRequest] = _get_prompt
    
    if bridge_config.bridge.aggregation.resources:
        logger.debug("Configuring resources aggregation...")
        
        async def _list_resources(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            try:
                resources = server_manager.get_aggregated_resources()
                result = types.ListResourcesResult(resources=resources)
                return types.ServerResult(result)
            except Exception as e:
                logger.error("Error listing resources: %s", str(e))
                return types.ServerResult(types.ListResourcesResult(resources=[]))
        
        app.request_handlers[types.ListResourcesRequest] = _list_resources
        
        async def _list_resource_templates(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            # For now, return empty templates as we don't aggregate templates yet
            result = types.ListResourceTemplatesResult(resourceTemplates=[])
            return types.ServerResult(result)
        
        app.request_handlers[types.ListResourceTemplatesRequest] = _list_resource_templates
        
        async def _read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
            try:
                result = await server_manager.read_resource(req.params.uri)
                return types.ServerResult(result)
            except Exception as e:
                logger.error("Error reading resource '%s': %s", req.params.uri, str(e))
                return types.ServerResult(
                    types.ReadResourceResult(
                        contents=[
                            types.TextResourceContents(
                                uri=req.params.uri,
                                mimeType="text/plain",
                                text=f"Error reading resource: {str(e)}"
                            )
                        ]
                    )
                )
        
        app.request_handlers[types.ReadResourceRequest] = _read_resource
        
        async def _subscribe_resource(req: types.SubscribeRequest) -> types.ServerResult:
            # For now, just acknowledge subscription
            # TODO: Implement proper resource subscription forwarding
            logger.warning("Resource subscription not yet implemented for bridge")
            return types.ServerResult(types.EmptyResult())
        
        app.request_handlers[types.SubscribeRequest] = _subscribe_resource
        
        async def _unsubscribe_resource(req: types.UnsubscribeRequest) -> types.ServerResult:
            # For now, just acknowledge unsubscription
            logger.warning("Resource unsubscription not yet implemented for bridge")
            return types.ServerResult(types.EmptyResult())
        
        app.request_handlers[types.UnsubscribeRequest] = _unsubscribe_resource
    
    if bridge_config.bridge.aggregation.tools:
        logger.debug("Configuring tools aggregation...")
        
        async def _list_tools(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            try:
                tools = server_manager.get_aggregated_tools()
                result = types.ListToolsResult(tools=tools)
                return types.ServerResult(result)
            except Exception as e:
                logger.error("Error listing tools: %s", str(e))
                return types.ServerResult(types.ListToolsResult(tools=[]))
        
        app.request_handlers[types.ListToolsRequest] = _list_tools
        
        async def _call_tool(req: types.CallToolRequest) -> types.ServerResult:
            try:
                result = await server_manager.call_tool(
                    req.params.name,
                    req.params.arguments or {}
                )
                return types.ServerResult(result)
            except Exception as e:
                logger.error("Error calling tool '%s': %s", req.params.name, str(e))
                return types.ServerResult(
                    types.CallToolResult(
                        content=[
                            types.TextContent(
                                type="text",
                                text=f"Error calling tool: {str(e)}"
                            )
                        ],
                        isError=True
                    )
                )
        
        app.request_handlers[types.CallToolRequest] = _call_tool
    
    # Add logging capability
    logger.debug("Configuring logging...")
    
    async def _set_logging_level(req: types.SetLevelRequest) -> types.ServerResult:
        try:
            # Set logging level for the bridge
            level = req.params.level
            bridge_logger = logging.getLogger("mcp_foxxy_bridge")
            
            if level == types.LoggingLevel.DEBUG:
                bridge_logger.setLevel(logging.DEBUG)
            elif level == types.LoggingLevel.INFO:
                bridge_logger.setLevel(logging.INFO)
            elif level == types.LoggingLevel.WARNING:
                bridge_logger.setLevel(logging.WARNING)
            elif level == types.LoggingLevel.ERROR:
                bridge_logger.setLevel(logging.ERROR)
            
            # TODO: Forward logging level to managed servers
            logger.info("Set logging level to %s", level.value)
            return types.ServerResult(types.EmptyResult())
        except Exception as e:
            logger.error("Error setting logging level: %s", str(e))
            return types.ServerResult(types.EmptyResult())
    
    app.request_handlers[types.SetLevelRequest] = _set_logging_level
    
    # Add progress notification handler
    async def _send_progress_notification(req: types.ProgressNotification) -> None:
        logger.debug("Progress notification: %s/%s", req.params.progress, req.params.total)
        # TODO: Forward progress notifications to managed servers if needed
    
    app.notification_handlers[types.ProgressNotification] = _send_progress_notification
    
    # Add completion handler
    async def _complete(req: types.CompleteRequest) -> types.ServerResult:
        try:
            # For now, return empty completions
            # TODO: Implement completion aggregation from managed servers
            result = types.CompleteResult(completion=types.Completion(values=[]))
            return types.ServerResult(result)
        except Exception as e:
            logger.error("Error handling completion: %s", str(e))
            return types.ServerResult(types.CompleteResult(completion=types.Completion(values=[])))
    
    app.request_handlers[types.CompleteRequest] = _complete
    
    logger.info("Bridge server created successfully with %d active servers", 
                len(server_manager.get_active_servers()))
    
    return app


async def shutdown_bridge_server(app: server.Server[object]) -> None:
    """Shutdown the bridge server and clean up resources.
    
    Args:
        app: The bridge server to shutdown.
    """
    logger.info("Shutting down bridge server...")
    
    # Stop the server manager if it exists
    if hasattr(app, '_server_manager'):
        server_manager = getattr(app, '_server_manager')
        if server_manager:
            await server_manager.stop()
    
    logger.info("Bridge server shutdown complete")