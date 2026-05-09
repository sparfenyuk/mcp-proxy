"""Tests for the HTML dashboard."""

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp_proxy.dashboard import DASHBOARD_HTML, create_dashboard_route


class TestDashboard:
    def _make_app(self):
        routes = create_dashboard_route()
        return Starlette(routes=routes)

    def test_dashboard_returns_html(self) -> None:
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_dashboard_contains_title(self) -> None:
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert "MCP Proxy Dashboard" in resp.text

    def test_dashboard_has_auto_refresh(self) -> None:
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert "setInterval(refresh,5000)" in resp.text

    def test_dashboard_fetches_status(self) -> None:
        app = self._make_app()
        client = TestClient(app)
        resp = client.get("/dashboard")
        assert "fetch('/status')" in resp.text

    def test_dashboard_no_external_deps(self) -> None:
        # Ensure no external CSS/JS links
        assert "http://" not in DASHBOARD_HTML
        assert "https://" not in DASHBOARD_HTML
        assert "<link" not in DASHBOARD_HTML
        assert "<script src" not in DASHBOARD_HTML

    def test_dashboard_inline_css(self) -> None:
        assert "<style>" in DASHBOARD_HTML
        assert "</style>" in DASHBOARD_HTML
