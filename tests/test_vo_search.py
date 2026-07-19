from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError, ValidationError
from casda_mcp.parsers import parse_sia1_surveys, parse_votable_rows
from casda_mcp.query import validate_catalogue_short_name
from casda_mcp.service import CasdaService

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_votable_rows_stringifies_fields() -> None:
    rows = parse_votable_rows((FIXTURES / "sia2_query.xml").read_bytes())
    assert rows == [
        {
            "obs_publisher_did": "cube-1",
            "access_url": "https://data.csiro.au/casda_vo_proxy/vo/datalink/links?ID=cube-1",
            "access_format": "application/x-votable+xml;content=datalink",
            "dataproduct_type": "cube",
            "obs_collection": "RACS",
            "s_ra": "187.5",
            "s_dec": "-60.0",
            "s_fov": "1.2",
            "obs_id": "ASKAP-1",
        }
    ]


def test_parse_votable_rows_rejects_query_errors() -> None:
    with pytest.raises(CasdaError) as error:
        parse_votable_rows((FIXTURES / "votable_query_error.xml").read_bytes())
    assert error.value.code == "ARCHIVE_QUERY_ERROR"
    assert error.value.details["archive_message"] == "bad coordinates"


def test_parse_sia1_surveys() -> None:
    surveys = parse_sia1_surveys((FIXTURES / "sia1_surveys.xml").read_bytes())
    assert [item["code"] for item in surveys] == ["RACS-Low", "RACS-Mid"]
    assert surveys[0]["name"] == "RACS-low DR1"
    assert surveys[0]["endpoint"] is not None


def test_catalogue_short_name_validation() -> None:
    assert validate_catalogue_short_name("racs_mid_sources_v01") == "racs_mid_sources_v01"
    with pytest.raises(ValidationError):
        validate_catalogue_short_name("../evil")
    with pytest.raises(ValidationError):
        validate_catalogue_short_name("bad name")


@respx.mock
async def test_search_images_sia2_circle() -> None:
    route = respx.get("https://casda.csiro.au/casda_vo_tools/sia2/query").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "sia2_query.xml").read_bytes())
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0, max_results=50))
    try:
        response = await service.search_images(
            pos_type="CIRCLE",
            ra_deg=187.5,
            dec_deg=-60.0,
            radius_deg=0.1,
            max_records=10,
        )
        assert response.returned == 1
        assert response.images[0].obs_publisher_did == "cube-1"
        assert response.images[0].s_ra == pytest.approx(187.5)
        assert response.provenance is not None
        assert route.called
        params = parse_qs(urlparse(str(route.calls[0].request.url)).query)
        assert params["POS"] == ["CIRCLE 187.5 -60 0.1"]
        assert params["MAXREC"] == ["10"]
    finally:
        await service.aclose()


@respx.mock
async def test_search_images_rejects_bad_coords() -> None:
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        with pytest.raises(ValidationError):
            await service.search_images(
                pos_type="CIRCLE",
                ra_deg=400.0,
                dec_deg=-60.0,
                radius_deg=0.1,
            )
    finally:
        await service.aclose()


@respx.mock
async def test_list_image_surveys_and_search_survey_images() -> None:
    respx.get("https://casda.csiro.au/casda_vo_tools/sia1/surveys").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "sia1_surveys.xml").read_bytes())
    )
    sia1 = respx.get("https://casda.csiro.au/casda_vo_tools/sia1/query").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "sia1_query.xml").read_bytes())
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        surveys = await service.list_image_surveys()
        assert [item.code for item in surveys.surveys] == ["RACS-Low", "RACS-Mid"]

        images = await service.search_survey_images(
            survey="RACS-Low",
            ra_deg=187.5,
            dec_deg=-60.0,
            size_deg=0.1,
            max_records=5,
        )
        assert images.images[0].survey == "RACS-Low"
        assert images.images[0].obs_publisher_did == "image-1"
        params = parse_qs(urlparse(str(sia1.calls[0].request.url)).query)
        assert params["SURVEY"] == ["RACS-Low"]
        assert params["POS"] == ["187.5,-60"]
        assert params["SIZE"] == ["0.1"]
    finally:
        await service.aclose()


@respx.mock
async def test_list_catalogues_cursor_pagination() -> None:
    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(
        return_value=httpx.Response(
            200,
            content=(
                b"id,observation_id,project_id,format,filename,freq_ref,image_id,"
                b"time_obs,time_obs_mjd,quality_level,released_date\n"
                b"1,10,2,votable,a.xml,1e8,100,2024-01-01T00:00:00Z,60000,GOOD,2024-01-02Z\n"
                b"2,11,2,votable,b.xml,,101,2024-01-01T00:00:00Z,60000,GOOD,2024-01-02Z\n"
                b"3,12,3,votable,c.xml,,102,2024-01-01T00:00:00Z,60000,GOOD,2024-01-02Z\n"
            ),
        )
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0, max_results=100))
    try:
        first = await service.list_catalogues(page_size=2)
        assert [item.id for item in first.catalogues] == [1, 2]
        assert first.pagination is not None
        assert first.pagination.has_more is True
        assert first.catalogues[0].filename == "a.xml"

        second = await service.list_catalogues(cursor=first.pagination.next_cursor, page_size=2)
        assert [item.id for item in second.catalogues] == [3]
        assert second.pagination is not None
        assert second.pagination.has_more is False
    finally:
        await service.aclose()


@respx.mock
async def test_search_catalogue_scs() -> None:
    route = respx.get("https://casda.csiro.au/casda_vo_tools/scs/racs_mid_sources_v01").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "scs_query.xml").read_bytes())
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        response = await service.search_catalogue(
            catalogue="racs_mid_sources_v01",
            ra_deg=187.5,
            dec_deg=-60.0,
            radius_deg=0.5,
            max_records=2,
        )
        assert response.catalogue == "racs_mid_sources_v01"
        assert response.returned == 1
        assert response.rows[0].name == "RACS-MID1 J123334.7-600330"
        assert response.rows[0].ra == pytest.approx(188.3945)
        assert "total_flux" in (response.rows[0].model_extra or {})
        assert route.called
        params = parse_qs(urlparse(str(route.calls[0].request.url)).query)
        assert params["RA"] == ["187.5"]
        assert params["DEC"] == ["-60"]
        assert params["SR"] == ["0.5"]
    finally:
        await service.aclose()


@respx.mock
async def test_search_catalogue_rejects_unsafe_name() -> None:
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        with pytest.raises(ValidationError):
            await service.search_catalogue(
                catalogue="../../admin",
                ra_deg=187.5,
                dec_deg=-60.0,
                radius_deg=0.1,
            )
    finally:
        await service.aclose()


@respx.mock
async def test_search_spectra_ssa() -> None:
    route = respx.get("https://casda.csiro.au/casda_vo_tools/ssa/query").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "ssa_query.xml").read_bytes())
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        response = await service.search_spectra(
            ra_deg=187.5,
            dec_deg=-60.0,
            size_deg=0.5,
            band="0.2/0.3",
            max_records=5,
        )
        assert response.spectra[0].obs_publisher_id == "spectrum-1"
        assert response.spectra[0].spectrum_type == "integrated"
        params = parse_qs(urlparse(str(route.calls[0].request.url)).query)
        assert params["REQUEST"] == ["queryData"]
        assert params["POS"] == ["187.5,-60"]
        assert params["BAND"] == ["0.2/0.3"]
    finally:
        await service.aclose()
