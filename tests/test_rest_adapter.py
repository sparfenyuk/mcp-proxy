"""Tests for REST-to-MCP adapter."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_proxy.rest_adapter import (
    RestToMcpAdapter,
    load_spec_from_url,
    parse_openapi_spec,
    tool_to_mcp_schema,
)

SAMPLE_OPENAPI_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Pet Store", "version": "1.0.0"},
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List all pets",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                        "description": "Max items to return",
                    }
                ],
            },
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "tag": {"type": "string"},
                                },
                            }
                        }
                    }
                },
            },
        },
        "/pets/{petId}": {
            "get": {
                "operationId": "getPet",
                "summary": "Get a pet by ID",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            },
            "delete": {
                "operationId": "deletePet",
                "summary": "Delete a pet",
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
            },
        },
    },
}

SWAGGER2_SPEC = {
    "swagger": "2.0",
    "info": {"title": "Legacy API", "version": "1.0"},
    "paths": {
        "/items": {
            "post": {
                "operationId": "createItem",
                "summary": "Create item",
                "parameters": [
                    {
                        "name": "body",
                        "in": "body",
                        "schema": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                        },
                    }
                ],
            }
        }
    },
}


class TestParseOpenAPISpec:
    def test_parse_basic_spec(self) -> None:
        tools = parse_openapi_spec(SAMPLE_OPENAPI_SPEC)
        assert len(tools) == 4
        names = [t["name"] for t in tools]
        assert "listPets" in names
        assert "createPet" in names
        assert "getPet" in names
        assert "deletePet" in names

    def test_parse_method_and_path(self) -> None:
        tools = parse_openapi_spec(SAMPLE_OPENAPI_SPEC)
        tool_map = {t["name"]: t for t in tools}
        assert tool_map["listPets"]["method"] == "GET"
        assert tool_map["listPets"]["path"] == "/pets"
        assert tool_map["createPet"]["method"] == "POST"
        assert tool_map["deletePet"]["method"] == "DELETE"

    def test_parse_parameters(self) -> None:
        tools = parse_openapi_spec(SAMPLE_OPENAPI_SPEC)
        tool_map = {t["name"]: t for t in tools}
        params = tool_map["listPets"]["parameters"]
        assert len(params) == 1
        assert params[0]["name"] == "limit"
        assert params[0]["in"] == "query"

    def test_parse_path_parameters(self) -> None:
        tools = parse_openapi_spec(SAMPLE_OPENAPI_SPEC)
        tool_map = {t["name"]: t for t in tools}
        params = tool_map["getPet"]["parameters"]
        assert params[0]["in"] == "path"
        assert params[0]["required"] is True

    def test_parse_request_body(self) -> None:
        tools = parse_openapi_spec(SAMPLE_OPENAPI_SPEC)
        tool_map = {t["name"]: t for t in tools}
        schema = tool_map["createPet"]["request_body_schema"]
        assert schema is not None
        assert schema["type"] == "object"
        assert "name" in schema["properties"]

    def test_parse_swagger2_body(self) -> None:
        tools = parse_openapi_spec(SWAGGER2_SPEC)
        assert len(tools) == 1
        assert tools[0]["request_body_schema"]["type"] == "object"

    def test_parse_generates_operation_id(self) -> None:
        spec = {
            "paths": {
                "/users/{id}": {
                    "get": {"summary": "Get user"},
                }
            }
        }
        tools = parse_openapi_spec(spec)
        assert tools[0]["name"] == "get_users_id"

    def test_parse_empty_spec(self) -> None:
        tools = parse_openapi_spec({})
        assert tools == []


class TestToolToMcpSchema:
    def test_basic_schema(self) -> None:
        tool = {
            "parameters": [
                {
                    "name": "limit",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer"},
                    "description": "Max",
                },
            ],
            "request_body_schema": None,
        }
        schema = tool_to_mcp_schema(tool)
        assert schema["type"] == "object"
        assert "limit" in schema["properties"]
        assert schema["properties"]["limit"]["type"] == "integer"

    def test_required_params(self) -> None:
        tool = {
            "parameters": [
                {
                    "name": "id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "",
                },
            ],
            "request_body_schema": None,
        }
        schema = tool_to_mcp_schema(tool)
        assert "id" in schema["required"]

    def test_with_request_body(self) -> None:
        tool = {
            "parameters": [],
            "request_body_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
        }
        schema = tool_to_mcp_schema(tool)
        assert "body" in schema["properties"]


class TestRestToMcpAdapter:
    def test_adapter_tools_list(self) -> None:
        adapter = RestToMcpAdapter("https://api.example.com", SAMPLE_OPENAPI_SPEC)
        tools = adapter.tools
        assert len(tools) == 4
        assert all("name" in t and "inputSchema" in t for t in tools)

    @pytest.mark.asyncio
    async def test_call_tool_get(self) -> None:
        adapter = RestToMcpAdapter("https://api.example.com", SAMPLE_OPENAPI_SPEC)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": 1, "name": "Fido"}]

        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_response):
            result = await adapter.call_tool("listPets", {"limit": 10})

        assert result["status_code"] == 200
        assert result["data"] == [{"id": 1, "name": "Fido"}]

    @pytest.mark.asyncio
    async def test_call_tool_path_param(self) -> None:
        adapter = RestToMcpAdapter("https://api.example.com", SAMPLE_OPENAPI_SPEC)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "123", "name": "Rex"}

        with patch(
            "httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_response
        ) as mock_req:
            await adapter.call_tool("getPet", {"petId": "123"})
            # Verify path parameter substitution
            call_kwargs = mock_req.call_args
            assert "/pets/123" in call_kwargs.kwargs["url"]

    @pytest.mark.asyncio
    async def test_call_tool_post_with_body(self) -> None:
        adapter = RestToMcpAdapter("https://api.example.com", SAMPLE_OPENAPI_SPEC)

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": 1, "name": "Buddy"}

        with patch(
            "httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_response
        ) as mock_req:
            await adapter.call_tool("createPet", {"body": {"name": "Buddy", "tag": "dog"}})
            call_kwargs = mock_req.call_args
            assert call_kwargs.kwargs["json"] == {"name": "Buddy", "tag": "dog"}

    @pytest.mark.asyncio
    async def test_call_tool_not_found(self) -> None:
        adapter = RestToMcpAdapter("https://api.example.com", SAMPLE_OPENAPI_SPEC)
        result = await adapter.call_tool("nonexistent", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_call_tool_with_extra_headers(self) -> None:
        adapter = RestToMcpAdapter(
            "https://api.example.com",
            SAMPLE_OPENAPI_SPEC,
            headers={"X-Api-Key": "base-key"},
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        with patch(
            "httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_response
        ) as mock_req:
            await adapter.call_tool(
                "listPets",
                {},
                extra_headers={"Authorization": "Bearer user-token"},
            )
            call_kwargs = mock_req.call_args
            headers = call_kwargs.kwargs["headers"]
            assert headers["X-Api-Key"] == "base-key"
            assert headers["Authorization"] == "Bearer user-token"

    @pytest.mark.asyncio
    async def test_call_tool_text_response(self) -> None:
        adapter = RestToMcpAdapter("https://api.example.com", SAMPLE_OPENAPI_SPEC)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = json.JSONDecodeError("", "", 0)
        mock_response.text = "plain text response"

        with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_response):
            result = await adapter.call_tool("listPets", {})
            assert result["data"] == "plain text response"


class TestLoadSpecFromUrl:
    @pytest.mark.asyncio
    async def test_load_spec(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_OPENAPI_SPEC
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_response):
            spec = await load_spec_from_url("https://example.com/openapi.json")
            assert spec == SAMPLE_OPENAPI_SPEC
