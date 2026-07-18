from __future__ import annotations

from datetime import datetime, timezone

import pytest

from casda_mcp.errors import CasdaError
from casda_mcp.models import ReadyArtifact
from casda_mcp.query import SearchCriteria
from casda_mcp.service import CasdaService

ROW = {
    "obs_publisher_did": "cube-1",
    "filename": "wallaby-cube.fits",
    "dataproduct_type": "cube",
    "dataproduct_subtype": "spectral.restored.3d",
    "obs_collection": "WALLABY",
    "project_code": "AS102",
    "obs_id": "ASKAP-2338",
    "access_estsize": "100",
    "access_url": "https://data.csiro.au/datalink?ID=cube-1",
    "s_ra": "10",
    "s_dec": "-20",
    "em_min": "0.2",
    "em_max": "0.3",
    "obs_release_date": "2020-01-01T00:00:00Z",
}


class ManifestClient:
    async def tap_query(self, query: str, *, max_records: int, correlation_id: str):
        return [dict(ROW)] if "'cube-1'" in query or "dataproduct_type" in query else []

    async def aclose(self) -> None:
        return None


@pytest.fixture
def manifest_service(settings) -> CasdaService:
    return CasdaService(settings, client=ManifestClient())  # type: ignore[arg-type]


async def test_manifest_is_reproducible_and_includes_known_search_criteria(
    manifest_service: CasdaService,
) -> None:
    await manifest_service.search_products(
        SearchCriteria(
            ra_deg=10,
            dec_deg=-20,
            radius_deg=0.1,
            project_code="AS102",
            product_types=["cube"],
        )
    )
    first = await manifest_service.create_manifest(
        ["cube-1", "cube-1"],
        source_name="WALLABY J0000-2000",
        workflow_name="cube_analysis",
        include_download_urls=False,
    )
    second = await manifest_service.create_manifest(
        ["cube-1"],
        source_name="WALLABY J0000-2000",
        workflow_name="cube_analysis",
        include_download_urls=False,
    )
    assert first.manifest is not None
    assert second.manifest is not None
    assert first.manifest.manifest_id == second.manifest.manifest_id
    assert first.manifest.manifest_id.startswith("sha256-")
    assert first.manifest.products[0].product.file_size_bytes == 102_400
    assert first.manifest.products[0].product.sbid == 2338
    assert first.manifest.original_search_criteria[0]["project_code"] == "AS102"
    assert manifest_service.state.get_manifest(first.manifest.manifest_id) is not None


async def test_manifest_omits_signed_url_even_when_requested(
    manifest_service: CasdaService,
) -> None:
    manifest_service.state.put_ready(
        ReadyArtifact(
            product_id="cube-1",
            request_id="job-1",
            download_url="https://data.csiro.au/file.fits?signature=secret",
            confirmed_at=datetime.now(timezone.utc),
        )
    )
    response = await manifest_service.create_manifest(
        ["cube-1"],
        source_name=None,
        workflow_name=None,
        include_download_urls=True,
    )
    assert response.manifest is not None
    assert response.manifest.products[0].download_url is None
    assert "artifact URL was omitted" in response.manifest.warnings[0]
    assert "secret" not in response.manifest.model_dump_json()


async def test_manifest_omits_queryless_confirmed_url_when_requested(
    manifest_service: CasdaService,
) -> None:
    manifest_service.state.put_ready(
        ReadyArtifact(
            product_id="cube-1",
            request_id="job-1",
            download_url="https://data.csiro.au/file.fits",
            confirmed_at=datetime.now(timezone.utc),
        )
    )
    response = await manifest_service.create_manifest(
        ["cube-1"],
        source_name=None,
        workflow_name=None,
        include_download_urls=True,
    )
    assert response.manifest is not None
    assert response.manifest.products[0].download_url is None
    assert "bearer credentials" in response.manifest.warnings[0]


async def test_manifest_limits_and_labels_are_validated(manifest_service: CasdaService) -> None:
    manifest_service.settings.max_manifest_products = 1
    with pytest.raises(CasdaError) as limit_error:
        await manifest_service.create_manifest(
            ["cube-1", "cube-2"],
            source_name=None,
            workflow_name=None,
            include_download_urls=False,
        )
    assert limit_error.value.code == "MANIFEST_LIMIT_EXCEEDED"
    with pytest.raises(CasdaError) as label_error:
        await manifest_service.create_manifest(
            ["cube-1"],
            source_name="bad\nlabel",
            workflow_name=None,
            include_download_urls=False,
        )
    assert label_error.value.code == "VALIDATION_ERROR"
