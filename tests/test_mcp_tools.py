from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from casda_mcp.config import Settings
from casda_mcp.server import create_mcp_server
from casda_mcp.service import CasdaService


@pytest.fixture
async def mcp_server():
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    server = create_mcp_server(service)
    yield server
    await service.aclose()


def _error_payload(exc: ToolError) -> dict[str, object]:
    text = str(exc)
    start = text.find("{")
    assert start >= 0, text
    return json.loads(text[start:])


async def test_mcp_search_returns_protocol_level_validation_error(mcp_server) -> None:
    with pytest.raises(ToolError) as exc_info:
        await mcp_server.call_tool("casda_search_products", {"source_name": "%"})
    payload = _error_payload(exc_info.value)
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["retryable"] is False


async def test_mcp_stage_and_download_return_guard_errors_by_default(mcp_server) -> None:
    with pytest.raises(ToolError) as staging_exc:
        await mcp_server.call_tool("casda_stage_products", {"product_ids": ["cube-1"]})
    with pytest.raises(ToolError) as download_exc:
        await mcp_server.call_tool("casda_download_product", {"product_id": "cube-1"})
    assert _error_payload(staging_exc.value)["code"] == "STAGING_DISABLED"
    assert _error_payload(download_exc.value)["code"] == "DOWNLOADS_DISABLED"


async def test_mcp_status_does_not_accept_unknown_or_arbitrary_request(mcp_server) -> None:
    with pytest.raises(ToolError) as exc_info:
        await mcp_server.call_tool(
            "casda_get_staging_status", {"request_id": "unknown-request"}
        )
    assert _error_payload(exc_info.value)["code"] == "STAGING_REQUEST_NOT_FOUND"


async def test_mcp_required_and_nonempty_schemas_are_enforced(mcp_server) -> None:
    with pytest.raises(ToolError, match="Field required"):
        await mcp_server.call_tool("casda_get_product", {})
    with pytest.raises(ToolError, match="at least 1 item"):
        await mcp_server.call_tool("casda_create_manifest", {"product_ids": []})


async def test_tool_annotations_are_declared(mcp_server) -> None:
    tools = {tool.name: tool for tool in await mcp_server.list_tools()}
    search = tools["casda_search_products"]
    assert search.annotations is not None
    assert search.annotations.readOnlyHint is True
    stage = tools["casda_stage_products"]
    assert stage.annotations is not None
    assert stage.annotations.readOnlyHint is False
    assert stage.annotations.idempotentHint is False
