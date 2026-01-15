# Auto-retry and clearer messaging for upstream failures

Purpose: capture the work needed so future agents can make mcp-proxy recover and report better when the upstream MCP endpoint (e.g., StreamableHTTP at `/mcp/`) drops or returns errors (as seen in the “Session terminated” logs).

## Context / signals
- Log excerpt (2025-12-09 03:17 UTC): upstream `POST http://localhost:5080/mcp` returned 404; immediately after, `resources/list` failed with `32600: Session terminated`, followed by cascading `-32601 Method not found` for multiple servers. Codex had to restart.
- Likely causes: backend restart/unavailable route, path without trailing slash, or transient 4xx/5xx leading to lost session.

## Goals
- Proxy should self-heal from transient upstream failures (404/4xx/5xx/connection reset) by re-initializing once before surfacing errors.
- If recovery fails, emit actionable messages (include URL, status code, hint about `/mcp/` path, and “backend unavailable” guidance) instead of opaque “Session terminated.”

## Plan (iteration outline)
- [x] Add **opt-in** auto-retry for remote StreamableHTTP/SSE init and request failures (single quick retry with backoff) behind `--retry-remote` (default: off) and configurable attempts via `--remote-retries N`.
- [ ] Normalize and log the exact upstream URL used (including trailing slash handling) so 404s point to a real path mismatch.
- [x] When upstream returns non-200/202 or the session dies (`32600 Session terminated`), attempt one re-init; if that fails, return a richer error to the client.
- [x] Surface send-path HTTP errors (e.g., 404 on POST) via an error queue so retries can re-init the session instead of silently timing out.
- [x] On HTTP 404 after idle, re-initialize the session first (clearing `MCP-Session-Id`), and only rebuild the transport if re-init fails; add test coverage for idle 404 + rebuild fallback.
- [x] Improve logging/messages surfaced to clients: include status code, upstream URL, and suggestion to check backend health/path/auth.
- [x] Tests: added coverage for retryable 404s, send-path errors, call timeouts, retry-budget guard, and connection-reset retry.

## Open decisions
- (Decided) Surface guidance in logs only; keep MCP error payload minimal/standard to avoid leaks and client incompatibility.

## Artifacts to update
- CLI help/README for the new retry flag (if introduced).
- Change log entry noting improved resilience and clearer upstream error messaging.
