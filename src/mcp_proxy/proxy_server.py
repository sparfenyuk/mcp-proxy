"""Create an MCP server that proxies requests through an MCP client.

This server is created independent of any transport mechanism.
"""

import asyncio
import logging
import typing as t

import httpx

from mcp import server, types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError

logger = logging.getLogger(__name__)


def _iter_exceptions(exc: BaseException) -> t.Iterator[BaseException]:
    """Yield all nested exceptions (including ExceptionGroup leaves)."""
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _iter_exceptions(inner)
        return

    yield exc

    # Walk common chaining mechanisms. Avoid infinite recursion via self-references.
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException) and cause is not exc:
        yield from _iter_exceptions(cause)

    context = getattr(exc, "__context__", None)
    if isinstance(context, BaseException) and context is not exc and context is not cause:
        yield from _iter_exceptions(context)


async def create_proxy_server(remote_app: ClientSession) -> server.Server[object]:  # noqa: C901, PLR0915
    """Create a server instance from a remote app."""
    logger.debug("Sending initialization request to remote MCP server...")
    response = await remote_app.initialize()
    capabilities = response.capabilities

    max_attempts = getattr(remote_app, "_retry_attempts", 0)
    max_attempts = 1 + max(0, max_attempts)

    semaphore = getattr(remote_app, "_proxy_semaphore", None)
    if semaphore is None:
        # Avoid thundering-herd behavior when Codex issues many concurrent reads.
        semaphore = asyncio.Semaphore(getattr(remote_app, "_proxy_max_inflight", 8))
        remote_app._proxy_semaphore = semaphore  # type: ignore[attr-defined]

    error_queue = getattr(remote_app, "_http_error_queue", None)
    call_timeout_s = getattr(remote_app, "_proxy_call_timeout_s", None)

    async def _drain_error_queue() -> None:
        if error_queue is None:
            return
        try:
            while True:
                error_queue.get_nowait()
        except asyncio.QueueEmpty:
            return

    async def _cancel_task(task: asyncio.Task[t.Any]) -> None:
        if task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    async def _await_remote_call(
        call: t.Callable[[], t.Awaitable[t.Any]],
        *,
        label: str,
    ) -> t.Any:
        await _drain_error_queue()

        async def _run() -> t.Any:
            return await call()

        if error_queue is None and call_timeout_s is None:
            return await _run()

        call_task = asyncio.create_task(_run())
        queue_task: asyncio.Task[httpx.HTTPStatusError] | None = None
        if error_queue is not None:
            queue_task = asyncio.create_task(error_queue.get())

        tasks = [call_task]
        if queue_task is not None:
            tasks.append(queue_task)

        done, pending = await asyncio.wait(
            tasks,
            timeout=call_timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            await _cancel_task(call_task)
            if queue_task is not None:
                await _cancel_task(queue_task)
            raise TimeoutError(f"{label} timed out after {call_timeout_s}s")

        if queue_task is not None and queue_task in done:
            err = queue_task.result()
            await _cancel_task(call_task)
            raise err

        if queue_task is not None:
            await _cancel_task(queue_task)

        return call_task.result()

    def _is_retryable_status(status: int | None) -> bool:
        if status is None:
            return False
        return 400 <= status < 500 or status == 503

    def _session_error_in_exception(err: BaseException) -> bool:
        for leaf in _iter_exceptions(err):
            if isinstance(leaf, McpError):
                code = getattr(leaf.error, "code", None)
                msg = getattr(leaf.error, "message", "") or ""
                if code == -32001:
                    return True
                if code == 32600 and "Session terminated" in msg:
                    return True
            text = str(leaf)
            if "Session not found" in text or "-32001" in text:
                return True
            if "Session terminated" in text:
                return True
        return False

    async def _call_remote(
        label: str,
        call: t.Callable[[], t.Awaitable[t.Any]],
    ) -> t.Any:
        attempts = 0
        backoff_s = 0.5

        while True:
            try:
                async with semaphore:
                    return await _await_remote_call(call, label=label)
            except httpx.HTTPStatusError as exc:
                attempts += 1
                status = exc.response.status_code if exc.response else None
                if not _is_retryable_status(status) or attempts >= max_attempts:
                    raise
                logger.warning(
                    "%s got HTTP %s; re-initializing session (%s/%s)",
                    label,
                    status,
                    attempts,
                    max_attempts - 1,
                )
                await remote_app.initialize()
                await asyncio.sleep(backoff_s)
                backoff_s = min(5.0, backoff_s * 2)
                continue
            except (httpx.TimeoutException, httpx.NetworkError, TimeoutError) as exc:
                attempts += 1
                if attempts >= max_attempts:
                    raise
                logger.warning(
                    "%s got network/timeout error; re-initializing session (%s/%s); error=%s",
                    label,
                    attempts,
                    max_attempts - 1,
                    exc,
                )
                await remote_app.initialize()
                await asyncio.sleep(backoff_s)
                backoff_s = min(5.0, backoff_s * 2)
                continue
            except Exception as exc:  # noqa: BLE001
                if _session_error_in_exception(exc) and attempts + 1 < max_attempts:
                    attempts += 1
                    logger.warning(
                        "%s got session error; re-initializing session (%s/%s)",
                        label,
                        attempts,
                        max_attempts - 1,
                    )
                    await remote_app.initialize()
                    await asyncio.sleep(backoff_s)
                    backoff_s = min(5.0, backoff_s * 2)
                    continue
                raise

    async def _call_remote_once(label: str, call: t.Callable[[], t.Awaitable[t.Any]]) -> t.Any:
        async with semaphore:
            return await _await_remote_call(call, label=label)

    logger.debug("Configuring proxied MCP server...")
    app: server.Server[object] = server.Server(name=response.serverInfo.name)

    if capabilities.prompts:
        logger.debug("Capabilities: adding Prompts...")

        async def _list_prompts(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            result = await _call_remote("ListPrompts", remote_app.list_prompts)
            return types.ServerResult(result)

        app.request_handlers[types.ListPromptsRequest] = _list_prompts

        async def _get_prompt(req: types.GetPromptRequest) -> types.ServerResult:
            result = await _call_remote(
                f"GetPrompt {req.params.name}",
                lambda: remote_app.get_prompt(req.params.name, req.params.arguments),
            )
            return types.ServerResult(result)

        app.request_handlers[types.GetPromptRequest] = _get_prompt

    if capabilities.resources:
        logger.debug("Capabilities: adding Resources...")

        async def _list_resources(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            result = await _call_remote("ListResources", remote_app.list_resources)
            return types.ServerResult(result)

        app.request_handlers[types.ListResourcesRequest] = _list_resources

        async def _list_resource_templates(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            result = await _call_remote("ListResourceTemplates", remote_app.list_resource_templates)
            return types.ServerResult(result)

        app.request_handlers[types.ListResourceTemplatesRequest] = _list_resource_templates

        async def _read_resource(req: types.ReadResourceRequest) -> types.ServerResult:
            result = await _call_remote(
                f"ReadResource {req.params.uri}",
                lambda: remote_app.read_resource(req.params.uri),
            )
            return types.ServerResult(result)

        app.request_handlers[types.ReadResourceRequest] = _read_resource

    if capabilities.logging:
        logger.debug("Capabilities: adding Logging...")

        async def _set_logging_level(req: types.SetLevelRequest) -> types.ServerResult:
            await _call_remote(
                f"SetLoggingLevel {req.params.level}",
                lambda: remote_app.set_logging_level(req.params.level),
            )
            return types.ServerResult(types.EmptyResult())

        app.request_handlers[types.SetLevelRequest] = _set_logging_level

    if capabilities.resources:
        logger.debug("Capabilities: adding Resources...")

        async def _subscribe_resource(req: types.SubscribeRequest) -> types.ServerResult:
            await _call_remote(
                f"SubscribeResource {req.params.uri}",
                lambda: remote_app.subscribe_resource(req.params.uri),
            )
            return types.ServerResult(types.EmptyResult())

        app.request_handlers[types.SubscribeRequest] = _subscribe_resource

        async def _unsubscribe_resource(req: types.UnsubscribeRequest) -> types.ServerResult:
            await _call_remote(
                f"UnsubscribeResource {req.params.uri}",
                lambda: remote_app.unsubscribe_resource(req.params.uri),
            )
            return types.ServerResult(types.EmptyResult())

        app.request_handlers[types.UnsubscribeRequest] = _unsubscribe_resource

    if capabilities.tools:
        logger.debug("Capabilities: adding Tools...")

        async def _list_tools(_: t.Any) -> types.ServerResult:  # noqa: ANN401
            tools = await _call_remote("ListTools", remote_app.list_tools)
            return types.ServerResult(tools)

        app.request_handlers[types.ListToolsRequest] = _list_tools

        async def _call_tool(req: types.CallToolRequest) -> types.ServerResult:
            attempts = 0
            backoff_s = 0.5

            def _retryable_status_in_error(err: BaseException) -> int | None:
                for leaf in _iter_exceptions(err):
                    if isinstance(leaf, httpx.HTTPStatusError):
                        status = leaf.response.status_code if leaf.response else None
                        if _is_retryable_status(status):
                            return status
                return None

            def _is_session_error_result(result: types.CallToolResult) -> bool:
                if not result.isError:
                    return False
                for item in result.content or []:
                    if getattr(item, "type", None) != "text":
                        continue
                    text = getattr(item, "text", "") or ""
                    if "Session not found" in text or "-32001" in text:
                        return True
                    if "Session terminated" in text:
                        return True
                return False

            try:
                while attempts < max_attempts:
                    try:
                        result = await _call_remote_once(
                            f"CallTool {req.params.name}",
                            lambda: remote_app.call_tool(req.params.name, (req.params.arguments or {})),
                        )
                        if _is_session_error_result(result) and attempts + 1 < max_attempts:
                            attempts += 1
                            logger.warning(
                                "CallTool %s got session error result; re-initializing session (%s/%s)",
                                req.params.name,
                                attempts,
                                max_attempts - 1,
                            )
                            await remote_app.initialize()
                            await asyncio.sleep(backoff_s)
                            backoff_s = min(5.0, backoff_s * 2)
                            continue
                        return types.ServerResult(result)
                    except httpx.HTTPStatusError as exc:
                        attempts += 1
                        status = exc.response.status_code if exc.response else None
                        if not _is_retryable_status(status) or attempts >= max_attempts:
                            raise
                        logger.warning(
                            "CallTool %s got HTTP %s; re-initializing session (%s/%s)",
                            req.params.name,
                            status,
                            attempts,
                            max_attempts - 1,
                        )
                        await remote_app.initialize()
                        await asyncio.sleep(backoff_s)
                        backoff_s = min(5.0, backoff_s * 2)
                        continue
                    except (httpx.TimeoutException, httpx.NetworkError, TimeoutError) as exc:
                        attempts += 1
                        if attempts >= max_attempts:
                            raise
                        logger.warning(
                            "CallTool %s got network/timeout error; re-initializing session (%s/%s); error=%s",
                            req.params.name,
                            attempts,
                            max_attempts - 1,
                            exc,
                        )
                        await remote_app.initialize()
                        await asyncio.sleep(backoff_s)
                        backoff_s = min(5.0, backoff_s * 2)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        status = _retryable_status_in_error(exc)
                        if status is not None and attempts + 1 < max_attempts:
                            attempts += 1
                            logger.warning(
                                "CallTool %s got HTTP %s (wrapped); re-initializing session (%s/%s)",
                                req.params.name,
                                status,
                                attempts,
                                max_attempts - 1,
                            )
                            await remote_app.initialize()
                            await asyncio.sleep(backoff_s)
                            backoff_s = min(5.0, backoff_s * 2)
                            continue

                        # Handle JSON-RPC session errors that come back as 200 with an error payload
                        if _session_error_in_exception(exc) and attempts + 1 < max_attempts:
                            attempts += 1
                            logger.warning(
                                "CallTool %s got session error; re-initializing session (%s/%s)",
                                req.params.name,
                                attempts,
                                max_attempts - 1,
                            )
                            await remote_app.initialize()
                            await asyncio.sleep(backoff_s)
                            backoff_s = min(5.0, backoff_s * 2)
                            continue
                        raise
            except Exception as e:  # noqa: BLE001
                return types.ServerResult(
                    types.CallToolResult(
                        content=[types.TextContent(type="text", text=str(e))],
                        isError=True,
                    ),
                )

        app.request_handlers[types.CallToolRequest] = _call_tool

    async def _send_progress_notification(req: types.ProgressNotification) -> None:
        await remote_app.send_progress_notification(
            req.params.progressToken,
            req.params.progress,
            req.params.total,
        )

    app.notification_handlers[types.ProgressNotification] = _send_progress_notification

    async def _complete(req: types.CompleteRequest) -> types.ServerResult:
        result = await _call_remote(
            f"Complete {req.params.ref.type}/{req.params.ref.name}",
            lambda: remote_app.complete(req.params.ref, req.params.argument.model_dump()),
        )
        return types.ServerResult(result)

    app.request_handlers[types.CompleteRequest] = _complete

    return app
