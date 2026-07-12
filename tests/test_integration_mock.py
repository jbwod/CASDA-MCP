from __future__ import annotations

import httpx
import pytest
import respx

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError
from casda_mcp.query import SearchCriteria
from casda_mcp.service import CasdaService


@respx.mock
async def test_search_flows_through_http_client_parser_and_service() -> None:
    csv_response = (
        b"obs_publisher_did,filename,dataproduct_type,dataproduct_subtype,"
        b"obs_collection,project_code,obs_id,access_estsize,access_url,obs_release_date\n"
        b"cube-1170,wallaby.fits,cube,spectral.restored.3d,WALLABY,AS102,ASKAP-2338,"
        b"10,https://data.csiro.au/datalink?ID=cube-1170,2020-01-01T00:00:00Z\n"
    )
    route = respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(
        return_value=httpx.Response(200, content=csv_response)
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        response = await service.search_products(
            SearchCriteria(
                ra_deg=333.8,
                dec_deg=-46,
                radius_deg=0.01,
                project_code="AS102",
                product_types=["cube"],
                page_size=1,
            )
        )
    finally:
        await service.aclose()
    assert response.products[0].product_id == "cube-1170"
    assert response.products[0].file_size_bytes == 10 * 1024
    body = route.calls[0].request.content.decode()
    assert "INTERSECTS" in body
    assert "DROP" not in body


@respx.mock
async def test_archive_outage_maps_to_typed_retryable_error() -> None:
    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(
        return_value=httpx.Response(503)
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        with pytest.raises(CasdaError) as error:
            await service.search_products(SearchCriteria(product_types=["cube"]))
        assert error.value.code == "ARCHIVE_UNAVAILABLE"
        assert error.value.retryable is True
    finally:
        await service.aclose()
