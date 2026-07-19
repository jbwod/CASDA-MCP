from __future__ import annotations

import json

from casda_mcp.config import Settings
from casda_mcp.service import CasdaService
from casda_mcp.server import create_mcp_server


async def test_get_dap_navigation_links_and_privileged_refusal() -> None:
    settings = Settings(_env_file=None, max_retries=0)
    service = CasdaService(settings)
    try:
        result = await service.get_dap_navigation(
            product_id="cube-1",
            scheduling_block_id=12345,
            project_code="AS102",
            collection="WALLABY",
            request_id="job-1",
            action="accept_licence",
        )
        titles = {link.title for link in result.links}
        assert "Observation Search" in titles
        assert "CASDA Skymap" in titles
        assert "DAP search for product" in titles
        assert any("data.csiro.au/search" in link.url for link in result.links)
        assert any("casdaObservation" in link.url for link in result.links)
        assert len(result.unsupported_actions) == 1
        assert result.unsupported_actions[0].code == "accept_licence"
        assert "never auto-accepts" in result.unsupported_actions[0].message
        assert result.unsupported_actions[0].navigation_url is not None
    finally:
        await service.aclose()


async def test_dap_navigation_resource_is_static() -> None:
    settings = Settings(_env_file=None, max_retries=0)
    service = CasdaService(settings)
    try:
        payload = service.dap_navigation_resource()
        assert payload["tool"] == "casda_get_dap_navigation"
        assert "accept_licence" in payload["privileged_actions_unsupported"]
        assert "observation_search" in payload["templates"]
    finally:
        await service.aclose()


async def test_mcp_exposes_dap_navigation_resource() -> None:
    server = create_mcp_server()
    try:
        static_resources = await server.list_resources()
        assert "casda://dap/navigation" in {str(resource.uri) for resource in static_resources}
        body = await server.read_resource("casda://dap/navigation")
        text = body[0].content  # type: ignore[index]
        if hasattr(text, "text"):
            text = text.text
        payload = json.loads(str(text))
        assert payload["tool"] == "casda_get_dap_navigation"
    finally:
        service = server.casda_service  # type: ignore[attr-defined]
        await service.aclose()
