from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from casda_mcp.config import Settings
from casda_mcp.doi import normalize_doi
from casda_mcp.errors import CasdaError, ValidationError
from casda_mcp.service import CasdaService

FIXTURES = Path(__file__).parent / "fixtures"


def test_normalize_doi_accepts_url_and_bare_forms() -> None:
    assert normalize_doi("10.25919/kqrt-pv24") == "10.25919/kqrt-pv24"
    assert normalize_doi("https://doi.org/10.25919/kqrt-pv24") == "10.25919/kqrt-pv24"
    assert normalize_doi("doi:10.25919/kqrt-pv24") == "10.25919/kqrt-pv24"
    with pytest.raises(ValidationError):
        normalize_doi("not-a-doi")


@respx.mock
async def test_resolve_doi_from_datacite() -> None:
    respx.get("https://api.datacite.org/dois/10.25919/kqrt-pv24").mock(
        return_value=httpx.Response(
            200,
            content=(FIXTURES / "datacite_doi.json").read_bytes(),
            headers={"Content-Type": "application/vnd.api+json"},
        )
    )
    settings = Settings(_env_file=None, max_retries=0)
    service = CasdaService(settings)
    try:
        result = await service.resolve_collection_doi(doi="https://doi.org/10.25919/kqrt-pv24")
        assert result.found is True
        assert result.record is not None
        assert result.record.doi == "10.25919/kqrt-pv24"
        assert result.record.is_csiro_dap is True
        assert result.record.prefix == "10.25919"
        assert result.record.title == "Example CSIRO DAP Collection"
        assert result.record.source == "datacite"
        assert result.record.publication_year == 2024
    finally:
        await service.aclose()


@respx.mock
async def test_resolve_doi_falls_back_to_csl() -> None:
    respx.get("https://api.datacite.org/dois/10.25919/kqrt-pv24").mock(
        return_value=httpx.Response(503, content=b"unavailable")
    )
    respx.get("https://doi.org/10.25919/kqrt-pv24").mock(
        return_value=httpx.Response(
            200,
            content=(FIXTURES / "doi_csl.json").read_bytes(),
            headers={"Content-Type": "application/vnd.citationstyles.csl+json"},
        )
    )
    settings = Settings(_env_file=None, max_retries=0)
    service = CasdaService(settings)
    try:
        result = await service.resolve_collection_doi(doi="10.25919/kqrt-pv24")
        assert result.found is True
        assert result.record is not None
        assert result.record.source == "doi_org_csl"
        assert result.record.title == "Example CSIRO DAP Collection (CSL)"
        assert result.record.is_csiro_dap is True
    finally:
        await service.aclose()


@respx.mock
async def test_resolve_collection_without_doi_returns_navigation() -> None:
    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(
        return_value=httpx.Response(
            200,
            content=(
                b"obs_collection,product_count,release_date_min,release_date_max\n"
                b"WALLABY,10,2020-01-01T00:00:00Z,2021-01-01T00:00:00Z\n"
            ),
        )
    )
    settings = Settings(_env_file=None, max_retries=0)
    service = CasdaService(settings)
    try:
        result = await service.resolve_collection_doi(collection="WALLABY")
        assert result.found is False
        assert result.collection == "WALLABY"
        assert result.record is None
        assert result.navigation_url is not None
        assert "WALLABY" in result.navigation_url
        assert "does not invent" in (result.message or "")
    finally:
        await service.aclose()


async def test_resolve_doi_disabled() -> None:
    settings = Settings(_env_file=None, enable_doi_resolve=False, max_retries=0)
    service = CasdaService(settings)
    try:
        with pytest.raises(CasdaError) as error:
            await service.resolve_collection_doi(doi="10.25919/kqrt-pv24")
        assert error.value.code == "DOI_RESOLVE_DISABLED"
    finally:
        await service.aclose()


async def test_resolve_doi_requires_exactly_one_input() -> None:
    settings = Settings(_env_file=None, max_retries=0)
    service = CasdaService(settings)
    try:
        with pytest.raises(ValidationError):
            await service.resolve_collection_doi(doi="10.25919/a", collection="WALLABY")
        with pytest.raises(ValidationError):
            await service.resolve_collection_doi()
    finally:
        await service.aclose()
