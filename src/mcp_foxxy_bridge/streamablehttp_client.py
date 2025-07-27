#
# MCP Foxxy Bridge - StreamableHTTP Client
#
# Copyright (C) 2024 Billy Bryant
# Portions copyright (C) 2024 Sergey Parfenyuk (original MIT-licensed author)
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
# MIT License attribution: Portions of this file were originally licensed under the MIT License by Sergey Parfenyuk (2024).
#
"""Create a local server that proxies requests to a remote server over SSE."""

from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.stdio import stdio_server

from .proxy_server import create_proxy_server


async def run_streamablehttp_client(url: str, headers: dict[str, Any] | None = None) -> None:
    """Run the SSE client.

    Args:
        url: The URL to connect to.
        headers: Headers for connecting to MCP server.

    """
    async with (
        streamablehttp_client(url=url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        app = await create_proxy_server(session)
        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream,
                write_stream,
                app.create_initialization_options(),
            )
