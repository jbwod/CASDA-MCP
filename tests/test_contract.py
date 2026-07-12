from __future__ import annotations

from casda_mcp.server import create_mcp_server


async def test_initial_tool_names_and_required_product_schema(settings) -> None:
    server = create_mcp_server()
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {
        "casda_search_products",
        "casda_get_product",
        "casda_get_observation",
    }
    product_tool = next(tool for tool in tools if tool.name == "casda_get_product")
    assert product_tool.inputSchema["required"] == ["product_id"]
    assert product_tool.outputSchema is not None
    service = server.casda_service  # type: ignore[attr-defined]
    await service.aclose()
