from __future__ import annotations

from datetime import datetime, timezone

import pytest

from casda_mcp.errors import CasdaError
from casda_mcp.parsers import (
    UwsStatus,
    parse_datalink_access,
    parse_tap_csv,
    parse_uws_status,
    product_from_row,
)


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
    assert product.release_date == datetime(2999, 1, 1, tzinfo=timezone.utc)
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
      <uws:results><uws:result id='cube-1' size='42' mime-type='application/fits'
        xlink:href='https%3A%2F%2Fdata.csiro.au%2Ffile.fits%3Fsignature%3Da%252Fb'/></uws:results>
    </uws:job>"""
    result = parse_uws_status(content)
    assert result.phase == "COMPLETED"
    assert result.destruction == datetime(2026, 7, 13, tzinfo=timezone.utc)
    assert result.result_urls == ["https://data.csiro.au/file.fits?signature=a%2Fb"]
    assert result.results[0].result_id == "cube-1"
    assert result.results[0].mime_type == "application/fits"
    assert result.results[0].size_bytes == 42


def test_uws_status_preserves_escaping_inside_absolute_result_url() -> None:
    content = b"""<uws:job xmlns:uws='http://www.ivoa.net/xml/UWS/v1.0'
      xmlns:xlink='http://www.w3.org/1999/xlink'>
      <uws:phase>COMPLETED</uws:phase>
      <uws:results><uws:result id='cube-1'
        xlink:href='https://data.csiro.au/folder%2Ffile.fits?signature=a%2Fb'/></uws:results>
    </uws:job>"""
    result = parse_uws_status(content)
    assert result.results[0].href == ("https://data.csiro.au/folder%2Ffile.fits?signature=a%2Fb")


def test_uws_status_accepts_legacy_result_url_construction() -> None:
    status = UwsStatus(
        "COMPLETED",
        None,
        None,
        ["https%3A%2F%2Fdata.csiro.au%2Ffile.fits%3Fsignature%3Dlegacy"],
    )
    assert status.result_urls == ["https://data.csiro.au/file.fits?signature=legacy"]
    assert status.results[0].result_id == "legacy-0"


@pytest.mark.parametrize(
    "result_xml",
    [
        "<uws:result xlink:href='https://data.csiro.au/file.fits'/>",
        "<uws:result id='cube-1'/>",
        "<uws:result id='cube-1' size='not-a-number' "
        "xlink:href='https://data.csiro.au/file.fits'/>",
        "<uws:result id='cube-1' size='-1' xlink:href='https://data.csiro.au/file.fits'/>",
        "<uws:result id='cube-1' xlink:href='https://data.csiro.au/one'/>"
        "<uws:result id='cube-1' xlink:href='https://data.csiro.au/two'/>",
    ],
)
def test_uws_invalid_result_identity_or_size_is_rejected(result_xml: str) -> None:
    content = f"""<uws:job xmlns:uws='http://www.ivoa.net/xml/UWS/v1.0'
      xmlns:xlink='http://www.w3.org/1999/xlink'>
      <uws:phase>COMPLETED</uws:phase>
      <uws:results>{result_xml}</uws:results>
    </uws:job>""".encode()
    with pytest.raises(CasdaError) as error:
        parse_uws_status(content)
    assert error.value.code == "MALFORMED_ARCHIVE_RESPONSE"


def test_uws_missing_phase_and_unexpected_root_are_rejected() -> None:
    with pytest.raises(CasdaError, match="missing UWS phase"):
        parse_uws_status(b"<uws:job xmlns:uws='http://www.ivoa.net/xml/UWS/v1.0'/>")
    with pytest.raises(CasdaError, match="unexpected UWS document"):
        parse_uws_status(b"<html><phase>COMPLETED</phase></html>")


@pytest.mark.parametrize(
    "row",
    [
        {"obs_publisher_did": "cube-1", "s_ra": "not-a-number"},
        {"obs_publisher_did": "cube-1", "s_ra": "nan"},
        {"obs_publisher_did": "cube-1", "access_estsize": str(2**80)},
        {"obs_publisher_did": "cube-1", "obs_release_date": "not-a-date"},
        {"obs_publisher_did": "cube-1", "t_min": "1e300"},
    ],
)
def test_product_parser_maps_invalid_archive_values_to_typed_errors(row) -> None:
    with pytest.raises(CasdaError) as error:
        product_from_row(row)
    assert error.value.code == "MALFORMED_ARCHIVE_RESPONSE"


def test_datalink_votable_extracts_authorised_async_service() -> None:
    content = b"""<?xml version='1.0'?>
    <VOTABLE xmlns='http://www.ivoa.net/xml/VOTable/v1.3' version='1.3'>
      <RESOURCE type='results'><TABLE>
        <FIELD datatype='char' arraysize='*' name='service_def'/>
        <FIELD datatype='char' arraysize='*' name='authenticated_id_token'/>
        <DATA><TABLEDATA><TR><TD>async_service</TD><TD>cube-1-token</TD></TR></TABLEDATA></DATA>
      </TABLE></RESOURCE>
      <RESOURCE ID='async_service' type='meta'>
        <PARAM datatype='char' arraysize='*' name='accessURL'
          value='https://casda.csiro.au/casda_data_access/data/async'/>
      </RESOURCE>
    </VOTABLE>"""
    result = parse_datalink_access(content)
    assert result.authenticated_id_token == "cube-1-token"  # noqa: S105
    assert result.service_url.endswith("/data/async")


def test_datalink_without_authorised_token_is_rejected() -> None:
    content = b"""<VOTABLE xmlns='http://www.ivoa.net/xml/VOTable/v1.3' version='1.3'>
      <RESOURCE type='results'><TABLE>
        <FIELD datatype='char' arraysize='*' name='service_def'/>
        <FIELD datatype='char' arraysize='*' name='authenticated_id_token'/>
        <DATA><TABLEDATA></TABLEDATA></DATA>
      </TABLE></RESOURCE>
    </VOTABLE>"""
    with pytest.raises(CasdaError) as error:
        parse_datalink_access(content)
    assert error.value.code == "AUTHORISATION_FAILED"
