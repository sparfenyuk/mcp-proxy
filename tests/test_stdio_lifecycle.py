"""Subprocess regressions for the lifetime of stdio-to-HTTP bridges."""

# These tests intentionally exercise the shared helper's internal resource contracts.
# ruff: noqa: SLF001

import asyncio
import io
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import anyio
import pytest
import uvicorn
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server as sdk_stdio_server
from mcp.shared.message import SessionMessage
from sse_starlette.sse import AppStatus

from mcp_proxy import stdio as stdio_module
from tests.test_mcp_server import BackgroundServer, create_starlette_app

_MAX_STDIN_FRAME_BYTES = 4 * 1024 * 1024
_EXPECTED_HELPER_READS = 3
_STDIO_SOURCE = str(Path(stdio_module.__file__).resolve())


def _wire_frame(size: int) -> bytes:
    prefix = b'{"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info","data":"'
    suffix = b'"}}'
    assert size >= len(prefix) + len(suffix)
    return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix


@pytest.mark.parametrize(
    ("payload", "sizes"),
    [
        (b"one\rtwo\r\nthree\nfour", [3, 3, 5, 4]),
        ("café\r🙂\n".encode(), [5, 4]),
        (b"\xff\n\n{unfinished", [1, 0, len(b"{unfinished")]),
        (b"", []),
        (b"\r\n", [0]),
    ],
)
def test_bounded_reader_matches_sdk_text_framing(
    payload: bytes,
    sizes: list[int],
    tmp_path: Path,
) -> None:
    """Preserve SDK newline normalization and replacement decoding."""
    source_path = tmp_path / "stdin.bin"
    source_path.write_bytes(payload)
    expected = list(io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8", errors="replace"))

    with source_path.open("rb") as source:
        reader = stdio_module._BoundedLines(source.fileno())
        lines: list[str] = []
        actual_sizes: list[int] = []
        while True:
            line, size = reader.readline()
            if not line:
                break
            lines.append(line)
            actual_sizes.append(size)

    assert lines == expected
    assert actual_sizes == sizes


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r", b"\r\n"])
def test_bounded_reader_accepts_exact_frame_limit_and_closes_duplicate(
    ending: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Accept a 4 MiB frame with each supported line ending."""
    source_path = tmp_path / "stdin.bin"
    source_path.write_bytes(b"x" * _MAX_STDIN_FRAME_BYTES + ending)
    duplicates: list[int] = []
    original_dup = os.dup

    def duplicate(fd: int) -> int:
        copied_fd = original_dup(fd)
        duplicates.append(copied_fd)
        return copied_fd

    monkeypatch.setattr(os, "dup", duplicate)
    with source_path.open("rb") as source:
        reader = stdio_module._BoundedLines(source.fileno())
        line, size = reader.readline()
        assert line == "x" * _MAX_STDIN_FRAME_BYTES + ("\n" if ending else "")
        assert size == _MAX_STDIN_FRAME_BYTES
        assert len(duplicates) == 1
        with pytest.raises(OSError, match=r".*"):
            os.fstat(duplicates[0])


def test_bounded_reader_rejects_oversize_before_decode_and_closes_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject oversized raw input before decoding and close its owned descriptor."""
    source_path = tmp_path / "stdin.bin"
    source_path.write_bytes(b"\xf0" * (_MAX_STDIN_FRAME_BYTES * 2))
    duplicates: list[int] = []
    original_dup = os.dup

    def duplicate(fd: int) -> int:
        copied_fd = original_dup(fd)
        duplicates.append(copied_fd)
        return copied_fd

    monkeypatch.setattr(os, "dup", duplicate)
    with source_path.open("rb") as source:
        reader = stdio_module._BoundedLines(source.fileno())
        with pytest.raises(ValueError, match="frame exceeds"):
            reader.readline()
        assert len(reader.pending) == _MAX_STDIN_FRAME_BYTES + 1
        assert len(duplicates) == 1
        with pytest.raises(OSError, match=r".*"):
            os.fstat(duplicates[0])


def test_bounded_reader_rejects_oversize_payload_before_delimiter(tmp_path: Path) -> None:
    """Reject a frame that exceeds the limit even when its delimiter follows."""
    source_path = tmp_path / "stdin.bin"
    source_path.write_bytes(b"x" * (_MAX_STDIN_FRAME_BYTES + 1) + b"\n")
    with source_path.open("rb") as source:
        reader = stdio_module._BoundedLines(source.fileno())
        with pytest.raises(ValueError, match="frame exceeds"):
            reader.readline()


def test_input_budget_counts_empty_frames_and_releases_for_later_traffic() -> None:
    """Count empty frames and release admission for later input."""
    budget = stdio_module._InputBudget()
    for _ in range(4):
        budget.admit(_MAX_STDIN_FRAME_BYTES)
        assert (budget.frames, budget.bytes) == (1, _MAX_STDIN_FRAME_BYTES)
        assert budget.sizes.popleft() == _MAX_STDIN_FRAME_BYTES
        budget.release(_MAX_STDIN_FRAME_BYTES)
        assert (budget.frames, budget.bytes) == (0, 0)

    for _ in range(128):
        budget.admit(0)
    with pytest.raises(ValueError, match="128 frames"):
        budget.admit(0)
    for _ in range(128):
        assert budget.sizes.popleft() == 0
        budget.release(0)

    budget.admit(_MAX_STDIN_FRAME_BYTES // 2 + 1)
    with pytest.raises(ValueError, match="4MiB payload"):
        budget.admit(_MAX_STDIN_FRAME_BYTES // 2 + 1)
    assert budget.sizes.popleft() == _MAX_STDIN_FRAME_BYTES // 2 + 1
    budget.release(_MAX_STDIN_FRAME_BYTES // 2 + 1)
    assert (budget.frames, budget.bytes) == (0, 0)


@pytest.mark.parametrize(
    ("payload", "prefill", "message"),
    [
        (b"x" * (_MAX_STDIN_FRAME_BYTES + 1), 0, "frame exceeds"),
        (b"x\n", _MAX_STDIN_FRAME_BYTES, "queued input exceeds"),
    ],
)
async def test_daemon_stdin_propagates_reader_and_admission_errors(
    payload: bytes,
    prefill: int,
    message: str,
    tmp_path: Path,
) -> None:
    """Propagate raw scanner and admission failures from the reader thread."""
    source_path = tmp_path / "stdin.bin"
    source_path.write_bytes(payload)
    budget = stdio_module._InputBudget()
    if prefill:
        budget.admit(prefill)
    with source_path.open("rb") as source:
        reader = stdio_module._DaemonStdin(stdio_module._BoundedLines(source.fileno()), budget)
        with pytest.raises(ValueError, match=message):
            await reader.readline()


async def test_stdio_helper_keeps_sdk_parser_and_releases_consumed_utf8_frames(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep SDK parsing behavior and release frame accounting as input is consumed."""
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "rb", buffering=0)
    stdout = (tmp_path / "stdout.txt").open("w", encoding="utf-8")
    duplicates: list[int] = []
    original_dup = os.dup
    write_open = True

    def duplicate(fd: int) -> int:
        copied_fd = original_dup(fd)
        if fd == read_fd:
            duplicates.append(copied_fd)
        return copied_fd

    @asynccontextmanager
    async def stdio_server_for_test(
        stdin: anyio.AsyncFile[str] | None = None,
    ) -> AsyncIterator[
        tuple[
            MemoryObjectReceiveStream[SessionMessage | Exception],
            MemoryObjectSendStream[SessionMessage],
        ]
    ]:
        async with sdk_stdio_server(
            stdin=stdin,
            stdout=anyio.wrap_file(stdout),
        ) as streams:
            yield streams

    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(os, "dup", duplicate)
    monkeypatch.setattr(stdio_module, "stdio_server", stdio_server_for_test)
    message = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
        '{"protocolVersion":"2024-11-05","capabilities":{},'
        '"clientInfo":{"name":"café 🙂","version":"1"}}}'
    ).encode()
    split = message.index(b"\xc3") + 1
    try:
        async with stdio_module.stdio_client_lifespan() as (read_stream, _write_stream):
            releasing_stream = cast("stdio_module._ReleasingInput", read_stream)
            os.write(write_fd, b"not-json\n" + message[:split])
            await asyncio.sleep(0.02)
            os.write(write_fd, message[split:] + b"\r\n")
            parse_error = await asyncio.wait_for(read_stream.receive(), timeout=3)
            parsed_message = await asyncio.wait_for(read_stream.receive(), timeout=3)
            assert isinstance(parse_error, Exception)
            assert isinstance(parsed_message, SessionMessage)
            parsed = json.loads(parsed_message.message.model_dump_json())
            assert parsed["params"]["clientInfo"]["name"] == "café 🙂"
            assert (releasing_stream.budget.frames, releasing_stream.budget.bytes) == (0, 0)
            os.close(write_fd)
            write_open = False
            cancelled_by_eof = False
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=2)
            except asyncio.CancelledError:
                cancelled_by_eof = True
            except TimeoutError:
                pass
            assert cancelled_by_eof
        assert len(duplicates) >= _EXPECTED_HELPER_READS
        for duplicate_fd in duplicates:
            with pytest.raises(OSError, match=r".*"):
                os.fstat(duplicate_fd)
    finally:
        if write_open:
            os.close(write_fd)
        stdin.close()
        stdout.close()


@pytest.mark.parametrize("release_reader", [False, True])
async def test_cancelled_stdin_read_is_daemon_owned_and_closes_its_duplicate(
    release_reader: bool,
) -> None:
    """Let a blocked read outlive cancellation without holding process exit."""
    child = """
import asyncio
import json
import os
import sys
import threading
from mcp_proxy import stdio

started = threading.Event()
closed = threading.Event()
owned = []
original_fdopen = os.fdopen

def fingerprint(fd):
    stat = os.fstat(fd)
    return stat.st_dev, stat.st_ino, stat.st_mode

def fdopen(fd, *args, **kwargs):
    file = original_fdopen(fd, *args, **kwargs)
    identity = fingerprint(fd)
    owned.append((fd, identity))
    class OwnedFile:
        def __enter__(self):
            return file
        def __exit__(self, exc_type, exc, traceback):
            try:
                return file.__exit__(exc_type, exc, traceback)
            finally:
                closed.set()
    return OwnedFile()

stdio.os.fdopen = fdopen
lines = stdio._BoundedLines(0)
readline = lines.readline
default_mode = sys.argv[1]

def start_read():
    started.set()
    return readline()

lines.readline = start_read
print(json.dumps({"kind": "source", "path": stdio.__file__}), file=sys.stderr, flush=True)

def same_fd():
    fd, identity = owned[0]
    try:
        return fingerprint(fd) == identity
    except OSError:
        return False

async def main():
    reader = stdio._DaemonStdin(lines, stdio._InputBudget())
    pending = asyncio.create_task(reader.readline())
    while not started.is_set():
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.03)
    pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        pass
    thread = next(item for item in threading.enumerate() if item.name == "mcp-proxy-stdin")
    threads = [(item.name, item.daemon) for item in threading.enumerate()]
    print(json.dumps({
        "kind": "cancelled",
        "thread_name": thread.name,
        "thread_daemon": thread.daemon,
        "executor_worker": any(name.startswith("asyncio_") for name, _ in threads),
        "duplicate_open": same_fd(),
    }), file=sys.stderr, flush=True)
    if default_mode == "release":
        while not closed.is_set():
            await asyncio.sleep(0.01)
        print(
            json.dumps({"kind": "reader_returned", "duplicate_open": same_fd()}),
            file=sys.stderr,
            flush=True,
        )
    raise RuntimeError("synthetic remote failure")

asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        "release" if release_reader else "exit",
        env=os.environ.copy(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stderr is not None
    retained_writer = os.dup(process.stdin.transport.get_extra_info("pipe").fileno())
    try:
        source = json.loads(await asyncio.wait_for(process.stderr.readline(), timeout=3))
        assert source == {"kind": "source", "path": _STDIO_SOURCE}
        cancelled = json.loads(await asyncio.wait_for(process.stderr.readline(), timeout=3))
        assert cancelled == {
            "kind": "cancelled",
            "thread_name": "mcp-proxy-stdin",
            "thread_daemon": True,
            "executor_worker": False,
            "duplicate_open": True,
        }
        if release_reader:
            process.stdin.close()
            os.close(retained_writer)
            retained_writer = -1
            returned = json.loads(await asyncio.wait_for(process.stderr.readline(), timeout=3))
            assert returned == {"kind": "reader_returned", "duplicate_open": False}
            await asyncio.wait_for(process.wait(), timeout=3)
        else:
            await asyncio.wait_for(process.wait(), timeout=3)
            os.fstat(retained_writer)
        assert process.returncode is not None
        assert process.returncode != 0
        stdout, stderr = await process.communicate()
        assert stdout == b""
        assert b"synthetic remote failure" in stderr
    finally:
        if retained_writer >= 0:
            os.close(retained_writer)
        process.stdin.close()
        if process.returncode is None:
            process.kill()
        await process.communicate()


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


@pytest.mark.parametrize(("transport", "path"), [("sse", "/sse"), ("streamablehttp", "/mcp")])
async def test_stdio_forwards_tools_list_and_utf8_tool_call(
    transport: str,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward tool discovery and an UTF-8 tool call over both transports."""
    monkeypatch.setattr(AppStatus, "should_exit", False)
    monkeypatch.setattr(AppStatus, "enable_automatic_graceful_drain", False)
    remote: Server[object] = Server("lifecycle-echo")
    tool = types.Tool(
        name="echo",
        description="Return the supplied message",
        inputSchema={"type": "object", "properties": {"message": {"type": "string"}}},
    )

    @remote.list_tools()  # type: ignore[no-untyped-call,misc]
    async def list_tools() -> list[types.Tool]:
        return [tool]

    @remote.call_tool()  # type: ignore[misc]
    async def call_tool(_name: str, arguments: dict[str, object] | None) -> list[types.Content]:
        value = arguments.get("message", "") if arguments else ""
        return [types.TextContent(type="text", text=str(value))]

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

            async def exchange(message: dict[str, object]) -> dict[str, object]:
                wire = json.dumps(message, ensure_ascii=False).encode() + b"\n"
                process.stdin.write(wire)
                await process.stdin.drain()
                response = await asyncio.wait_for(process.stdout.readline(), timeout=10)
                return json.loads(response)

            initialized = await exchange(
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
            )
            assert initialized["result"]["serverInfo"]["name"] == "lifecycle-echo"
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            await process.stdin.drain()

            listed = await exchange({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert listed["result"]["tools"][0]["name"] == "echo"
            called = await exchange(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"message": "café 🙂"}},
                },
            )
            assert called["result"]["content"][0]["text"] == "café 🙂"
            process.stdin.close()
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            assert process.returncode == 0, stderr.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()


@pytest.mark.parametrize("transport", ["sse", "streamablehttp"])
async def test_stdin_eof_after_http_acceptance_during_mcp_initialize(  # noqa: PLR0915
    transport: str,
) -> None:
    """EOF cancels an initialization whose HTTP response has begun but has no MCP result."""
    accepted = asyncio.Event()
    disconnected = asyncio.Event()
    handlers: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            headers = request.lower().split(b"\r\n")
            content_length = next(
                (
                    int(header.split(b":", 1)[1])
                    for header in headers
                    if header.startswith(b"content-length:")
                ),
                0,
            )
            if content_length:
                await reader.readexactly(content_length)

            if transport == "sse" and request.startswith(b"GET "):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
                    b"event: endpoint\ndata: /messages\n\n",
                )
            elif transport == "sse":
                writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")
                accepted.set()
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Mcp-Session-Id: lifecycle-test\r\n\r\n",
                )
                accepted.set()
            await writer.drain()
            await reader.read()
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

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
                b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
                b'{"protocolVersion":"2024-11-05","capabilities":{},'
                b'"clientInfo":{"name":"test","version":"1"}}}\n',
            )
            await process.stdin.drain()
            await asyncio.wait_for(accepted.wait(), timeout=10)
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


@pytest.mark.parametrize("transport", ["sse", "streamablehttp"])
@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("count", b"queued input exceeds"),
        ("bytes", b"queued input exceeds"),
        ("frame", b"frame exceeds"),
    ],
)
async def test_stdin_frame_overflow_fails_while_upstream_initialize_is_stalled(
    transport: str,
    case: str,
    expected_error: bytes,
) -> None:
    """The bounded input contract rejects the 129th queued frame before stdin EOF."""
    stalled = asyncio.Event()
    disconnected = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            stalled.set()
            await reader.read()
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
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
        assert process.stdin is not None
        retained_writer = os.dup(process.stdin.transport.get_extra_info("pipe").fileno())
        try:
            await asyncio.wait_for(stalled.wait(), timeout=10)
            if case == "count":
                payload = (_wire_frame(100) + b"\n") * 129
            elif case == "bytes":
                payload = (_wire_frame(_MAX_STDIN_FRAME_BYTES // 2 + 1) + b"\n") * 2
            else:
                payload = b"x" * (_MAX_STDIN_FRAME_BYTES + 1)
            process.stdin.write(payload)
            await process.stdin.drain()
            await asyncio.wait_for(process.wait(), timeout=5)
            assert process.returncode != 0
            stdout, stderr = await process.communicate()
            os.fstat(retained_writer)
            assert stdout == b""
            assert expected_error in stderr
            await asyncio.wait_for(disconnected.wait(), timeout=5)
        finally:
            os.close(retained_writer)
            if process.returncode is None:
                process.kill()
            process.stdin.close()
            await process.communicate()


@pytest.mark.parametrize(("transport", "path"), [("sse", "/sse"), ("streamablehttp", "/mcp")])
@pytest.mark.parametrize("pending_request", [False, True])
async def test_stdin_eof_after_initialize(
    transport: str,
    path: str,
    pending_request: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward real messages, then exit on EOF even with a request in flight."""
    # Reset SSE's global shutdown flag for each test server.
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
