"""Provenance construction and secret-safe canonical identifiers."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from casda_mcp import __version__
from casda_mcp.models import Provenance

SECRET_KEY = re.compile(r"(?i)(authorization|password|token|secret|cookie|credential|signature)")
REDACTED = "[REDACTED]"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sanitize_url(url: str) -> str:
    """Remove query strings, fragments, and user information from a URL."""

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def redact(value: Any) -> Any:
    """Recursively redact values whose keys are likely to contain secrets."""

    if isinstance(value, dict):
        return {
            key: REDACTED if SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def make_provenance(
    *,
    request_timestamp: datetime,
    endpoint: str,
    parameters: dict[str, Any],
    result_count: int,
    cached: bool = False,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> Provenance:
    safe_parameters = redact(parameters)
    return Provenance(
        server_version=__version__,
        request_timestamp=request_timestamp,
        response_timestamp=utc_now(),
        query_id=canonical_hash(
            {"endpoint": sanitize_url(endpoint), "parameters": safe_parameters}
        ),
        request_id=request_id,
        endpoint=sanitize_url(endpoint),
        parameters=safe_parameters,
        result_count=result_count,
        cached=cached,
        correlation_id=correlation_id or str(uuid.uuid4()),
    )
