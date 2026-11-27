"""Outbound transport factory security and diagnostics tests."""

# ruff: noqa: PLR2004

import json
import logging
from collections.abc import Callable
from functools import partial
from http import HTTPStatus
from typing import Any

import anyio
import httpx
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.sse import request_within_origin, sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server

from mcp_proxy.__main__ import _setup_logging
from mcp_proxy.httpx_client import custom_httpx_client


@pytest.mark.asyncio
async def test_custom_httpx_client_selects_class_and_keeps_shared_options() -> None:
    """The optional client class uses the same TLS, auth, header and hook config."""
    async with custom_httpx_client() as default_client:
        assert type(default_client) is httpx.AsyncClient

    supplied: dict[str, Any] = {}

    class SelectedClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
            supplied.update(kwargs)
            super().__init__(**kwargs)

    timeout = httpx.Timeout(2.5)
    async with custom_httpx_client(
        headers={"X-Test": "header"},
        timeout=timeout,
        auth=None,
        verify_ssl=False,
        client_class=SelectedClient,
    ) as selected_client:
        assert type(selected_client) is SelectedClient
        assert selected_client.headers["X-Test"] == "header"
        assert selected_client.follow_redirects is False

    assert supplied["timeout"] is timeout
    assert supplied["auth"] is None
    assert supplied["verify"] is False
    assert supplied["follow_redirects"] is False
    assert set(supplied["event_hooks"]) == {"request", "response"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("verify_ssl", "expected"), [("true", True), ("false", False)])
async def test_selected_httpx_client_keeps_string_ssl_normalization(
    verify_ssl: str,
    expected: bool,
) -> None:
    """The client-class selector preserves normalized SSL string values."""
    supplied: dict[str, Any] = {}

    class SelectedClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
            supplied.update(kwargs)
            super().__init__(**kwargs)

    async with custom_httpx_client(verify_ssl=verify_ssl, client_class=SelectedClient):
        pass

    assert supplied["verify"] is expected


def test_custom_httpx_client_does_not_log_certificate_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The factory forwards custom CA paths without putting them in logs."""
    supplied: dict[str, Any] = {}

    class SelectedClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
            supplied.update(kwargs)

    certificate_path = "/private/secret-cert-bundle.pem"
    with caplog.at_level(logging.DEBUG, logger="mcp_proxy.httpx_client"):
        custom_httpx_client(verify_ssl=certificate_path, client_class=SelectedClient)

    assert supplied["verify"] == certificate_path
    assert certificate_path not in caplog.text


@pytest.mark.asyncio
async def test_custom_httpx_client_does_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw client from the public factory must stop at the redirect response."""
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(200, request=request)
        return httpx.Response(
            307,
            headers={"Location": "https://capture.invalid/redirected"},
            request=request,
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(respond)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr("mcp_proxy.httpx_client.httpx.AsyncClient", client_factory)
    async with custom_httpx_client() as client:
        response = await client.get("https://source.invalid/start")

    assert response.status_code == HTTPStatus.TEMPORARY_REDIRECT
    assert [request.url.host for request in requests] == ["source.invalid"]


def _factory_with_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.AsyncBaseTransport,
) -> Callable[..., httpx.AsyncClient]:
    """Use the production factory with an in-process HTTPX transport."""
    original_client = httpx.AsyncClient

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr("mcp_proxy.httpx_client.httpx.AsyncClient", client_factory)
    return partial(custom_httpx_client)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_name", ["sse", "streamablehttp"])
async def test_mcp_transports_do_not_follow_initial_cross_origin_redirect(
    transport_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual MCP client transports keep their initial connection on-origin."""
    requests: list[httpx.Request] = []

    async def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://capture.invalid/initial"},
            request=request,
        )

    factory = _factory_with_transport(monkeypatch, httpx.MockTransport(redirect))
    if transport_name == "sse":
        with pytest.raises(Exception, match="TaskGroup") as error:
            async with sse_client(
                "https://source.invalid/sse",
                httpx_client_factory=factory,
            ):
                pytest.fail("SSE transport unexpectedly followed the cross-origin redirect")
    else:
        with pytest.raises(Exception, match="TaskGroup") as error:
            async with (
                factory() as http_client,
                streamable_http_client(
                    "https://source.invalid/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                ) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
    assert "capture.invalid" in repr(error.value)

    assert len(requests) == 1
    assert requests[0].url.host == "source.invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "redirect_status", "redirect_location", "follows_redirect"),
    [
        ("http://source.invalid/sse", 307, "/messages/redirected?sessionId=session-secret", True),
        ("http://source.invalid/sse", 307, "https://capture.invalid/steal", False),
        ("http://source.invalid/sse", 307, "http://source.invalid:8080/steal", False),
        ("https://source.invalid/sse", 307, "http://source.invalid/steal", False),
        ("http://source.invalid/sse", 302, "/messages/redirected?sessionId=session-secret", False),
    ],
)
async def test_sse_tools_call_redirect_policy(
    base_url: str,
    redirect_status: int,
    redirect_location: str,
    follows_redirect: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE message POSTs follow only same-origin method-preserving redirects."""
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                redirect_status,
                headers={"Location": redirect_location},
                request=request,
            )
        return httpx.Response(202, request=request)

    factory = _factory_with_transport(monkeypatch, httpx.MockTransport(respond))
    async with factory() as client:
        response = await request_within_origin(
            client,
            "POST",
            httpx.URL(base_url).copy_with(path="/messages/", query=b"sessionId=session-secret"),
            headers={"X-Auth-Token": "key-secret"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"message": "body-secret"}},
            },
        )

    assert response.status_code == (202 if follows_redirect else redirect_status)
    assert requests[0].method == "POST"
    assert requests[0].headers["x-auth-token"] == "key-secret"
    assert "session-secret" in requests[0].url.query.decode()
    assert b"body-secret" in requests[0].content
    if follows_redirect:
        assert len(requests) == 2
        assert requests[-1].content == requests[0].content
        assert requests[-1].headers["x-auth-token"] == "key-secret"
    else:
        assert len(requests) == 1
        redirect_target = httpx.URL(base_url).join(redirect_location)
        assert all(request.url != redirect_target for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "redirect_status", "redirect_location", "follows_redirect"),
    [
        ("http://source.invalid/mcp", 307, "/mcp/redirected", True),
        ("http://source.invalid/mcp", 307, "https://capture.invalid/steal", False),
        ("http://source.invalid/mcp", 307, "http://source.invalid:8080/steal", False),
        ("https://source.invalid/mcp", 307, "http://source.invalid/steal", False),
        ("http://source.invalid/mcp", 302, "/mcp/redirected", False),
    ],
)
async def test_streamablehttp_tools_call_redirect_policy(
    base_url: str,
    redirect_status: int,
    redirect_location: str,
    follows_redirect: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An initialized tools/call follows only same-origin method-preserving redirects."""
    requests: list[httpx.Request] = []
    initialize_result = {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "redirect-test", "version": "1"},
    }

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(405, request=request)
        message = json.loads(request.content)
        if message["method"] == "notifications/initialized":
            return httpx.Response(202, request=request)
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": message["id"], "result": initialize_result},
                headers={"Mcp-Session-Id": "session-secret", "Mcp-Protocol-Version": "2025-03-26"},
                request=request,
            )
        if message["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"message": {"type": "string"}},
                                },
                            }
                        ]
                    },
                },
                headers={"Mcp-Protocol-Version": "2025-03-26"},
                request=request,
            )
        if request.url.path == "/mcp":
            return httpx.Response(
                redirect_status,
                headers={"Location": redirect_location},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
            headers={"Mcp-Protocol-Version": "2025-03-26"},
            request=request,
        )

    factory = _factory_with_transport(monkeypatch, httpx.MockTransport(respond))

    async def make_call() -> types.CallToolResult:
        async with (
            factory(headers={"X-Auth-Token": "key-secret"}) as http_client,
            streamable_http_client(
                base_url,
                http_client=http_client,
                terminate_on_close=False,
            ) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            return await session.call_tool("echo", {"message": "body-secret"})

    if follows_redirect:
        result = await make_call()
        assert result.content[0].text == "ok"
    else:
        with pytest.raises(Exception, match="TaskGroup"):
            await make_call()

    post_requests = [request for request in requests if request.method == "POST"]
    call_requests = [
        request
        for request in post_requests
        if json.loads(request.content).get("method") == "tools/call"
    ]
    assert call_requests
    assert call_requests[0].headers["mcp-session-id"] == "session-secret"
    assert call_requests[0].headers["x-auth-token"] == "key-secret"
    assert b"body-secret" in call_requests[0].content
    if follows_redirect:
        assert [request.url.path for request in call_requests] == ["/mcp", "/mcp/redirected"]
        assert call_requests[-1].content == call_requests[0].content
        assert call_requests[-1].headers["mcp-session-id"] == "session-secret"
        assert call_requests[-1].headers["x-auth-token"] == "key-secret"
    else:
        assert len(call_requests) == 1
        redirect_target = httpx.URL(base_url).join(redirect_location)
        assert all(request.url != redirect_target for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(("level", "debug"), [("INFO", False), ("DEBUG", True)])
async def test_cli_logging_hides_transport_secrets_and_keeps_safe_diagnostics(  # noqa: C901, PLR0915
    level: str,
    debug: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Actual CLI logging setup keeps method/status while hiding request/response data."""
    root = logging.getLogger()
    root_state = (root.level, root.handlers[:])
    dependency_names = (
        "httpx",
        "httpcore",
        "mcp.client.sse",
        "mcp.client.streamable_http",
        "mcp.server.lowlevel.server",
        "mcp.server.streamable_http_manager",
        "mcp.server.sse",
        "mcp.server.streamable_http",
        "sse_starlette.sse",
    )
    dependency_states = {
        name: (
            logger.level,
            logger.handlers[:],
            logger.propagate,
            logger.disabled,
        )
        for name in dependency_names
        if (logger := logging.getLogger(name))
    }
    requests: list[httpx.Request] = []
    response_headers = {
        "Set-Cookie": "cookie-secret",
        "Location": "https://redirect.invalid/path?location-secret",
    }
    initialize_result = {
        "protocolVersion": "2025-03-26",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "logging-test", "version": "1"},
    }

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/sse":
            return httpx.Response(
                200,
                content=b"event: endpoint\ndata: /messages/?sessionId=session-secret\n\n",
                headers={"Content-Type": "text/event-stream", **response_headers},
                request=request,
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                content=b"",
                headers={"Content-Type": "text/event-stream", **response_headers},
                request=request,
            )
        message = json.loads(request.content)
        if message["method"] == "notifications/initialized":
            return httpx.Response(202, headers=response_headers, request=request)
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": message["id"], "result": initialize_result},
                headers={
                    **response_headers,
                    "Mcp-Session-Id": "session-secret",
                    "Mcp-Protocol-Version": "2025-03-26",
                },
                request=request,
            )
        if message["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"message": {"type": "string"}},
                                },
                            }
                        ]
                    },
                },
                headers={**response_headers, "Mcp-Protocol-Version": "2025-03-26"},
                request=request,
            )
        return httpx.Response(
            200,
            extensions={"reason_phrase": b"arbitrary-response-reason-secret"},
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
            headers={**response_headers, "Mcp-Protocol-Version": "2025-03-26"},
            request=request,
        )

    factory = _factory_with_transport(monkeypatch, httpx.MockTransport(respond))

    try:
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        _setup_logging(level=level, debug=debug)
        async with sse_client(
            "http://user-secret:password-secret@source.invalid/sse?query-secret#fragment-secret",
            headers={"Private-Token": "header-secret"},
            httpx_client_factory=factory,
        ):
            pass
        async with (
            factory(headers={"Private-Token": "header-secret"}) as http_client,
            streamable_http_client(
                "http://user-secret:password-secret@source.invalid/mcp?query-secret#fragment-secret",
                http_client=http_client,
                terminate_on_close=False,
            ) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            result = await session.call_tool("echo", {"message": "body-secret"})
        assert result.content[0].text == "ok"

        server: Server[Any] = Server("lowlevel-log-test")

        @server.list_tools()  # type: ignore[misc,no-untyped-call]
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name="echo",
                    description="Echo",
                    inputSchema={"type": "object", "properties": {"message": {"type": "string"}}},
                ),
            ]

        @server.call_tool()  # type: ignore[misc]
        async def call_tool(
            name: str,
            arguments: dict[str, Any] | None,
        ) -> list[types.Content]:
            assert name == "echo"
            return [types.TextContent(type="text", text=str(arguments))]

        client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream(0)
        server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream(0)
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server.run,
                client_to_server_receive,
                server_to_client_send,
                server.create_initialization_options(),
            )
            async with ClientSession(server_to_client_receive, client_to_server_send) as session:
                await session.initialize()
                await session.call_tool("echo", {"message": "body-secret"})
            task_group.cancel_scope.cancel()

        logging.getLogger("httpx").warning("visible-warning-marker")
        logging.getLogger("mcp.client.sse").error("visible-error-marker")
        logging.getLogger("sse_starlette.sse").warning("visible-sse-warning-marker")
        logging.getLogger("sse_starlette.sse").error("visible-sse-error-marker")
        output = capsys.readouterr().err
    finally:
        root.handlers[:] = root_state[1]
        root.setLevel(root_state[0])
        for name, state in dependency_states.items():
            logger = logging.getLogger(name)
            logger.setLevel(state[0])
            logger.handlers[:] = state[1]
            logger.propagate = state[2]
            logger.disabled = state[3]

    for secret in (
        "user-secret",
        "password-secret",
        "query-secret",
        "fragment-secret",
        "header-secret",
        "cookie-secret",
        "location-secret",
        "session-secret",
        "body-secret",
        "arbitrary-response-reason-secret",
    ):
        assert secret not in output
    assert "HTTP Request: GET" in output
    assert "HTTP Request: POST" in output
    assert "HTTP Response: POST - 200" in output
    assert "source.invalid" not in output
    assert "/sse" not in output
    assert "/mcp" not in output
    assert "visible-warning-marker" in output
    assert "visible-error-marker" in output
    assert "visible-sse-warning-marker" in output
    assert "visible-sse-error-marker" in output
