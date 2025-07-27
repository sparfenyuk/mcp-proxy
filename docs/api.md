# API Reference

This document describes the HTTP endpoints and programmatic interfaces
provided by MCP Foxxy Bridge.

## HTTP Endpoints

### SSE Endpoint

## Primary MCP client connection endpoint

```http
GET /sse
```

This is the main endpoint where MCP clients connect to access aggregated
tools from all configured MCP servers.

**Usage:**

```javascript
// Connect MCP client
const session = new MCPSession("http://localhost:8080/sse");
```

**Claude Desktop Configuration:**

```json
{
  "mcpServers": {
    "foxxy-bridge": {
      "command": "mcp-foxxy-bridge",
      "args": ["http://localhost:8080/sse"]
    }
  }
}
```

### Status Endpoint

## Bridge and server status monitoring

```http
GET /status
```

Returns detailed status information about the bridge and all configured MCP servers.

**Response Format:**

```json
{
  "api_last_activity": "2025-07-27T03:40:49.553409+00:00",
  "server_instances": {
    "server_name": {
      "enabled": true,
      "command": "npx",
      "status": "connected|failed|connecting|disabled",
      "last_seen": 1640995200.0,
      "failure_count": 0,
      "last_error": null,
      "capabilities": {
        "tools": 15,
        "resources": 0,
        "prompts": 1
      },
      "config": {
        "enabled": true,
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "priority": 100,
        "tags": ["github", "git"]
      }
    }
  }
}
```

**Status Values:**

- `connected` - Server is running and healthy
- `failed` - Server failed to start or has failed
- `connecting` - Server is currently connecting
- `disabled` - Server is disabled in configuration

## MCP Protocol Support

The bridge implements the full MCP (Model Context Protocol) specification and supports:

### Capabilities

- **Tools** - Function calling capabilities from all servers
- **Resources** - File and data resource access
- **Prompts** - Predefined prompt templates
- **Logging** - Centralized logging from all servers

### Tool Invocation

Tools from different servers are namespaced to prevent conflicts:

```json
{
  "method": "tools/call",
  "params": {
    "name": "github.search_repositories",  // Namespaced tool name
    "arguments": {
      "query": "mcp protocol"
    }
  }
}
```

### Resource Access

Resources are accessed with optional namespacing:

```json
{
  "method": "resources/read",
  "params": {
    "uri": "fs://file.txt"  // Namespaced resource URI
  }
}
```

### Prompt Access

Prompts can be retrieved by namespaced name:

```json
{
  "method": "prompts/get",
  "params": {
    "name": "fetch.fetch",  // Namespaced prompt name
    "arguments": {
      "url": "https://example.com"
    }
  }
}
```

## Configuration API

While not exposed as HTTP endpoints, the bridge provides programmatic
configuration through the config loader.

### Environment Variable Expansion

The bridge automatically expands environment variables in
configuration:

```json
{
  "env": {
    "API_TOKEN": "${GITHUB_TOKEN}",              // Required variable
    "API_URL": "${API_URL:https://api.github.com}", // With default
    "DEBUG": "${DEBUG_MODE:false}"               // Boolean default
  }
}
```

**Expansion Rules:**

- `${VAR_NAME}` - Use environment variable value or empty string
- `${VAR_NAME:default}` - Use environment variable value or default
- Expansion is recursive through nested objects and arrays

### Server Configuration Schema

Each server configuration supports these fields:

```typescript
interface ServerConfig {
  enabled: boolean;                    // Whether server is active
  command: string;                     // Command to execute
  args: string[];                      // Command arguments
  env?: Record<string, string>;        // Environment variables
  timeout?: number;                    // Connection timeout (seconds)
  transportType: "stdio";              // Always stdio
  retryAttempts?: number;              // Retry count on failure
  retryDelay?: number;                 // Delay between retries (ms)
  healthCheck?: {
    enabled: boolean;                  // Enable health monitoring
    interval: number;                  // Check interval (ms)
    timeout: number;                   // Health check timeout (ms)
  };
  toolNamespace?: string;              // Namespace for tools
  resourceNamespace?: string;          // Namespace for resources
  promptNamespace?: string;            // Namespace for prompts
  priority?: number;                   // Server priority (lower = higher)
  tags?: string[];                     // Metadata tags
}
```

### Bridge Configuration Schema

```typescript
interface BridgeConfig {
  conflictResolution: "priority" | "namespace" | "first" | "error";
  defaultNamespace: boolean;           // Use server name as namespace
  aggregation: {
    tools: boolean;                    // Aggregate tools
    resources: boolean;                // Aggregate resources
    prompts: boolean;                  // Aggregate prompts
  };
  failover: {
    enabled: boolean;                  // Enable automatic failover
    maxFailures: number;               // Max failures before marking failed
    recoveryInterval: number;          // Recovery attempt interval (ms)
  };
}
```

## Client Integration

### MCP Client Libraries

Use standard MCP client libraries to connect:

**Python:**

```python
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession

async def connect_to_bridge():
    async with sse_client("http://localhost:8080/sse") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return tools
```

**JavaScript:**

```javascript
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { SSEClientTransport } from '@modelcontextprotocol/sdk/client/sse.js';

const transport = new SSEClientTransport('http://localhost:8080/sse');
const client = new Client({
  name: "my-client",
  version: "1.0.0"
}, {
  capabilities: {}
});

await client.connect(transport);
const tools = await client.listTools();
```

### Direct HTTP Integration

For custom integrations, you can connect directly to the SSE endpoint:

```bash
# Connect to SSE stream
curl -N -H "Accept: text/event-stream" http://localhost:8080/sse

# Send MCP message
curl -X POST http://localhost:8080/messages/ \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/list", "params": {}}'
```

## Error Handling

### HTTP Status Codes

- `200 OK` - Successful request
- `404 Not Found` - Endpoint not found
- `500 Internal Server Error` - Bridge error
- `502 Bad Gateway` - MCP server error

### MCP Error Responses

Standard MCP error format:

```json
{
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {
      "details": "Tool 'unknown.tool' not found"
    }
  }
}
```

### Common Error Scenarios

1. **Tool not found**: Requested tool doesn't exist on any server
2. **Server unavailable**: Target MCP server is not connected
3. **Timeout**: Server took too long to respond
4. **Authentication**: Missing or invalid credentials for server

## Rate Limiting

The bridge doesn't implement rate limiting itself, but respects limits from
underlying MCP servers. Consider implementing rate limiting at the reverse
proxy level for production deployments.

## Monitoring and Observability

### Metrics Collection

Monitor these key metrics:

- **Request count** - Total requests to each endpoint
- **Response time** - Latency for tool calls and resource access
- **Error rate** - Failed requests by server and error type
- **Server status** - Health and availability of each MCP server
- **Connection count** - Active SSE connections

### Health Check Integration

Use the status endpoint for health monitoring:

```bash
# Simple health check
curl -f http://localhost:8080/status

# Detailed monitoring
curl -s http://localhost:8080/status | \\
  jq '.server_instances[] | select(.status != "connected")'
```

### Logging

Enable debug logging for detailed request tracing:

```bash
mcp-foxxy-bridge --bridge-config config.json --debug
```

## Security Considerations

### Authentication

The bridge doesn't implement authentication itself. Use these approaches:

1. **Network-level security** - Bind to localhost, use firewall rules
2. **Reverse proxy authentication** - Add auth at nginx/Caddy level
3. **VPN or private networks** - Restrict network access

### Environment Variables

Always use environment variables for secrets:

```json
{
  "env": {
    "API_TOKEN": "${SECRET_TOKEN}",     // ✅ Good
    "API_TOKEN": "hardcoded-secret"     // ❌ Never do this
  }
}
```

### CORS

The bridge supports CORS configuration:

```bash
mcp-foxxy-bridge --bridge-config config.json --allow-origin="*"
```

For production, specify specific origins:

```bash
mcp-foxxy-bridge --bridge-config config.json --allow-origin="https://app.example.com"
```
