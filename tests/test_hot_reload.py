"""Hot reload watcher and reload tests."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.shared.session import RequestResponder

from mcp_proxy import mcp_server
from mcp_proxy.httpx_client import custom_httpx_client
from mcp_proxy.mcp_server import _hot_reload_watch, _reload, _snapshot

from .test_mcp_server import _wait_for_proxy_url


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.ping_error: Exception | None = None

    def mark_stale(self) -> None:
        self.calls.append("stale")

    async def send_ping(self) -> None:
        self.calls.append("ping")
        if self.ping_error is not None:
            raise self.ping_error

    async def initialize(self) -> types.InitializeResult:
        self.calls.append("initialize")
        return types.InitializeResult(
            protocolVersion="2025-03-26",
            capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
            serverInfo=types.Implementation(name="fake", version="1"),
        )


class _FakeSubscribers:
    def __init__(self) -> None:
        self.notified: list[types.ServerCapabilities] = []
        self.error: Exception | None = None

    async def notify(self, capabilities: types.ServerCapabilities) -> None:
        self.notified.append(capabilities)
        if self.error is not None:
            raise self.error


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_HOT_RELOAD_POLL_SECONDS", 0.02)
    monkeypatch.setattr(mcp_server, "_HOT_RELOAD_DEBOUNCE_SECONDS", 0.01)


async def _wait_until(predicate: Any) -> None:  # noqa: ANN401
    with anyio.fail_after(3.0):
        while not predicate():  # noqa: ASYNC110
            await anyio.sleep(0.01)


def test_snapshot_changes_and_missing(tmp_path: Path) -> None:
    """A change or missing path shows in the snapshot."""
    f = tmp_path / "a.py"
    f.write_text("1")
    first = _snapshot([f, tmp_path / "gone"])
    assert first[1] is None
    f.write_text("22")
    assert _snapshot([f, tmp_path / "gone"]) != first


async def test_reload_propagates_ping_failure_without_notify() -> None:
    """A failed ping raises and sends no notification."""
    session, subs = _FakeSession(), _FakeSubscribers()
    session.ping_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await _reload(session, subs)  # type: ignore[arg-type]
    assert subs.notified == []


async def test_watch_reloads_on_change_not_otherwise(tmp_path: Path) -> None:
    """The watcher reloads on change and not while idle."""
    f = tmp_path / "server.py"
    f.write_text("a")
    session, subs = _FakeSession(), _FakeSubscribers()
    async with _hot_reload_watch("srv", session, [f], subs):  # type: ignore[arg-type]
        await anyio.sleep(0.2)
        assert session.calls == []
        f.write_text("bb")
        await _wait_until(lambda: len(subs.notified) == 1)
    assert session.calls == ["stale", "ping", "initialize"]
    assert subs.notified[0].tools is not None


async def test_watch_survives_notify_failure_and_reloads_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A notify error is logged with the server name and the next change reloads."""
    f = tmp_path / "server.py"
    f.write_text("a")
    session, subs = _FakeSession(), _FakeSubscribers()
    subs.error = RuntimeError("notify broke")
    caplog.set_level(logging.WARNING)
    async with _hot_reload_watch("my-server", session, [f], subs):  # type: ignore[arg-type]
        await anyio.sleep(0.1)  # let the watcher take its baseline
        f.write_text("bb")
        await _wait_until(lambda: len(subs.notified) == 1)
        f.write_text("ccc")
        await _wait_until(lambda: len(subs.notified) == 2)  # noqa: PLR2004
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("'my-server'" in m and "notify broke" in m for m in messages)


# ---------------------------------------------------------------- end to end

_SERVER_TEMPLATE = """
import os, anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

open({pidfile!r}, "w").write(str(os.getpid()))
server = Server("hr", version={version!r})

@server.list_tools()
async def list_tools():
    return [types.Tool(name=n, inputSchema={{"type": "object"}}) for n in {tools!r}]

@server.list_resources()
async def list_resources():
    return []

@server.subscribe_resource()
async def subscribe(uri):
    pass

@server.set_logging_level()
async def set_level(level):
    pass

{prompts}

async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

anyio.run(main)
"""

_PROMPTS = """
@server.list_prompts()
async def list_prompts():
    return []
"""

_BROKEN = "def broken(:\n"
_HANGING = "import time\ntime.sleep(3600)\n"
_INIT_TIMEOUT = 3


def _server_source(
    *,
    pidfile: Path,
    tools: tuple[str, ...] = ("one",),
    version: str = "1",
    prompts: bool = True,
) -> str:
    return _SERVER_TEMPLATE.format(
        pidfile=str(pidfile),
        version=version,
        tools=list(tools),
        prompts=_PROMPTS if prompts else "",
    )


class _Proxy:
    def __init__(self, tmp_path: Path, base_url: str) -> None:
        self.script = tmp_path / "child.py"
        self.pidfile = tmp_path / "child.pid"
        self.base_url = base_url
        self.mcp_url = f"{base_url}/servers/hr/mcp"
        self.other_url = f"{base_url}/servers/other/mcp"
        self._bump = 1

    def write(self, source: str | None = None, **kwargs: Any) -> None:  # noqa: ANN401
        text = source if source is not None else _server_source(pidfile=self.pidfile, **kwargs)
        self.script.write_text(text)
        # Guarantee a snapshot change even when size and mtime granularity collide.
        mtime = time.time_ns() + 10**9 * self._bump
        self._bump += 1
        os.utime(self.script, ns=(mtime, mtime))


class _Client:
    def __init__(self, session: ClientSession, notifications: list[str]) -> None:
        self.session = session
        self.notifications = notifications

    async def tool_names(self) -> list[str]:
        return [t.name for t in (await self.session.list_tools()).tools]


def _kill_child(pidfile: Path) -> None:
    with contextlib.suppress(OSError, ValueError):
        os.kill(int(pidfile.read_text()), signal.SIGKILL)


@contextlib.asynccontextmanager
async def _run_proxy(
    tmp_path: Path,
    *,
    args: list[str] | None = None,
    initial: str | None = None,
    other: bool = False,
) -> AsyncIterator[_Proxy]:
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "child.py"
    script.write_text(initial or _server_source(pidfile=pidfile))
    config = tmp_path / "servers.json"
    entry = {
        "command": sys.executable,
        "args": args if args is not None else [str(script)],
        "hotreload": True,
    }
    servers: dict[str, Any] = {"hr": entry}
    if other:  # a second, non-hot server that must keep serving
        other_script = tmp_path / "other.py"
        other_script.write_text(_server_source(pidfile=tmp_path / "other.pid", tools=("other",)))
        servers["other"] = {"command": sys.executable, "args": [str(other_script)]}
    config.write_text(json.dumps({"mcpServers": servers}))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcp_proxy",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--initialization-timeout",
        str(_INIT_TIMEOUT),
        "--named-server-config",
        str(config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    drain: asyncio.Task[None] | None = None
    try:
        base_url, _ = await _wait_for_proxy_url(process)
        assert process.stdout is not None
        stdout = process.stdout

        async def _drain() -> None:
            while await stdout.readline():
                pass

        drain = asyncio.create_task(_drain())
        yield _Proxy(tmp_path, base_url)
        assert process.returncode is None, "proxy exited"
    finally:
        try:
            if process.returncode is None:
                process.send_signal(signal.SIGTERM)
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(process.wait(), timeout=10)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            if drain is not None:
                drain.cancel()
            _kill_child(pidfile)
            _kill_child(tmp_path / "other.pid")


@contextlib.asynccontextmanager
async def _client(proxy: _Proxy, url: str | None = None) -> AsyncIterator[_Client]:
    notifications: list[str] = []

    async def handler(
        message: RequestResponder[types.ServerRequest, types.ClientResult]
        | types.ServerNotification
        | Exception,
    ) -> None:
        if isinstance(message, types.ServerNotification):
            notifications.append(type(message.root).__name__)

    async with (
        custom_httpx_client(timeout=httpx.Timeout(15)) as http_client,
        streamable_http_client(url or proxy.mcp_url, http_client=http_client) as streams,
        ClientSession(streams[0], streams[1], message_handler=handler) as session,
    ):
        await session.initialize()
        yield _Client(session, notifications)


async def _eventually(check: Any, seconds: float = 8.0) -> Any:  # noqa: ANN401
    """Retry an async check until it stops raising."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return await check()
        except (McpError, AssertionError):  # noqa: PERF203
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.2)


async def _wait_notified(client: _Client, name: str) -> None:
    with anyio.fail_after(5.0):
        while name not in client.notifications:  # noqa: ASYNC110
            await asyncio.sleep(0.05)


async def _status_ok(proxy: _Proxy) -> bool:
    async with httpx.AsyncClient() as http:
        return (await http.get(f"{proxy.base_url}/status")).status_code == httpx.codes.OK


async def test_e2e_edit_adds_tool_and_notifies(tmp_path: Path) -> None:
    """An edit adds a tool; the same session sees it within 5 s and is notified."""
    async with _run_proxy(tmp_path) as proxy, _client(proxy) as client:
        assert await client.tool_names() == ["one"]
        old_pid = int(proxy.pidfile.read_text())
        proxy.write(tools=("one", "two"))
        with anyio.fail_after(5.0):
            await _wait_notified(client, "ToolListChangedNotification")
            # REQ-004: the child was replaced eagerly, before any further client request.
            assert int(proxy.pidfile.read_text()) != old_pid
            while True:  # old child must be gone
                try:
                    os.kill(old_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.05)
            assert await client.tool_names() == ["one", "two"]


async def test_e2e_broken_edit_then_fix_recovers(tmp_path: Path) -> None:
    """A broken edit keeps the proxy up and fails requests (a, f); a fix recovers."""
    async with _run_proxy(tmp_path, other=True) as proxy, _client(proxy) as client:
        assert await client.tool_names() == ["one"]
        proxy.write(_BROKEN)
        await asyncio.sleep(2.5)  # watcher poll, debounce and failed replacement
        with pytest.raises(McpError) as excinfo:
            await client.tool_names()
        assert str(excinfo.value) == "Connection closed"  # (f) the real cause, not a TaskGroup text
        assert await _status_ok(proxy)
        assert client.notifications == []  # (a)
        async with _client(proxy, proxy.other_url) as other:  # REQ-005
            assert await other.tool_names() == ["other"]
        proxy.write(tools=("fixed",))
        assert await _eventually(client.tool_names) == ["fixed"]


async def test_e2e_killed_child_leaves_proxy_up(tmp_path: Path) -> None:
    """Killing the child does not stop the proxy; the next request respawns it."""
    async with _run_proxy(tmp_path) as proxy, _client(proxy) as client:
        assert await client.tool_names() == ["one"]
        old_pid = int(proxy.pidfile.read_text())
        os.kill(old_pid, signal.SIGKILL)
        await asyncio.sleep(1.0)
        assert await _status_ok(proxy)
        assert await _eventually(client.tool_names) == ["one"]
        assert int(proxy.pidfile.read_text()) != old_pid


async def test_e2e_client_that_never_listed_gets_no_notification(tmp_path: Path) -> None:
    """Only clients that listed are notified."""
    async with _run_proxy(tmp_path) as proxy, _client(proxy) as quiet, _client(proxy) as lister:
        assert await lister.tool_names() == ["one"]
        proxy.write(tools=("one", "two"))
        await _wait_notified(lister, "ToolListChangedNotification")
        await asyncio.sleep(0.5)
        assert quiet.notifications == []


async def test_e2e_args_without_existing_file_serves(tmp_path: Path) -> None:
    """(b) A hotreload server whose args name no file still serves tools/list."""
    source = _server_source(pidfile=tmp_path / "child.pid")
    async with (
        _run_proxy(tmp_path, args=["-c", source], initial=source) as proxy,
        _client(proxy) as client,
    ):
        assert await client.tool_names() == ["one"]


async def test_e2e_version_edit_ok_and_capability_removal_fails(tmp_path: Path) -> None:
    """(c) A version-only edit is fine; removing prompts fails with the capabilities message."""
    async with _run_proxy(tmp_path) as proxy, _client(proxy) as client:
        assert await client.tool_names() == ["one"]
        proxy.write(version="2")
        await _wait_notified(client, "ToolListChangedNotification")
        assert await client.tool_names() == ["one"]
        proxy.write(version="2", prompts=False)
        await asyncio.sleep(2.5)
        with pytest.raises(McpError, match="Upstream capabilities changed; restart mcp-proxy"):
            await client.tool_names()


async def test_e2e_hung_initialize_errors_within_timeout_then_recovers(tmp_path: Path) -> None:
    """(d) A child that never answers initialize fails requests in time; a fix recovers."""
    async with _run_proxy(tmp_path) as proxy, _client(proxy) as client:
        assert await client.tool_names() == ["one"]
        proxy.write(_HANGING)
        await asyncio.sleep(1.5)  # let the watcher start the failing replacement
        started = time.monotonic()
        with pytest.raises(McpError, match="did not answer initialize"):
            await client.tool_names()
        assert time.monotonic() - started < _INIT_TIMEOUT + 3
        proxy.write(tools=("fixed",))
        assert await _eventually(client.tool_names, seconds=15) == ["fixed"]


async def test_e2e_state_not_carried_and_other_client_ok(tmp_path: Path) -> None:
    """(e) Reload after set-logging-level and subscribe leaves both clients working."""
    async with _run_proxy(tmp_path) as proxy, _client(proxy) as a, _client(proxy) as b:
        assert await a.tool_names() == ["one"]
        await a.session.set_logging_level("info")
        await a.session.subscribe_resource(types.AnyUrl("file:///x"))
        proxy.write(tools=("one", "two"))
        await _wait_notified(a, "ToolListChangedNotification")
        assert await a.tool_names() == ["one", "two"]
        assert await b.tool_names() == ["one", "two"]
