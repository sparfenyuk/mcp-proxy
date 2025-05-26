# mcp-proxy

![GitHub License](https://img.shields.io/github/license/sparfenyuk/mcp-proxy)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/mcp-proxy)
![PyPI - Downloads](https://img.shields.io/pypi/dm/mcp-proxy)
[![codecov](https://codecov.io/gh/sparfenyuk/mcp-proxy/graph/badge.svg?token=31VV9L7AZQ)](https://codecov.io/gh/sparfenyuk/mcp-proxy)
[![smithery badge](https://smithery.ai/badge/mcp-proxy)](https://smithery.ai/server/mcp-proxy)

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
    - [Installing via Smithery](#installing-via-smithery)
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

Arguments

| Name             | Required | Description                                                                                                       | Example                                       |
| ---------------- | -------- | ----------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `command_or_url` | Yes      | The MCP server SSE endpoint to connect to                                                                         | http://example.io/sse                         |
| `--headers`      | No       | Headers to use for the MCP server SSE connection                                                                  | Authorization 'Bearer my-secret-access-token' |
| `--transport`    | No       | Decides which transport protocol to use when connecting to an MCP server. Can be either 'sse' or 'streamablehttp' | streamablehttp                                |

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

This mode requires the `--sse-port` argument to be set. The `--sse-host` argument can be set to specify the host IP
address that the SSE server will listen on. Additional environment variables can be passed to the local stdio server
using the `--env` argument. The command line arguments for the local stdio server must be passed after the `--`
separator.

Arguments

| Name                                 | Required                   | Description                                                                                 | Example                                     |
|--------------------------------------|----------------------------|---------------------------------------------------------------------------------------------|---------------------------------------------|
| `command_or_url`                     | Yes                        | The command to spawn the MCP stdio server                                                   | uvx mcp-server-fetch                        |
| `--port`                             | No, random available       | The MCP server port to listen on                                                            | 8080                                        |
| `--host`                             | No, `127.0.0.1` by default | The host IP address that the MCP server will listen on                                      | 0.0.0.0                                     |
| `--env`                              | No                         | Additional environment variables to pass to the MCP stdio server. Can be used multiple times. | FOO BAR                                     |
| `--cwd`                              | No                         | The working directory to pass to the MCP stdio server process.                              | /tmp                                        |
| `--pass-environment`                 | No                         | Pass through all environment variables when spawning the server                             | --no-pass-environment                       |
| `--allow-origin`                     | No                         | Allowed origins for the SSE server. Can be used multiple times. Default is no CORS allowed. | --allow-origin "\*"                           |
| `--stateless`                        | No                         | Enable stateless mode for streamable http transports. Default is False                      | --no-stateless                              |
| `--named-server NAME COMMAND_STRING` | No                         | Defines a named stdio server.                                                               | --named-server fetch 'uvx mcp-server-fetch' |
| `--named-server-config FILE_PATH`    | No                         | Path to a JSON file defining named stdio servers.                                           | --named-server-config /path/to/servers.json |
| `--sse-port` (deprecated)            | No, random available       | The SSE server port to listen on                                                            | 8080                                        |
| `--sse-host` (deprecated)            | No, `127.0.0.1` by default | The host IP address that the SSE server will listen on                                      | 0.0.0.0                                     |

### 2.2 Example usage

To start the `mcp-proxy` server that listens on port 8080 and connects to the local MCP server:

```bash
# Start the MCP server behind the proxy
mcp-proxy uvx mcp-server-fetch

# Start the MCP server behind the proxy with a custom port
# (deprecated) mcp-proxy --sse-port=8080 uvx mcp-server-fetch
mcp-proxy --port=8080 uvx mcp-server-fetch

# Start the MCP server behind the proxy with a custom host and port
# (deprecated) mcp-proxy --sse-host=0.0.0.0 --sse-port=8080 uvx mcp-server-fetch
mcp-proxy --host=0.0.0.0 --port=8080 uvx mcp-server-fetch

# Start the MCP server behind the proxy with a custom user agent
# Note that the `--` separator is used to separate the `mcp-proxy` arguments from the `mcp-server-fetch` arguments
# (deprecated) mcp-proxy --sse-port=8080 -- uvx mcp-server-fetch --user-agent=YourUserAgent
mcp-proxy --port=8080 -- uvx mcp-server-fetch --user-agent=YourUserAgent

# Start multiple named MCP servers behind the proxy
mcp-proxy --port=8080 --named-server fetch 'uvx mcp-server-fetch' --named-server fetch2 'uvx mcp-server-fetch'

# Start multiple named MCP servers using a configuration file
mcp-proxy --port=8080 --named-server-config ./servers.json
```

## Named Servers

- `NAME` is used in the URL path `/servers/NAME/`.
- `COMMAND_STRING` is the command to start the server (e.g., 'uvx mcp-server-fetch').
  - Can be used multiple times.
  - This argument is ignored if `--named-server-config` is used.
- `FILE_PATH` - If provided, this is the exclusive source for named servers, and `--named-server` CLI arguments are ignored.

If a default server is specified (the `command_or_url` argument without `--named-server` or `--named-server-config`), it will be accessible at the root paths (e.g., `http://127.0.0.1:8080/sse`).

Named servers (whether defined by `--named-server` or `--named-server-config`) will be accessible under `/servers/<server-name>/` (e.g., `http://127.0.0.1:8080/servers/fetch1/sse`).
The `/status` endpoint provides global status.

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

### Installing via Smithery

To install MCP Proxy for Claude Desktop automatically via [Smithery](https://smithery.ai/server/mcp-proxy):

```bash
npx -y @smithery/cli install mcp-proxy --client claude
```

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

Starting from version 0.3.2, it's possible to pull and run the corresponding container image:

```bash
docker run -t ghcr.io/sparfenyuk/mcp-proxy:v0.3.2-alpine --help
```

### Troubleshooting

- **Problem**: Claude Desktop can't start the server: ENOENT code in the logs

  **Solution**: Try to use the full path to the binary. To do so, open a terminal and run the command`where mcp-proxy` (
  macOS, Linux) or `where.exe mcp-proxy` (Windows). Then, use the output path as a value for 'command' attribute:
  ```json
    "fetch": {
      "command": "/full/path/to/bin/mcp-proxy",
      "args": [
        "http://localhost:8932/sse"
      ]
    }
  ```

## Extending the container image

You can extend the `mcp-proxy` container image to include additional executables. For instance, `uv` is not included by
default, but you can create a custom image with it:

```Dockerfile
# file: mcp-proxy.Dockerfile

FROM ghcr.io/sparfenyuk/mcp-proxy:latest

# Install the 'uv' package
RUN python3 -m ensurepip && pip install --no-cache-dir uv

ENV PATH="/usr/local/bin:$PATH" \
    UV_PYTHON_PREFERENCE=only-system

ENTRYPOINT [ "mcp-proxy" ]
```

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
    command: "--pass-environment --port=8096 --sse-host 0.0.0.0 uvx mcp-server-fetch"
```

> [!NOTE]
> Don't forget to set `--pass-environment` argument, otherwise you'll end up with the error "No interpreter found in
> managed installations or search path"

## Command line arguments

```bash
usage: mcp-proxy [-h] [-H KEY VALUE] [--transport {sse,streamablehttp}] [-e KEY VALUE] [--cwd CWD] [--pass-environment | --no-pass-environment]
                 [--debug | --no-debug] [--named-server NAME COMMAND_STRING] [--named-server-config FILE_PATH] [--port PORT] [--host HOST]
                 [--stateless | --no-stateless] [--sse-port SSE_PORT] [--sse-host SSE_HOST] [--allow-origin ALLOW_ORIGIN [ALLOW_ORIGIN ...]]
                 [command_or_url] [args ...]

Start the MCP proxy.
It can run as an SSE client (connecting to a remote SSE server and exposing stdio).
Or, it can run as an SSE server (connecting to local stdio command(s) and exposing them over SSE).
When running as an SSE server, it can proxy a single default stdio command or multiple named stdio commands (defined via CLI or a config file).

positional arguments:
  command_or_url        Command or URL.
                        If URL (http/https): Runs in SSE/StreamableHTTP client mode.
                        If command string: Runs in SSE server mode, this is the default stdio server.
                        If --named-server or --named-server-config is used, this can be omitted if no default server is desired.

options:
  -h, --help            show this help message and exit

SSE/StreamableHTTP client options:
  -H, --headers KEY VALUE
                        Headers to pass to the SSE server. Can be used multiple times.
  --transport {sse,streamablehttp}
                        The transport to use for the client. Default is SSE.

stdio client options:
  args                  Any extra arguments to the command to spawn the default server. Ignored if only named servers are defined.
  -e, --env KEY VALUE   Environment variables used when spawning the default server. Can be used multiple times. For named servers, environment is inherited or passed via --pass-environment.
  --cwd CWD             The working directory to use when spawning the default server process. Named servers inherit the proxy's CWD.
  --pass-environment, --no-pass-environment
                        Pass through all environment variables when spawning all server processes.
  --debug, --no-debug   Enable debug mode with detailed logging output.
  --named-server NAME COMMAND_STRING
                        Define a named stdio server. NAME is for the URL path /servers/NAME/. COMMAND_STRING is a single string with the command and its arguments (e.g., 'uvx mcp-server-fetch --timeout 10'). These servers inherit the proxy's CWD and environment from --pass-environment. Can be specified multiple times. Ignored if --named-server-config is used.
  --named-server-config FILE_PATH
                        Path to a JSON configuration file for named stdio servers. If provided, this will be the exclusive source for named server definitions, and any --named-server CLI arguments will be ignored.

SSE server options:
  --port PORT           Port to expose an SSE server on. Default is a random port
  --host HOST           Host to expose an SSE server on. Default is 127.0.0.1
  --stateless, --no-stateless
                        Enable stateless mode for streamable http transports. Default is False
  --sse-port SSE_PORT   (deprecated) Same as --port
  --sse-host SSE_HOST   (deprecated) Same as --host
  --allow-origin ALLOW_ORIGIN [ALLOW_ORIGIN ...]
                        Allowed origins for the SSE server. Can be used multiple times. Default is no CORS allowed.

Examples:
  mcp-proxy http://localhost:8080/sse
  mcp-proxy --transport streamablehttp http://localhost:8080/mcp
  mcp-proxy --headers Authorization 'Bearer YOUR_TOKEN' http://localhost:8080/sse
  mcp-proxy --port 8080 -- my-default-command --arg1 value1
  mcp-proxy --port 8080 --named-server fetch1 'uvx mcp-server-fetch' --named-server tool2 'my-custom-tool --verbose'
  mcp-proxy --port 8080 --named-server-config /path/to/servers.json
  mcp-proxy --port 8080 --named-server-config /path/to/servers.json -- my-default-command --arg1
  mcp-proxy --port 8080 -e KEY VALUE -e ANOTHER_KEY ANOTHER_VALUE -- my-default-command
  mcp-proxy --port 8080 --allow-origin='*' -- my-default-command
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
