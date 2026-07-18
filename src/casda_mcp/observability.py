"""Secret-safe structured logging and process-local metrics."""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from casda_mcp.provenance import REDACTED, redact, sanitize_url

URL_IN_MESSAGE = re.compile(r"https?://[^\s\"'<>]+")
SECRET_IN_MESSAGE = re.compile(
    r"(?i)\b(authorization|password|token|secret|cookie|credential|signature)"
    r"\s*[:=]\s*(?:(?:Basic|Bearer)\s+)?[^\s,;]+"
)


def sanitize_log_message(message: str) -> str:
    """Remove URL credentials/query data and common inline secret fields."""

    without_url_secrets = URL_IN_MESSAGE.sub(lambda match: sanitize_url(match.group(0)), message)
    return SECRET_IN_MESSAGE.sub(lambda match: f"{match.group(1)}={REDACTED}", without_url_secrets)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": sanitize_log_message(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(redact(fields))
        if record.exc_info:
            payload["exception_type"] = (
                record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            )
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: str = "INFO") -> None:
    """Configure stderr logging so stdio MCP messages on stdout remain valid."""

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx/httpcore INFO messages contain the complete request URL. Even though the formatter
    # sanitises arbitrary messages, suppressing routine transport logs avoids duplicate records.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Metrics:
    def __init__(self) -> None:
        self._counters: Counter[str] = Counter()
        self._lock = threading.Lock()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)
