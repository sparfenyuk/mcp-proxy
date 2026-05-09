"""REST-to-MCP Adapter: generates MCP tools from OpenAPI specs."""

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


def parse_openapi_spec(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse OpenAPI spec into a list of tool definitions.

    Each tool has: name (operationId), description, method, path, parameters, request_body_schema.
    """
    tools: list[dict[str, Any]] = []
    paths = spec.get("paths", {})

    for path, path_item in paths.items():
        for method in ("get", "post", "put", "delete", "patch"):
            operation = path_item.get(method)
            if not operation:
                continue

            operation_id = operation.get("operationId")
            if not operation_id:
                # Generate from method + path
                operation_id = f"{method}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"

            params = []
            for p in operation.get("parameters", []):
                params.append(
                    {
                        "name": p["name"],
                        "in": p.get("in", "query"),
                        "required": p.get("required", False),
                        "schema": p.get("schema", {"type": "string"}),
                        "description": p.get("description", ""),
                    }
                )

            # Request body (OpenAPI 3.x)
            request_body_schema = None
            rb = operation.get("requestBody")
            if rb:
                content = rb.get("content", {})
                json_content = content.get("application/json", {})
                request_body_schema = json_content.get("schema")

            # Swagger 2.x body parameter
            if not request_body_schema:
                for p in operation.get("parameters", []):
                    if p.get("in") == "body":
                        request_body_schema = p.get("schema")
                        break

            tools.append(
                {
                    "name": operation_id,
                    "description": operation.get("summary", operation.get("description", "")),
                    "method": method.upper(),
                    "path": path,
                    "parameters": params,
                    "request_body_schema": request_body_schema,
                }
            )

    return tools


def tool_to_mcp_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert a parsed tool definition to MCP tool input schema."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param in tool["parameters"]:
        if param["in"] == "body":
            continue  # Handled separately
        prop = dict(param.get("schema", {"type": "string"}))
        if param.get("description"):
            prop["description"] = param["description"]
        properties[param["name"]] = prop
        if param.get("required"):
            required.append(param["name"])

    if tool["request_body_schema"]:
        properties["body"] = tool["request_body_schema"]
        properties["body"]["description"] = "Request body (JSON)"

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


class RestToMcpAdapter:
    """Adapter that exposes REST API endpoints as MCP tools."""

    def __init__(
        self,
        base_url: str,
        spec: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = headers or {}
        self._tools = parse_openapi_spec(spec)
        self._tool_map = {t["name"]: t for t in self._tools}
        self._client = httpx.AsyncClient(headers=self._headers, timeout=30.0)
        logger.info(
            "REST adapter initialized: %s (%d tools)",
            base_url,
            len(self._tools),
        )

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Return MCP-compatible tool list."""
        return [
            {
                "name": t["name"],
                "description": t["description"] or f"{t['method']} {t['path']}",
                "inputSchema": tool_to_mcp_schema(t),
            }
            for t in self._tools
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a tool call by making the corresponding HTTP request."""
        tool = self._tool_map.get(name)
        if not tool:
            return {"error": f"Tool '{name}' not found"}

        # Build URL with path parameters
        path = tool["path"]
        query_params: dict[str, str] = {}
        headers = {**self._headers, **(extra_headers or {})}

        for param in tool["parameters"]:
            value = arguments.get(param["name"])
            if value is None:
                continue
            if param["in"] == "path":
                path = path.replace(f"{{{param['name']}}}", quote(str(value), safe=""))
            elif param["in"] == "query":
                query_params[param["name"]] = str(value)
            elif param["in"] == "header":
                headers[param["name"]] = str(value)

        url = f"{self._base_url}{path}"
        body = arguments.get("body")

        try:
            resp = await self._client.request(
                method=tool["method"],
                url=url,
                params=query_params or None,
                json=body,
                headers=headers,
            )
        except httpx.RequestError as e:
            return {"error": f"Network error calling {tool['method']} {url}: {e}"}

        # Return response
        try:
            response_data = resp.json()
        except (json.JSONDecodeError, ValueError):
            response_data = resp.text

        return {
            "status_code": resp.status_code,
            "data": response_data,
        }

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


async def load_spec_from_url(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Fetch OpenAPI spec from URL."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(url, headers=headers or {}, timeout=30.0)
            resp.raise_for_status()
            return resp.json()  # type: ignore
        except httpx.RequestError as e:
            msg = f"Failed to fetch OpenAPI spec from {url}: {e}"
            raise RuntimeError(msg) from e
