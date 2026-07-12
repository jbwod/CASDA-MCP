from __future__ import annotations

from datetime import UTC, datetime

import pytest

from casda_mcp.errors import CasdaError
from casda_mcp.parsers import parse_tap_csv, parse_uws_status, product_from_row


def test_csv_preserves_nulls_and_unexpected_columns() -> None:
    rows = parse_tap_csv(b"obs_publisher_did,filename,new_column\r\ncube-1,,future\r\n")
    assert rows == [{"obs_publisher_did": "cube-1", "filename": None, "new_column": "future"}]
    product = product_from_row(rows[0])
    assert product.filename is None
    assert product.model_extra == {"new_column": "future"}


def test_product_parser_converts_units_and_sbid() -> None:
    row = {
        "obs_publisher_did": "cube-1170",
        "access_estsize": "100",
        "em_min": "0.2",
        "em_max": "0.3",
        "obs_id": "ASKAP-2338",
        "obs_release_date": "2020-01-01T00:00:00Z",
        "access_url": "https://data.csiro.au/datalink?id=cube-1170",
    }
    product = product_from_row(row)
    assert product.file_size_bytes == 102_400
    assert product.frequency_min_hz == pytest.approx(299_792_458 / 0.3)
    assert product.frequency_max_hz == pytest.approx(299_792_458 / 0.2)
    assert product.sbid == 2338
    assert product.access_state == "STAGING_REQUIRED"


def test_future_release_is_restricted() -> None:
    product = product_from_row(
        {
            "obs_publisher_did": "cube-2",
            "obs_release_date": "2999-01-01T00:00:00Z",
            "access_url": "https://data.csiro.au/datalink?id=cube-2",
        }
    )
    assert product.release_date == datetime(2999, 1, 1, tzinfo=UTC)
    assert product.access_state == "RESTRICTED"


def test_tap_xml_error_is_machine_readable() -> None:
    content = (
        b"<VOTABLE><RESOURCE><INFO name='QUERY_STATUS' value='ERROR'>"
        b"bad query</INFO></RESOURCE></VOTABLE>"
    )
    with pytest.raises(CasdaError) as error:
        parse_tap_csv(content)
    assert error.value.code == "ARCHIVE_QUERY_ERROR"
    assert error.value.details == {"archive_message": "bad query"}


def test_uws_status_parses_phase_expiry_error_and_results() -> None:
    content = b"""<?xml version='1.0'?>
    <uws:job xmlns:uws='http://www.ivoa.net/xml/UWS/v1.0'
      xmlns:xlink='http://www.w3.org/1999/xlink'>
      <uws:phase>COMPLETED</uws:phase>
      <uws:destruction>2026-07-13T00:00:00Z</uws:destruction>
      <uws:results><uws:result xlink:href='https://data.csiro.au/file.fits'/></uws:results>
    </uws:job>"""
    result = parse_uws_status(content)
    assert result.phase == "COMPLETED"
    assert result.destruction == datetime(2026, 7, 13, tzinfo=UTC)
    assert result.result_urls == ["https://data.csiro.au/file.fits"]


def test_uws_missing_phase_is_unknown() -> None:
    result = parse_uws_status(b"<uws:job xmlns:uws='http://www.ivoa.net/xml/UWS/v1.0'/>")
    assert result.phase == "UNKNOWN"
