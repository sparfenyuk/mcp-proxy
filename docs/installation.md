# Installation Guide

This guide covers the different ways to install and run MCP Foxxy Bridge.

## Prerequisites

- **Python 3.10+** (for local installation)
- **Node.js 18+** (for MCP servers that use npm)
- **Docker** (for containerized deployment)

## Installation Methods

### 1. UV Tool (Recommended)

The easiest way to install and use MCP Foxxy Bridge:

```bash
# Install the bridge
uv tool install mcp-foxxy-bridge

# Run the bridge
mcp-foxxy-bridge --bridge-config config.json

# Install from git (latest development)
uv tool install git+https://github.com/billyjbryant/mcp-foxxy-bridge
```

### 2. Local Development

For development or when you need to modify the code:

```bash
# Clone the repository
git clone https://github.com/billyjbryant/mcp-foxxy-bridge
cd mcp-foxxy-bridge

# Install dependencies
uv sync

# Run the bridge
uv run mcp-foxxy-bridge --bridge-config bridge_config_example.json
```

### 3. Docker Container

For production deployments or isolated environments:

```bash
# Build the image
docker build -t mcp-foxxy-bridge .

# Run with configuration
docker run -p 8080:8080 \
  -v ./config.json:/app/config/config.json \
  -e GITHUB_TOKEN=your_token \
  mcp-foxxy-bridge --bridge-config /app/config/config.json

# Or use Docker Compose
docker-compose up -d
```

### 4. Pipx Installation

Alternative to UV for Python tool installation:

```bash
# Install via pipx
pipx install mcp-foxxy-bridge

# Run the bridge
mcp-foxxy-bridge --bridge-config config.json
```

## Verification

After installation, verify the bridge is working:

```bash
# Check version
mcp-foxxy-bridge --version

# Test with example config
mcp-foxxy-bridge --bridge-config bridge_config_example.json

# Check status endpoint (in another terminal)
curl http://localhost:8080/status
```

## Environment Setup

### Required Environment Variables

Some MCP servers require environment variables:

```bash
# GitHub server
export GITHUB_TOKEN=ghp_your_token_here

# Brave Search server  
export BRAVE_API_KEY=your_brave_api_key

# Run bridge
mcp-foxxy-bridge --bridge-config config.json
```

### Optional Dependencies

Install additional MCP servers as needed:

```bash
# Install common MCP servers globally
npm install -g @modelcontextprotocol/server-github
npm install -g @modelcontextprotocol/server-filesystem
npm install -g @modelcontextprotocol/server-brave-search

# Or install UV-based servers
uvx install mcp-server-fetch
```

## Next Steps

1. Create a configuration file (see [Configuration Guide](configuration.md))
2. Set up your deployment method (see [Deployment Guide](deployment.md))
3. Connect your MCP client to the bridge endpoint
