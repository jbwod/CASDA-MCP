"""Reusable asynchronous client for CASDA TAP, Datalink, and SODA/UWS services."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError, map_http_error
from casda_mcp.observability import Metrics
from casda_mcp.parsers import (
    DatalinkAccess,
    UwsStatus,
    parse_datalink_access,
    parse_tap_csv,
    parse_uws_status,
)
from casda_mcp.provenance import sanitize_url

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class CasdaClient:
    """Connection-pooled CASDA protocol client with safe retry boundaries."""

    def __init__(self, settings: Settings, *, metrics: Metrics | None = None) -> None:
        self.settings = settings
        self.metrics = metrics or Metrics()
        headers = {
            "User-Agent": settings.user_agent,
            "Accept": "application/json, text/csv, application/xml",
        }
        auth: httpx.BasicAuth | None = None
        if settings.token is not None:
            headers["Authorization"] = f"Bearer {settings.token.get_secret_value()}"
        elif settings.username is not None and settings.password is not None:
            auth = httpx.BasicAuth(settings.username, settings.password.get_secret_value())
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30)
        self.http = httpx.AsyncClient(
            headers=headers,
            auth=auth,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            limits=limits,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    def validate_archive_url(self, url: str) -> str:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.settings.allowed_hosts
            or parsed.username
            or parsed.password
        ):
            raise CasdaError(
                "UNSAFE_ARCHIVE_URL",
                "CASDA returned a URL outside the configured archive host allowlist.",
                details={"url": sanitize_url(url)},
            )
        return url

    async def tap_query(
        self, query: str, *, max_records: int, correlation_id: str
    ) -> list[dict[str, str | None]]:
        response = await self.request(
            "POST",
            self.settings.tap_url,
            data={
                "REQUEST": "doQuery",
                "LANG": "ADQL",
                "FORMAT": "csv",
                "MAXREC": str(max_records),
                "QUERY": query,
            },
            safe_to_retry=True,
            correlation_id=correlation_id,
        )
        self.metrics.increment("search_result_count", len(response.content.splitlines()) - 1)
        return parse_tap_csv(response.content)

    async def verify_authentication(self, *, correlation_id: str) -> None:
        if not self.settings.has_credentials:
            raise CasdaError(
                "AUTHENTICATION_REQUIRED",
                "This CASDA operation requires configured OPAL credentials.",
            )
        await self.request(
            "GET",
            self.settings.login_url,
            safe_to_retry=True,
            correlation_id=correlation_id,
        )

    async def resolve_datalink(self, access_url: str, *, correlation_id: str) -> DatalinkAccess:
        self.validate_archive_url(access_url)
        response = await self.request(
            "GET", access_url, safe_to_retry=True, correlation_id=correlation_id
        )
        result = parse_datalink_access(response.content)
        self.validate_archive_url(result.service_url)
        return result

    async def create_staging_job(
        self, service_url: str, tokens: list[str], *, correlation_id: str
    ) -> str:
        """Create one SODA job. This non-idempotent request is never automatically retried."""

        self.validate_archive_url(service_url)
        response = await self.request(
            "POST",
            service_url,
            params=[("ID", token) for token in tokens],
            safe_to_retry=False,
            correlation_id=correlation_id,
        )
        job_url = str(response.url)
        self.validate_archive_url(job_url)
        return job_url.rstrip("/")

    async def start_staging_job(self, job_url: str, *, correlation_id: str) -> None:
        self.validate_archive_url(job_url)
        await self.request(
            "POST",
            f"{job_url}/phase",
            data={"phase": "RUN"},
            safe_to_retry=False,
            correlation_id=correlation_id,
        )

    async def get_staging_status(self, job_url: str, *, correlation_id: str) -> UwsStatus:
        self.validate_archive_url(job_url)
        response = await self.request(
            "GET", job_url, safe_to_retry=True, correlation_id=correlation_id
        )
        return parse_uws_status(response.content)

    async def request(
        self,
        method: str,
        url: str,
        *,
        safe_to_retry: bool,
        correlation_id: str,
        **kwargs: Any,
    ) -> httpx.Response:
        self.validate_archive_url(url)
        attempts = self.settings.max_retries + 1 if safe_to_retry else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                response = await self.http.request(method, url, **kwargs)
                latency_ms = round((time.monotonic() - started) * 1000, 2)
                LOGGER.info(
                    "archive_request",
                    extra={
                        "fields": {
                            "correlation_id": correlation_id,
                            "method": method,
                            "endpoint": sanitize_url(url),
                            "status_code": response.status_code,
                            "latency_ms": latency_ms,
                            "attempt": attempt + 1,
                        }
                    },
                )
                self.metrics.increment("archive_request_count")
                if response.status_code < 400:
                    return response
                if (
                    safe_to_retry
                    and response.status_code in RETRYABLE_STATUS
                    and attempt + 1 < attempts
                ):
                    self.metrics.increment("archive_retry_count")
                    await asyncio.sleep(self._retry_delay(response, attempt))
                    continue
                self.metrics.increment("archive_error_count")
                raise map_http_error(response.status_code)
            except CasdaError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                self.metrics.increment("archive_error_count")
                if not safe_to_retry or attempt + 1 >= attempts:
                    break
                self.metrics.increment("archive_retry_count")
                await asyncio.sleep(self._backoff(attempt))
        raise CasdaError(
            "ARCHIVE_UNAVAILABLE",
            "CASDA could not be reached within the configured retry limit.",
            retryable=True,
            details={"endpoint": sanitize_url(url)},
        ) from last_error

    @asynccontextmanager
    async def stream_download(
        self,
        url: str,
        *,
        offset: int,
        correlation_id: str,
    ) -> AsyncIterator[httpx.Response]:
        self.validate_archive_url(url)
        headers = {"Range": f"bytes={offset}-"} if offset else None
        timeout = httpx.Timeout(self.settings.download_timeout_seconds)
        started = time.monotonic()
        async with self.http.stream("GET", url, headers=headers, timeout=timeout) as response:
            LOGGER.info(
                "archive_download_response",
                extra={
                    "fields": {
                        "correlation_id": correlation_id,
                        "endpoint": sanitize_url(url),
                        "status_code": response.status_code,
                        "latency_ms": round((time.monotonic() - started) * 1000, 2),
                        "offset": offset,
                    }
                },
            )
            if response.status_code >= 400:
                raise map_http_error(response.status_code, "CASDA download failed.")
            yield response

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return min(float(value), 60.0)
            except ValueError:
                try:
                    return max(
                        0.0,
                        min(
                            (
                                parsedate_to_datetime(value)
                                - parsedate_to_datetime(response.headers["Date"])
                            ).total_seconds(),
                            60.0,
                        ),
                    )
                except (KeyError, TypeError, ValueError):
                    pass
        return CasdaClient._backoff(attempt)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(min(0.5 * (2**attempt) + random.uniform(0, 0.25), 10.0))  # noqa: S311
