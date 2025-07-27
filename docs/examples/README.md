# Configuration Examples

This directory contains various configuration examples for MCP Foxxy Bridge to help you get started quickly with different use cases.

## Quick Start Examples

### [minimal-config.json](minimal-config.json)
The absolute minimum configuration to get started. Perfect for testing or simple setups.

**What it includes:**
- Single filesystem server
- Basic bridge settings
- No advanced features

**Usage:**
```bash
mcp-foxxy-bridge --bridge-config docs/examples/minimal-config.json
```

### [basic-config.json](basic-config.json)
A simple but practical configuration for everyday use.

**What it includes:**
- GitHub integration
- Filesystem access
- Web fetching capability
- Environment variable expansion

**Usage:**
```bash
export GITHUB_TOKEN=your_token_here
mcp-foxxy-bridge --bridge-config docs/examples/basic-config.json
```

## Production Examples

### [production-config.json](production-config.json)
Enterprise-ready configuration with reliability features.

**What it includes:**
- Multiple MCP servers with health checks
- Failover configuration
- Optimized timeouts and retry settings
- Comprehensive monitoring

**Usage:**
```bash
export GITHUB_TOKEN=your_token_here
export BRAVE_API_KEY=your_api_key_here
mcp-foxxy-bridge --bridge-config docs/examples/production-config.json
```

### [docker-config.json](docker-config.json)
Optimized for Docker container deployment.

**What it includes:**
- Container-friendly paths (`/app/data`)
- Network-accessible configuration
- Environment variable based secrets
- Docker-optimized health checks

**Usage:**
```bash
docker run -v ./docs/examples/docker-config.json:/app/config.json:ro \\
  -e GITHUB_TOKEN=your_token \\
  mcp-foxxy-bridge --bridge-config /app/config.json
```

## Specialized Examples

### [development-config.json](development-config.json)
Perfect for local development and testing.

**What it includes:**
- Relaxed timeouts for debugging
- Detailed logging configuration
- Local filesystem access
- Development-friendly settings

### [full-featured-config.json](full-featured-config.json)
Demonstrates all available configuration options.

**What it includes:**
- Every supported MCP server type
- All configuration options with comments
- Advanced bridge features
- Complete feature showcase

## Configuration Tips

### Environment Variables

All examples support environment variable expansion:

```json
{
  "env": {
    "API_KEY": "${YOUR_API_KEY}",
    "API_URL": "${API_ENDPOINT:https://api.default.com}",
    "DEBUG": "${DEBUG_MODE:false}"
  }
}
```

**Syntax:**
- `${VAR_NAME}` - Required variable
- `${VAR_NAME:default}` - Variable with fallback default

### Server Prioritization

Use the `priority` field to control server selection during conflicts:

```json
{
  "priority": 50  // Lower number = higher priority
}
```

### Health Monitoring

Enable health checks for production reliability:

```json
{
  "healthCheck": {
    "enabled": true,
    "interval": 30000,  // Check every 30 seconds
    "timeout": 5000     // 5 second timeout
  }
}
```

### Namespace Management

Prevent tool name conflicts with namespaces:

```json
{
  "toolNamespace": "github",        // github.search_repositories
  "resourceNamespace": "gh",        // gh://repo/file.txt
  "promptNamespace": "github"       // github.commit_message
}
```

## Testing Your Configuration

Validate your configuration before deployment:

```bash
# Test configuration syntax
python -m json.tool your_config.json

# Test with debug mode
mcp-foxxy-bridge --bridge-config your_config.json --debug

# Check server connectivity
curl http://localhost:8080/status
```

## Getting Help

- See [Configuration Guide](../configuration.md) for detailed options
- Check [Troubleshooting Guide](../troubleshooting.md) for common issues
- Review [API Reference](../api.md) for endpoint details