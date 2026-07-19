from __future__ import annotations

from urllib.parse import unquote_plus

import httpx
import pytest
import respx

from casda_mcp.adql import validate_adql
from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError, ValidationError
from casda_mcp.query import SearchCriteria
from casda_mcp.service import CasdaService


def test_accepts_simple_select() -> None:
    validated = validate_adql(
        "SELECT TOP 10 obs_publisher_did FROM ivoa.obscore",
        max_length=8000,
        max_rows=1000,
    )
    assert validated.query.startswith("SELECT TOP 10")
    assert validated.max_rows == 10


def test_accepts_select_without_top_using_maxrec_bound() -> None:
    validated = validate_adql(
        "SELECT obs_publisher_did FROM ivoa.obscore",
        max_length=8000,
        max_rows=250,
    )
    assert validated.max_rows == 250


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO ivoa.obscore VALUES (1)",
        "UPDATE ivoa.obscore SET x=1",
        "DELETE FROM ivoa.obscore",
        "DROP TABLE ivoa.obscore",
        "CREATE TABLE evil (id INT)",
        "ALTER TABLE ivoa.obscore ADD x INT",
        "TRUNCATE TABLE ivoa.obscore",
        "MERGE INTO ivoa.obscore USING x",
        "GRANT SELECT ON ivoa.obscore TO public",
        "REVOKE SELECT ON ivoa.obscore FROM public",
        "SELECT * INTO evil FROM ivoa.obscore",
    ],
)
def test_rejects_mutation_keywords(query: str) -> None:
    with pytest.raises(ValidationError, match="must not contain|must start with SELECT"):
        validate_adql(query, max_length=8000, max_rows=1000)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM ivoa.obscore; DROP TABLE ivoa.obscore",
        "SELECT * FROM ivoa.obscore -- comment\nWHERE 1=1",
        "SELECT * FROM ivoa.obscore /* smuggle */ WHERE 1=1",
    ],
)
def test_rejects_comments_and_multi_statement(query: str) -> None:
    with pytest.raises(ValidationError):
        validate_adql(query, max_length=8000, max_rows=1000)


def test_rejects_disallowed_schema_and_unqualified_table() -> None:
    with pytest.raises(ValidationError, match="schema outside the allowlist"):
        validate_adql(
            "SELECT * FROM pg_catalog.pg_tables",
            max_length=8000,
            max_rows=1000,
        )
    with pytest.raises(ValidationError, match="schema-qualified"):
        validate_adql(
            "SELECT * FROM obscore",
            max_length=8000,
            max_rows=1000,
        )


def test_rejects_top_above_limit_and_oversized_query() -> None:
    with pytest.raises(ValidationError, match="TOP exceeds"):
        validate_adql(
            "SELECT TOP 5000 * FROM ivoa.obscore",
            max_length=8000,
            max_rows=1000,
        )
    with pytest.raises(ValidationError, match="maximum length"):
        validate_adql(
            "SELECT * FROM ivoa.obscore WHERE x = '" + ("a" * 100) + "'",
            max_length=64,
            max_rows=1000,
        )


def test_allows_project_and_tap_schema_tables() -> None:
    validate_adql(
        "SELECT TOP 1 schema_name FROM TAP_SCHEMA.schemas",
        max_length=8000,
        max_rows=100,
    )
    validate_adql(
        "SELECT TOP 1 * FROM AS102.catalogue",
        max_length=8000,
        max_rows=100,
    )


async def test_tap_query_disabled_by_default(settings) -> None:
    service = CasdaService(settings)
    try:
        with pytest.raises(CasdaError) as error:
            await service.tap_query("SELECT TOP 1 obs_publisher_did FROM ivoa.obscore")
        assert error.value.code == "ADVANCED_ADQL_DISABLED"
    finally:
        await service.aclose()


async def test_build_and_validate_are_local(settings) -> None:
    service = CasdaService(settings)
    try:
        built = service.build_adql(SearchCriteria(project_code="AS102", page_size=10))
        assert "FROM ivoa.obscore" in built.query
        assert built.max_records >= 1
        assert built.parameters["project_code"] == "AS102"
        validated = service.validate_adql_query(built.query)
        assert validated.valid is True
        assert validated.query == built.query
    finally:
        await service.aclose()


@respx.mock
async def test_tap_query_enabled_path_with_mock() -> None:
    def tap_response(request: httpx.Request) -> httpx.Response:
        body = unquote_plus(request.content.decode())
        assert "SELECT TOP 2 obs_publisher_did FROM ivoa.obscore" in body
        assert "MAXREC=2" in body
        return httpx.Response(
            200,
            content=b"obs_publisher_did\ncube-1\ncube-2\n",
        )

    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(side_effect=tap_response)
    service = CasdaService(
        Settings(_env_file=None, enable_advanced_adql=True, max_retries=0, max_tap_rows=100)
    )
    try:
        response = await service.tap_query("SELECT TOP 2 obs_publisher_did FROM ivoa.obscore")
        assert response.returned == 2
        assert response.rows[0]["obs_publisher_did"] == "cube-1"
        assert response.provenance is not None
        assert response.provenance.parameters["query"].startswith("SELECT TOP 2")
    finally:
        await service.aclose()
