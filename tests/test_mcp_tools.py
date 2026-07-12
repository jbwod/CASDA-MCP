from __future__ import annotations

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


async def test_mcp_search_returns_structured_validation_error(mcp_server) -> None:
    _content, structured = await mcp_server.call_tool("casda_search_products", {"source_name": "%"})
    assert structured["error"]["code"] == "VALIDATION_ERROR"
    assert structured["error"]["retryable"] is False


async def test_mcp_stage_and_download_return_guard_errors_by_default(mcp_server) -> None:
    _content, staging = await mcp_server.call_tool(
        "casda_stage_products", {"product_ids": ["cube-1"]}
    )
    _content, download = await mcp_server.call_tool(
        "casda_download_product", {"product_id": "cube-1"}
    )
    assert staging["error"]["code"] == "STAGING_DISABLED"
    assert download["error"]["code"] == "DOWNLOADS_DISABLED"


async def test_mcp_status_does_not_accept_unknown_or_arbitrary_request(mcp_server) -> None:
    _content, status = await mcp_server.call_tool(
        "casda_get_staging_status", {"request_id": "unknown-request"}
    )
    assert status["error"]["code"] == "STAGING_REQUEST_NOT_FOUND"


async def test_mcp_required_and_nonempty_schemas_are_enforced(mcp_server) -> None:
    with pytest.raises(ToolError, match="Field required"):
        await mcp_server.call_tool("casda_get_product", {})
    with pytest.raises(ToolError, match="at least 1 item"):
        await mcp_server.call_tool("casda_create_manifest", {"product_ids": []})
