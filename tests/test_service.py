from __future__ import annotations

import pytest

from casda_mcp.errors import CasdaError
from casda_mcp.query import SearchCriteria
from casda_mcp.service import CasdaService

PRODUCT_ROW = {
    "obs_publisher_did": "cube-1170",
    "filename": "wallaby.fits",
    "dataproduct_type": "cube",
    "dataproduct_subtype": "spectral.restored.3d",
    "obs_collection": "WALLABY",
    "project_code": "AS102",
    "obs_id": "ASKAP-2338",
    "access_estsize": "10",
    "access_url": "https://data.csiro.au/casda_vo_proxy/vo/datalink/links?ID=cube-1170",
    "obs_release_date": "2020-01-01T00:00:00Z",
}


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0
        self.metrics = None

    async def tap_query(self, query: str, *, max_records: int, correlation_id: str):
        self.calls += 1
        if "casda.observation WHERE" in query:
            return [
                {
                    "id": "1",
                    "sbid": "2338",
                    "obs_start": "2017-01-01T00:00:00Z",
                    "obs_end": "2017-01-01T01:00:00Z",
                    "telescope": "ASKAP",
                    "obs_program": "WALLABY",
                    "deposit_state": "DEPOSITED",
                }
            ]
        if "SELECT DISTINCT p.id" in query:
            return [
                {
                    "id": "2",
                    "opal_code": "AS102",
                    "short_name": "WALLABY",
                    "principal_first_name": "Ada",
                    "principal_last_name": "Researcher",
                }
            ]
        if "missing" in query:
            return []
        return [dict(PRODUCT_ROW)]

    async def aclose(self) -> None:
        return None


@pytest.fixture
def service(settings) -> CasdaService:
    return CasdaService(settings, client=FakeClient())  # type: ignore[arg-type]


async def test_search_returns_stable_product_and_provenance(service: CasdaService) -> None:
    response = await service.search_products(SearchCriteria(product_types=["cube"]))
    assert response.products[0].product_id == "cube-1170"
    assert response.products[0].project_code == "AS102"
    assert response.pagination is not None
    assert response.pagination.returned == 1
    assert response.provenance is not None
    assert response.provenance.archive == "CASDA"
    assert response.provenance.cached is False


async def test_read_cache_is_reported(service: CasdaService) -> None:
    criteria = SearchCriteria(product_types=["cube"])
    await service.search_products(criteria)
    second = await service.search_products(criteria)
    assert second.provenance is not None
    assert second.provenance.cached is True
    assert service.client.calls == 1  # type: ignore[attr-defined]


async def test_get_product_not_found_is_typed(service: CasdaService) -> None:
    with pytest.raises(CasdaError) as error:
        await service.get_product("missing")
    assert error.value.code == "PRODUCT_NOT_FOUND"


async def test_get_observation_resolves_projects_and_products(service: CasdaService) -> None:
    response = await service.get_observation(2338)
    assert response.observation is not None
    assert response.observation.sbid == 2338
    assert response.projects[0].project_code == "AS102"
    assert response.products[0].product_id == "cube-1170"
