from __future__ import annotations

import os

import pytest

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
    """A small metadata-only query; this test never stages or downloads data."""

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
