# Quick Memory Usage

- **Default endpoint:** `mcp-proxy` (inherits `shared`; includeShared is on by default). Keep `project` = `mcp-proxy` on all entries.
- **Cold start each session:** `listRecentEntries { endpoint:"mcp-proxy", maxResults:20 }` to catch up before doing work.
- **Common flow:** `searchEntries` for the topic at hand; then `upsertEntry`/`patchEntry` with concise `title`, `kind` (`procedure`/`decision`/`note`), 3–6 tags (e.g., `mcp-proxy`, `run`, `wsl`, `dotnet`, `tests`, `decision`), and any `epicSlug`/`case` links.
- **Recording lessons:** capture steps, validation, root cause, and follow-ups; set `isPermanent=false` unless explicitly canonical. Prefer relations to existing IDs instead of duplicating context.
- **Prompts repository:** prompt templates live in `prompts-repository` (`prompts/list` → `prompts/get`)—use these instead of copying prompts by hand.
- **Operational tips:** if commands time out, restart the session and re-run `listProjects`; keep secrets out of entries and omit credentials/PHI.

# Build / Tooling Notes (WSL on /mnt)

- Repo is under `/mnt/...`; prefer Windows .NET SDK to avoid I/O slowness. Set `NEXPORT_WINDOTNET="/mnt/c/Program Files/dotnet/"` and use `"$NEXPORT_WINDOTNETdotnet.exe"` for restore/build/test.
- Keep repo under `/mnt` (do not relocate) and use the Windows Git binary if available at `/mnt/c/Program Files/Git/bin/git.exe` for faster operations.

# Dev Notes (mcp-proxy codebase)

- Python project (pyproject; Python ≥3.10). Install deps with `uv sync`; run tests via `uv run pytest` (uses importlib mode; dev deps defined under `[tool.uv]`). Lint/type-check via `uv run ruff check` and `uv run mypy src`.
- CLI entrypoint `mcp-proxy` supports two modes: SSE/StreamableHTTP client (URL first arg; use `--transport streamablehttp` for /mcp endpoints; `API_ACCESS_TOKEN` env auto-adds `Authorization: Bearer …`) and stdio→SSE server (`mcp-proxy --port 8080 <stdio command> ...`; add `--pass-environment` if child needs current env).
- Named servers: define multiple stdio servers via repeated `--named-server NAME 'command args'` or `--named-server-config servers.json` (expects top-level `mcpServers` with `command`, optional `args`, `enabled`; bad/disabled entries are skipped, config errors exit 1).
- Reliability: opt-in retry flag `--retry-remote` (default off) retries the remote MCP once; use `--remote-retries N` to set the retry count (backoff 0.5s).
- HTTP/CORS: set `--allow-origin '*'` when exposing to browsers; `--stateless` toggles Streamable HTTP stateless mode. Deprecated flags `--sse-host/--sse-port` map to `--host/--port`.
- Env defaults: `SSE_URL` provides fallback for `command_or_url`; `API_ACCESS_TOKEN` and `-H/--headers` combine for upstream auth; `--debug` forces log level DEBUG.
