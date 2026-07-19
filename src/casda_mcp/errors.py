"""Typed, client-safe CASDA errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CasdaError(Exception):
    """An expected failure that is safe to return to an MCP caller."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    http_status: int | None = None

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class ValidationError(CasdaError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("VALIDATION_ERROR", message, details=details or {})


class ConfigurationError(CasdaError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("CONFIGURATION_ERROR", message, details=details or {})


def map_http_error(status: int, message: str = "CASDA request failed.") -> CasdaError:
    """Map an HTTP status to a stable, client-safe error code."""

    if status == 401:
        return CasdaError(
            "AUTHENTICATION_FAILED", "CASDA rejected the configured credentials.", http_status=401
        )
    if status == 403:
        return CasdaError(
            "AUTHORISATION_FAILED", "CASDA denied access to this operation.", http_status=403
        )
    if status == 404:
        return CasdaError(
            "NOT_FOUND", "The requested CASDA resource was not found.", http_status=404
        )
    if status in {408, 425}:
        return CasdaError(
            "ARCHIVE_UNAVAILABLE",
            "CASDA asked the client to retry the request later.",
            True,
            http_status=status,
        )
    if status == 429:
        return CasdaError("RATE_LIMITED", "CASDA rate-limited the request.", True, http_status=429)
    if 500 <= status <= 599:
        return CasdaError("ARCHIVE_UNAVAILABLE", message, True, http_status=status)
    return CasdaError("ARCHIVE_ERROR", message, False, http_status=status)
