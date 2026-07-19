"""Opaque cursor helpers for stable list pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from casda_mcp.errors import ValidationError

_CURSOR_VERSION = 1


def encode_cursor(*, query_hash: str, offset: int, page_size: int) -> str:
    """Encode a signed opaque cursor for the next page."""

    if offset < 0:
        raise ValidationError("Cursor offset must be non-negative.")
    if page_size < 1:
        raise ValidationError("Cursor page size must be positive.")
    payload = {
        "v": _CURSOR_VERSION,
        "q": query_hash,
        "o": offset,
        "n": page_size,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = hmac.new(query_hash.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:16]
    token = raw + b"." + digest.encode("ascii")
    return base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, expected_query_hash: str) -> tuple[int, int]:
    """Decode a cursor and return ``(offset, page_size)`` after signature checks."""

    if not cursor or len(cursor) > 4096:
        raise ValidationError("Pagination cursor is invalid.")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        token = base64.urlsafe_b64decode(padded.encode("ascii"))
        raw, digest = token.rsplit(b".", 1)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError("Pagination cursor is invalid.") from exc
    expected = hmac.new(expected_query_hash.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(digest.decode("ascii"), expected):
        raise ValidationError("Pagination cursor signature is invalid.")
    if payload.get("v") != _CURSOR_VERSION or payload.get("q") != expected_query_hash:
        raise ValidationError("Pagination cursor does not match this query.")
    try:
        offset = int(payload["o"])
        page_size = int(payload["n"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("Pagination cursor is invalid.") from exc
    if offset < 0 or page_size < 1:
        raise ValidationError("Pagination cursor is invalid.")
    return offset, page_size


def query_hash(parameters: dict[str, Any]) -> str:
    """Deterministic hash of query parameters used to bind cursors to a query."""

    canonical = json.dumps(parameters, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
