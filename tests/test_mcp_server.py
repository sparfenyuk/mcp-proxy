"""Tests for the sse server."""
# ruff: noqa: PLR2004

import asyncio
import builtins
import contextlib
import logging
import os
import shlex
import signal
import socket
import sys
import typing as t
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import anyio
import httpx
import pytest
import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client, streamablehttp_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import McpError
from sse_starlette import sse as sse_module
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from mcp_proxy.__main__ import _setup_logging
from mcp_proxy.httpx_client import custom_httpx_client
from mcp_proxy.mcp_server import (
    DEFAULT_EXPOSE_HEADERS,
    MCPServerSettings,
    _StdioExitWatch,
    create_single_instance_routes,
    run_mcp_server,
)

_LOGGING_STDIO_SERVER = """
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server = Server("logging-test")

@server.list_tools()
async def list_tools():
    return [Tool(name="echo", description="Echo", inputSchema={"type": "object"})]

@server.call_tool()
async def call_tool(name, arguments):
    return [TextContent(type="text", text=str(arguments["message"]))]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

asyncio.run(main())
"""


_BLOCKING_STDIO_SERVER = """
import asyncio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool

entry_port = __ENTRY_PORT__
server = Server("blocking-test")

@server.list_tools()
async def list_tools():
    return [Tool(name="block", description="Wait forever", inputSchema={"type": "object"})]

@server.call_tool()
async def call_tool(name, arguments):
    _reader, writer = await asyncio.open_connection("127.0.0.1", entry_port)
    writer.close()
    await writer.wait_closed()
    await asyncio.Event().wait()

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

asyncio.run(main())
"""


def _backend_entry_handler(
    entry_event: asyncio.Event,
) -> t.Callable[[asyncio.StreamReader, asyncio.StreamWriter], t.Awaitable[None]]:
    async def handle_entry(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        entry_event.set()
        writer.close()
        await writer.wait_closed()

    return handle_entry


async def _wait_for_proxy_ready(base_url: str, health_path: str = "/healthz") -> None:
    async with custom_httpx_client(timeout=httpx.Timeout(1, connect=0.1)) as client:
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(f"{base_url}{health_path}")
            except httpx.HTTPError:
                await asyncio.sleep(0.05)
                continue
            if response.status_code == 204:
                return
            await asyncio.sleep(0.05)
    raise AssertionError("proxy did not become ready")


async def _send_sigterm_and_wait(process: asyncio.subprocess.Process) -> float:
    started = asyncio.get_running_loop().time()
    process.send_signal(signal.SIGTERM)
    await asyncio.wait_for(process.wait(), timeout=12)
    return asyncio.get_running_loop().time() - started


async def _initialize_streamable_http(client: httpx.AsyncClient, url: str) -> dict[str, str]:
    initialize = types.InitializeRequest(
        params=types.InitializeRequestParams(
            protocolVersion=types.LATEST_PROTOCOL_VERSION,
            capabilities=types.ClientCapabilities(),
            clientInfo=types.Implementation(name="shutdown-test", version="1"),
        ),
    )
    headers = {"Accept": "application/json, text/event-stream"}
    response = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            **initialize.model_dump(by_alias=True, mode="json", exclude_none=True),
        },
        headers=headers,
    )
    response.raise_for_status()
    headers.update(
        {
            "Mcp-Session-Id": response.headers["mcp-session-id"],
            "MCP-Protocol-Version": str(response.json()["result"]["protocolVersion"]),
        },
    )

    notification = types.InitializedNotification()
    response = await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            **notification.model_dump(by_alias=True, mode="json", exclude_none=True),
        },
        headers=headers,
    )
    assert response.status_code == 202
    return headers


def _initialization_timeout_stdio_server_script(
    tmp_path: Path,
    *,
    initialize_delay: float,
    tool_call_delay: float,
) -> str:
    return f"""
import json, os, pathlib, sys, time
initialize_received = pathlib.Path({str(tmp_path / "initialize-received")!r})
initialized = pathlib.Path({str(tmp_path / "initialized")!r})
tool_call_entered = pathlib.Path({str(tmp_path / "tool-call-entered")!r})
tool_call_finished = pathlib.Path({str(tmp_path / "tool-call-finished")!r})
child_pid = pathlib.Path({str(tmp_path / "child-pid")!r})
child_stopped = pathlib.Path({str(tmp_path / "child-stopped")!r})
initialize_delay = {initialize_delay!r}
tool_call_delay = {tool_call_delay!r}
child_pid.write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        initialize_received.touch()
        time.sleep(initialize_delay)
        result = dict(
            protocolVersion=request["params"]["protocolVersion"],
            capabilities=dict(tools=dict()),
            serverInfo=dict(name="timeout-test", version="1"),
        )
    elif method == "notifications/initialized":
        initialized.touch()
        continue
    elif method == "tools/list":
        result = dict(
            tools=[
                dict(
                    name="delay",
                    description="Wait longer than the initialization budget",
                    inputSchema=dict(type="object", properties=dict()),
                ),
            ],
        )
    elif method == "tools/call":
        tool_call_entered.touch()
        time.sleep(tool_call_delay)
        tool_call_finished.touch()
        result = dict(content=[dict(type="text", text="finished")], isError=False)
    else:
        result = dict()
    if "id" in request:
        print(json.dumps(dict(jsonrpc="2.0", id=request["id"], result=result)), flush=True)
child_stopped.touch()
"""


async def _start_stdio_proxy(
    backend_script: Path,
    port: int,
    *,
    initialization_timeout: float | None = None,
) -> asyncio.subprocess.Process:
    command = [
        sys.executable,
        "-m",
        "mcp_proxy",
        sys.executable,
        str(backend_script),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if initialization_timeout is not None:
        command.extend(["--initialization-timeout", str(initialization_timeout)])
    return await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def _stop_stdio_proxy(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    await process.communicate()


def _assert_pid_reaped(pid_file: Path) -> None:
    if sys.platform != "win32":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


def _issue198_stdio_server_script(  # noqa: PLR0913
    name: str,
    pid_file: Path,
    stopped_file: Path,
    *,
    mode: str = "healthy",
    initialize_received: Path | None = None,
    checked_pid_file: Path | None = None,
    pid_check_result: Path | None = None,
) -> str:
    """Build a small real stdio child for named-startup integration tests."""
    initialize_received_path = str(initialize_received) if initialize_received else None
    checked_pid_path = str(checked_pid_file) if checked_pid_file else None
    pid_check_result_path = str(pid_check_result) if pid_check_result else None
    return f"""
import json, os, pathlib, sys
name = {name!r}
mode = {mode!r}
pid_file = pathlib.Path({str(pid_file)!r})
stopped_file = pathlib.Path({str(stopped_file)!r})
pid_file.write_text(str(os.getpid()))
checked_pid_file = {checked_pid_path!r}
pid_check_result = {pid_check_result_path!r}
if checked_pid_file is not None and pid_check_result is not None:
    try:
        os.kill(int(pathlib.Path(checked_pid_file).read_text()), 0)
    except ProcessLookupError:
        pathlib.Path(pid_check_result).write_text("reaped")
    else:
        pathlib.Path(pid_check_result).write_text("alive")
try:
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            initialize_received = {initialize_received_path!r}
            if initialize_received is not None:
                pathlib.Path(initialize_received).touch()
            if mode == "eof":
                break
            if mode == "timeout":
                continue
            result = dict(
                protocolVersion=request["params"]["protocolVersion"],
                capabilities=dict(tools=dict()),
                serverInfo=dict(name="issue198-" + name, version="1"),
            )
        elif method == "notifications/initialized":
            if mode == "exit-after-initialized":
                break
            continue
        elif method == "tools/list":
            result = dict(tools=[dict(
                name="echo",
                description="Synthetic echo",
                inputSchema=dict(type="object", properties=dict(message=dict(type="string"))),
            )])
        elif method == "tools/call":
            result = dict(
                content=[dict(type="text", text=request["params"]["arguments"]["message"])],
                isError=False,
            )
        else:
            result = dict()
        if "id" in request:
            print(json.dumps(dict(jsonrpc="2.0", id=request["id"], result=result)), flush=True)
finally:
    stopped_file.touch()
"""


async def _start_named_stdio_proxy(
    servers: list[tuple[str, str]],
    *,
    initialization_timeout: float = 1,
) -> asyncio.subprocess.Process:
    command = [
        sys.executable,
        "-m",
        "mcp_proxy",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--initialization-timeout",
        str(initialization_timeout),
    ]
    for name, server_command in servers:
        command.extend(["--named-server", name, server_command])
    return await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def _wait_for_proxy_url(
    process: asyncio.subprocess.Process,
    *,
    before_listen: t.Callable[[], None] | None = None,
) -> tuple[str, str]:
    assert process.stdout is not None
    output: list[str] = []
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        try:
            line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=max(0.01, deadline - asyncio.get_running_loop().time()),
            )
        except TimeoutError as exc:
            message = "proxy did not log its listening address\n" + "".join(output)
            raise AssertionError(message) from exc
        if not line:
            await process.wait()
            raise AssertionError(
                f"proxy exited before listening with status {process.returncode}\n"
                + "".join(output)
            )
        output.append(line.decode(errors="replace"))
        marker = "Uvicorn running on http://127.0.0.1:"
        if marker in output[-1]:
            if before_listen is not None:
                before_listen()
            port = output[-1].split(marker, 1)[1].split()[0]
            return f"http://127.0.0.1:{port}", "".join(output)
    raise AssertionError("proxy did not log its listening address\n" + "".join(output))


async def _wait_for_file(path: Path) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    exists = await asyncio.to_thread(path.exists)
    while not exists and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        exists = await asyncio.to_thread(path.exists)
    assert exists, f"expected child event was not written: {path.name}"


async def _exercise_backend(base_url: str, path_prefix: str, expected_server: str) -> None:
    prefix = f"{base_url}{path_prefix}"
    async with (
        custom_httpx_client(timeout=httpx.Timeout(8)) as http_client,
        streamable_http_client(f"{prefix}/mcp", http_client=http_client) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        initialized = await session.initialize()
        assert initialized.serverInfo.name == expected_server
        assert [tool.name for tool in (await session.list_tools()).tools] == ["echo"]
        result = await session.call_tool("echo", {"message": "issue198 transport proof"})
        assert result.content[0].text == "issue198 transport proof"

    async with (
        sse_client(
            url=f"{prefix}/sse",
            httpx_client_factory=custom_httpx_client,
        ) as streams,
        ClientSession(*streams) as session,
    ):
        initialized = await session.initialize()
        assert initialized.serverInfo.name == expected_server
        assert [tool.name for tool in (await session.list_tools()).tools] == ["echo"]
        result = await session.call_tool("echo", {"message": "issue198 transport proof"})
        assert result.content[0].text == "issue198 transport proof"


async def _exercise_positional_backend_with_named_default(
    base_url: str,
) -> tuple[dict[str, str], int, int]:
    async with custom_httpx_client(timeout=httpx.Timeout(8)) as client:
        status_response = await client.get(f"{base_url}/status")
        assert status_response.status_code == httpx.codes.OK
        server_instances = status_response.json()["server_instances"]
        root_health_status = (await client.get(f"{base_url}/healthz")).status_code
        named_health_status = (await client.get(f"{base_url}/servers/default/healthz")).status_code

    await _exercise_backend(base_url, "", "issue198-root")
    return server_instances, root_health_status, named_health_status


def create_starlette_app(
    mcp_server: Server[t.Any],
    allow_origins: list[str] | None = None,
    *,
    debug: bool = False,
    stateless: bool = False,
) -> Starlette:
    """Create a Starlette application for the MCP server.

    Args:
        mcp_server: The MCP server instance to wrap
        allow_origins: List of allowed CORS origins
        debug: Enable debug mode
        stateless: Whether to use stateless HTTP sessions

    Returns:
        Starlette application instance
    """
    routes, http_manager = create_single_instance_routes(mcp_server, stateless_instance=stateless)

    middleware: list[Middleware] = []
    if allow_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=allow_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> t.AsyncIterator[None]:
        async with http_manager.run():
            yield

    app = Starlette(
        debug=debug,
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )
    app.router.redirect_slashes = False
    return app


class BackgroundServer(uvicorn.Server):
    """A test server that runs in a background thread."""

    def install_signal_handlers(self) -> None:
        """Do not install signal handlers."""

    @contextlib.asynccontextmanager
    async def run_in_background(self) -> t.AsyncIterator[None]:
        """Run the server in a background thread."""
        task = asyncio.create_task(self.serve())
        try:
            while not self.started:  # noqa: ASYNC110
                await asyncio.sleep(1e-3)
            yield
        finally:
            self.should_exit = self.force_exit = True
            await task

    @property
    def url(self) -> str:
        """Return the url of the started server."""
        hostport = next(
            iter([socket.getsockname() for server in self.servers for socket in server.sockets]),
        )
        return f"http://{hostport[0]}:{hostport[1]}"


def make_background_server(*, debug: bool = False, stateless: bool = False) -> BackgroundServer:
    """Create a BackgroundServer instance with specified parameters."""
    mcp_server: Server[object, t.Any] = Server("TestServer")

    @mcp_server.list_prompts()  # type: ignore[misc,no-untyped-call]
    async def list_prompts() -> list[types.Prompt]:
        return [types.Prompt(name="prompt1")]

    @mcp_server.list_tools()  # type: ignore[misc,no-untyped-call]
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="echo",
                description="Echo tool",
                inputSchema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            ),
        ]

    @mcp_server.call_tool()  # type: ignore[misc]
    async def call_tool(
        name: str,
        arguments: dict[str, t.Any] | None,
    ) -> list[types.Content]:
        assert name == "echo"
        message_value = ""
        if arguments:
            message_value = str(arguments.get("message", ""))
        return [types.TextContent(type="text", text=f"Echo: {message_value}")]

    app = create_starlette_app(
        mcp_server,
        allow_origins=["*"],
        debug=debug,
        stateless=stateless,
    )

    config = uvicorn.Config(app, port=0, log_level="info")
    return BackgroundServer(config)


@contextlib.asynccontextmanager
async def _isolated_sse_server_lifetime() -> t.AsyncIterator[set[asyncio.Task[t.Any]]]:
    """Settle this test's real SSE shutdown watcher before restoring shared state."""
    app_status = sse_module.AppStatus
    original_watcher = sse_module._shutdown_watcher  # noqa: SLF001
    owned_watchers: set[asyncio.Task[t.Any]] = set()
    watcher_started = asyncio.Event()

    async def owned_shutdown_watcher() -> None:
        task = asyncio.current_task()
        if task is not None:
            owned_watchers.add(task)
        watcher_started.set()
        await original_watcher()

    def is_shutdown_watcher(task: asyncio.Task[t.Any]) -> bool:
        return getattr(task.get_coro(), "cr_code", None) in {
            original_watcher.__code__,
            owned_shutdown_watcher.__code__,
        }

    if any(is_shutdown_watcher(task) for task in asyncio.all_tasks()):
        raise RuntimeError("cannot isolate an SSE lifetime with an unowned shutdown watcher")

    previous_state = (
        app_status.should_exit,
        app_status.enable_automatic_graceful_drain,
        app_status.original_handler,
    )

    app_status.should_exit = False
    app_status.enable_automatic_graceful_drain = True
    sse_module._shutdown_watcher = owned_shutdown_watcher  # noqa: SLF001
    try:
        yield owned_watchers
    finally:
        sse_module._shutdown_watcher = original_watcher  # noqa: SLF001
        with anyio.CancelScope(shield=True):
            owned_watchers.update(
                task
                for task in asyncio.all_tasks()
                if getattr(task.get_coro(), "cr_code", None) is owned_shutdown_watcher.__code__
            )
            if any(not task.done() for task in owned_watchers) and not watcher_started.is_set():
                await watcher_started.wait()
            for task in owned_watchers:
                if not task.done():
                    task.cancel()
            if owned_watchers:
                await asyncio.gather(*owned_watchers, return_exceptions=True)
            unowned_watchers = {
                task for task in asyncio.all_tasks() if is_shutdown_watcher(task)
            } - owned_watchers
            if unowned_watchers:
                raise RuntimeError(
                    "cannot restore SSE state while an unowned shutdown watcher is live"
                )
            (
                app_status.should_exit,
                app_status.enable_automatic_graceful_drain,
                app_status.original_handler,
            ) = previous_state


async def test_sse_transport() -> None:
    """Test basic glue code for the SSE transport and a fake MCP server."""
    async with _isolated_sse_server_lifetime() as watchers:
        server = make_background_server(debug=True)
        async with server.run_in_background():
            sse_url = f"{server.url}/sse"
            async with sse_client(url=sse_url) as streams, ClientSession(*streams) as session:
                await session.initialize()
                response = await session.list_prompts()
                assert len(response.prompts) == 1
                assert response.prompts[0].name == "prompt1"
    assert watchers
    assert all(task.done() for task in watchers)


async def test_sse_shutdown_state_does_not_poison_next_server_lifetime() -> None:
    """A late watcher from one server must not terminate the next server's SSE stream."""
    app_status = sse_module.AppStatus
    async with _isolated_sse_server_lifetime() as first_watchers:
        first_server = make_background_server()
        async with (
            first_server.run_in_background(),
            sse_client(url=f"{first_server.url}/sse") as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            prompts = await session.list_prompts()
            assert [prompt.name for prompt in prompts.prompts] == ["prompt1"]

    assert first_watchers
    assert all(task.done() for task in first_watchers)
    assert not app_status.should_exit

    async with _isolated_sse_server_lifetime() as second_watchers:
        second_server = make_background_server()
        async with second_server.run_in_background():
            with anyio.fail_after(15):
                async with (
                    sse_client(url=f"{second_server.url}/sse") as streams,
                    ClientSession(*streams) as session,
                ):
                    await session.initialize()
                    prompts = await session.list_prompts()
                    assert [prompt.name for prompt in prompts.prompts] == ["prompt1"]
                    result = await session.call_tool("echo", {"message": "second-server"})
                    assert result.content[0].text == "Echo: second-server"
    assert second_watchers
    assert all(task.done() for task in second_watchers)
    assert not app_status.should_exit


@pytest.mark.parametrize("path_suffix", ["/mcp/", "/mcp"])
async def test_http_transport(path_suffix: str) -> None:
    """Test HTTP transport layer functionality."""
    server = make_background_server(debug=True)
    async with server.run_in_background():
        http_url = f"{server.url}{path_suffix}"
        async with (
            streamablehttp_client(url=http_url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"

            for i in range(3):
                tool_result = await session.call_tool("echo", {"message": f"test_{i}"})
                assert len(tool_result.content) == 1
                assert isinstance(tool_result.content[0], types.TextContent)
                assert tool_result.content[0].text == f"Echo: test_{i}"


@pytest.mark.parametrize(("level", "debug"), [("INFO", False), ("DEBUG", True)])
async def test_run_mcp_server_keeps_transport_secrets_out_of_logs(  # noqa: PLR0915
    level: str,
    debug: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real default and named transports work without logging their credentials or payloads."""
    root = logging.getLogger()
    root_state = (root.level, root.handlers[:])
    logger_names = (
        "httpx",
        "httpcore",
        "mcp.client.sse",
        "mcp.client.streamable_http",
        "mcp.server.lowlevel.server",
        "mcp.server.streamable_http_manager",
        "mcp.server.sse",
        "mcp.server.streamable_http",
        "sse_starlette.sse",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "uvicorn.asgi",
    )
    logger_states = {
        name: (logger.level, logger.handlers[:], logger.propagate, logger.disabled)
        for name in logger_names
        if (logger := logging.getLogger(name))
    }
    session_ids: list[str] = []
    body_marker = "mcp-transport-body-secret"
    access_query_marker = "access-query-secret"

    async def exercise_transports(base_url: str) -> None:
        async with custom_httpx_client() as client:
            response = await client.get(f"{base_url}/healthz?{access_query_marker}")
        assert response.status_code == 204

        for prefix in ("", "/servers/named"):
            async with (
                custom_httpx_client() as http_client,
                streamable_http_client(
                    f"{base_url}{prefix}/mcp/",
                    http_client=http_client,
                ) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                session_id = streams[2]()
                assert session_id is not None
                session_ids.append(session_id)
                result = await session.call_tool("echo", {"message": body_marker})
                assert len(result.content) == 1
                assert isinstance(result.content[0], types.TextContent)
                assert result.content[0].text == body_marker

            async with (
                sse_client(
                    url=f"{base_url}{prefix}/sse",
                    httpx_client_factory=custom_httpx_client,
                    on_session_created=session_ids.append,
                ) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                result = await session.call_tool("echo", {"message": body_marker})
                assert len(result.content) == 1
                assert isinstance(result.content[0], types.TextContent)
                assert result.content[0].text == body_marker

    class CapturingBackgroundServer(BackgroundServer):
        async def serve(self) -> None:
            task = asyncio.create_task(super().serve())
            try:
                with anyio.fail_after(10):
                    while not self.started:  # noqa: ASYNC110
                        await anyio.sleep(1e-3)
                with anyio.fail_after(15):
                    await exercise_transports(self.url)
            finally:
                self.should_exit = self.force_exit = True
                await task

    try:
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        _setup_logging(level=level, debug=debug)
        monkeypatch.setattr("mcp_proxy.mcp_server.uvicorn.Server", CapturingBackgroundServer)
        params = StdioServerParameters(command=sys.executable, args=["-c", _LOGGING_STDIO_SERVER])
        async with _isolated_sse_server_lifetime():
            await run_mcp_server(
                MCPServerSettings(bind_host="127.0.0.1", port=0, log_level=level),
                params,
                {"named": params},
            )
        captured = capsys.readouterr()
        output = captured.out + captured.err
    finally:
        root.handlers[:] = root_state[1]
        root.setLevel(root_state[0])
        for name, state in logger_states.items():
            logger = logging.getLogger(name)
            logger.setLevel(state[0])
            logger.handlers[:] = state[1]
            logger.propagate = state[2]
            logger.disabled = state[3]

    assert len(session_ids) == 4
    leaked = [
        secret for secret in (*session_ids, body_marker, access_query_marker) if secret in output
    ]
    leaked_lines = [
        line for line in output.splitlines() if any(secret in line for secret in leaked)
    ]
    assert not leaked, f"Sensitive markers appeared in server logs: {leaked}\n{leaked_lines}"


async def test_stateless_http_transport() -> None:
    """Test stateless HTTP transport functionality."""
    server = make_background_server(debug=True, stateless=True)
    async with server.run_in_background():
        http_url = f"{server.url}/mcp/"
        async with (
            streamablehttp_client(url=http_url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"

            for i in range(3):
                tool_result = await session.call_tool("echo", {"message": f"test_{i}"})
                assert len(tool_result.content) == 1
                assert isinstance(tool_result.content[0], types.TextContent)
                assert tool_result.content[0].text == f"Echo: test_{i}"


# Unit tests for run_mcp_server method


@pytest.fixture
def mock_settings() -> MCPServerSettings:
    """Create mock MCP server settings for testing."""
    return MCPServerSettings(
        bind_host="127.0.0.1",
        port=8080,
        stateless=False,
        allow_origins=["*"],
        log_level="INFO",
    )


def test_mcp_server_settings_preserves_existing_positional_field_order() -> None:
    """The initialization timeout follows every pre-existing positional field."""
    stateless = False
    allowed_hosts = ["proxy.example.com"]
    settings = MCPServerSettings(
        "127.0.0.1",
        8080,
        stateless,
        None,
        ["X-Example"],
        "DEBUG",
        allowed_hosts,
        2.5,
    )

    assert settings.expose_headers == ["X-Example"]
    assert settings.log_level == "DEBUG"
    assert settings.allowed_hosts == allowed_hosts
    assert settings.initialization_timeout == 2.5


@pytest.fixture
def mock_stdio_params() -> StdioServerParameters:
    """Create mock stdio server parameters for testing."""
    return StdioServerParameters(
        command="echo",
        args=["hello"],
        env={"TEST_VAR": "test_value"},
        cwd="/tmp",  # noqa: S108
    )


class _OpenStdout:
    """Stdout of a stdio server that keeps running (run_mcp_server exits when it closes)."""

    def __aiter__(self) -> "_OpenStdout":
        return self

    async def __anext__(self) -> t.Any:  # noqa: ANN401
        await asyncio.Event().wait()


def setup_async_context_mocks() -> tuple[
    contextlib.AbstractContextManager[tuple[t.Any, AsyncMock]],
    contextlib.AbstractContextManager[tuple[AsyncMock, AsyncMock]],
    AsyncMock,
    MagicMock,
    list[MagicMock],
]:
    """Helper function to set up async context manager mocks."""
    # Setup stdio client mock
    mock_streams = (_OpenStdout(), AsyncMock())

    # Setup client session mock
    mock_session = AsyncMock()

    # Setup HTTP manager mock
    mock_http_manager = MagicMock()
    session_manager = create_autospec(StreamableHTTPSessionManager, spec_set=True)
    mock_http_manager.run.return_value = contextlib.nullcontext(session_manager)
    mock_routes = [MagicMock()]

    return (
        contextlib.nullcontext(mock_streams),
        contextlib.nullcontext(mock_session),
        mock_session,
        mock_http_manager,
        mock_routes,
    )


async def test_run_mcp_server_no_servers_configured(mock_settings: MCPServerSettings) -> None:
    """Test run_mcp_server when no servers are configured."""
    with patch("mcp_proxy.mcp_server.logger") as mock_logger:
        await run_mcp_server(mock_settings, None, {})
        mock_logger.error.assert_called_once_with("No servers configured to run.")
        mock_logger.info.assert_not_called()


@pytest.mark.parametrize(
    "configured_timeout",
    [0, -1, float("nan"), float("inf"), float("-inf")],
)
async def test_run_mcp_server_rejects_invalid_initialization_timeout_before_child_start(
    mock_stdio_params: StdioServerParameters,
    configured_timeout: float,
) -> None:
    """Invalid programmatic deadlines fail before crossing the stdio spawn boundary."""
    settings = MCPServerSettings(
        bind_host="127.0.0.1",
        port=0,
        initialization_timeout=configured_timeout,
    )

    with (
        patch(
            "mcp_proxy.mcp_server.stdio_client",
            side_effect=AssertionError("stdio child creation reached"),
        ) as mock_stdio_client,
        pytest.raises(ValueError, match="initialization_timeout"),
    ):
        await run_mcp_server(settings, mock_stdio_params)

    mock_stdio_client.assert_not_called()


async def test_named_spawn_timeout_is_fatal_and_does_not_start_later_server(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """A spawn timeout remains fatal rather than becoming a skippable OSError."""
    from mcp_proxy.mcp_server import _global_status  # noqa: PLC0415

    _global_status["server_instances"].clear()
    settings = MCPServerSettings(bind_host="127.0.0.1", port=0, initialization_timeout=0.5)

    with (
        patch(
            "mcp_proxy.mcp_server.stdio_client",
            side_effect=TimeoutError("issue198 spawn timeout"),
        ) as mock_stdio_client,
        pytest.raises(TimeoutError, match="issue198 spawn timeout"),
    ):
        await run_mcp_server(
            settings,
            named_server_params={"first": mock_stdio_params, "later": mock_stdio_params},
        )

    mock_stdio_client.assert_called_once_with(mock_stdio_params)
    assert _global_status["server_instances"] == {}


async def test_run_mcp_server_with_default_server(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with a default server configuration."""
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, {})

        # Verify calls
        mock_stdio_client.assert_called_once_with(mock_stdio_params)
        mock_create_proxy.assert_called_once_with(mock_session)
        mock_create_routes.assert_called_once_with(
            mock_proxy,
            stateless_instance=mock_settings.stateless,
        )
        mock_logger.info.assert_any_call(
            "Setting up default server: %s %s",
            mock_stdio_params.command,
            " ".join(mock_stdio_params.args),
        )
        mock_server_instance.serve.assert_called_once()


async def test_run_mcp_server_with_named_servers(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with named servers configuration."""
    from mcp_proxy.mcp_server import _global_status  # noqa: PLC0415

    _global_status["server_instances"].clear()
    named_servers = {
        "default": mock_stdio_params,
        "server2": StdioServerParameters(
            command="python",
            args=["-m", "mcp_server"],
            env={"PYTHON_PATH": "/usr/bin/python"},
            cwd="/home/user",
        ),
    }

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, None, named_servers)

        # Verify calls
        assert mock_stdio_client.call_count == 2
        assert mock_create_proxy.call_count == 2
        assert mock_create_routes.call_count == 2

        # Check that named servers were logged
        mock_logger.info.assert_any_call(
            "Setting up named server '%s': %s %s",
            "default",
            mock_stdio_params.command,
            " ".join(mock_stdio_params.args),
        )
        mock_logger.info.assert_any_call(
            "Setting up named server '%s': %s %s",
            "server2",
            "python",
            "-m mcp_server",
        )

        assert _global_status["server_instances"] == {
            "default": "configured",
            "server2": "configured",
        }
        mock_server_instance.serve.assert_called_once()


async def test_run_mcp_server_with_cors_middleware(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server adds CORS middleware when allow_origins is set."""
    settings_with_cors = MCPServerSettings(
        bind_host="0.0.0.0",  # noqa: S104
        port=9090,
        allow_origins=["http://localhost:3000", "https://example.com"],
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("mcp_proxy.mcp_server.Starlette") as mock_starlette,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(settings_with_cors, mock_stdio_params, {})

        # Verify Starlette was called with middleware
        mock_starlette.assert_called_once()
        call_args = mock_starlette.call_args
        middleware = call_args.kwargs["middleware"]

        assert len(middleware) == 1
        assert middleware[0].cls == CORSMiddleware
        assert middleware[0].kwargs["expose_headers"] == list(DEFAULT_EXPOSE_HEADERS)


async def test_run_mcp_server_custom_expose_headers(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server honors custom expose headers."""
    settings_with_custom_headers = MCPServerSettings(
        bind_host="0.0.0.0",  # noqa: S104
        port=9090,
        allow_origins=["http://localhost"],
        expose_headers=["Header-One", "Header-Two"],
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("mcp_proxy.mcp_server.Starlette") as mock_starlette,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        (
            mock_stdio_context,
            mock_session_context,
            _,
            mock_http_manager,
            mock_routes,
        ) = setup_async_context_mocks()
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        await run_mcp_server(settings_with_custom_headers, mock_stdio_params, {})

        middleware = mock_starlette.call_args.kwargs["middleware"]

        assert len(middleware) == 1
        assert middleware[0].kwargs["expose_headers"] == ["Header-One", "Header-Two"]


async def test_run_mcp_server_debug_mode(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with debug mode enabled."""
    debug_settings = MCPServerSettings(
        bind_host="127.0.0.1",
        port=8080,
        log_level="DEBUG",
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("mcp_proxy.mcp_server.Starlette") as mock_starlette,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(debug_settings, mock_stdio_params, {})

        # Verify Starlette was called with debug=True
        mock_starlette.assert_called_once()
        call_args = mock_starlette.call_args
        assert call_args.kwargs["debug"] is True


async def test_run_mcp_server_stateless_mode(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with stateless mode enabled."""
    stateless_settings = MCPServerSettings(
        bind_host="127.0.0.1",
        port=8080,
        stateless=True,
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(stateless_settings, mock_stdio_params, {})

        # Verify create_single_instance_routes was called with stateless_instance=True
        mock_create_routes.assert_called_once_with(
            mock_proxy,
            stateless_instance=True,
        )


async def test_run_mcp_server_uvicorn_config(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server creates correct uvicorn configuration."""
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Config") as mock_uvicorn_config,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_config = MagicMock()
        mock_uvicorn_config.return_value = mock_config

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, {})

        # Verify uvicorn.Config was called with correct parameters
        mock_uvicorn_config.assert_called_once()
        call_args = mock_uvicorn_config.call_args

        assert call_args.kwargs["host"] == mock_settings.bind_host
        assert call_args.kwargs["port"] == mock_settings.port
        assert call_args.kwargs["log_level"] == mock_settings.log_level.lower()
        assert call_args.kwargs["access_log"] is False
        # SIGTERM must not hang on in-flight connections: uvicorn's default
        # graceful-shutdown timeout is unbounded (None). See #281.
        assert call_args.kwargs["timeout_graceful_shutdown"] == 5


async def test_run_mcp_server_global_status_updates(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server updates global status correctly."""
    from mcp_proxy.mcp_server import _global_status  # noqa: PLC0415

    # Clear global status before test
    _global_status["server_instances"].clear()

    named_servers = {"test_server": mock_stdio_params}

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, named_servers)

        # Verify global status was updated
        assert "default" in _global_status["server_instances"]
        assert "test_server" in _global_status["server_instances"]
        assert _global_status["server_instances"]["default"] == "configured"
        assert _global_status["server_instances"]["test_server"] == "configured"


@pytest.mark.parametrize(
    "log_case",
    [
        (
            "::1",
            8080,
            ["http://[::1]:8080/sse", "http://[::1]:8080/servers/test_server/sse"],
            ["http://[::1]:8080/mcp", "http://[::1]:8080/servers/test_server/mcp"],
        ),
        (
            "127.0.0.1",
            0,
            ["/sse", "/servers/test_server/sse"],
            ["/mcp", "/servers/test_server/mcp"],
        ),
    ],
    ids=["ipv6-fixed-port", "port-zero"],
)
async def test_run_mcp_server_endpoint_url_logging(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
    caplog: pytest.LogCaptureFixture,
    log_case: tuple[str, int, list[str], list[str]],
) -> None:
    """Test run_mcp_server logs usable endpoint URLs for fixed and dynamic ports."""
    bind_host, port, expected_sse, expected_streamable = log_case
    mock_settings.bind_host = bind_host
    mock_settings.port = port
    named_servers = {"test_server": mock_stdio_params}
    caplog.set_level(logging.INFO, logger="mcp_proxy.mcp_server")

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, named_servers)

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "mcp_proxy.mcp_server"
        ]

        assert _advertised_urls(messages) == {
            "SSE:": expected_sse,
            "StreamableHTTP:": expected_streamable,
        }
        dynamic_port_note = (
            "The server is using an ephemeral port; combine these endpoint paths "
            "with the Uvicorn listening address."
        )
        if port == 0:
            assert messages.count(dynamic_port_note) == 1
            assert messages.index(dynamic_port_note) < messages.index(
                "Serving MCP Servers via SSE:"
            )
        else:
            assert dynamic_port_note not in messages


def _advertised_urls(messages: list[str]) -> dict[str, list[str]]:
    """Group the startup log's '  - <url>' lines under their 'Serving MCP Servers via' header."""
    advertised: dict[str, list[str]] = {}
    current: list[str] | None = None
    for message in messages:
        if message.startswith("Serving MCP Servers via "):
            current = advertised.setdefault(message.removeprefix("Serving MCP Servers via "), [])
        elif current is not None and message.startswith("  - "):
            current.append(message.removeprefix("  - "))
        else:
            current = None
    return advertised


@pytest.mark.parametrize(
    ("with_default", "named"),
    [(True, []), (False, ["alpha"]), (True, ["alpha", "beta"])],
    ids=["default", "named", "default-and-named"],
)
async def test_startup_log_advertises_reachable_sse_and_streamable_http_urls(
    *,
    with_default: bool,
    named: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every URL the startup log advertises, for both transports, serves a real MCP session."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    prefixes = ([""] if with_default else []) + [f"/servers/{name}" for name in named]
    connected: list[str] = []

    async def connect_to_advertised(advertised: dict[str, list[str]]) -> None:
        for url in advertised["StreamableHTTP:"]:
            async with (
                custom_httpx_client() as http_client,
                streamable_http_client(url, http_client=http_client) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                assert [tool.name for tool in (await session.list_tools()).tools] == ["echo"]
                connected.append(url)
        for url in advertised["SSE:"]:
            async with (
                sse_client(url=url, httpx_client_factory=custom_httpx_client) as streams,
                ClientSession(*streams) as session,
            ):
                await session.initialize()
                assert [tool.name for tool in (await session.list_tools()).tools] == ["echo"]
                connected.append(url)

    class ConnectingBackgroundServer(BackgroundServer):
        async def serve(self) -> None:
            task = asyncio.create_task(super().serve())
            try:
                with anyio.fail_after(10):
                    while not self.started:  # noqa: ASYNC110
                        await anyio.sleep(1e-3)
                messages = [
                    record.getMessage()
                    for record in caplog.records
                    if record.name == "mcp_proxy.mcp_server"
                ]
                advertised = _advertised_urls(messages)
                assert advertised == {
                    "SSE:": [f"{base_url}{prefix}/sse" for prefix in prefixes],
                    "StreamableHTTP:": [f"{base_url}{prefix}/mcp" for prefix in prefixes],
                }
                with anyio.fail_after(20):
                    await connect_to_advertised(advertised)
            finally:
                self.should_exit = self.force_exit = True
                await task

    caplog.set_level(logging.INFO, logger="mcp_proxy.mcp_server")
    monkeypatch.setattr("mcp_proxy.mcp_server.uvicorn.Server", ConnectingBackgroundServer)
    params = StdioServerParameters(command=sys.executable, args=["-c", _LOGGING_STDIO_SERVER])
    async with _isolated_sse_server_lifetime():
        await run_mcp_server(
            MCPServerSettings(bind_host="127.0.0.1", port=port),
            params if with_default else None,
            dict.fromkeys(named, params),
        )

    assert len(connected) == 2 * len(prefixes)


async def test_run_mcp_server_exception_handling(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server handles exceptions properly."""
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession"),
    ):
        # Setup mocks to raise an exception
        mock_stdio_client.side_effect = Exception("Connection failed")

        # Should not raise, function should handle exceptions gracefully
        try:
            await run_mcp_server(mock_settings, mock_stdio_params, {})
        except Exception as e:  # noqa: BLE001
            # If an exception is raised, it should be the expected one
            assert "Connection failed" in str(e)  # noqa: PT017


async def test_run_mcp_server_both_default_and_named_servers(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with both default and named servers."""
    named_servers = {"named_server": mock_stdio_params}

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function with both default and named servers
        await run_mcp_server(mock_settings, mock_stdio_params, named_servers)

        # Verify both servers were set up
        assert mock_stdio_client.call_count == 2  # One for default, one for named
        assert mock_create_proxy.call_count == 2
        assert mock_create_routes.call_count == 2

        # Verify logging for both servers
        mock_logger.info.assert_any_call(
            "Setting up default server: %s %s",
            mock_stdio_params.command,
            " ".join(mock_stdio_params.args),
        )
        mock_logger.info.assert_any_call(
            "Setting up named server '%s': %s %s",
            "named_server",
            mock_stdio_params.command,
            " ".join(mock_stdio_params.args),
        )

        mock_server_instance.serve.assert_called_once()


_EXITING_STDIO_SERVER = """
import json, os, sys, threading
for line in sys.stdin:
    req = json.loads(line)
    if req.get("method") == "initialize":
        result = {"protocolVersion": req["params"]["protocolVersion"], "capabilities": {},
                  "serverInfo": {"name": "exiting", "version": "1"}}
        print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}), flush=True)
    elif req.get("method") == "notifications/initialized":
        threading.Timer(1, os._exit, [0]).start()  # exit once the proxy has started
"""


@pytest.mark.skipif(
    sys.platform == "win32", reason="PID liveness checks are unavailable on Windows"
)
@pytest.mark.parametrize("failure", ["missing", "eof", "timeout"])
async def test_named_stdio_startup_failure_keeps_healthy_siblings_serving(  # noqa: PLR0915
    tmp_path: Path,
    failure: str,
) -> None:
    """A named spawn or initialization failure leaves healthy named servers usable."""
    good_pid = tmp_path / "good-pid"
    good_stopped = tmp_path / "good-stopped"
    good_initialized = tmp_path / "good-initialized"
    broken_pid = tmp_path / "broken-pid"
    broken_stopped = tmp_path / "broken-stopped"
    broken_initialized = tmp_path / "broken-initialize-received"
    later_pid = tmp_path / "later-pid"
    later_stopped = tmp_path / "later-stopped"
    later_initialized = tmp_path / "later-initialized"
    later_pid_check = tmp_path / "broken-reaped-before-later-spawn"

    def child_command(  # noqa: PLR0913
        name: str,
        pid_file: Path,
        stopped_file: Path,
        *,
        mode: str = "healthy",
        initialize_received: Path | None = None,
        checked_pid_file: Path | None = None,
        pid_check_result: Path | None = None,
    ) -> str:
        script = tmp_path / f"{name}.py"
        script.write_text(
            _issue198_stdio_server_script(
                name,
                pid_file,
                stopped_file,
                mode=mode,
                initialize_received=initialize_received,
                checked_pid_file=checked_pid_file,
                pid_check_result=pid_check_result,
            ),
        )
        return shlex.join([sys.executable, str(script)])

    healthy_command = child_command(
        "good",
        good_pid,
        good_stopped,
        initialize_received=good_initialized,
    )
    if failure == "missing":
        broken_command = shlex.join([str(tmp_path / "issue198-command-does-not-exist")])
    else:
        broken_command = child_command(
            "broken",
            broken_pid,
            broken_stopped,
            mode=failure,
            initialize_received=broken_initialized,
        )
    later_command = child_command(
        "later",
        later_pid,
        later_stopped,
        initialize_received=later_initialized,
        checked_pid_file=None if failure == "missing" else broken_pid,
        pid_check_result=None if failure == "missing" else later_pid_check,
    )
    process = await _start_named_stdio_proxy(
        [("good", healthy_command), ("broken", broken_command), ("later", later_command)],
        initialization_timeout=0.5,
    )
    startup_output = ""
    try:
        base_url, startup_output = await _wait_for_proxy_url(process)
        await _wait_for_proxy_ready(base_url, "/servers/good/healthz")
        await _wait_for_file(good_initialized)
        await _wait_for_file(later_initialized)
        if failure != "missing":
            await _wait_for_file(broken_stopped)
            await _wait_for_file(later_pid_check)
            assert later_pid_check.read_text() == "reaped"
            _assert_pid_reaped(broken_pid)

        async with custom_httpx_client(timeout=httpx.Timeout(8)) as client:
            status_response = await client.get(f"{base_url}/status")
            assert status_response.status_code == httpx.codes.OK
            assert status_response.json()["server_instances"] == {
                "good": "configured",
                "broken": "failed",
                "later": "configured",
            }
            for name in ("good", "later"):
                assert (
                    await client.get(f"{base_url}/servers/{name}/healthz")
                ).status_code == httpx.codes.NO_CONTENT
            assert (
                await client.get(f"{base_url}/servers/broken/healthz")
            ).status_code == httpx.codes.NOT_FOUND

        await _exercise_backend(base_url, "/servers/good", "issue198-good")
        await _exercise_backend(base_url, "/servers/later", "issue198-later")
        assert os.kill(int(good_pid.read_text()), 0) is None
        assert os.kill(int(later_pid.read_text()), 0) is None
        assert "/servers/broken/sse" not in startup_output
        assert "/servers/broken/mcp" not in startup_output
        failure_lines = [
            line for line in startup_output.splitlines() if "Skipping named server" in line
        ]
        assert len(failure_lines) == 1
        assert "issue198-command-does-not-exist" not in failure_lines[0]
        assert "FileNotFoundError" not in failure_lines[0]
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=12)
            except TimeoutError:
                process.kill()
                await process.wait()
        await process.communicate()
        for pid_file in (good_pid, broken_pid, later_pid):
            if pid_file.exists():
                await _wait_for_file(pid_file.with_name(pid_file.name.replace("pid", "stopped")))
                _assert_pid_reaped(pid_file)

    assert process.returncode is not None


@pytest.mark.skipif(
    sys.platform == "win32", reason="PID liveness checks are unavailable on Windows"
)
async def test_named_default_collision_preserves_root_backend_identity(tmp_path: Path) -> None:
    """A named default failure cannot mask the positional backend's active exit."""
    root_pid = tmp_path / "root-pid"
    root_stopped = tmp_path / "root-stopped"
    root_initialized = tmp_path / "root-initialized"
    named_pid = tmp_path / "named-pid"
    named_stopped = tmp_path / "named-stopped"
    named_initialized = tmp_path / "named-initialized"
    root_script = tmp_path / "root.py"
    named_script = tmp_path / "named.py"
    root_script.write_text(
        _issue198_stdio_server_script(
            "root",
            root_pid,
            root_stopped,
            initialize_received=root_initialized,
        ),
    )
    named_script.write_text(
        _issue198_stdio_server_script(
            "default",
            named_pid,
            named_stopped,
            mode="eof",
            initialize_received=named_initialized,
        ),
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcp_proxy",
        sys.executable,
        str(root_script),
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--initialization-timeout",
        "0.5",
        "--named-server",
        "default",
        shlex.join([sys.executable, str(named_script)]),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    root_exit_timed_out = False
    startup_output = ""
    collision_state: tuple[dict[str, str], int, int] = ({}, 0, 0)
    try:

        def assert_named_default_reaped() -> None:
            assert named_stopped.exists()
            _assert_pid_reaped(named_pid)

        base_url, startup_output = await _wait_for_proxy_url(
            process,
            before_listen=assert_named_default_reaped,
        )
        await _wait_for_proxy_ready(base_url)
        await _wait_for_file(root_initialized)
        await _wait_for_file(named_initialized)

        collision_state = await _exercise_positional_backend_with_named_default(base_url)
        assert os.kill(int(root_pid.read_text()), 0) is None
        os.kill(int(root_pid.read_text()), signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            root_exit_timed_out = True
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=12)
            except TimeoutError:
                process.kill()
                await process.wait()
        remaining_output, _ = await process.communicate()
        _assert_pid_reaped(root_pid)
        if named_pid.exists():
            _assert_pid_reaped(named_pid)

    output = startup_output + remaining_output.decode(errors="replace")
    assert (
        collision_state,
        "/servers/default/sse" not in startup_output,
        "/servers/default/mcp" not in startup_output,
        root_exit_timed_out,
        process.returncode,
        "Stdio server 'default' exited, shutting down" in output,
    ) == (
        (
            {"/": "configured", "/servers/default": "failed"},
            httpx.codes.NO_CONTENT,
            httpx.codes.NOT_FOUND,
        ),
        True,
        True,
        False,
        1,
        True,
    ), output


@pytest.mark.skipif(
    sys.platform == "win32", reason="PID liveness checks are unavailable on Windows"
)
async def test_named_stdio_all_startup_failures_exit_without_listening(tmp_path: Path) -> None:
    """All unavailable named servers fail startup instead of serving an empty proxy."""
    broken_pid = tmp_path / "broken-pid"
    broken_stopped = tmp_path / "broken-stopped"
    broken_initialized = tmp_path / "broken-initialize-received"
    broken_script = tmp_path / "broken.py"
    broken_script.write_text(
        _issue198_stdio_server_script(
            "broken",
            broken_pid,
            broken_stopped,
            mode="eof",
            initialize_received=broken_initialized,
        ),
    )
    process = await _start_named_stdio_proxy(
        [
            ("broken", shlex.join([sys.executable, str(broken_script)])),
            ("missing", shlex.join([str(tmp_path / "issue198-command-does-not-exist")])),
        ],
        initialization_timeout=0.5,
    )
    output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    output = output_bytes.decode(errors="replace")

    assert process.returncode == 1
    assert "No named servers could be started." in output
    assert "Skipping named server 'broken'" in output
    assert "Skipping named server 'missing'" in output
    assert "Uvicorn running on" not in output
    assert "Main application lifespan starting" not in output
    await _wait_for_file(broken_initialized)
    await _wait_for_file(broken_stopped)
    _assert_pid_reaped(broken_pid)


@pytest.mark.skipif(
    sys.platform == "win32", reason="PID liveness checks are unavailable on Windows"
)
async def test_stdio_exit_observed_during_route_setup_is_not_activated(tmp_path: Path) -> None:
    """A child EOF observed before activation is treated as a failed startup candidate."""
    pid_file = tmp_path / "candidate-pid"
    stopped_file = tmp_path / "candidate-stopped"
    initialized_file = tmp_path / "candidate-initialized"
    script = tmp_path / "candidate.py"
    script.write_text(
        _issue198_stdio_server_script(
            "candidate",
            pid_file,
            stopped_file,
            mode="exit-after-initialized",
            initialize_received=initialized_file,
        ),
    )

    class DelayedHTTPManager:
        @contextlib.asynccontextmanager
        async def run(self) -> t.AsyncIterator[None]:
            await _wait_for_file(stopped_file)
            await asyncio.sleep(0.05)
            yield

    with (
        patch(
            "mcp_proxy.mcp_server.create_single_instance_routes",
            return_value=([], DelayedHTTPManager()),
        ),
        patch("uvicorn.Server") as mock_uvicorn_server,
        pytest.raises(SystemExit) as exc_info,
    ):
        await run_mcp_server(
            MCPServerSettings(bind_host="127.0.0.1", port=0),
            named_server_params={
                "candidate": StdioServerParameters(command=sys.executable, args=[str(script)]),
            },
        )

    assert exc_info.value.code == 1
    mock_uvicorn_server.assert_not_called()
    await _wait_for_file(initialized_file)
    await _wait_for_file(stopped_file)
    _assert_pid_reaped(pid_file)


@pytest.mark.parametrize("failure", ["runtime", "group", "cleanup", "oserror-initialize"])
async def test_named_startup_does_not_skip_unsupported_or_cleanup_errors(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
    failure: str,
) -> None:
    """Unsupported errors and cleanup failures escape the named skip boundary."""
    if failure == "group" and sys.version_info < (3, 11):
        pytest.skip("exception groups require Python 3.11")

    mock_stdio_context, mock_session_context, _, _, _ = setup_async_context_mocks()

    @contextlib.asynccontextmanager
    async def broken_cleanup(
        _params: StdioServerParameters,
    ) -> t.AsyncIterator[tuple[t.Any, t.Any]]:
        yield _OpenStdout(), AsyncMock()
        raise RuntimeError("issue198 cleanup failure")

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        mock_stdio_client.return_value = (
            broken_cleanup(mock_stdio_params) if failure == "cleanup" else mock_stdio_context
        )
        mock_client_session.return_value = mock_session_context
        if failure == "runtime":
            mock_create_proxy.side_effect = RuntimeError(
                "issue198 unsupported initialization error"
            )
        elif failure == "group":
            exception_group = builtins.__dict__["BaseExceptionGroup"]
            mock_create_proxy.side_effect = exception_group(
                "issue198 cancellation group",
                [RuntimeError("issue198 group error"), asyncio.CancelledError()],
            )
        elif failure == "oserror-initialize":
            mock_create_proxy.side_effect = OSError("issue198 initialization OS error")
        else:
            mock_create_proxy.side_effect = McpError(
                types.ErrorData(code=-32000, message="init failure"),
            )

        with pytest.raises(BaseException, match="issue198") as exc_info:
            await run_mcp_server(
                mock_settings,
                named_server_params={"broken": mock_stdio_params, "later": mock_stdio_params},
            )

        mock_stdio_client.assert_called_once_with(mock_stdio_params)
        mock_create_proxy.assert_called_once()
        mock_create_routes.assert_not_called()
        mock_uvicorn_server.assert_not_called()
        assert type(exc_info.value).__name__ != "_StdioStartupFailureError"
        if failure == "cleanup":
            assert "issue198 cleanup failure" in str(exc_info.value)


@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM and PID checks are unavailable on Windows"
)
async def test_active_named_server_exit_during_later_startup_aborts_proxy(tmp_path: Path) -> None:
    """An already active backend dying during another initialization remains fatal."""
    good_pid = tmp_path / "good-pid"
    good_stopped = tmp_path / "good-stopped"
    good_initialized = tmp_path / "good-initialized"
    pending_pid = tmp_path / "pending-pid"
    pending_stopped = tmp_path / "pending-stopped"
    pending_initialized = tmp_path / "pending-initialize-received"

    def command(name: str, *, mode: str, pid: Path, stopped: Path, initialized: Path) -> str:
        script = tmp_path / f"{name}.py"
        script.write_text(
            _issue198_stdio_server_script(
                name,
                pid,
                stopped,
                mode=mode,
                initialize_received=initialized,
            ),
        )
        return shlex.join([sys.executable, str(script)])

    process = await _start_named_stdio_proxy(
        [
            (
                "good",
                command(
                    "good",
                    mode="healthy",
                    pid=good_pid,
                    stopped=good_stopped,
                    initialized=good_initialized,
                ),
            ),
            (
                "pending",
                command(
                    "pending",
                    mode="timeout",
                    pid=pending_pid,
                    stopped=pending_stopped,
                    initialized=pending_initialized,
                ),
            ),
        ],
        initialization_timeout=10,
    )
    try:
        await _wait_for_file(good_initialized)
        await _wait_for_file(pending_initialized)
        os.kill(int(good_pid.read_text()), signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=5)
        output_bytes, _ = await process.communicate()
        output = output_bytes.decode(errors="replace")
        assert process.returncode == 1
        assert "Stdio server 'good' exited, shutting down" in output
        assert "Skipping named server 'pending'" not in output
        assert "Uvicorn running on" not in output
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=12)
        await process.communicate()
        await _wait_for_file(pending_stopped)
        _assert_pid_reaped(good_pid)
        _assert_pid_reaped(pending_pid)


@pytest.mark.skipif(
    sys.platform == "win32", reason="PID liveness checks are unavailable on Windows"
)
async def test_external_cancellation_reaps_active_and_pending_named_servers(tmp_path: Path) -> None:
    """External cancellation propagates after pending and active child cleanup."""
    child_paths = {
        name: (
            tmp_path / f"{name}-pid",
            tmp_path / f"{name}-stopped",
            tmp_path / f"{name}-initialized",
        )
        for name in ("good", "pending")
    }
    params = {}
    for name, mode in (("good", "healthy"), ("pending", "timeout")):
        pid_file, stopped_file, initialized_file = child_paths[name]
        script = tmp_path / f"{name}.py"
        script.write_text(
            _issue198_stdio_server_script(
                name,
                pid_file,
                stopped_file,
                mode=mode,
                initialize_received=initialized_file,
            ),
        )
        params[name] = StdioServerParameters(command=sys.executable, args=[str(script)])

    task = asyncio.create_task(
        run_mcp_server(
            MCPServerSettings(bind_host="127.0.0.1", port=0, initialization_timeout=20),
            named_server_params=params,
        ),
    )
    try:
        await _wait_for_file(child_paths["good"][2])
        await _wait_for_file(child_paths["pending"][2])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=8)
        for pid_file, stopped_file, _ in child_paths.values():
            await _wait_for_file(stopped_file)
            _assert_pid_reaped(pid_file)


def test_stdio_exit_watch_ignores_closed_children_and_cancels_startup() -> None:
    """The watcher ignores shutdown noise and remains idempotent before HTTP attach."""
    stdio_exit = _StdioExitWatch()
    stdio_exit.closing.add("closing")
    stdio_exit.active.add("closing")
    stdio_exit._on_exit("closing")  # noqa: SLF001
    stdio_exit._on_exit("untracked")  # noqa: SLF001
    assert stdio_exit.exited == set()

    startup_cancel_scope = MagicMock()
    stdio_exit.startup_cancel_scope = startup_cancel_scope
    stdio_exit.active.add("active")
    stdio_exit._on_exit("active")  # noqa: SLF001
    stdio_exit._on_exit("active")  # noqa: SLF001
    startup_cancel_scope.cancel.assert_called()

    assert stdio_exit.exited == {"active"}


@pytest.mark.parametrize("named", [False, True])
async def test_run_mcp_server_exits_non_zero_when_stdio_server_exits(*, named: bool) -> None:
    """When the stdio server exits, the proxy must exit non-zero instead of serving errors."""
    settings = MCPServerSettings(bind_host="127.0.0.1", port=0, initialization_timeout=0.5)
    params = StdioServerParameters(command=sys.executable, args=["-c", _EXITING_STDIO_SERVER])

    with anyio.fail_after(15), pytest.raises(SystemExit) as exc_info:
        await run_mcp_server(
            settings, None if named else params, {"healthy": params} if named else {}
        )
    assert exc_info.value.code == 1


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is not available on Windows")
async def test_sigterm_bounds_shutdown_with_pending_streamable_call(tmp_path: Path) -> None:
    """An active StreamableHTTP tool call cannot keep the real CLI proxy alive."""
    entry_event = asyncio.Event()
    async with await asyncio.start_server(
        _backend_entry_handler(entry_event), "127.0.0.1", 0
    ) as entry_server:
        entry_port = entry_server.sockets[0].getsockname()[1]
        backend_script = tmp_path / "stdio_server.py"
        backend_script.write_text(_BLOCKING_STDIO_SERVER.replace("__ENTRY_PORT__", str(entry_port)))

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mcp_proxy",
            sys.executable,
            str(backend_script),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        call_task: asyncio.Task[t.Any] | None = None
        timed_out = False
        elapsed: float | None = None

        try:
            base_url = f"http://127.0.0.1:{port}"
            await _wait_for_proxy_ready(base_url)
            async with custom_httpx_client(timeout=httpx.Timeout(None)) as client:
                headers = await _initialize_streamable_http(client, f"{base_url}/mcp")
                call = types.CallToolRequest(
                    params=types.CallToolRequestParams(name="block", arguments={}),
                )
                call_task = asyncio.create_task(
                    client.post(
                        f"{base_url}/mcp",
                        json={
                            "jsonrpc": "2.0",
                            "id": 2,
                            **call.model_dump(by_alias=True, mode="json", exclude_none=True),
                        },
                        headers=headers,
                    ),
                )
                try:
                    await asyncio.wait_for(entry_event.wait(), timeout=10)
                    assert not call_task.done(), "tools/call completed before SIGTERM"
                    try:
                        elapsed = await _send_sigterm_and_wait(process)
                    except TimeoutError:
                        timed_out = True
                finally:
                    if process.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
                    if not call_task.done():
                        call_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await call_task
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            output, _ = await process.communicate()

        if timed_out:
            pytest.fail(
                "proxy remained alive beyond 12 seconds after SIGTERM\n"
                f"{output.decode(errors='replace')}"
            )
        assert elapsed is not None
        assert elapsed >= 4
        assert b"timeout graceful shutdown exceeded" in output.lower()


@pytest.mark.parametrize("named", [False, True])
async def test_stdio_initialization_timeout_reaps_child(  # noqa: PLR0915
    tmp_path: Path, *, named: bool
) -> None:
    """An unresponsive child times out and stack cleanup reaps all started children."""

    def child_script(
        pid_file: Path,
        received: Path,
        initialized: Path,
        stopped: Path,
        *,
        respond_to_initialize: bool,
    ) -> str:
        return f"""
import json, os, pathlib, sys
pid_file = pathlib.Path({str(pid_file)!r})
received = pathlib.Path({str(received)!r})
initialized = pathlib.Path({str(initialized)!r})
stopped = pathlib.Path({str(stopped)!r})
pid_file.write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        received.write_text(line)
        if {respond_to_initialize!r}:
            result = dict(
                protocolVersion=request["params"]["protocolVersion"],
                capabilities={{}},
                serverInfo=dict(name="test", version="1"),
            )
            print(json.dumps(dict(jsonrpc="2.0", id=request["id"], result=result)), flush=True)
    elif request.get("method") == "notifications/initialized":
        initialized.touch()
stopped.touch()
"""

    silent_pid = tmp_path / "silent-pid"
    silent_received = tmp_path / "silent-initialize-received"
    silent_initialized = tmp_path / "silent-initialized"
    silent_stopped = tmp_path / "silent-stopped"
    silent = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            child_script(
                silent_pid,
                silent_received,
                silent_initialized,
                silent_stopped,
                respond_to_initialize=False,
            ),
        ],
    )
    settings = MCPServerSettings(bind_host="127.0.0.1", port=0, initialization_timeout=0.5)
    default_pid: Path | None = None
    default_initialized: Path | None = None
    default_stopped: Path | None = None
    default_params: StdioServerParameters | None = None
    named_params = {"silent": silent} if named else {}
    if named:
        default_pid = tmp_path / "default-pid"
        default_received = tmp_path / "default-initialize-received"
        default_initialized = tmp_path / "default-initialized"
        default_stopped = tmp_path / "default-stopped"
        default_params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-c",
                child_script(
                    default_pid,
                    default_received,
                    default_initialized,
                    default_stopped,
                    respond_to_initialize=True,
                ),
            ],
        )

    pid_files = [silent_pid]
    if default_pid is not None:
        pid_files.append(default_pid)
    server_task = asyncio.create_task(
        run_mcp_server(settings, silent if not named else default_params, named_params),
    )
    try:
        done, _ = await asyncio.wait({server_task}, timeout=3)
        if named:
            assert server_task not in done, "proxy should keep serving the healthy default"
            await _wait_for_file(silent_received)
            await _wait_for_file(silent_stopped)
            _assert_pid_reaped(silent_pid)
            assert default_initialized is not None
            await _wait_for_file(default_initialized)
            assert default_pid is not None
            assert os.kill(int(default_pid.read_text()), 0) is None
        else:
            assert server_task in done, "startup did not hit the configured initialization deadline"
            with pytest.RaisesGroup(TimeoutError, flatten_subgroups=True):
                await server_task
    finally:
        if not server_task.done():
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(server_task), timeout=5)
            if not server_task.done():
                for pid_file in pid_files:
                    if pid_file.exists():
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(int(pid_file.read_text()), signal.SIGTERM)
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(server_task), timeout=5)
        assert silent_received.exists(), "silent child did not receive initialize"
        assert silent_stopped.exists(), "silent child did not exit after stack cleanup"
        _assert_pid_reaped(silent_pid)
        if named:
            assert default_initialized is not None
            assert default_initialized.exists()
            assert default_stopped is not None
            assert default_stopped.exists()
            assert default_pid is not None
            _assert_pid_reaped(default_pid)


async def test_omitted_initialization_timeout_allows_delayed_initialize(tmp_path: Path) -> None:
    """Omitting the budget permits initialization beyond the opted-in duration."""
    nominal_timeout = 0.5
    initialize_received = tmp_path / "initialize-received"
    initialized = tmp_path / "initialized"
    child_pid = tmp_path / "child-pid"
    child_stopped = tmp_path / "child-stopped"
    backend_script = tmp_path / "stdio_server.py"
    backend_script.write_text(
        _initialization_timeout_stdio_server_script(
            tmp_path,
            initialize_delay=nominal_timeout + 0.25,
            tool_call_delay=0,
        ),
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    process = await _start_stdio_proxy(backend_script, port)
    try:
        await _wait_for_proxy_ready(f"http://127.0.0.1:{port}")
    finally:
        await _stop_stdio_proxy(process)

    assert initialize_received.exists()
    assert initialized.exists()
    assert child_stopped.exists()
    _assert_pid_reaped(child_pid)


async def test_initialization_timeout_does_not_limit_later_tool_call(tmp_path: Path) -> None:
    """A real tool call may outlast the configured initialization budget."""
    initialization_timeout = 0.5
    initialize_received = tmp_path / "initialize-received"
    initialized = tmp_path / "initialized"
    tool_call_entered = tmp_path / "tool-call-entered"
    tool_call_finished = tmp_path / "tool-call-finished"
    child_pid = tmp_path / "child-pid"
    child_stopped = tmp_path / "child-stopped"
    backend_script = tmp_path / "stdio_server.py"
    backend_script.write_text(
        _initialization_timeout_stdio_server_script(
            tmp_path,
            initialize_delay=0,
            tool_call_delay=initialization_timeout + 0.25,
        ),
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    process = await _start_stdio_proxy(
        backend_script,
        port,
        initialization_timeout=initialization_timeout,
    )
    try:
        await _wait_for_proxy_ready(base_url)
        async with custom_httpx_client(timeout=httpx.Timeout(10)) as client:
            headers = await _initialize_streamable_http(client, f"{base_url}/mcp")
            call = types.CallToolRequest(
                params=types.CallToolRequestParams(name="delay", arguments={}),
            )
            response = await client.post(
                f"{base_url}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    **call.model_dump(by_alias=True, mode="json", exclude_none=True),
                },
                headers=headers,
            )
            response.raise_for_status()
            assert response.json()["result"]["content"][0]["text"] == "finished"
    finally:
        await _stop_stdio_proxy(process)

    assert initialize_received.exists()
    assert initialized.exists()
    assert tool_call_entered.exists()
    assert tool_call_finished.exists()
    assert child_stopped.exists()
    _assert_pid_reaped(child_pid)
