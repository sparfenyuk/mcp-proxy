# Configuration Guide

This guide covers how to configure MCP Foxxy Bridge for your use case.

## Configuration File Format

The bridge uses JSON configuration files with two main sections:

```json
{
  "mcpServers": {
    "server_name": {
      // Server configuration
    }
  },
  "bridge": {
    // Bridge-wide settings
  }
}
```

## Server Configuration

Each MCP server in the `mcpServers` object has these options:

### Basic Settings

```json
{
  "enabled": true,                    // Whether to start this server
  "command": "npx",                   // Command to run
  "args": ["-y", "@modelcontextprotocol/server-github"],  // Arguments
  "timeout": 60,                      // Connection timeout (seconds)
  "transportType": "stdio"            // Always "stdio"
}
```

### Environment Variables

```json
{
  "env": {
    "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}",
    "API_URL": "${API_URL:https://api.github.com}",  // With default
    "DEBUG": "${DEBUG_MODE:false}"
  }
}
```

Environment variable syntax:

- `${VAR_NAME}` - Use environment variable value
- `${VAR_NAME:default}` - Use value or default if not set

### Namespacing

```json
{
  "toolNamespace": "github",          // Prefix for tools (github.search_repositories)
  "resourceNamespace": "gh",          // Prefix for resources  
  "promptNamespace": "github"         // Prefix for prompts
}
```

### Reliability Settings

```json
{
  "retryAttempts": 3,                 // Connection retry attempts
  "retryDelay": 1000,                 // Delay between retries (ms)
  "priority": 100,                    // Server priority (lower = higher priority)
  "healthCheck": {
    "enabled": true,                  // Enable health monitoring
    "interval": 30000,                // Check interval (ms)
    "timeout": 5000                   // Health check timeout (ms)
  }
}
```

### Metadata

```json
{
  "tags": ["github", "git", "version-control"]  // Tags for organization
}
```

## Bridge Configuration

The `bridge` section controls bridge-wide behavior:

```json
{
  "bridge": {
    "conflictResolution": "namespace",  // How to handle tool name conflicts
    "defaultNamespace": true,           // Use server name as default namespace
    "aggregation": {
      "tools": true,                    // Aggregate tools from all servers
      "resources": true,                // Aggregate resources
      "prompts": true                   // Aggregate prompts
    },
    "failover": {
      "enabled": true,                  // Enable automatic failover
      "maxFailures": 3,                 // Max failures before failed
      "recoveryInterval": 60000         // Time before retry (ms)
    }
  }
}
```

### Conflict Resolution Options

- `"namespace"` - Use namespaces to avoid conflicts (recommended)
- `"priority"` - Higher priority server wins
- `"first"` - First server to provide the tool wins
- `"error"` - Throw error on conflicts

## Example Configurations

### Minimal Configuration

```json
{
  "mcpServers": {
    "filesystem": {
      "enabled": true,
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    }
  },
  "bridge": {
    "conflictResolution": "namespace"
  }
}
```

### Production Configuration

```json
{
  "mcpServers": {
    "fetch": {
      "enabled": true,
      "timeout": 60,
      "command": "uvx",
      "args": ["mcp-server-fetch"],
      "retryAttempts": 3,
      "retryDelay": 1000,
      "healthCheck": {
        "enabled": true,
        "interval": 30000,
        "timeout": 5000
      },
      "toolNamespace": "fetch",
      "priority": 100,
      "tags": ["web", "http", "fetch"]
    },
    "github": {
      "enabled": true,
      "timeout": 60,
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      },
      "retryAttempts": 3,
      "retryDelay": 1000,
      "healthCheck": {
        "enabled": true,
        "interval": 30000,
        "timeout": 5000
      },
      "toolNamespace": "github",
      "priority": 100,
      "tags": ["github", "git", "version-control"]
    },
    "filesystem": {
      "enabled": true,
      "timeout": 30,
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/app/data"],
      "retryAttempts": 2,
      "retryDelay": 500,
      "healthCheck": {
        "enabled": true,
        "interval": 45000,
        "timeout": 3000
      },
      "toolNamespace": "fs",
      "resourceNamespace": "fs",
      "priority": 50,
      "tags": ["filesystem", "files", "local"]
    }
  },
  "bridge": {
    "conflictResolution": "namespace",
    "defaultNamespace": true,
    "aggregation": {
      "tools": true,
      "resources": true,
      "prompts": true
    },
    "failover": {
      "enabled": true,
      "maxFailures": 3,
      "recoveryInterval": 60000
    }
  }
}
```

## Command Line Options

Override configuration with command-line arguments:

```bash
# Basic usage
mcp-foxxy-bridge --bridge-config config.json

# Custom port and host
mcp-foxxy-bridge --bridge-config config.json --port 8081 --host 0.0.0.0

# Debug mode
mcp-foxxy-bridge --bridge-config config.json --debug

# Pass environment variables
GITHUB_TOKEN=abc123 mcp-foxxy-bridge --bridge-config config.json
```

## Validation

The bridge validates configuration files on startup. Common validation errors:

- **Missing required fields**: Every server needs a `command`
- **Invalid JSON**: Syntax errors in the configuration file
- **Unknown fields**: Typos in field names
- **Invalid values**: Wrong types or out-of-range values

## Best Practices

1. **Use environment variables** for secrets instead of hardcoding them
2. **Set appropriate timeouts** based on your MCP servers' response times
3. **Enable health checks** for production deployments
4. **Use namespaces** to avoid tool name conflicts
5. **Set priorities** to control which server handles conflicts
6. **Tag your servers** for better organization
7. **Test configurations** with a single server first

## Next Steps

- See [Deployment Guide](deployment.md) for running the configured bridge
- Check [Troubleshooting Guide](troubleshooting.md) for common issues
- Review [API Reference](api.md) for endpoint usage details
