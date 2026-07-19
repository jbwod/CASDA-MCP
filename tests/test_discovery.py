from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
import respx

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError, ValidationError
from casda_mcp.query import QueryBuilder, validate_schema_name, validate_table_name
from casda_mcp.service import CasdaService
from casda_mcp.vosi import parse_vosi_availability, parse_vosi_capabilities

FIXTURES = Path(__file__).parent / "fixtures"


def _tap_query(request: httpx.Request) -> str:
    return unquote_plus(request.content.decode())


def test_parse_vosi_availability_true() -> None:
    availability = parse_vosi_availability((FIXTURES / "vosi_availability.xml").read_bytes())
    assert availability.available is True
    assert availability.up_since == "2026-07-17T16:03:45.037Z"
    assert availability.notes == ["CASDA TAP is operating normally"]


def test_parse_vosi_availability_false_collects_notes() -> None:
    availability = parse_vosi_availability((FIXTURES / "vosi_availability_down.xml").read_bytes())
    assert availability.available is False
    assert availability.up_since is None
    assert availability.notes == ["Scheduled maintenance", "Retry later"]


def test_parse_vosi_capabilities() -> None:
    capabilities = parse_vosi_capabilities((FIXTURES / "vosi_capabilities.xml").read_bytes())
    assert [item.standard_id for item in capabilities] == [
        "ivo://ivoa.net/std/VOSI#capabilities",
        "ivo://ivoa.net/std/VOSI#availability",
        "ivo://ivoa.net/std/TAP",
        "ivo://ivoa.net/std/DALI#examples",
    ]
    assert capabilities[0].interface_url == (
        "https://casda.csiro.au/casda_vo_tools/tap/capabilities"
    )
    assert capabilities[0].interface_type == "vs:ParamHTTP"
    assert capabilities[0].interface_version == "1.0"
    assert capabilities[3].interface_type == "vr:WebBrowser"


def test_parse_vosi_availability_rejects_malformed_xml() -> None:
    with pytest.raises(CasdaError) as error:
        parse_vosi_availability(b"<not-availability/>")
    assert error.value.code == "MALFORMED_ARCHIVE_RESPONSE"


def test_schema_and_table_name_validation() -> None:
    assert validate_schema_name("TAP_SCHEMA") == "TAP_SCHEMA"
    assert validate_schema_name("AS102") == "AS102"
    assert validate_table_name("obscore") == "obscore"
    with pytest.raises(ValidationError):
        validate_schema_name("ivoa.obscore")
    with pytest.raises(ValidationError):
        validate_table_name("bad-name")


def test_tap_schema_query_builders() -> None:
    builder = QueryBuilder(max_results=100, max_cone_radius_deg=5)
    assert builder.build_list_schemas(fetch_count=11).startswith(
        "SELECT TOP 11 schema_name, description FROM TAP_SCHEMA.schemas"
    )
    tables = builder.build_list_tables(schema_name="ivoa", fetch_count=50)
    assert "WHERE schema_name = 'ivoa'" in tables
    columns = builder.build_describe_table("ivoa", "obscore", fetch_count=200)
    assert "WHERE table_name = 'ivoa.obscore'" in columns
    keys = builder.build_list_foreign_keys("casda", "catalogue", fetch_count=50)
    assert "WHERE k.from_table = 'casda.catalogue'" in keys


@respx.mock
async def test_get_archive_status_updates_readiness() -> None:
    respx.get("https://casda.csiro.au/casda_vo_tools/tap/availability").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "vosi_availability.xml").read_bytes())
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        response = await service.get_archive_status()
        assert response.availability is not None
        assert response.availability.available is True
        assert response.provenance is not None
        snapshot = service.readiness_snapshot()
        assert snapshot["archive_available"] is True
        assert snapshot["detail"] == "CASDA TAP is operating normally"
    finally:
        await service.aclose()


@respx.mock
async def test_list_capabilities_via_client() -> None:
    respx.get("https://casda.csiro.au/casda_vo_tools/tap/capabilities").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "vosi_capabilities.xml").read_bytes())
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        response = await service.list_capabilities()
        assert len(response.capabilities) == 4
        assert response.capabilities[2].standard_id == "ivo://ivoa.net/std/TAP"
        assert response.provenance is not None
    finally:
        await service.aclose()


@respx.mock
async def test_list_schemas_and_tables_from_tap_schema_csv() -> None:
    def tap_response(request: httpx.Request) -> httpx.Response:
        body = _tap_query(request)
        if "FROM TAP_SCHEMA.schemas" in body:
            return httpx.Response(
                200,
                content=(
                    b"schema_name,description\n"
                    b"TAP_SCHEMA,TAP schema metadata\n"
                    b"casda,CASDA tables\n"
                    b"ivoa,IVOA tables\n"
                ),
            )
        if "FROM TAP_SCHEMA.tables" in body:
            assert "schema_name = 'ivoa'" in body
            return httpx.Response(
                200,
                content=(
                    b"table_name,schema_name,table_type,description\n"
                    b"ivoa.obscore,ivoa,view,ObsCore 1.1\n"
                    b"ivoa.spectrum_dm,ivoa,view,Spectrum DM\n"
                ),
            )
        raise AssertionError(f"Unexpected TAP query: {body}")

    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(side_effect=tap_response)
    service = CasdaService(Settings(_env_file=None, max_retries=0, max_results=100))
    try:
        schemas = await service.list_schemas(page_size=2)
        assert [item.schema_name for item in schemas.schemas] == ["TAP_SCHEMA", "casda"]
        assert schemas.pagination is not None
        assert schemas.pagination.has_more is True
        assert schemas.pagination.next_cursor is not None

        next_page = await service.list_schemas(cursor=schemas.pagination.next_cursor, page_size=2)
        assert [item.schema_name for item in next_page.schemas] == ["ivoa"]
        assert next_page.pagination is not None
        assert next_page.pagination.has_more is False

        tables = await service.list_tables(schema_name="ivoa", page_size=10)
        assert [item.table_name for item in tables.tables] == ["obscore", "spectrum_dm"]
        assert tables.tables[0].schema_name == "ivoa"
        assert tables.tables[0].table_type == "view"
    finally:
        await service.aclose()


@respx.mock
async def test_describe_table_and_foreign_keys() -> None:
    def tap_response(request: httpx.Request) -> httpx.Response:
        body = _tap_query(request)
        if "FROM TAP_SCHEMA.columns" in body:
            assert "table_name = 'ivoa.obscore'" in body
            return httpx.Response(
                200,
                content=(
                    b"column_name,datatype,size,ucd,unit,utype,description,principal,indexed,std\n"
                    b"obs_publisher_did,VARCHAR,255,meta.ref.ivoid,,obscore:Curation.PublisherDID,"
                    b"Dataset identifier,1,1,1\n"
                    b"s_ra,DOUBLE,15,pos.eq.ra,deg,obscore:Char.SpatialAxis.Coverage."
                    b"Location.Coord.Position2D.Value2.C1,Central RA,1,0,1\n"
                ),
            )
        if "FROM TAP_SCHEMA.keys" in body:
            assert "from_table = 'casda.catalogue'" in body
            return httpx.Response(
                200,
                content=(
                    b"key_id,from_table,target_table,description,from_column,target_column\n"
                    b"observation_catalogue,casda.catalogue,casda.observation,"
                    b"Foreign key from catalogue to observation table,observation_id,id\n"
                    b"project_catalogue,casda.catalogue,casda.project,"
                    b"Foreign key from project to catalogue table,project_id,id\n"
                ),
            )
        raise AssertionError(f"Unexpected TAP query: {body}")

    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(side_effect=tap_response)
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        described = await service.describe_table("ivoa", "obscore")
        assert described.schema_name == "ivoa"
        assert described.table_name == "obscore"
        assert described.columns[0].column_name == "obs_publisher_did"
        assert described.columns[0].principal is True
        assert described.columns[0].indexed is True
        assert described.columns[1].unit == "deg"
        assert described.columns[1].indexed is False

        keys = await service.list_foreign_keys("casda", "catalogue")
        assert len(keys.foreign_keys) == 2
        assert keys.foreign_keys[0].from_table == "catalogue"
        assert keys.foreign_keys[0].target_table == "observation"
        assert keys.foreign_keys[0].from_column == "observation_id"
        assert keys.foreign_keys[1].target_schema == "casda"
    finally:
        await service.aclose()


@respx.mock
async def test_describe_missing_table_is_typed() -> None:
    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(
        return_value=httpx.Response(200, content=b"column_name,datatype\n")
    )
    service = CasdaService(Settings(_env_file=None, max_retries=0))
    try:
        with pytest.raises(CasdaError) as error:
            await service.describe_table("ivoa", "missing")
        assert error.value.code == "TABLE_NOT_FOUND"
    finally:
        await service.aclose()
