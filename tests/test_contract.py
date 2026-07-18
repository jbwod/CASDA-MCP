from __future__ import annotations

import httpx

from casda_mcp.server import create_http_app, create_mcp_server


async def test_initial_tool_names_and_required_product_schema(settings) -> None:
    server = create_mcp_server()
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {
        "casda_search_products",
        "casda_get_product",
        "casda_get_observation",
        "casda_stage_products",
        "casda_get_staging_status",
        "casda_download_product",
        "casda_create_manifest",
    }
    product_tool = next(tool for tool in tools if tool.name == "casda_get_product")
    assert product_tool.inputSchema["required"] == ["product_id"]
    assert product_tool.outputSchema is not None
    search_tool = next(tool for tool in tools if tool.name == "casda_search_products")
    assert search_tool.inputSchema["properties"]["sort_order"]["enum"] == ["asc", "desc"]
    resources = await server.list_resource_templates()
    assert {str(resource.uriTemplate) for resource in resources} == {
        "casda://products/{product_id}",
        "casda://observations/{scheduling_block_id}",
        "casda://staging/{request_id}",
        "casda://manifests/{manifest_id}",
    }
    service = server.casda_service  # type: ignore[attr-defined]
    await service.aclose()


async def test_http_health_endpoint_is_non_sensitive() -> None:
    server = create_mcp_server()
    transport = httpx.ASGITransport(app=create_http_app(server))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "server": "casda-mcp",
        "version": "0.1.0",
        "staging_enabled": False,
        "downloads_enabled": False,
    }
    service = server.casda_service  # type: ignore[attr-defined]
    await service.aclose()
