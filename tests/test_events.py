from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
import respx

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError, ValidationError
from casda_mcp.parsers import parse_observation_events
from casda_mcp.query import QueryBuilder
from casda_mcp.service import CasdaService

FIXTURES = Path(__file__).parent / "fixtures"


def _tap_query(request: httpx.Request) -> str:
    return unquote_plus(request.content.decode())


def test_parse_observation_events_xml() -> None:
    events = parse_observation_events((FIXTURES / "events.xml").read_bytes())
    assert [event.event_id for event in events] == ["74659", "74658", "74657"]
    assert events[0].project_code == "AS201"
    assert events[0].event_type == "DEPOSITED"
    assert events[0].scheduling_block_id == 86654
    assert events[0].telescope == "ASKAP"
    assert events[2].event_type == "VALIDATED"


def test_parse_observation_events_json_array() -> None:
    content = b"""[
      {
        "ivorn": "ivo://casda.csiro.au/VOEvent#99",
        "timestamp": "2026-07-18T00:00:00Z",
        "description": "Observation deposited",
        "event": "DEPOSITED",
        "project_code": "AS102",
        "scheduling_block_id": 1
      }
    ]"""
    events = parse_observation_events(content)
    assert len(events) == 1
    assert events[0].event_id == "99"
    assert events[0].event_type == "DEPOSITED"
    assert events[0].project_code == "AS102"


def test_project_and_collection_query_builders() -> None:
    builder = QueryBuilder(max_results=100, max_cone_radius_deg=5)
    search = builder.build_search_projects(project_code="AS102", short_name=None, fetch_count=11)
    assert "FROM casda.project" in search
    assert "opal_code = 'AS102'" in search
    assert builder.build_get_project("as102").endswith("opal_code = 'AS102'")
    summary = builder.build_collection_summary("WALLABY")
    assert "GROUP BY obs_collection" in summary
    assert "obs_collection = 'WALLABY'" in summary
    with pytest.raises(ValidationError):
        builder.build_get_project("not-a-code")


@respx.mock
async def test_search_projects_and_get_project() -> None:
    def tap_response(request: httpx.Request) -> httpx.Response:
        body = _tap_query(request)
        if "FROM casda.project" in body and "opal_code = 'AS102'" in body and "TOP 2" in body:
            return httpx.Response(
                200,
                content=(
                    b"id,opal_code,short_name,principal_first_name,principal_last_name\n"
                    b"7,AS102,WALLABY,Jane,Doe\n"
                ),
            )
        if "FROM casda.project" in body:
            return httpx.Response(
                200,
                content=(
                    b"id,opal_code,short_name,principal_first_name,principal_last_name\n"
                    b"7,AS102,WALLABY,Jane,Doe\n"
                    b"8,AS201,EMU,John,Smith\n"
                    b"9,AS203,POSSUM,Ada,Lovelace\n"
                ),
            )
        raise AssertionError(f"Unexpected TAP query: {body}")

    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(side_effect=tap_response)
    service = CasdaService(Settings(_env_file=None, max_retries=0, max_results=100))
    try:
        page = await service.search_projects(page_size=2)
        assert [item.project_code for item in page.projects] == ["AS102", "AS201"]
        assert page.pagination is not None
        assert page.pagination.has_more is True
        assert page.pagination.next_cursor is not None

        next_page = await service.search_projects(cursor=page.pagination.next_cursor, page_size=2)
        assert [item.project_code for item in next_page.projects] == ["AS203"]

        project = await service.get_project("AS102")
        assert project.project is not None
        assert project.project.short_name == "WALLABY"
        assert project.project.principal_investigator == "Jane Doe"
    finally:
        await service.aclose()


@respx.mock
async def test_get_collection_aggregates_obscore() -> None:
    def tap_response(request: httpx.Request) -> httpx.Response:
        body = _tap_query(request)
        if "GROUP BY dataproduct_type" in body:
            return httpx.Response(
                200,
                content=b"dataproduct_type,product_count\ncube,2\nimage,1\n",
            )
        if "GROUP BY facility_name" in body:
            return httpx.Response(
                200,
                content=b"facility_name\nASKAP\n",
            )
        if "release_date_min" in body and "GROUP BY obs_collection" in body:
            return httpx.Response(
                200,
                content=(
                    b"obs_collection,product_count,release_date_min,release_date_max\n"
                    b"WALLABY,3,2020-01-01T00:00:00Z,2021-06-01T00:00:00Z\n"
                ),
            )
        raise AssertionError(f"Unexpected TAP query: {body}")

    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(side_effect=tap_response)
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        response = await service.get_collection("WALLABY")
        assert response.collection is not None
        assert response.collection.obs_collection == "WALLABY"
        assert response.collection.product_count == 3
        assert response.collection.product_types == ["cube", "image"]
        assert response.collection.facility_names == ["ASKAP"]
        assert response.collection.release_date_min is not None
        assert response.collection.release_date_max is not None
    finally:
        await service.aclose()


@respx.mock
async def test_list_events_filters_and_paginates() -> None:
    respx.get("https://casda.csiro.au/casda_data_access/observations/events").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "events.xml").read_bytes())
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0, max_results=100))
    try:
        page = await service.list_events(page_size=1, project_code="AS201")
        assert len(page.events) == 1
        assert page.events[0].event_id == "74659"
        assert page.pagination is not None
        assert page.pagination.has_more is True
        assert page.pagination.next_cursor is not None

        next_page = await service.list_events(
            cursor=page.pagination.next_cursor, page_size=1, project_code="AS201"
        )
        assert [event.event_id for event in next_page.events] == ["74657"]

        filtered = await service.list_events(event_type="VALIDATED")
        assert [event.event_id for event in filtered.events] == ["74657"]

        single = await service.get_event("74658")
        assert single.event is not None
        assert single.event.project_code == "AS203"
    finally:
        await service.aclose()


@respx.mock
async def test_get_event_not_found() -> None:
    respx.get("https://casda.csiro.au/casda_data_access/observations/events").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "events.xml").read_bytes())
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        with pytest.raises(CasdaError) as error:
            await service.get_event("missing")
        assert error.value.code == "EVENT_NOT_FOUND"
    finally:
        await service.aclose()
