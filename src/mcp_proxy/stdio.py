"""Tie remote client lifetimes to the local stdio connection."""

import asyncio
import os
import sys
from collections import deque
from collections.abc import AsyncIterator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from threading import Thread
from typing import IO, cast

import anyio
from anyio.abc import ObjectReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

_MAX_FRAME_BYTES = 4 * 1024 * 1024
_MAX_QUEUED_FRAMES = 128


class _InputBudget:
    def __init__(self) -> None:
        self.bytes = 0
        self.frames = 0
        self.sizes: deque[int] = deque()

    def admit(self, size: int) -> None:
        if self.bytes + size > _MAX_FRAME_BYTES or self.frames >= _MAX_QUEUED_FRAMES:
            message = "stdin queued input exceeds 4MiB payload or 128 frames"
            raise ValueError(message)
        self.bytes += size
        self.frames += 1
        self.sizes.append(size)

    def release(self, size: int) -> None:
        self.bytes -= size
        self.frames -= 1
        if self.bytes < 0:
            raise AssertionError("input byte accounting became negative")
        if self.frames < 0:
            raise AssertionError("input frame accounting became negative")


class _BoundedLines:
    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.pending = bytearray()
        self.eof = False

    def readline(self) -> tuple[str, int]:
        with os.fdopen(os.dup(self.fd), "rb", buffering=0) as raw:
            scan_from = 0
            while True:
                endings = (
                    index
                    for index in (
                        self.pending.find(b"\r", scan_from),
                        self.pending.find(b"\n", scan_from),
                    )
                    if index >= 0
                )
                end = min(endings, default=-1)
                if end >= 0:
                    stop = end + 1
                    if self.pending[end] == ord("\r"):
                        if stop == len(self.pending) and not self.eof:
                            extra = raw.read(1)
                            self.eof = not extra
                            self.pending.extend(extra)
                        if self.pending[stop : stop + 1] == b"\n":
                            stop += 1
                    payload = bytes(self.pending[:end])
                    del self.pending[:stop]
                    return payload.decode("utf-8", errors="replace") + "\n", len(payload)
                if len(self.pending) > _MAX_FRAME_BYTES:
                    message = "stdin frame exceeds 4MiB UTF-8 wire payload"
                    raise ValueError(message)
                if self.eof:
                    payload = bytes(self.pending)
                    self.pending.clear()
                    return payload.decode("utf-8", errors="replace"), len(payload)
                scan_from = len(self.pending)
                chunk = raw.read(min(8192, _MAX_FRAME_BYTES + 1 - len(self.pending)))
                self.eof = not chunk
                self.pending.extend(chunk)


class _DaemonStdin(anyio.AsyncFile[str]):
    def __init__(self, lines: _BoundedLines, budget: _InputBudget) -> None:
        super().__init__(cast("IO[str]", lines))
        self.lines = lines
        self.budget = budget

    async def readline(self) -> str:
        result: Future[tuple[str, int]] = Future()
        result.set_running_or_notify_cancel()

        def read() -> None:
            try:
                result.set_result(self.lines.readline())
            except Exception as exc:  # noqa: BLE001
                result.set_exception(exc)

        Thread(target=read, daemon=True, name="mcp-proxy-stdin").start()
        line, size = await asyncio.wrap_future(result)
        if line:
            self.budget.admit(size)
        return line


class _ReleasingInput(ObjectReceiveStream[SessionMessage | Exception]):
    def __init__(
        self,
        stream: MemoryObjectReceiveStream[tuple[SessionMessage | Exception, int]],
        budget: _InputBudget,
    ) -> None:
        self.stream = stream
        self.budget = budget

    async def receive(self) -> SessionMessage | Exception:
        message, size = await self.stream.receive()
        self.budget.release(size)
        return message

    async def aclose(self) -> None:
        await self.stream.aclose()


@asynccontextmanager
async def stdio_client_lifespan() -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Cancel remote work on stdin EOF, including connection and initialization."""
    budget = _InputBudget()
    stdin_file = _DaemonStdin(_BoundedLines(sys.stdin.fileno()), budget)
    async with (
        anyio.create_task_group() as tasks,
        stdio_server(stdin=stdin_file) as (stdin, stdout),
    ):
        sender, receiver = anyio.create_memory_object_stream[
            tuple[SessionMessage | Exception, int]
        ](_MAX_QUEUED_FRAMES)

        async def forward_stdin() -> None:
            async with sender:
                async for message in stdin:
                    sender.send_nowait((message, budget.sizes.popleft()))
            tasks.cancel_scope.cancel()

        async with _ReleasingInput(receiver, budget) as read_stream:
            tasks.start_soon(forward_stdin)
            try:
                yield (
                    cast("MemoryObjectReceiveStream[SessionMessage | Exception]", read_stream),
                    stdout,
                )
            finally:
                tasks.cancel_scope.cancel()
