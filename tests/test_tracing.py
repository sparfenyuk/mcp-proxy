"""Tests for OpenTelemetry tracing module."""

import os
from typing import NoReturn
from unittest.mock import MagicMock, patch

import pytest

from mcp_proxy.tracing import reset_tracer, trace_tool_call


@pytest.fixture(autouse=True)
def _cleanup():
    """Reset tracer state between tests."""
    reset_tracer()
    yield
    reset_tracer()


class TestTraceToolCallNoOp:
    """Test tracing when OTEL is not configured (no-op behavior)."""

    def test_noop_when_no_endpoint(self) -> None:
        """Without OTEL_EXPORTER_OTLP_ENDPOINT, tracing is a no-op."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove the env var if present
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            with trace_tool_call("test-server", "test-tool") as attrs:
                attrs["status"] = "ok"
        # Should not raise, just pass through

    def test_noop_yields_dict(self) -> None:
        """No-op context manager yields a mutable dict."""
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        with trace_tool_call("server", "tool") as attrs:
            assert isinstance(attrs, dict)
            assert attrs["status"] == "ok"
            attrs["status"] = "error"

    def test_noop_when_import_fails(self) -> None:
        """When otel packages not installed, gracefully falls back to no-op."""
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://localhost:4317"
        try:
            with patch.dict("sys.modules", {"opentelemetry": None}):
                reset_tracer()
                with trace_tool_call("server", "tool") as attrs:
                    attrs["status"] = "ok"
        finally:
            del os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]


class TestTraceToolCallWithOtel:
    """Test tracing when OTEL is configured (mocked)."""

    def test_span_created_with_attributes(self) -> None:
        """When tracer is available, span is created with correct attributes."""
        mock_span = MagicMock()
        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=mock_span)
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

        # Inject mock tracer directly
        import mcp_proxy.tracing as tracing_mod

        tracing_mod._tracer = mock_tracer
        tracing_mod._initialized = True

        with trace_tool_call("my-server", "my-tool") as attrs:
            attrs["status"] = "ok"

        mock_tracer.start_as_current_span.assert_called_once_with("call_tool/my-tool")
        mock_span.set_attribute.assert_any_call("server.name", "my-server")
        mock_span.set_attribute.assert_any_call("tool.name", "my-tool")
        mock_span.set_attribute.assert_any_call("status", "ok")
        # latency_ms should also be set
        latency_calls = [
            c for c in mock_span.set_attribute.call_args_list if c[0][0] == "latency_ms"
        ]
        assert len(latency_calls) == 1
        assert latency_calls[0][0][1] >= 0  # latency should be non-negative

    def test_span_records_error_status(self) -> None:
        """When status is error, span records error."""
        mock_span = MagicMock()
        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=mock_span)
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

        import mcp_proxy.tracing as tracing_mod

        tracing_mod._tracer = mock_tracer
        tracing_mod._initialized = True

        with trace_tool_call("srv", "tool") as attrs:
            attrs["status"] = "error"
            attrs["error_message"] = "connection refused"

        mock_span.set_attribute.assert_any_call("status", "error")
        # set_status should be called for errors
        mock_span.set_status.assert_called_once()

    def test_span_handles_exception_in_body(self) -> NoReturn:
        """Span still records attributes even if body raises."""
        mock_span = MagicMock()
        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=mock_span)
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)

        import mcp_proxy.tracing as tracing_mod

        tracing_mod._tracer = mock_tracer
        tracing_mod._initialized = True

        with pytest.raises(ValueError, match="test error"):
            with trace_tool_call("srv", "tool") as attrs:
                attrs["status"] = "error"
                raise ValueError("test error")

        # Attributes should still be set in finally block
        mock_span.set_attribute.assert_any_call("status", "error")
