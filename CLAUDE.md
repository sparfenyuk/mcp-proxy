# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

mcp-proxy is a Python tool that enables transport switching for MCP (Model Context Protocol) servers. It supports three primary modes:

1. **stdio to SSE/StreamableHTTP**: Allows clients like Claude Desktop to connect to remote MCP servers over SSE/StreamableHTTP transport
2. **SSE/StreamableHTTP to stdio**: Exposes local stdio MCP servers as both SSE and StreamableHTTP endpoints
3. **stdio to SSE and StreamableHTTP server**: Same as mode 2, providing dual transport support

## Core Architecture

- **Entry Point**: `src/mcp_proxy/__main__.py` - CLI argument parsing and mode dispatch
- **MCP Server**: `src/mcp_proxy/mcp_server.py` - SSE server implementation that proxies to stdio servers
- **SSE Client**: `src/mcp_proxy/sse_client.py` - Client for connecting to remote SSE servers
- **StreamableHTTP Client**: `src/mcp_proxy/streamablehttp_client.py` - Client for StreamableHTTP transport
- **Proxy Server**: `src/mcp_proxy/proxy_server.py` - Core proxy logic for bridging transports
- **Config Loader**: `src/mcp_proxy/config_loader.py` - Loads named server configurations from JSON files

## Development Commands

### Installation and Setup
```bash
# Install with uv (recommended)
uv tool install .

# Install for development
uv sync --dev
```

### Testing
```bash
# Run all tests
uv run pytest

# Run tests with coverage
uv run coverage run -m pytest
uv run coverage report
```

### Code Quality
```bash
# Type checking
uv run mypy src/

# Linting (configured with ruff)
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

### Running the Application
```bash
# Run as module
uv run -m mcp_proxy

# Run installed command
mcp-proxy

# Example: stdio to SSE mode (proxy remote SSE to local stdio)
mcp-proxy http://example.io/sse

# Example: SSE to stdio mode (expose local stdio as SSE)
mcp-proxy --port 8080 uvx mcp-server-fetch
```

## Key Configuration Patterns

### Named Servers
The proxy supports multiple named servers via CLI args or JSON config:

```bash
# CLI-defined named servers
mcp-proxy --port 8080 --named-server fetch 'uvx mcp-server-fetch' --named-server github 'npx @modelcontextprotocol/server-github'

# JSON config file (see config_example.json)
mcp-proxy --port 8080 --named-server-config ./servers.json
```

### Environment Variables
- `API_ACCESS_TOKEN`: Used for Bearer token authentication with remote SSE servers
- `SSE_URL`: Deprecated fallback for command_or_url argument

## Transport Modes

1. **Client Mode**: Connects to remote server, exposes stdio interface
2. **Server Mode**: Spawns local stdio servers, exposes both SSE and StreamableHTTP interfaces

The mode is determined by whether `command_or_url` starts with `http://` or `https://`.

### Exposed Endpoints (Server Mode)
- **SSE endpoint**: `http://localhost:$port/sse`
- **StreamableHTTP endpoint**: `http://localhost:$port/mcp` 
- **Status endpoint**: `http://localhost:$port/status`
- **Named servers**: `/servers/<name>/sse` and `/servers/<name>/mcp`

## Testing Strategy

Tests are located in `tests/` and cover:
- Config loading (`test_config_loader.py`)
- MCP server functionality (`test_mcp_server.py`)
- Proxy server logic (`test_proxy_server.py`)

Use `pytest-asyncio` for async test support.