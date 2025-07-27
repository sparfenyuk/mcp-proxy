# MCP Foxxy Bridge Documentation

Welcome to the MCP Foxxy Bridge documentation! This directory contains
comprehensive guides and documentation for using and contributing to the MCP
Foxxy Bridge project.

## Documentation Structure

- [Installation Guide](installation.md) - How to install and set up the bridge
- [Configuration Guide](configuration.md) - Detailed configuration options and examples
- [Deployment Guide](deployment.md) - Docker, local, and UV deployment options
- [API Reference](api.md) - Endpoints and programmatic usage
- [Architecture Overview](architecture.md) - Technical architecture and design
- [Contributing Guide](../CONTRIBUTING.md) - Development setup and guidelines
- [Troubleshooting Guide](troubleshooting.md) - Common issues and solutions
- [Release Process](releasing.md) - How releases are created and published to PyPI
- [Repository Maintenance](maintenance.md) - Automated maintenance and configuration management

## Quick Start

1. **Install**: `uv tool install mcp-foxxy-bridge`
2. **Configure**: Create a bridge configuration file
3. **Run**: `mcp-foxxy-bridge --bridge-config config.json`
4. **Connect**: Point your MCP client to `http://localhost:8080/sse`

## Key Features

- **One-to-Many Bridge**: Connect multiple MCP servers through a single endpoint
- **Tool Aggregation**: Unified access to tools from all connected servers
- **Namespace Management**: Automatic tool namespacing to prevent conflicts
- **Environment Variables**: Support for `${VAR_NAME}` expansion in configs
- **Multiple Deployment Options**: Local process, Docker container, or UV tool
- **Health Monitoring**: Built-in status endpoint for monitoring

## Getting Help

- Review [Configuration Guide](configuration.md) for setup patterns
- Check [Troubleshooting Guide](troubleshooting.md) for common issues
- Check [API Reference](api.md) for detailed endpoint documentation
- Check the [Contributing Guide](../CONTRIBUTING.md) for development setup
