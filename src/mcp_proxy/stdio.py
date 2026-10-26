"""Tie remote client lifetimes to the local stdio connection."""

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from concurrent.futures import Future
from contextlib import asynccontextmanager
from io import TextIOWrapper
from math import inf
from threading import Thread

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage


class _CancellableStdin(anyio.AsyncFile[str]):
    async def readline(self) -> str:
        # 使用 daemon 執行緒避免仍開啟的 stdin 阻止遠端錯誤後退出。
        result: Future[str] = Future()
        result.set_running_or_notify_cancel()

        def read() -> None:
            try:
                result.set_result(self.wrapped.readline())
            except Exception as exc:  # noqa: BLE001
                result.set_exception(exc)

        Thread(target=read, daemon=True).start()
        return await asyncio.wrap_future(result)


@asynccontextmanager
async def stdio_client_lifespan() -> AsyncIterator[
    tuple[
        MemoryObjectReceiveStream[SessionMessage | Exception],
        MemoryObjectSendStream[SessionMessage],
    ]
]:
    """Cancel remote work on stdin EOF, including connection and initialization."""
    stdin_file = _CancellableStdin(
        TextIOWrapper(
            os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0),
            encoding="utf-8",
            errors="replace",
        ),
    )
    async with (
        anyio.create_task_group() as tasks,
        stdio_server(stdin=stdin_file) as (stdin, stdout),
    ):
        # 在遠端初始化期間仍持續讀取 stdin 以免訊息背壓遮蔽 EOF。
        sender, receiver = anyio.create_memory_object_stream[SessionMessage | Exception](inf)

        async def forward_stdin() -> None:
            async with sender:
                async for message in stdin:
                    await sender.send(message)
            tasks.cancel_scope.cancel()

        async with receiver:
            tasks.start_soon(forward_stdin)
            yield receiver, stdout
            tasks.cancel_scope.cancel()
