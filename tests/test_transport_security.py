"""Security policy tests at the application assembled by ``run_mcp_server``."""

# ruff: noqa: PLR2004

import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import pytest
import uvicorn
from mcp.client.stdio import StdioServerParameters
from starlette.types import Receive, Scope, Send

from mcp_proxy.mcp_server import MCPServerSettings, run_mcp_server

_INITIALIZING_STDIO_SERVER = """
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {},
            "serverInfo": {"name": "security-test", "version": "1"},
        }
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""

_AUTH_CONFIG_TEST_VALUE = "configured-test-value"
_AUTH_VALID_TEST_VALUE = "a+/._~-Z=="


async def _run_production_server(
    monkeypatch: pytest.MonkeyPatch,
    settings: MCPServerSettings,
    probe: Callable[[Any], Awaitable[None]],
) -> None:
    """Probe the application created by ``run_mcp_server`` while its backends are live."""

    class CapturingServer:
        def __init__(self, config: uvicorn.Config) -> None:
            self.config = config
            self.should_exit = False

        async def serve(self) -> None:
            await probe(self.config.app)

    monkeypatch.setattr("mcp_proxy.mcp_server.uvicorn.Server", CapturingServer)
    params = StdioServerParameters(command=sys.executable, args=["-c", _INITIALIZING_STDIO_SERVER])
    await run_mcp_server(settings, params, {"named": params})


async def _request(  # noqa: PLR0913
    app: Callable[[Scope, Receive, Send], Awaitable[None]],
    path: str,
    headers: list[tuple[bytes, bytes]],
    *,
    method: str = "GET",
    body: bytes = b"",
    query_string: bytes = b"",
    stop_after_start: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    messages: list[dict[str, Any]] = []
    body_reads = 0

    async def receive() -> dict[str, Any]:
        nonlocal body_reads
        body_reads += 1
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)
        if stop_after_start and message["type"] == "http.response.start":
            raise asyncio.CancelledError

    try:
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": query_string,
                "root_path": "",
                "headers": headers,
                "server": ("127.0.0.1", 8080),
                "client": ("127.0.0.1", 12345),
            },
            receive,
            send,
        )
    except asyncio.CancelledError:
        if not stop_after_start:
            raise
    return messages, body_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_run_mcp_server_rejects_untrusted_host_before_production_routes(
    monkeypatch: pytest.MonkeyPatch,
    stateless: bool,
) -> None:
    """The actual default, global, and named routes reject a foreign Host."""
    responses: dict[str, tuple[list[dict[str, Any]], int]] = {}

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        requests = (
            ("/status", "GET", False),
            ("/healthz", "GET", False),
            ("/servers/named/healthz", "GET", False),
            ("/mcp", "POST", False),
            ("/mcp/", "POST", False),
            ("/servers/named/mcp", "POST", False),
            ("/servers/named/mcp/", "POST", False),
            ("/sse", "GET", True),
            ("/servers/named/sse", "GET", True),
            ("/messages/", "POST", False),
            ("/servers/named/messages/", "POST", False),
        )
        for path, method, stop_after_start in requests:
            messages, body_reads = await _request(
                app,
                path,
                [
                    (b"host", b"attacker.example"),
                    *([(b"content-type", b"application/json")] if method == "POST" else []),
                ],
                method=method,
                stop_after_start=stop_after_start,
            )
            responses[f"{method} {path}"] = (messages, body_reads)

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(
            bind_host="127.0.0.1",
            port=0,
            stateless=stateless,
            allow_origins=["*"],
        ),
        capture_app,
    )

    for path, (messages, body_reads) in responses.items():
        starts = [message for message in messages if message["type"] == "http.response.start"]
        assert [message["status"] for message in starts] == [421], path
        assert body_reads == 0, path


@pytest.mark.asyncio
async def test_production_authority_header_shapes_and_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw Host and Origin cardinality and syntax are checked before SDK matching."""
    statuses: dict[str, list[int]] = {}

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        cases = [
            ("missing-host", [], 421),
            ("duplicate-host-equal", [(b"host", b"localhost"), (b"HOST", b"localhost")], 421),
            (
                "duplicate-host-allowed-first",
                [(b"host", b"localhost"), (b"host", b"attacker.example")],
                421,
            ),
            (
                "duplicate-host-foreign-first",
                [(b"host", b"attacker.example"), (b"host", b"localhost")],
                421,
            ),
            (
                "invalid-host-and-origin",
                [(b"host", b"attacker.example"), (b"origin", b"https://evil.example")],
                421,
            ),
            ("empty-host", [(b"host", b"")], 421),
            ("host-empty-port", [(b"host", b"localhost:")], 421),
            ("host-port-zero", [(b"host", b"localhost:0")], 421),
            ("host-empty-name-with-port", [(b"host", b":8080")], 421),
            ("host-wildcard-port", [(b"host", b"localhost:*")], 421),
            ("host-bad-port", [(b"host", b"localhost:abc")], 421),
            ("host-malformed-port-suffix", [(b"host", b"localhost:8123.evil")], 421),
            ("host-control-character", [(b"host", b"localhost\x01")], 421),
            ("host-delete-character", [(b"host", b"localhost\x7f")], 421),
            ("host-url", [(b"host", b"http://localhost")], 421),
            ("host-userinfo", [(b"host", b"user@localhost")], 421),
            ("host-comma-list", [(b"host", b"localhost,evil.example")], 421),
            ("host-unbracketed-ipv6", [(b"host", b"::1")], 421),
            (
                "foreign-origin",
                [(b"host", b"localhost"), (b"origin", b"https://evil.example")],
                403,
            ),
            ("null-origin", [(b"host", b"localhost"), (b"origin", b"null")], 403),
            ("empty-origin", [(b"host", b"localhost"), (b"origin", b"")], 403),
            (
                "whitespace-origin",
                [(b"host", b"localhost"), (b"origin", b" https://localhost")],
                403,
            ),
            (
                "origin-trailing-slash",
                [(b"host", b"localhost"), (b"origin", b"http://localhost/")],
                403,
            ),
            (
                "origin-userinfo",
                [(b"host", b"localhost"), (b"origin", b"http://user@localhost")],
                403,
            ),
            (
                "origin-query",
                [(b"host", b"localhost"), (b"origin", b"http://localhost?token=x")],
                403,
            ),
            (
                "origin-empty-query-delimiter",
                [(b"host", b"localhost"), (b"origin", b"http://localhost?")],
                403,
            ),
            (
                "origin-fragment",
                [(b"host", b"localhost"), (b"origin", b"http://localhost#fragment")],
                403,
            ),
            (
                "origin-empty-fragment-delimiter",
                [(b"host", b"localhost"), (b"origin", b"http://localhost#")],
                403,
            ),
            ("origin-unclosed-ipv6", [(b"host", b"localhost"), (b"origin", b"http://[::1")], 403),
            (
                "origin-control-character",
                [(b"host", b"localhost"), (b"origin", b"http://localhost\x01")],
                403,
            ),
            ("origin-bad-port", [(b"host", b"localhost"), (b"origin", b"http://localhost:0")], 403),
            ("origin-scheme", [(b"host", b"localhost"), (b"origin", b"ftp://localhost")], 403),
            (
                "duplicate-origin",
                [
                    (b"host", b"localhost"),
                    (b"origin", b"http://localhost"),
                    (b"Origin", b"http://localhost"),
                ],
                403,
            ),
        ]
        for name, headers, expected_status in cases:
            messages, body_reads = await _request(app, "/status", headers)
            starts = [message for message in messages if message["type"] == "http.response.start"]
            statuses[name] = [message["status"] for message in starts]
            assert body_reads == 0
            assert expected_status == statuses[name][0]
            assert len(starts) == 1

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(bind_host="127.0.0.1", port=0),
        capture_app,
    )
    assert all(len(response) == 1 for response in statuses.values())


@pytest.mark.asyncio
async def test_authority_guard_does_not_validate_post_bodies_for_other_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer guard checks authority; unrelated route methods keep Starlette semantics."""
    result: tuple[list[dict[str, Any]], int] | None = None

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        nonlocal result
        result = await _request(app, "/status", [(b"host", b"localhost")], method="POST")

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(bind_host="127.0.0.1", port=0),
        capture_app,
    )

    assert result is not None
    messages, body_reads = result
    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert [message["status"] for message in starts] == [405]
    assert body_reads == 0


@pytest.mark.asyncio
async def test_loopback_and_wildcard_origin_keep_expected_authority_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native clients and explicit wildcard CORS work while Host remains strict."""
    results: dict[str, tuple[list[dict[str, Any]], int]] = {}

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        cases = {
            "bare-localhost": ("/healthz", [(b"host", b"localhost")], "GET"),
            "ipv4-port-min": ("/healthz", [(b"host", b"localhost:1")], "GET"),
            "ipv4-port-max": ("/healthz", [(b"host", b"localhost:65535")], "GET"),
            "ipv6-port": ("/healthz", [(b"host", b"[::1]:51321")], "GET"),
            "wildcard-origin": (
                "/status",
                [(b"host", b"localhost"), (b"origin", b"https://outside.example")],
                "GET",
            ),
            "wildcard-origin-null": (
                "/status",
                [(b"host", b"localhost"), (b"origin", b"null")],
                "GET",
            ),
            "wildcard-preflight": (
                "/healthz",
                [
                    (b"host", b"localhost"),
                    (b"origin", b"https://outside.example"),
                    (b"access-control-request-method", b"GET"),
                ],
                "OPTIONS",
            ),
            "wildcard-still-rejects-host": (
                "/healthz",
                [(b"host", b"outside.example"), (b"origin", b"https://outside.example")],
                "GET",
            ),
            "wildcard-rejects-malformed-origin": (
                "/healthz",
                [(b"host", b"localhost"), (b"origin", b"https://outside.example/")],
                "GET",
            ),
            "wildcard-rejects-empty-query": (
                "/healthz",
                [(b"host", b"localhost"), (b"origin", b"https://outside.example?")],
                "GET",
            ),
            "wildcard-rejects-empty-fragment": (
                "/healthz",
                [(b"host", b"localhost"), (b"origin", b"https://outside.example#")],
                "GET",
            ),
            "wildcard-rejects-control-character": (
                "/healthz",
                [(b"host", b"localhost"), (b"origin", b"https://outside.example\x01")],
                "GET",
            ),
            "wildcard-rejects-internal-tab": (
                "/healthz",
                [(b"host", b"localhost"), (b"origin", b"https://outside.\texample")],
                "GET",
            ),
        }
        for name, (path, headers, method) in cases.items():
            results[name] = await _request(app, path, headers, method=method)

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(bind_host="127.0.0.1", port=0, allow_origins=["*"]),
        capture_app,
    )

    def status(name: str) -> int:
        return next(
            message["status"]
            for message in results[name][0]
            if message["type"] == "http.response.start"
        )

    assert status("bare-localhost") == 204
    assert status("ipv4-port-min") == 204
    assert status("ipv4-port-max") == 204
    assert status("ipv6-port") == 204
    assert status("wildcard-origin") == 200
    assert status("wildcard-origin-null") == 200
    assert status("wildcard-preflight") == 200
    assert status("wildcard-still-rejects-host") == 421
    assert status("wildcard-rejects-malformed-origin") == 403
    assert status("wildcard-rejects-empty-query") == 403
    assert status("wildcard-rejects-empty-fragment") == 403
    assert status("wildcard-rejects-control-character") == 403
    assert status("wildcard-rejects-internal-tab") == 403
    assert dict(results["wildcard-origin"][0][0]["headers"])[b"access-control-allow-origin"] == b"*"
    assert (
        dict(results["wildcard-preflight"][0][0]["headers"])[b"access-control-allow-origin"] == b"*"
    )


@pytest.mark.asyncio
async def test_configured_authorities_remain_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additional Host trust does not imply Origin trust or an implicit port wildcard."""
    results: dict[str, tuple[list[dict[str, Any]], int]] = {}

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        cases = {
            "host-bare": [(b"host", b"mcp.example.com")],
            "host-bare-extra-port": [(b"host", b"mcp.example.com:9443")],
            "host-wildcard-port": [(b"host", b"proxy.example.com:9443")],
            "host-wildcard-bind": [(b"host", b"0.0.0.0:8123")],
            "explicit-origin": [
                (b"host", b"mcp.example.com"),
                (b"origin", b"https://browser.example"),
            ],
            "host-does-not-add-origin": [
                (b"host", b"mcp.example.com"),
                (b"origin", b"https://mcp.example.com"),
            ],
        }
        for name, headers in cases.items():
            results[name] = await _request(app, "/status", headers)

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(
            bind_host="0.0.0.0",  # noqa: S104
            port=8123,
            allowed_hosts=["mcp.example.com", "proxy.example.com:*"],
            allow_origins=["https://browser.example"],
        ),
        capture_app,
    )

    def status(name: str) -> int:
        return next(
            message["status"]
            for message in results[name][0]
            if message["type"] == "http.response.start"
        )

    assert status("host-bare") == 200
    assert status("host-bare-extra-port") == 421
    assert status("host-wildcard-port") == 200
    assert status("host-wildcard-bind") == 421
    assert status("explicit-origin") == 200
    assert status("host-does-not-add-origin") == 403


@pytest.mark.asyncio
async def test_nonloopback_ipv6_bind_uses_bracketed_exact_authorities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw IPv6 bind adds bracketed bare and configured-port Host/HTTP Origin values."""
    results: dict[str, tuple[list[dict[str, Any]], int]] = {}

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        cases = {
            "bare-host": [(b"host", b"[2001:db8::1]")],
            "configured-port": [(b"host", b"[2001:db8::1]:8123")],
            "other-port": [(b"host", b"[2001:db8::1]:8124")],
            "zero-port": [(b"host", b"[2001:db8::1]:0")],
            "derived-origin": [
                (b"host", b"[2001:db8::1]:8123"),
                (b"origin", b"http://[2001:db8::1]:8123"),
            ],
            "other-origin-port": [
                (b"host", b"[2001:db8::1]:8123"),
                (b"origin", b"http://[2001:db8::1]:8124"),
            ],
        }
        for name, headers in cases.items():
            results[name] = await _request(app, "/status", headers)

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(bind_host="2001:db8::1", port=8123),
        capture_app,
    )

    def status(name: str) -> int:
        return next(
            message["status"]
            for message in results[name][0]
            if message["type"] == "http.response.start"
        )

    assert status("bare-host") == 200
    assert status("configured-port") == 200
    assert status("other-port") == 421
    assert status("zero-port") == 421
    assert status("derived-origin") == 200
    assert status("other-origin-port") == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("port", "host", "origin"),
    [
        (80, "example.com", "http://example.com"),
        (65535, "example.com:65535", "http://example.com:65535"),
    ],
)
async def test_nonloopback_bind_derives_default_and_maximum_port_origins(
    monkeypatch: pytest.MonkeyPatch,
    port: int,
    host: str,
    origin: str,
) -> None:
    """Concrete bind ports derive their exact HTTP Origin values."""
    results: list[tuple[list[dict[str, Any]], int]] = []

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        results.append(
            await _request(
                app,
                "/status",
                [(b"host", host.encode()), (b"origin", origin.encode())],
            )
        )

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(bind_host="example.com", port=port),
        capture_app,
    )

    starts = [message for message in results[0][0] if message["type"] == "http.response.start"]
    assert [message["status"] for message in starts] == [200]
    assert results[0][1] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed_hosts", "allow_origins"),
    [
        ([""], None),
        (["*"], None),
        (["*.example.com"], None),
        (["https://example.com"], None),
        (["example.com:"], None),
        (["example.com:0"], None),
        ([":8080"], None),
        (["example.com:65536"], None),
        (["example.com "], None),
        (["example.com\x01"], None),
        (["example.com\x7f"], None),
        (["ab@evil.example:*"], None),
        (None, [""]),
        (None, ["https://example.com/"]),
        (None, ["https://user@example.com"]),
        (None, ["https://example.com?token=x"]),
        (None, ["https://example.com?"]),
        (None, ["https://example.com#"]),
        (None, ["https://[::1"]),
        (None, ["https://example.com\x01"]),
        (None, ["ftp://example.com"]),
    ],
)
async def test_invalid_authority_configuration_fails_before_child_start(
    allowed_hosts: list[str] | None,
    allow_origins: list[str] | None,
) -> None:
    """Malformed policy never starts a backend process."""
    params = StdioServerParameters(command=sys.executable, args=["-c", _INITIALIZING_STDIO_SERVER])
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as stdio_client,
        pytest.raises(ValueError, match=r"Invalid allowed (Host|Origin)"),
    ):
        await run_mcp_server(
            MCPServerSettings(
                bind_host="127.0.0.1",
                port=0,
                allowed_hosts=allowed_hosts,
                allow_origins=allow_origins,
            ),
            params,
            {},
        )
    stdio_client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [-1, 65536])
async def test_invalid_server_port_fails_before_child_start(port: int) -> None:
    """Out-of-range ports fail before a backend starts."""
    params = StdioServerParameters(command=sys.executable, args=["-c", _INITIALIZING_STDIO_SERVER])
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as stdio_client,
        pytest.raises(ValueError, match="Server port"),
    ):
        await run_mcp_server(MCPServerSettings(bind_host="127.0.0.1", port=port), params, {})
    stdio_client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_host", ["example.com:80", "[::1]:80"])
async def test_bind_host_cannot_include_its_own_port(bind_host: str) -> None:
    """The bind host is a host only; its listener port comes from settings.port."""
    params = StdioServerParameters(command=sys.executable, args=["-c", _INITIALIZING_STDIO_SERVER])
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as stdio_client,
        pytest.raises(ValueError, match="Invalid server bind host"),
    ):
        await run_mcp_server(MCPServerSettings(bind_host=bind_host, port=8080), params, {})
    stdio_client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stateless", [False, True])
async def test_sdk_body_limit_rejects_declared_and_streamed_oversize_requests(
    monkeypatch: pytest.MonkeyPatch,
    stateless: bool,
) -> None:
    """SDK 1.30 bounds both POST transports for default/named stateful/stateless routes."""
    results: dict[str, tuple[list[dict[str, Any]], int]] = {}
    payload = b"x" * (4 * 1024 * 1024 + 1)

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        paths = (
            "/mcp/",
            "/servers/named/mcp/",
            "/messages/",
            "/servers/named/messages/",
        )
        for path in paths:
            for kind in ("declared", "streamed"):
                headers = [(b"host", b"localhost"), (b"content-type", b"application/json")]
                body = b"{}"
                if kind == "declared":
                    headers.append((b"content-length", str(len(payload)).encode()))
                else:
                    body = payload
                results[f"{kind} {path}"] = await _request(
                    app,
                    path,
                    headers,
                    method="POST",
                    body=body,
                )
        results["small valid-sized body"] = await _request(
            app,
            "/mcp/",
            [
                (b"host", b"localhost"),
                (b"content-type", b"application/json"),
                (b"content-length", b"2"),
            ],
            method="POST",
            body=b"{}",
        )

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(bind_host="127.0.0.1", port=0, stateless=stateless),
        capture_app,
    )

    for key, (messages, body_reads) in results.items():
        status = next(
            message["status"] for message in messages if message["type"] == "http.response.start"
        )
        if key.startswith("declared"):
            assert status == 413, key
            assert body_reads == 0, key
        elif key.startswith("streamed"):
            assert status == 413, key
            assert body_reads == 1, key
        else:
            assert status != 413


@pytest.mark.asyncio
async def test_run_mcp_server_rejects_requests_without_bearer_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production app rejects unauthenticated status, health, and MCP routes."""
    responses: dict[str, tuple[list[dict[str, Any]], int]] = {}
    settings = MCPServerSettings(
        bind_host="127.0.0.1",
        port=0,
        auth_bearer_token=_AUTH_CONFIG_TEST_VALUE,
    )

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        for path, method, stop_after_start in (
            ("/status", "GET", False),
            ("/healthz", "GET", False),
            ("/mcp", "POST", False),
            ("/mcp/", "GET", True),
            ("/mcp/", "DELETE", False),
            ("/servers/named/healthz", "GET", False),
            ("/servers/named/mcp/", "POST", False),
            ("/sse", "GET", True),
            ("/servers/named/sse", "GET", True),
            ("/messages/", "POST", False),
            ("/servers/named/messages/", "POST", False),
            ("/healthz", "HEAD", False),
        ):
            responses[f"{method} {path}"] = await _request(
                app,
                path,
                [
                    (b"host", b"localhost"),
                    *([(b"content-type", b"application/json")] if method == "POST" else []),
                ],
                method=method,
                stop_after_start=stop_after_start,
            )

    await _run_production_server(monkeypatch, settings, capture_app)

    for request, (messages, body_reads) in responses.items():
        status = next(
            message["status"] for message in messages if message["type"] == "http.response.start"
        )
        assert status == 401, request
        assert body_reads == 0, request
        response_start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        challenge = dict(response_start["headers"])[b"www-authenticate"]
        assert challenge == b'Bearer realm="mcp-proxy"', request
        if request.startswith("HEAD"):
            response_body = next(
                message["body"] for message in messages if message["type"] == "http.response.body"
            )
            assert response_body == b""


@pytest.mark.asyncio
async def test_bearer_headers_are_strict_and_host_origin_guards_remain_outermost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one exact Bearer credential passes; existing authority checks still reject."""
    cases: dict[str, tuple[list[tuple[bytes, bytes]], bytes, int]] = {
        "missing": ([(b"host", b"localhost")], b"", 401),
        "empty-credential": (
            [(b"host", b"localhost"), (b"authorization", b"Bearer ")],
            b"",
            401,
        ),
        "mixed-case-valid": (
            [(b"host", b"localhost"), (b"AuthorIZATION", b"bEaReR  a+/._~-Z==")],
            b"",
            200,
        ),
        "duplicate": (
            [
                (b"host", b"localhost"),
                (b"authorization", b"Bearer a+/._~-Z=="),
                (b"Authorization", b"Bearer a+/._~-Z=="),
            ],
            b"",
            401,
        ),
        "combined": (
            [(b"host", b"localhost"), (b"authorization", b"Bearer a+/._~-Z==, Bearer a+/._~-Z==")],
            b"",
            401,
        ),
        "wrong": (
            [(b"host", b"localhost"), (b"authorization", b"Bearer wrong")],
            b"",
            401,
        ),
        "tab-separator": (
            [(b"host", b"localhost"), (b"authorization", b"Bearer\ta+/._~-Z==")],
            b"",
            401,
        ),
        "trailing-space": (
            [(b"host", b"localhost"), (b"authorization", b"Bearer a+/._~-Z== ")],
            b"",
            401,
        ),
        "non-ascii": (
            [(b"host", b"localhost"), (b"authorization", b"Bearer \xff")],
            b"",
            401,
        ),
        "basic": (
            [(b"host", b"localhost"), (b"authorization", b"Basic a+/._~-Z==")],
            b"",
            401,
        ),
        "query-only": ([(b"host", b"localhost")], b"access_token=a+/._~-Z==", 401),
        "cookie-only": (
            [(b"host", b"localhost"), (b"cookie", b"access_token=a+/._~-Z==")],
            b"",
            401,
        ),
        "valid-token-bad-host": (
            [(b"host", b"attacker.example"), (b"authorization", b"Bearer a+/._~-Z==")],
            b"",
            421,
        ),
        "valid-token-bad-origin": (
            [
                (b"host", b"localhost"),
                (b"origin", b"https://attacker.example"),
                (b"authorization", b"Bearer a+/._~-Z=="),
            ],
            b"",
            403,
        ),
    }
    results: dict[str, tuple[list[dict[str, Any]], int]] = {}

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        for name, (headers, query_string, _) in cases.items():
            results[name] = await _request(app, "/status", headers, query_string=query_string)

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(
            bind_host="127.0.0.1",
            port=0,
            auth_bearer_token=_AUTH_VALID_TEST_VALUE,
        ),
        capture_app,
    )

    for name, (messages, body_reads) in results.items():
        start = next(message for message in messages if message["type"] == "http.response.start")
        assert start["status"] == cases[name][2], name
        if start["status"] == 401:
            assert body_reads == 0, name
            assert dict(start["headers"])[b"www-authenticate"] == b'Bearer realm="mcp-proxy"'


@pytest.mark.asyncio
async def test_bearer_gate_preserves_cors_preflight_and_cors_headers_on_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only CORS preflight skips auth; ordinary unauthorized requests keep CORS headers."""
    results: dict[str, tuple[list[dict[str, Any]], int]] = {}

    async def capture_app(app: Any) -> None:  # noqa: ANN401
        common = [(b"host", b"localhost"), (b"origin", b"https://browser.example")]
        results["preflight"] = await _request(
            app,
            "/status",
            [
                *common,
                (b"access-control-request-method", b"GET"),
                (b"access-control-request-headers", b"authorization"),
            ],
            method="OPTIONS",
        )
        results["ordinary-options"] = await _request(app, "/status", common, method="OPTIONS")
        results["unauthorized-get"] = await _request(app, "/status", common)

    await _run_production_server(
        monkeypatch,
        MCPServerSettings(
            bind_host="127.0.0.1",
            port=0,
            allow_origins=["https://browser.example"],
            auth_bearer_token=_AUTH_CONFIG_TEST_VALUE,
        ),
        capture_app,
    )

    preflight = results["preflight"][0][0]
    assert preflight["status"] == 200
    assert dict(preflight["headers"])[b"access-control-allow-origin"] == b"https://browser.example"
    for name in ("ordinary-options", "unauthorized-get"):
        start = results[name][0][0]
        headers = dict(start["headers"])
        assert start["status"] == 401, name
        assert headers[b"www-authenticate"] == b'Bearer realm="mcp-proxy"'
        assert headers[b"access-control-allow-origin"] == b"https://browser.example"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "bad token", "bad=token", "bad\tvalue", "tøkën"])
async def test_invalid_bearer_configuration_fails_before_backend_spawn(token: str) -> None:
    """Invalid credentials fail at the server boundary without exposing their value."""
    params = StdioServerParameters(command=sys.executable, args=["-c", _INITIALIZING_STDIO_SERVER])
    error_message = "auth_bearer_token must be a valid non-empty RFC 6750 b64token"
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as stdio_client,
        pytest.raises(ValueError, match=error_message) as error,
    ):
        await run_mcp_server(
            MCPServerSettings(bind_host="127.0.0.1", port=0, auth_bearer_token=token),
            params,
            {},
        )
    assert not token or token not in str(error.value)
    stdio_client.assert_not_called()
