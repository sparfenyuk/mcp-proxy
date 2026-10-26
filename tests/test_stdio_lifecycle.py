"""Subprocess regressions for the lifetime of stdio-to-HTTP bridges."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import uvicorn
from mcp import types
from mcp.server import Server
from sse_starlette.sse import AppStatus

from tests.test_mcp_server import BackgroundServer, create_starlette_app


@pytest.mark.parametrize("transport", ["sse", "streamablehttp"])
async def test_remote_error_with_stdin_open(transport: str) -> None:
    """Remote errors must remain visible without waiting for the client to close stdin."""

    async def reject(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        headers = await reader.readuntil(b"\r\n\r\n")
        for header in headers.split(b"\r\n"):
            if header.lower().startswith(b"content-length:"):
                await reader.readexactly(int(header.split(b":", 1)[1]))
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(reject, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mcp_proxy",
            "--transport",
            transport,
            f"http://127.0.0.1:{port}/mcp",
            env={**os.environ, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stderr is not None
            stderr = await asyncio.wait_for(process.stderr.read(), timeout=10)
            await asyncio.wait_for(process.wait(), timeout=5)
            assert process.returncode != 0
            assert b"403 Forbidden" in stderr
        finally:
            assert process.stdin is not None
            process.stdin.close()
            if process.returncode is None:
                process.kill()
            await process.communicate()


@pytest.mark.parametrize("transport", ["sse", "streamablehttp"])
@pytest.mark.parametrize("stage", ["connect", "initialize"])
async def test_stdin_eof_during_startup(transport: str, stage: str) -> None:
    """Closing stdin must stop a proxy even when the remote server stalls."""
    stalled = asyncio.Event()
    disconnected = asyncio.Event()
    handlers: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            if stage == "initialize" and request.startswith(b"GET "):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\nevent: endpoint\ndata: /messages\n\n",
                )
                await writer.drain()
            else:
                stalled.set()
            await reader.read()
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.remove(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            str(Path(sys.executable).with_name("mcp-proxy.exe"))
            if sys.platform == "win32"
            else str(Path(sys.executable).with_name("mcp-proxy")),
            "--transport",
            transport,
            f"http://127.0.0.1:{port}/mcp",
            env={**os.environ, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdin is not None
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                ).encode()
                + b"\n",
            )
            await process.stdin.drain()
            await asyncio.wait_for(stalled.wait(), timeout=10)
            assert process.returncode is None
            process.stdin.close()
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            assert process.returncode == 0, stderr.decode()
            assert stdout == b""
            await asyncio.wait_for(disconnected.wait(), timeout=5)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.parametrize(("transport", "path"), [("sse", "/sse"), ("streamablehttp", "/mcp")])
@pytest.mark.parametrize("pending_request", [False, True])
async def test_stdin_eof_after_initialize(
    transport: str,
    path: str,
    pending_request: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward real messages, then exit on EOF even with a request in flight."""
    # 各測試啟動獨立伺服器時不沿用上一次的 SSE 全域關機狀態。
    monkeypatch.setattr(AppStatus, "should_exit", False)
    monkeypatch.setattr(AppStatus, "enable_automatic_graceful_drain", False)
    remote: Server[object] = Server("lifecycle-test")
    requested = asyncio.Event()

    @remote.list_tools()  # type: ignore[no-untyped-call,misc]
    async def list_tools() -> list[types.Tool]:
        requested.set()
        await asyncio.Event().wait()
        return []

    server = BackgroundServer(
        uvicorn.Config(create_starlette_app(remote), port=0, log_level="error"),
    )
    async with server.run_in_background():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "mcp_proxy",
            "--transport",
            transport,
            server.url + path,
            env={**os.environ, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(
                b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
                b'{"protocolVersion":"2024-11-05","capabilities":{},'
                b'"clientInfo":{"name":"test","version":"1"}}}\n',
            )
            await process.stdin.drain()
            response = json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=10))
            assert response["id"] == 1
            assert response["result"]["serverInfo"]["name"] == "lifecycle-test"
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            if pending_request:
                process.stdin.write(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
                await process.stdin.drain()
                await asyncio.wait_for(requested.wait(), timeout=10)
            process.stdin.close()
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            assert process.returncode == 0, stderr.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
