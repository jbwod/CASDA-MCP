from __future__ import annotations

import pytest

from casda_mcp.errors import ValidationError
from casda_mcp.query import (
    QueryBuilder,
    SearchCriteria,
    datetime_to_mjd,
    normalize_product_ids,
    parse_datetime,
)


@pytest.fixture
def builder() -> QueryBuilder:
    return QueryBuilder(max_results=100, max_cone_radius_deg=5)


def test_builds_allowlisted_spatial_project_and_type_query(builder: QueryBuilder) -> None:
    query, limit = builder.build_search(
        SearchCriteria(
            ra_deg=333.8,
            dec_deg=-46,
            radius_deg=0.1,
            project_code="as102",
            product_types=["cube", "weight", "cube"],
            page_size=10,
        )
    )
    assert "INTERSECTS" in query
    assert "p.opal_code = 'AS102'" in query
    assert "o.dataproduct_type = 'cube'" in query
    assert "spectral.weight" in query
    assert query.startswith("SELECT TOP 101")
    assert "CURRENT_TIMESTAMP" not in query
    assert limit == 101


@pytest.mark.parametrize(
    ("criteria", "message"),
    [
        (SearchCriteria(ra_deg=1), "supplied together"),
        (SearchCriteria(ra_deg=360, dec_deg=0, radius_deg=1), "ra_deg"),
        (SearchCriteria(ra_deg=0, dec_deg=-91, radius_deg=1), "dec_deg"),
        (SearchCriteria(ra_deg=0, dec_deg=0, radius_deg=6), "radius_deg"),
        (SearchCriteria(frequency_min_hz=2, frequency_max_hz=1), "must not exceed"),
        (
            SearchCriteria(observation_start="2025-02-01", observation_end="2025-01-01"),
            "must not be after",
        ),
        (SearchCriteria(product_types=["raw_adql"]), "Unsupported product type"),
        (SearchCriteria(sort_by="drop table"), "Unsupported sort field"),
        (SearchCriteria(page=5, page_size=25), "result window"),
    ],
)
def test_rejects_unsafe_or_invalid_searches(
    builder: QueryBuilder, criteria: SearchCriteria, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        builder.build_search(criteria)


@pytest.mark.parametrize("source", ["%", "_", "x'; DROP TABLE ivoa.obscore --", "\nNGC 1"])
def test_rejects_wildcard_heavy_or_unsafe_text(builder: QueryBuilder, source: str) -> None:
    with pytest.raises(ValidationError):
        builder.build_search(SearchCriteria(source_name=source))


def test_frequency_conversion_uses_obscore_wavelength_overlap(builder: QueryBuilder) -> None:
    query, _ = builder.build_search(SearchCriteria(frequency_min_hz=1.4e9, frequency_max_hz=1.5e9))
    assert "o.em_min <= 0.21413747" in query
    assert "o.em_max >= 0.199861638" in query


def test_dates_convert_to_mjd() -> None:
    assert datetime_to_mjd(parse_datetime("1858-11-17T00:00:00Z", field="date")) == 0


def test_product_ids_are_normalised_and_deduplicated() -> None:
    assert normalize_product_ids([" cube-1 ", "cube-1", "catalogue-2"]) == [
        "cube-1",
        "catalogue-2",
    ]
    with pytest.raises(ValidationError):
        normalize_product_ids([])
    with pytest.raises(ValidationError):
        normalize_product_ids(["cube-1' OR 1=1"])


def test_product_query_is_exact(builder: QueryBuilder) -> None:
    query = builder.build_product("cube-1170")
    assert "WHERE o.obs_publisher_did = 'cube-1170'" in query
    assert "LIKE" not in query
