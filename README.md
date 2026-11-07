# mcp-proxy

![GitHub License](https://img.shields.io/github/license/sparfenyuk/mcp-proxy)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/mcp-proxy)
![PyPI - Downloads](https://img.shields.io/pypi/dm/mcp-proxy)
[![codecov](https://codecov.io/gh/sparfenyuk/mcp-proxy/graph/badge.svg?token=31VV9L7AZQ)](https://codecov.io/gh/sparfenyuk/mcp-proxy)

- [mcp-proxy](#mcp-proxy)
  - [About](#about)
  - [1. stdio to SSE/StreamableHTTP](#1-stdio-to-ssestreamablehttp)
    - [1.1 Configuration](#11-configuration)
    - [1.2 Example usage](#12-example-usage)
  - [2. SSE to stdio](#2-sse-to-stdio)
    - [2.1 Configuration](#21-configuration)
    - [2.2 Example usage](#22-example-usage)
  - [Named Servers](#named-servers)
  - [Installation](#installation)
    - [Installing via PyPI](#installing-via-pypi)
    - [Installing via Github repository (latest)](#installing-via-github-repository-latest)
    - [Installing as container](#installing-as-container)
    - [Troubleshooting](#troubleshooting)
  - [Extending the container image](#extending-the-container-image)
  - [Docker Compose Setup](#docker-compose-setup)
  - [Command line arguments](#command-line-arguments)
    - [Example config file](#example-config-file)
  - [Testing](#testing)

## About

The `mcp-proxy` is a tool that lets you switch between server transports. There are two supported modes:

1. stdio to SSE/StreamableHTTP
2. SSE to stdio

## 1. stdio to SSE/StreamableHTTP

Run a proxy server from stdio that connects to a remote SSE server.

This mode allows clients like Claude Desktop to communicate to a remote server over SSE even though it is not supported
natively.

```mermaid
graph LR
    A["Claude Desktop"] <--> |stdio| B["mcp-proxy"]
    B <--> |SSE| C["External MCP Server"]

    style A fill:#ffe6f9,stroke:#333,color:black,stroke-width:2px
    style B fill:#e6e6ff,stroke:#333,color:black,stroke-width:2px
    style C fill:#e6ffe6,stroke:#333,color:black,stroke-width:2px
```

### 1.1 Configuration

This mode requires providing the URL of the MCP Server's SSE endpoint as the program’s first argument. If the server uses Streamable HTTP transport, make sure to enforce it on the `mcp-proxy` side by passing `--transport=streamablehttp`.

When the proxy is a **Streamable HTTP client**, it bounds the final MCP session-termination `DELETE` request, including reading its response body, to five seconds. After cancellation or timeout, closing a received response gets a shielded, cooperative grace of up to one second; cleanup code that ignores cancellation can take longer. Graceful CLI teardown after SIGINT requires Python 3.11+; on Python 3.10, SIGINT may interrupt the event loop before the final `DELETE` is sent ([asyncio runner documentation](https://docs.python.org/3.11/library/asyncio-runner.html#handling-keyboard-interruption)). This limit applies only to that termination operation; normal `GET` streams and `POST` tool calls retain their usual HTTP timeouts. It is specific to reverse-client mode and does not change the stateful/stateless behavior of proxied HTTP servers.

In reverse Streamable HTTP mode, an HTTP `404` on a `POST` for an established upstream session marks that session lost. The operation that received the `404` is never retried; its failure follows the existing proxy handling, and the server may already have applied it. A later explicit request may use a replacement session only when its complete initialization handshake matches the original. The proxy does not restore subscriptions, logging levels, or other upstream session-local state; after stateful resource or logging operations, or when the handshake changes, it refuses later requests and the MCP client must restart. Progress notifications are sent only to the current healthy session and are not buffered or replayed.

Arguments

| Name             | Required | Description                                                                                                       | Example                                       |
| ---------------- | -------- | ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `command_or_url` | Yes      | The MCP server SSE endpoint to connect to                                                                         | http://example.io/sse                         |
| `--headers`      | No       | Headers to use for the MCP server SSE connection                                                                  | Authorization 'Bearer my-secret-access-token' |
| `--transport`    | No       | Decides which transport protocol to use when connecting to an MCP server. Can be either 'sse' or 'streamablehttp' | streamablehttp                                |
| `--client-id`    | No       | OAuth2 client ID for authentication                                                                               | your_client_id                                |
| `--client-secret`| No       | OAuth2 client secret for authentication                                                                           | your_client_secret                            |
| `--token-url`    | No       | OAuth2 token endpoint URL for authentication                                                                      | https://auth.example.com/oauth/token          |
| `--username`     | No       | Username for authentication                                                                                       | user                                          |
| `--password`     | No       | Password for authentication                                                                                       | password                                      |
| `--digest`       | No       | Use digest authentication                                                                                         |                                               |
| [CrakSEO/skills](https://github.com/CrakSEO/skills) | Connect Claude/Cursor/Codex to CrakSEO remote MCP servers for keyword research, SERP, backlinks, rank tracking, site audit, local SEO, AI visibility, Search Console and URL indexing | Claude Code, Cursor, Codex | MIT |

Environment Variables

| Name               | Required | Description                                                                  | Example    |
| ------------------ | -------- | ---------------------------------------------------------------------------- | ---------- |
| `API_ACCESS_TOKEN` | No       | Can be used instead of `--headers Authorization 'Bearer <API_ACCESS_TOKEN>'` | YOUR_TOKEN |

### 1.2 Example usage

`mcp-proxy` is supposed to be started by the MCP Client, so the configuration must be done accordingly.

For Claude Desktop, the configuration entry can look like this:

```json
{
  "mcpServers": {
    "mcp-proxy": {
      "command": "mcp-proxy",
      "args": [
        "http://example.io/sse"
      ],
      "env": {
        "API_ACCESS_TOKEN": "access-token"
      }
    }
  }
}
```

Closing the client's stdin disconnects the remote session and cancels connection setup, initialization, and active
requests. Queued input is abandoned. Each input frame is limited to 4 MiB of UTF-8 wire payload bytes, excluding
CR/LF delimiters. At most 4 MiB and 128 frames may be admitted but not yet consumed; empty and malformed frames count
toward the frame limit. Exceeding either limit exits with an error instead of silently dropping input. These are
admission limits, not an exact memory limit, because decoded text and protocol objects use additional memory.

The client must read or close stdout. A full, unread stdout pipe can prevent process exit. A cancelled blocking stdin
read may leave one daemon thread and its private descriptor until the read returns or the process exits. The helper
supports one active CLI lifespan per process.

## 2. SSE to stdio

Run a proxy server exposing a SSE server that connects to a local stdio server.

This allows remote connections to the local stdio server. The `mcp-proxy` opens a port to listen for SSE requests,
spawns a local stdio server that handles MCP requests.

```mermaid
graph LR
    A["LLM Client"] <-->|SSE| B["mcp-proxy"]
    B <-->|stdio| C["Local MCP Server"]

    style A fill:#ffe6f9,stroke:#333,color:black,stroke-width:2px
    style B fill:#e6e6ff,stroke:#333,color:black,stroke-width:2px
    style C fill:#e6ffe6,stroke:#333,color:black,stroke-width:2px
```

### 2.1 Configuration

Configure the HTTP server with `--host` and `--port`; the defaults are `127.0.0.1` and an available port. A wildcard
bind such as `0.0.0.0` requires `--allowed-host` entries for the Host authorities clients will use. These host checks
and CORS settings do not authenticate callers by default. `--headers` and `API_ACCESS_TOKEN` authenticate upstream
client requests; they do not authenticate callers of this server.

To require inbound authentication, set `MCP_PROXY_AUTH_TOKEN` or pass `--auth-bearer-token TOKEN`. An explicit CLI
value takes precedence over the environment variable. Prefer the environment variable because local process tools may
show command-line arguments. With neither value set, the server keeps its unauthenticated default. When enabled, every
HTTP request needs `Authorization: Bearer TOKEN`, including requests to `/status`, `/healthz`, SSE, StreamableHTTP,
and named-server routes. Health probes must send the token. Configured CORS preflight requests still work without a
token; ordinary `OPTIONS` requests require one. Host and Origin checks continue to apply.

`MCP_PROXY_AUTH_TOKEN` is removed from child processes when `--pass-environment` copies the proxy environment.
Values passed explicitly with `--env` or a named-server configuration remain part of that server's environment.
This shared token does not provide user identity or roles and does not make the proxy compliant with MCP OAuth.
Use TLS or a trusted network/reverse proxy when exposing the server beyond a trusted local network.

The deprecated `--sse-host` and `--sse-port` options are ineffective; use `--host` and `--port` instead. Additional
environment variables can be passed to the local stdio server using `--env`. Put local server arguments after the `--` separator.

Startup logs list the SSE and StreamableHTTP endpoints for each configured backend. With `--port=0`, they list endpoint
paths because Uvicorn selects the actual port during startup; combine each path with Uvicorn's listening address.

Arguments

| Name                                 | Required                   | Description                                                                                   | Example                                     |
| ------------------------------------ | -------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------- |
| `command_or_url`                     | Yes                        | The command to spawn the MCP stdio server                                                     | uvx mcp-server-fetch                        |
| `--port`                             | No, random available       | The MCP server port to listen on                                                              | 8080                                        |
| `--host`                             | No, `127.0.0.1` by default | Host IP address to bind. Wildcard binds require explicit `--allowed-host` authorities.       | 0.0.0.0                                     |
| `--auth-bearer-token`                | No                         | Require one shared Bearer token on every inbound HTTP request. Prefer `MCP_PROXY_AUTH_TOKEN`. | `<TOKEN>`                                  |
| `--allowed-host`                     | No                         | Additional trusted Host authority; repeat for each hostname/IP and port clients use.         | --allowed-host mcp.example.com:8080         |
| `--env`                              | No                         | Additional environment variables to pass to the MCP stdio server. Can be used multiple times. | FOO BAR                                     |
| `--cwd`                              | No                         | The working directory to pass to the MCP stdio server process.                                | /tmp                                        |
| `--pass-environment`                 | No                         | Pass through all environment variables when spawning the server                               | --no-pass-environment                       |
| `--allow-origin`                     | No                         | Allowed origins for the SSE server. Can be used multiple times. Default is no CORS allowed.   | --allow-origin "\*"                         |
| `--expose-header`                    | No                         | Headers added to `Access-Control-Expose-Headers`. Can be used multiple times. Defaults to `mcp-session-id`. | --expose-header Custom-Header              |
| `--initialization-timeout SECONDS`   | No                         | Deadline for each stdio server initialization; default is unlimited. Cleanup may take additional time. | --initialization-timeout 30                 |
| `--stateless`                        | No                         | Enable stateless mode for streamable http transports. Default is False                        | --no-stateless                              |
| `--named-server NAME COMMAND_STRING` | No                         | Defines a named stdio server.                                                                 | --named-server fetch 'uvx mcp-server-fetch' |
| `--named-server-config FILE_PATH`    | No                         | Path to a JSON file defining named stdio servers.                                             | --named-server-config /path/to/servers.json |
| `--sse-port` (deprecated)            | No                         | Ineffective; use `--port` instead.                                                            | —                                           |
| `--sse-host` (deprecated)            | No                         | Ineffective; use `--host` instead.                                                            | —                                           |

### 2.2 Example usage

To start the `mcp-proxy` server that listens on port 8080 and connects to the local MCP server:

```bash
# Start the MCP server behind the proxy
mcp-proxy uvx mcp-server-fetch

# Start the MCP server behind the proxy with a custom port
mcp-proxy --port=8080 uvx mcp-server-fetch

# Start the MCP server behind the proxy with a custom host and port
mcp-proxy --host=0.0.0.0 --port=8080 --allowed-host mcp.example.com:8080 uvx mcp-server-fetch
# Replace mcp.example.com:8080 with the hostname/IP and port clients send in their Host header.

# Start the MCP server behind the proxy with a custom user agent
# Note that the `--` separator is used to separate the `mcp-proxy` arguments from the `mcp-server-fetch` arguments
mcp-proxy --port=8080 -- uvx mcp-server-fetch --user-agent=YourUserAgent

# Start multiple named MCP servers behind the proxy
mcp-proxy --port=8080 --named-server fetch 'uvx mcp-server-fetch' --named-server fetch2 'uvx mcp-server-fetch'

# Start multiple named MCP servers using a configuration file
mcp-proxy --port=8080 --named-server-config ./servers.json

# Start the MCP server with CORS enabled and custom exposed headers
mcp-proxy --port=8080 --allow-origin='*' --expose-header Custom-Header uvx mcp-server-fetch
```

## Named Servers

- `NAME` is used in the URL path `/servers/NAME/`.
- `COMMAND_STRING` is the command to start the server (e.g., 'uvx mcp-server-fetch').
  - Can be used multiple times.
  - This argument is ignored if `--named-server-config` is used.
- `FILE_PATH` - If provided, this is the exclusive source for named servers, and `--named-server` CLI arguments are ignored.

If a default server is specified (the `command_or_url` argument without `--named-server` or `--named-server-config`), it will be accessible at the root paths (e.g., `http://127.0.0.1:8080/sse`).

Named servers (whether defined by `--named-server` or `--named-server-config`) will be accessible under `/servers/<server-name>/` (e.g., `http://127.0.0.1:8080/servers/fetch1/sse`).
The `/status` endpoint provides global status. Its `server_instances` keys normally use the configured
named-server names and `default` for the positional server. If both a positional server and a named
server called `default` are configured, all entries use route keys instead: `/` for the positional
server and `/servers/<name>` for named servers. This keeps both instances distinct without reserving
a configured name.

If a named server fails with a spawn-stage operating-system error other than `TimeoutError`, an MCP
initialization error, an initialization timeout, or stdout EOF observed before activation, `mcp-proxy`
marks it as `failed` in `/status` and continues with the remaining named servers. Failed servers have
no routes or advertised endpoints. Other startup failures, cleanup failures, and failures in the
positional server remain fatal. Startup exits with an error if none of the configured servers starts.

**JSON Configuration File Format for `--named-server-config`:**

The JSON file should follow this structure:

```json
{
  "mcpServers": {
    "fetch": {
      "disabled": false,
      "timeout": 60,
      "command": "uvx",
      "args": [
        "mcp-server-fetch"
      ],
      "transportType": "stdio"
    },
    "github": {
      "timeout": 60,
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-github"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "<YOUR_TOKEN>"
      },
      "transportType": "stdio"
    }
  }
}
```

- `mcpServers`: A dictionary where each key is the server name (used in the URL path, e.g., `/servers/fetch/`) and the value is an object defining the server.
- `command`: (Required) The command to execute for the stdio server.
- `args`: (Optional) A list of arguments for the command. Defaults to an empty list.
- `enabled`: (Optional) If `false`, this server definition will be skipped. Defaults to `true`.
- `timeout` and `transportType`: These fields are present in standard MCP client configurations but are currently **ignored** by `mcp-proxy` when loading named servers. The transport type is implicitly "stdio".

## Installation

### Installing via PyPI

The stable version of the package is available on the PyPI repository. You can install it using the following command:

```bash
# Option 1: With uv (recommended)
uv tool install mcp-proxy

# Option 2: With pipx (alternative)
pipx install mcp-proxy
```

Once installed, you can run the server using the `mcp-proxy` command. See configuration options for each mode above.

### Installing via Github repository (latest)

The latest version of the package can be installed from the git repository using the following command:

```bash
uv tool install git+https://github.com/sparfenyuk/mcp-proxy
```

> [!NOTE]
> If you have already installed the server, you can update it using `uv tool upgrade --reinstall` command.

> [!NOTE]
> If you want to delete the server, use the `uv tool uninstall mcp-proxy` command.

### Installing as container

Starting from version 0.3.2, it's possible to pull and run the corresponding container image.
Images are published to GHCR and Docker Hub as multi-platform manifests for `linux/amd64` and `linux/arm64`;
Docker selects the matching image for the host architecture automatically.

The runtime image flavors are:

| Flavor | Base | Tools |
| --- | --- | --- |
| `alpine` | Python 3.13 on Alpine Linux (musl) | `uv`, `uvx` |
| `debian` | Python 3.13 on Debian Bookworm (glibc) | `uv`, `uvx` |
| `debian-node` | Debian Bookworm (glibc) | `uv`, `uvx`, Node.js 24, `npm`, `npx` |

The unsuffixed tags and `-alpine` tags select the Alpine image. For a release
tag such as `vX.Y.Z`, the Debian images use `vX.Y.Z-debian` and
`vX.Y.Z-debian-node`; main-branch commit tags use the matching
`commit-<short-sha>-debian` and `commit-<short-sha>-debian-node` forms. New
flavors are available only for builds made after this change is released;
existing tags are not rebuilt. The existing automatic `latest` release alias
continues to select Alpine. Debian flavors have no moving alias.

Choose Debian when a Python package needs glibc. Choose `debian-node` when the
MCP server needs Node.js tools; it can run `npx` packages but does not bundle
all possible server-specific system libraries. Installing tools with
dependencies that are not already cached still requires network access.

```bash
docker run --rm -t ghcr.io/sparfenyuk/mcp-proxy:v0.12.0 --help
docker run --rm -t sparfenyuk/mcp-proxy:v0.12.0 --help
```

### Troubleshooting

- **Problem**: Claude Desktop can't start the server: ENOENT code in the logs

  **Solution**: Try to use the full path to the binary. To do so, open a terminal and run the command`which mcp-proxy` (
  macOS, Linux) or `where.exe mcp-proxy` (Windows). Then, use the output path as a value for 'command' attribute:
  ```json
    "fetch": {
      "command": "/full/path/to/bin/mcp-proxy",
      "args": [
        "http://localhost:8932/sse"
      ]
    }
  ```

## Extending a container image

The `debian-node` flavor includes Node.js, `npm`, and `npx`. For other
operating-system packages, extend a flavor with that distribution's package
manager. Keep Python virtual environments on the same libc family as their
build stage.

```Dockerfile
# file: mcp-proxy.Dockerfile

FROM ghcr.io/sparfenyuk/mcp-proxy:vX.Y.Z-alpine

RUN apk add --no-cache curl
ENV UV_PYTHON_PREFERENCE=only-system
```

Build and run the custom image:

```bash
docker build -f mcp-proxy.Dockerfile -t mcp-proxy-custom .
docker run --rm -p 127.0.0.1:8080:8080 mcp-proxy-custom \
  --host=0.0.0.0 --port=8080 --allowed-host 127.0.0.1:8080 \
  -- uvx mcp-server-fetch
```

The first `uvx` or `npx` launch needs network access to download a package that is not already cached.
When exposing the proxy beyond a local test, continue to configure `--allowed-host`
and allowed origins for the client-facing address. `--pass-environment` controls
which host environment values reach spawned server processes; use it only when needed.

To check a locally built image, run the opt-in container tests:

```bash
docker build -t mcp-proxy:flavors-alpine .
MCP_PROXY_TEST_IMAGE=mcp-proxy:flavors-alpine MCP_PROXY_TEST_FLAVOR=alpine uv run --frozen pytest -q tests/test_container_image.py

docker build -f Dockerfile.debian --target debian -t mcp-proxy:flavors-debian .
MCP_PROXY_TEST_IMAGE=mcp-proxy:flavors-debian MCP_PROXY_TEST_FLAVOR=debian uv run --frozen pytest -q tests/test_container_image.py

docker build -f Dockerfile.debian --target debian-node -t mcp-proxy:flavors-debian-node .
MCP_PROXY_TEST_IMAGE=mcp-proxy:flavors-debian-node MCP_PROXY_TEST_FLAVOR=debian-node uv run --frozen pytest -q tests/test_container_image.py
```

These tests disable container network access, run a local wheel through `uvx`,
launch an MCP server through `uv`, and exercise a local `npx` package. Flavor
checks are skipped when `MCP_PROXY_TEST_FLAVOR` is unset; all container tests
are skipped when `MCP_PROXY_TEST_IMAGE` is unset.

## Docker Compose Setup

With the custom Dockerfile, you can define a service in your Docker Compose file:

```yaml
services:
  mcp-proxy-custom:
    build:
      context: .
      dockerfile: mcp-proxy.Dockerfile
    network_mode: host
    restart: unless-stopped
    ports:
      - 8096:8096
    command: "--pass-environment --host=0.0.0.0 --port=8096 --allowed-host mcp.example.com:8096 uvx mcp-server-fetch"
```

> [!NOTE]
> Don't forget to set `--pass-environment` argument, otherwise you'll end up with the error "No interpreter found in
> managed installations or search path"
> Replace `mcp.example.com:8096` with the hostname/IP and port clients send in their `Host` header.

## Command line arguments

```bash
usage: mcp-proxy [-h] [--version] [-H KEY VALUE]
                 [--transport {sse,streamablehttp}] [--verify-ssl [VALUE]]
                 [--no-verify-ssl] [-e KEY VALUE] [--cwd CWD]
                 [--client-id CLIENT_ID] [--client-secret CLIENT_SECRET] [--token-url TOKEN_URL]
                 [--pass-environment | --no-pass-environment]
                 [--log-level LEVEL] [--debug | --no-debug]
                 [--named-server NAME COMMAND_STRING]
                 [--named-server-config FILE_PATH] [--port PORT] [--host HOST]
                 [--stateless | --no-stateless] [--sse-port SSE_PORT]
                 [--sse-host SSE_HOST]
                 [--allow-origin ALLOW_ORIGIN [ALLOW_ORIGIN ...]]
                 [--allowed-host HOST]
                 [--expose-header HEADER]
                 [command_or_url] [args ...]

Start the MCP proxy in one of two possible modes: as a client or a server.

positional arguments:
  command_or_url        Command or URL to connect to. When a URL, will run an SSE/StreamableHTTP client. Otherwise, if --named-server is not used, this will be the command for the default stdio client. If --named-server is used, this argument is ignored for stdio mode unless no default server is desired. See corresponding options for more details.

options:
  -h, --help            show this help message and exit
  --version             Show the version and exit

SSE/StreamableHTTP client options:
  -H, --headers KEY VALUE
                        Headers to pass to the SSE server. Can be used multiple times.
  --transport {sse,streamablehttp}
                        The transport to use for the client. Default is SSE.
  --verify-ssl [VALUE]  Control SSL verification when acting as a client. Use without a value to force verification, pass 'false' to disable, or provide a path to a PEM bundle.
  --no-verify-ssl       Disable SSL verification (alias for --verify-ssl false).
  --client-id CLIENT_ID
                        OAuth2 client ID for authentication
  --client-secret CLIENT_SECRET
                        OAuth2 client secret for authentication
  --token-url TOKEN_URL
                        OAuth2 token URL for authentication
  --username USERNAME   Username for authentication
  --password PASSWORD   Password for authentication
  --digest              Use digest authentication

stdio client options:
  args                  Any extra arguments to the command to spawn the default server. Ignored if only named servers are defined.
  -e, --env KEY VALUE   Environment variables used when spawning the default server. Can be used multiple times. For named servers, environment is inherited or passed via --pass-environment.
  --cwd CWD             The working directory to use when spawning the default server process. Named servers inherit the proxy's CWD.
  --pass-environment, --no-pass-environment
                        Pass through all environment variables when spawning all server processes.
  --log-level LEVEL     Set the log level. Default is INFO.
  --debug, --no-debug   Enable debug mode with detailed logging output. Equivalent to --log-level DEBUG. If both --debug and --log-level are provided, --debug takes precedence.
  --named-server NAME COMMAND_STRING
                        Define a named stdio server. NAME is for the URL path /servers/NAME/. COMMAND_STRING is a single string with the command and its arguments (e.g., 'uvx mcp-server-fetch --timeout 10'). These servers inherit the proxy's CWD and environment from --pass-environment.
  --named-server-config FILE_PATH
                        Path to a JSON configuration file for named stdio servers. If provided, this will be the exclusive source for named server definitions, and any --named-server CLI arguments will be ignored.

SSE server options:
  --port PORT           Port to expose an SSE server on. Default is a random port
  --host HOST           Host to expose an SSE server on. Default is 127.0.0.1
  --initialization-timeout SECONDS
                        Maximum time to initialize each stdio server. By default, wait indefinitely.
                        A timeout fails proxy startup and does not limit tool calls. Cleanup may take additional time.
  --stateless, --no-stateless
                        Enable stateless mode for streamable http transports. Default is False
  --sse-port SSE_PORT   (deprecated and ineffective; use --port)
  --sse-host SSE_HOST   (deprecated and ineffective; use --host)
  --allow-origin ALLOW_ORIGIN [ALLOW_ORIGIN ...]
                        Allowed origins for the SSE server. Can be used multiple times. Default is no CORS allowed.
  --allowed-host HOST   Additional trusted Host authority. Can be used multiple times.
  --expose-header HEADER
                        Headers to expose via Access-Control-Expose-Headers. Defaults to 'Mcp-Session-Id'. Can be used multiple times.

HTTP server requests are checked against trusted Host authorities before reaching routes. Loopback hostnames and IPs are trusted by default; a non-wildcard `--host` also trusts that bind address. A wildcard bind does not trust arbitrary Host values. Add the public hostname or reverse-proxy Host with `--allowed-host`, repeating the option for each authority (for example, `--allowed-host mcp.example.com --allowed-host mcp.example.com:8443`). A terminal `:*` may be used for an authority that accepts any port.

Raw Uvicorn HTTP access-log lines are disabled because SSE session IDs can appear in request query strings.

Host and browser Origin are separate checks. `--allow-origin '*'` allows browser Origins but does not bypass Host validation. Native clients that omit Origin remain supported. These checks do not authenticate callers; protect publicly reachable deployments with network controls or an authenticated reverse proxy.

Examples:
  mcp-proxy http://localhost:8080/sse
  mcp-proxy --no-verify-ssl https://server.local/sse
  mcp-proxy --transport streamablehttp http://localhost:8080/mcp
  mcp-proxy --transport streamablehttp --username user --password pass https://server.local/mcp
  mcp-proxy --headers Authorization 'Bearer YOUR_TOKEN' http://localhost:8080/sse
  mcp-proxy --client-id CLIENT_ID --client-secret CLIENT_SECRET --token-url https://auth.example.com/token http://localhost:8080/sse
  mcp-proxy --port 8080 -- your-command --arg1 value1 --arg2 value2
  mcp-proxy --named-server fetch 'uvx mcp-server-fetch' --port 8080
  mcp-proxy your-command --port 8080 -e KEY VALUE -e ANOTHER_KEY ANOTHER_VALUE
  mcp-proxy your-command --port 8080 --allow-origin='*'
  mcp-proxy your-command --port 8080 --allow-origin='*' --expose-header Custom-Header
```

### Example config file

```json
{
  "mcpServers": {
    "fetch": {
      "enabled": true,
      "timeout": 60,
      "command": "uvx",
      "args": [
        "mcp-server-fetch"
      ],
      "transportType": "stdio"
    },
    "github": {
      "timeout": 60,
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-github"
      ],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "<YOUR_TOKEN>"
      },
      "transportType": "stdio"
    }
  }
}
```

## Testing

Check the `mcp-proxy` server by running it with the `mcp-server-fetch` server. You can use
the [inspector tool](https://modelcontextprotocol.io/docs/tools/inspector) to test the target server.

```bash
# Run the stdio server called mcp-server-fetch behind the proxy over SSE
mcp-proxy --port=8080 uvx mcp-server-fetch &

# Connect to the SSE proxy server spawned above using another instance of mcp-proxy given the URL of the SSE server
mcp-proxy http://127.0.0.1:8080/sse

# Send CTRL+C to stop the second server

# Bring the first server to the foreground
fg

# Send CTRL+C to stop the first server
```
