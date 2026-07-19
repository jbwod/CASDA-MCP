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
        "casda_get_archive_status",
        "casda_list_capabilities",
        "casda_list_schemas",
        "casda_list_tables",
        "casda_describe_table",
        "casda_list_foreign_keys",
        "casda_build_adql",
        "casda_validate_adql",
        "casda_tap_query",
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
        "casda://skills/{skill_name}",
    }
    static_resources = await server.list_resources()
    assert {str(resource.uri) for resource in static_resources} >= {
        "casda://server/status",
        "casda://skills",
        "casda://archive/status",
        "casda://archive/capabilities",
    }
    prompts = await server.list_prompts()
    assert {prompt.name for prompt in prompts} == {
        "find-and-inspect-products",
        "stage-and-download",
        "build-reproducible-selection",
        "query-catalogue",
        "make-cutout",
        "monitor-releases",
    }
    service = server.casda_service  # type: ignore[attr-defined]
    await service.aclose()


async def test_http_health_endpoint_is_non_sensitive() -> None:
    server = create_mcp_server()
    transport = httpx.ASGITransport(app=create_http_app(server))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")
        ready = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "server": "casda-mcp",
        "version": "0.1.0",
        "staging_enabled": False,
        "downloads_enabled": False,
    }
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["archive_available"] is None
    service = server.casda_service  # type: ignore[attr-defined]
    await service.aclose()
