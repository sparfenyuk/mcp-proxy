"""Optional OpenTelemetry tracing for MCP proxy tool calls.

Zero overhead when OTEL_EXPORTER_OTLP_ENDPOINT is not set or otel packages not installed.
"""

import logging
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Lazy-initialized tracer
_tracer: Any = None
_initialized: bool = False


def _init_tracer() -> Any:
    """Initialize OpenTelemetry tracer if configured. Returns None if not available."""
    global _tracer, _initialized  # noqa: PLW0603
    if _initialized:
        return _tracer
    _initialized = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None

    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter  # type: ignore
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore

        resource = Resource.create({"service.name": "mcp-proxy"})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("mcp-proxy")
        logger.info("OpenTelemetry tracing enabled, exporting to %s", endpoint)
    except ImportError:
        logger.debug(
            "OpenTelemetry packages not installed. Install with: pip install mcp-proxy-plus[otel]"
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to initialize OpenTelemetry")

    return _tracer


@contextmanager
def trace_tool_call(server_name: str, tool_name: str) -> Generator[dict[str, Any], None, None]:
    """Context manager that traces a tool call. No-op if OTEL not configured.

    Yields a dict where caller can set 'status' before exiting.
    """
    tracer = _init_tracer()
    attrs: dict[str, Any] = {"status": "ok"}

    if tracer is None:
        yield attrs
        return

    start = time.monotonic()
    with tracer.start_as_current_span(f"call_tool/{tool_name}") as span:
        span.set_attribute("server.name", server_name)
        span.set_attribute("tool.name", tool_name)
        try:
            yield attrs
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            span.set_attribute("latency_ms", latency_ms)
            span.set_attribute("status", attrs.get("status", "ok"))
            if attrs.get("status") == "error":
                span.set_status(
                    _get_status_code_error(),
                    attrs.get("error_message", ""),
                )


def _get_status_code_error() -> Any:
    """Get StatusCode.ERROR safely."""
    try:
        from opentelemetry.trace import StatusCode  # type: ignore

        return StatusCode.ERROR
    except ImportError:
        return None


def reset_tracer() -> None:
    """Reset tracer state (for testing)."""
    global _tracer, _initialized  # noqa: PLW0603
    _tracer = None
    _initialized = False
