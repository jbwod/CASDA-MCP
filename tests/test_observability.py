from __future__ import annotations

import json
import logging

from casda_mcp.observability import JsonFormatter, configure_logging


def test_json_formatter_removes_secrets_from_arbitrary_log_messages() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        (
            "HTTP Request: GET "
            "https://user:password@data.csiro.au/file?signature=TOPSECRET "
            "Authorization: Basic ALSOSECRET"
        ),
        (),
        None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "TOPSECRET" not in payload["message"]
    assert "ALSOSECRET" not in payload["message"]
    assert "user:password" not in payload["message"]
    assert payload["message"].count("[REDACTED]") == 1


def test_logging_configuration_suppresses_transport_info_logs() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_root_level = root.level
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    original_httpx_level = httpx_logger.level
    original_httpcore_level = httpcore_logger.level
    try:
        configure_logging()
        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_root_level)
        httpx_logger.setLevel(original_httpx_level)
        httpcore_logger.setLevel(original_httpcore_level)
