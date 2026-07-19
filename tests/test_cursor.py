from __future__ import annotations

import pytest

from casda_mcp.cursor import decode_cursor, encode_cursor, query_hash
from casda_mcp.errors import ValidationError


def test_cursor_round_trip_and_query_binding() -> None:
    digest = query_hash({"project_code": "AS102", "page_size": 25})
    cursor = encode_cursor(query_hash=digest, offset=25, page_size=25)
    offset, page_size = decode_cursor(cursor, expected_query_hash=digest)
    assert offset == 25
    assert page_size == 25


def test_cursor_rejects_tampering_and_mismatched_query() -> None:
    digest = query_hash({"a": 1})
    cursor = encode_cursor(query_hash=digest, offset=10, page_size=5)
    with pytest.raises(ValidationError):
        decode_cursor(cursor[:-1] + ("A" if cursor[-1] != "A" else "B"), expected_query_hash=digest)
    with pytest.raises(ValidationError):
        decode_cursor(cursor, expected_query_hash=query_hash({"a": 2}))
