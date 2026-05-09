# Architecture — mcp-proxy (Enterprise-Lite Fork)

Fork of [sparfenyuk/mcp-proxy](https://github.com/sparfenyuk/mcp-proxy).

## Overview

Bridge between **Streamable HTTP** and **stdio** MCP transports. Allows HTTP-based clients to communicate with stdio-based MCP servers and vice versa.

## Stack

- Python 3.11+
- MCP SDK (Model Context Protocol)
- aiohttp (HTTP server/client)

## Upstream Functionality

- Bidirectional transport bridging (HTTP ↔ stdio)
- SSE streaming support
- Basic MCP protocol handling (tools, resources, prompts)
- Configuration via YAML/CLI

## Our Additions (enterprise-lite branch, 13 PRs)

### Security & Auth

| Component | Description |
|-----------|-------------|
| **APIKeyMiddleware** | Header-based API key validation on all inbound requests |
| **Multi API Key + RBAC** | Multiple keys with role-based access control per tool/resource |
| **Audit Log** | Structured log of all tool invocations with caller identity, timestamp, params |

### Resilience

| Component | Description |
|-----------|-------------|
| **Rate Limiting** | Per-key request throttling (token bucket) |
| **Circuit Breaker** | Prevents cascading failures to downstream MCP servers |
| **Retry Backoff** | Exponential backoff with jitter on transient failures |

### Observability

| Component | Description |
|-----------|-------------|
| **OpenTelemetry** | Traces and metrics export (OTLP) for all proxy operations |
| **JSON Logging** | Structured JSON logs (replaces plain text) |
| **Progress Notifications** | MCP progress tokens forwarded to clients for long-running tools |
| **Dashboard** | Web UI showing connected servers, request stats, health |

### Operations

| Component | Description |
|-----------|-------------|
| **Dynamic Registration** | Register/unregister MCP servers at runtime via API |
| **Hot-Reload** | Config changes applied without restart |
| **REST-to-MCP Adapter** | Expose MCP tools as REST endpoints for non-MCP clients |

## Request Flow

```
Client (HTTP) → APIKeyMiddleware → Rate Limiter → Circuit Breaker
  → MCP Protocol Handler → stdio transport → MCP Server
  ← Response (with progress notifications) ← Client
```

## Configuration

```yaml
proxy:
  listen: 0.0.0.0:8080
  auth:
    keys:
      - key: "sk-..."
        roles: [admin]
      - key: "sk-..."
        roles: [readonly]
  rate_limit:
    requests_per_minute: 60
  circuit_breaker:
    failure_threshold: 5
    recovery_timeout: 30
  telemetry:
    otlp_endpoint: "http://otel-collector:4317"

servers:
  - name: my-server
    command: ["python", "server.py"]
    transport: stdio
```

## Directory Structure

```
src/
├── proxy/
│   ├── server.py          # aiohttp app setup
│   ├── middleware/
│   │   ├── auth.py        # APIKeyMiddleware, RBAC
│   │   ├── rate_limit.py  # Token bucket rate limiter
│   │   └── audit.py       # Audit logging
│   ├── resilience/
│   │   ├── circuit_breaker.py
│   │   └── retry.py       # Exponential backoff
│   ├── transport/
│   │   ├── http.py        # Streamable HTTP transport
│   │   └── stdio.py       # stdio transport bridge
│   ├── telemetry.py       # OpenTelemetry setup
│   ├── dashboard.py       # Web dashboard
│   ├── rest_adapter.py    # REST-to-MCP adapter
│   └── registry.py        # Dynamic server registration
└── config.py              # Hot-reload config management
```
