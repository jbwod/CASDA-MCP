"""Optional read-only live checks against public CASDA services.

Never stage, download, create cutouts, or submit authenticated jobs here.
Enable with CASDA_RUN_LIVE_TESTS=true.
"""

from __future__ import annotations

import os

import pytest

from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
from casda_mcp.query import SearchCriteria
from casda_mcp.service import CasdaService

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CASDA_RUN_LIVE_TESTS", "").lower() not in {"1", "true", "yes"},
        reason="Set CASDA_RUN_LIVE_TESTS=true to run bounded read-only CASDA checks.",
    ),
]


async def test_live_bounded_public_cube_search() -> None:
    """A small metadata-only ObsCore query; this test never stages or downloads data."""

    service = CasdaService()
    try:
        response = await service.search_products(
            SearchCriteria(
                ra_deg=333.8,
                dec_deg=-46.0,
                radius_deg=0.01,
                product_types=["cube"],
                page_size=2,
            )
        )
    finally:
        await service.aclose()
    assert len(response.products) <= 2
    assert all(product.product_id for product in response.products)
    assert response.provenance is not None
    assert response.provenance.archive == "CASDA"


async def test_live_vosi_availability() -> None:
    service = CasdaService()
    try:
        response = await service.get_archive_status()
    finally:
        await service.aclose()
    assert response.availability is not None
    assert isinstance(response.availability.available, bool)
    assert response.provenance is not None


async def test_live_vosi_capabilities() -> None:
    service = CasdaService()
    try:
        response = await service.list_capabilities()
    finally:
        await service.aclose()
    assert response.capabilities
    assert any("TAP" in (item.standard_id or "") for item in response.capabilities)
    assert response.provenance is not None


async def test_live_tap_sync_small_query() -> None:
    """Direct TAP sync with a tiny TOP query; never uses advanced-ADQL tools."""

    settings = Settings()
    client = CasdaClient(settings)
    try:
        rows = await client.tap_query(
            "SELECT TOP 1 obs_publisher_did FROM ivoa.obscore",
            max_records=1,
            correlation_id="live-tap-sync",
        )
    finally:
        await client.aclose()
    assert len(rows) <= 1
    if rows:
        assert rows[0].get("obs_publisher_did")


async def test_live_list_schemas() -> None:
    service = CasdaService()
    try:
        response = await service.list_schemas(page_size=10)
    finally:
        await service.aclose()
    names = {item.schema_name for item in response.schemas}
    assert "ivoa" in names or "TAP_SCHEMA" in names
    assert response.pagination is not None
    assert response.pagination.returned == len(response.schemas)


async def test_live_sia2_small_cone() -> None:
    service = CasdaService()
    try:
        response = await service.search_images(
            pos_type="CIRCLE",
            ra_deg=187.5,
            dec_deg=-60.0,
            radius_deg=0.05,
            max_records=2,
        )
    finally:
        await service.aclose()
    assert response.returned <= 2
    assert len(response.images) == response.returned
    assert response.provenance is not None


async def test_live_sia1_surveys() -> None:
    service = CasdaService()
    try:
        response = await service.list_image_surveys()
    finally:
        await service.aclose()
    assert response.surveys
    assert all(survey.code for survey in response.surveys)


async def test_live_list_catalogues() -> None:
    service = CasdaService()
    try:
        response = await service.list_catalogues(page_size=5)
    finally:
        await service.aclose()
    assert response.pagination is not None
    assert response.pagination.returned == len(response.catalogues)
    if response.catalogues:
        assert all(item.id is not None or item.filename for item in response.catalogues)


async def test_live_events_feed() -> None:
    service = CasdaService()
    try:
        response = await service.list_events(page_size=5)
    finally:
        await service.aclose()
    assert response.pagination is not None
    assert response.pagination.returned == len(response.events)
    assert response.provenance is not None
    assert "events" in (response.provenance.endpoint or "")
