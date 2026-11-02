"""Regression tests for Streamable HTTP reverse-client DELETE teardown.

Cover both the SDK 1.30 direct-send session termination and ordinary tool
requests, which must not inherit the short termination deadline.
"""

# ruff: noqa: SLF001, PLR2004
# pylint: disable=protected-access  # Intentionally test the private HTTP factory.
from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import request_within_origin, streamablehttp_client

reverse_client = importlib.import_module("mcp_proxy.streamablehttp_client")
_SOURCE_FILE = Path(reverse_client.__file__).resolve()
_SOURCE_ROOT = _SOURCE_FILE.parent.parent
_PACKAGE_ROOT = _SOURCE_FILE.parents[2]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.AsyncBaseTransport,
) -> list[httpx.AsyncClient]:
    clients: list[httpx.AsyncClient] = []

    def factory(**kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401
        client_class = kwargs.pop("client_class")
        verify_ssl = kwargs.pop("verify_ssl")
        if verify_ssl is not None:
            kwargs["verify"] = verify_ssl
        client = client_class(transport=transport, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(reverse_client, "custom_httpx_client", factory)
    return clients


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_mode", ["headers", "body", "immediate"])
@pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="asyncio.run handles SIGINT gracefully starting with Python 3.11",
)
async def test_cli_sigint_delete_shutdown_is_bounded_and_preserves_interrupt(  # noqa: C901, PLR0915
    delete_mode: str,
) -> None:
    """A real stdio CLI bounds DELETE teardown and preserves SIGINT."""
    session_id = "cli-delete-body-session"
    delete_entered = asyncio.Event()
    release_delete = asyncio.Event()
    responses: dict[int, dict[str, Any]] = {}
    response_available = asyncio.Event()
    source_file = _SOURCE_FILE
    source_root = _SOURCE_ROOT
    package_root = _PACKAGE_ROOT
    child_launcher = (
        "import runpy, sys; "
        "import mcp_proxy.streamablehttp_client as reverse; "
        "print('MCP_PROXY_STREAMABLE_SOURCE=' + reverse.__file__, file=sys.stderr); "
        "runpy.run_module('mcp_proxy', run_name='__main__')"
    )
    handlers: set[asyncio.Task[Any]] = set()

    async def handle(  # noqa: C901, PLR0912, PLR0915
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method, _, _ = lines[0].split()
            headers = {
                name.lower(): value
                for line in lines[1:]
                if ": " in line
                for name, value in [line.split(": ", 1)]
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            message = json.loads(body) if body else {}
            rpc_method = message.get("method")

            if method == "GET":
                writer.write(
                    b"HTTP/1.1 405 Method Not Allowed\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            elif method == "DELETE":
                delete_entered.set()
                if delete_mode == "headers":
                    await release_delete.wait()
                    writer.write(
                        b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                elif delete_mode == "body":
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nx"
                    )
                    await writer.drain()
                    await release_delete.wait()
                    writer.write(b"23456789")
                else:
                    writer.write(
                        b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
            elif rpc_method == "notifications/initialized":
                writer.write(
                    b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            else:
                if rpc_method == "initialize":
                    result: dict[str, Any] = {
                        "protocolVersion": message["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "termination-test", "version": "1"},
                    }
                    response: dict[str, Any] = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": result,
                    }
                    extra_headers = (
                        f"Mcp-Session-Id: {session_id}\r\n"
                        f"Mcp-Protocol-Version: {result['protocolVersion']}\r\n"
                    )
                elif rpc_method == "tools/list":
                    response = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]},
                    }
                    extra_headers = "Mcp-Protocol-Version: 2025-11-25\r\n"
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"content": [{"type": "text", "text": "ok"}]},
                    }
                    extra_headers = "Mcp-Protocol-Version: 2025-11-25\r\n"
                wire = json.dumps(response).encode()
                response_headers = (
                    f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(wire)}\r\n{extra_headers}Connection: close\r\n\r\n"
                )
                writer.write(response_headers.encode() + wire)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            if task is not None:
                handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    env = {
        **os.environ,
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
        "PYTHONPATH": os.pathsep.join(
            path for path in (str(source_root), os.environ.get("PYTHONPATH", "")) if path
        ),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child_launcher,
        "--transport",
        "streamablehttp",
        f"http://127.0.0.1:{port}/mcp",
        cwd=package_root,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    retained_writer = os.dup(process.stdin.transport.get_extra_info("pipe").fileno())

    async def read_responses() -> None:
        async for line in process.stdout:
            message = json.loads(line)
            if "id" in message:
                responses[message["id"]] = message
                response_available.set()

    stdout_task = asyncio.create_task(read_responses())
    stderr_task = asyncio.create_task(process.stderr.read())

    async def send(message: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message).encode() + b"\n")
        await process.stdin.drain()

    async def wait_response(request_id: int) -> dict[str, Any]:
        async def wait_until_available() -> None:
            while request_id not in responses:
                response_available.clear()
                if request_id in responses:
                    break
                await response_available.wait()

        await asyncio.wait_for(wait_until_available(), timeout=10)
        return responses[request_id]

    exited_within_deadline = False
    delete_observed = False
    sigint_at: float | None = None
    try:
        await send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "termination-test", "version": "1"},
                },
            }
        )
        await wait_response(1)
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        await wait_response(2)
        await send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {}},
            }
        )
        await wait_response(3)
        sigint_at = time.monotonic()
        process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(delete_entered.wait(), timeout=5)
            delete_observed = True
        except (TimeoutError, asyncio.TimeoutError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=6.5)
            exited_within_deadline = True
            os.fstat(retained_writer)
        except TimeoutError:
            os.fstat(retained_writer)
    finally:
        release_delete.set()
        if process.returncode is None:
            await asyncio.wait_for(process.wait(), timeout=5)
        process.stdin.close()
        os.close(retained_writer)
        await stdout_task
        stderr = (await stderr_task).decode()
        server.close()
        await server.wait_closed()
        if handlers:
            _done, pending = await asyncio.wait(handlers, timeout=2)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    assert delete_observed, f"CLI did not issue DELETE after SIGINT: {stderr!r}"
    assert exited_within_deadline, "CLI remained blocked past the five-second DELETE deadline"
    assert sigint_at is not None
    elapsed = time.monotonic() - sigint_at
    assert elapsed < (3 if delete_mode == "immediate" else 6.5)
    assert process.returncode != 0
    assert "KeyboardInterrupt" in stderr
    assert f"MCP_PROXY_STREAMABLE_SOURCE={source_file}" in stderr
    assert not handlers


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_normal_requests_are_not_limited_by_termination_deadline(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """A short DELETE deadline must not terminate normal upstream traffic."""
    monkeypatch.setattr(reverse_client, "SESSION_TERMINATION_TIMEOUT_SECONDS", 0.02)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == method
        await anyio.sleep(0.05)
        return httpx.Response(200, json={"ok": True})

    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    async def scenario() -> None:
        async with reverse_client._termination_bounded_httpx_client() as client:
            response = await client.send(client.build_request(method, "http://unused/mcp"))
            assert response.status_code == 200
        assert client.is_closed

    anyio.run(scenario)


def test_direct_send_delete_cancels_stalled_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK's raw send(DELETE) path is cancellation-bounded."""
    monkeypatch.setattr(reverse_client, "SESSION_TERMINATION_TIMEOUT_SECONDS", 0.025)
    methods: list[str] = []
    cancelled = anyio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()
        return httpx.Response(204)

    clients = _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    async def scenario() -> None:
        with anyio.fail_after(3):
            async with reverse_client._termination_bounded_httpx_client() as client:
                with pytest.raises(TimeoutError):
                    await client.send(client.build_request("DELETE", "http://unused/mcp"))
        assert methods == ["DELETE"]
        assert cancelled.is_set()
        assert clients[0].is_closed

    anyio.run(scenario)


@pytest.mark.asyncio
async def test_sdk_redirect_chain_shares_delete_body_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK's same-origin DELETE redirect chain has one body-inclusive budget."""
    monkeypatch.setattr(reverse_client, "SESSION_TERMINATION_TIMEOUT_SECONDS", 0.1)
    requests: list[httpx.Request] = []
    completed_second_body = anyio.Event()
    closed_bodies: list[int] = []

    class DelayedBody(httpx.AsyncByteStream):
        def __init__(self, number: int, delay: float) -> None:
            self.number = number
            self.delay = delay

        async def __aiter__(self) -> AsyncIterator[bytes]:
            await anyio.sleep(self.delay)
            if self.number == 2:
                completed_second_body.set()
            yield b"body"

        async def aclose(self) -> None:
            closed_bodies.append(self.number)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "DELETE"
        delete_count = len(requests)
        if delete_count == 1:
            return httpx.Response(
                307,
                headers={"Location": "/mcp/redirected"},
                stream=DelayedBody(1, 0.06),
                request=request,
            )
        return httpx.Response(200, stream=DelayedBody(2, 0.06), request=request)

    clients = _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    started = time.monotonic()
    with anyio.fail_after(3):
        async with reverse_client._termination_bounded_httpx_client() as configured_client:
            with pytest.raises(TimeoutError):
                await request_within_origin(
                    configured_client,
                    "DELETE",
                    "http://source.invalid/mcp",
                )
    elapsed = time.monotonic() - started

    delete_requests = [request for request in requests if request.method == "DELETE"]
    assert [request.url.path for request in delete_requests] == ["/mcp", "/mcp/redirected"]
    assert not completed_second_body.is_set()
    assert sorted(closed_bodies) == [1, 2]
    assert elapsed < 0.15
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_external_cancellation_finishes_delete_response_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close a received DELETE response even while its caller is cancelled."""
    body_started = anyio.Event()
    body_closed = anyio.Event()
    cancellation_seen = anyio.Event()

    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            body_started.set()
            await anyio.sleep_forever()
            yield b"unreachable"

        async def aclose(self) -> None:
            await anyio.sleep(0.01)
            body_closed.set()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=StalledBody(), request=request)

    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    async with reverse_client._termination_bounded_httpx_client() as client:

        async def send_delete() -> None:
            try:
                await client.send(
                    client.build_request("DELETE", "http://unused/mcp"),
                    stream=True,
                )
            except BaseException as exc:
                if isinstance(exc, anyio.get_cancelled_exc_class()):
                    cancellation_seen.set()
                raise

        with anyio.fail_after(1):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(send_delete)
                await body_started.wait()
                task_group.cancel_scope.cancel()

    assert cancellation_seen.is_set()
    assert body_closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("close_behavior", ["stalls", "raises"])
async def test_delete_cleanup_has_finite_grace_and_preserves_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    close_behavior: str,
) -> None:
    """Bound shielded response cleanup without replacing the request timeout."""
    monkeypatch.setattr(reverse_client, "SESSION_TERMINATION_TIMEOUT_SECONDS", 0.025)
    body_started = anyio.Event()
    close_started = anyio.Event()
    release_close = anyio.Event()
    request_done = anyio.Event()
    request_error: BaseException | None = None
    request_elapsed = 0.0

    class CleanupBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            body_started.set()
            await anyio.sleep_forever()
            yield b"unreachable"

        async def aclose(self) -> None:
            close_started.set()
            if close_behavior == "raises":
                raise RuntimeError("synthetic cleanup secret")
            await release_close.wait()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=CleanupBody(), request=request)

    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))
    async with reverse_client._termination_bounded_httpx_client() as client:

        async def send_delete() -> None:
            nonlocal request_error, request_elapsed
            started = time.monotonic()
            try:
                await client.send(
                    client.build_request("DELETE", "http://unused/mcp"),
                    stream=True,
                )
            except (TimeoutError, RuntimeError) as exc:
                request_error = exc
            finally:
                request_elapsed = time.monotonic() - started
                request_done.set()

        finished_within_grace = False
        with anyio.fail_after(2):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(send_delete)
                await body_started.wait()
                await close_started.wait()
                with anyio.move_on_after(1.3):
                    await request_done.wait()
                finished_within_grace = request_done.is_set()
                if not finished_within_grace:
                    release_close.set()
                await request_done.wait()

    assert finished_within_grace
    assert isinstance(request_error, TimeoutError)
    if close_behavior == "raises":
        assert "Response cleanup failed" in caplog.text
        assert "synthetic cleanup secret" not in caplog.text
    else:
        assert 0.75 < request_elapsed < 1.3


def test_mcp_sdk_session_teardown_uses_bounded_direct_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real SDK 1.30 session termination sequence on context exit."""
    monkeypatch.setattr(reverse_client, "SESSION_TERMINATION_TIMEOUT_SECONDS", 0.025)
    methods: list[str] = []
    cancelled = anyio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405, request=request)
        if request.method == "DELETE":
            methods.append(request.method)
            try:
                await anyio.sleep_forever()
            finally:
                cancelled.set()
            return httpx.Response(204, request=request)
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": message["params"]["protocolVersion"],
                        "capabilities": {},
                        "serverInfo": {"name": "teardown-test", "version": "1"},
                    },
                },
                headers={
                    "Mcp-Session-Id": "initialized-session",
                    "Mcp-Protocol-Version": "2025-11-25",
                },
                request=request,
            )
        return httpx.Response(202, request=request)

    clients = _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    async def scenario() -> None:
        # The real deprecated factory delegates to the modern transport in SDK 1.30.
        # The transport's test session id simulates a successfully initialized
        # stateful remote server without making a real external connection.
        with anyio.fail_after(3):
            # Initialize a real SDK session so teardown follows its public path.
            async with streamablehttp_client(
                "http://unused/mcp",
                httpx_client_factory=reverse_client._termination_bounded_httpx_client,
            ) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
        assert methods == ["DELETE"]
        assert cancelled.is_set()
        assert clients[0].is_closed

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("initialization_failure", "expected_message"),
    [("unsupported", "protocol"), ("jsonrpc_error", "synthetic initialization error")],
)
def test_sdk_initialization_error_survives_delete_timeout(
    monkeypatch: pytest.MonkeyPatch,
    initialization_failure: str,
    expected_message: str,
) -> None:
    """A failed initialize keeps its original error when assigned-session DELETE times out."""
    monkeypatch.setattr(reverse_client, "SESSION_TERMINATION_TIMEOUT_SECONDS", 0.025)
    methods: list[str] = []
    delete_cancelled = anyio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(405, request=request)
        if request.method == "DELETE":
            methods.append(request.method)
            try:
                await anyio.sleep_forever()
            finally:
                delete_cancelled.set()
            return httpx.Response(204, request=request)

        message = json.loads(request.content)
        if message["method"] == "initialize":
            if initialization_failure == "jsonrpc_error":
                response = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32603, "message": expected_message},
                }
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": "2099-01-01",
                        "capabilities": {},
                        "serverInfo": {"name": "teardown-test", "version": "1"},
                    },
                }
            return httpx.Response(
                200,
                json=response,
                headers={
                    "Mcp-Session-Id": "assigned-before-error",
                    "Mcp-Protocol-Version": "2025-11-25",
                },
                request=request,
            )
        return httpx.Response(202, request=request)

    _install_mock_transport(monkeypatch, httpx.MockTransport(handler))

    async def scenario() -> None:
        error: Exception | None = None
        try:
            with anyio.fail_after(3):
                async with streamablehttp_client(
                    "http://unused/mcp",
                    httpx_client_factory=reverse_client._termination_bounded_httpx_client,
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
        except Exception as exc:  # noqa: BLE001
            error = exc

        def exception_text(exc: BaseException) -> str:
            children = getattr(exc, "exceptions", ())
            return " ".join((str(exc), *(exception_text(child) for child in children)))

        assert error is not None, "SDK initialization unexpectedly succeeded"
        assert expected_message in exception_text(error)
        assert methods == ["DELETE"]
        assert delete_cancelled.is_set()

    anyio.run(scenario)


def test_reverse_client_factory_preserves_tls_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not lose the existing HTTP client options when installing the guard."""
    supplied: dict[str, Any] = {}

    def factory(**kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401
        supplied.update(kwargs)
        return kwargs["client_class"]()

    monkeypatch.setattr(reverse_client, "custom_httpx_client", factory)
    client = reverse_client._termination_bounded_httpx_client(
        headers={"X-Test": "header"}, verify_ssl=False, auth=None
    )
    assert isinstance(client, reverse_client._SessionTerminationAsyncClient)
    assert supplied == {
        "headers": {"X-Test": "header"},
        "verify_ssl": False,
        "auth": None,
        "timeout": None,
        "client_class": reverse_client._SessionTerminationAsyncClient,
    }
    anyio.run(client.aclose)
