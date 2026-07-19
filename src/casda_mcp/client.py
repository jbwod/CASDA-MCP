"""Reusable asynchronous client for CASDA TAP, Datalink, and SODA/UWS services."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError, map_http_error
from casda_mcp.models import ArchiveAvailability, Capability
from casda_mcp.observability import Metrics
from casda_mcp.parsers import (
    DatalinkAccess,
    UwsStatus,
    parse_datalink_access,
    parse_tap_csv,
    parse_uws_status,
)
from casda_mcp.provenance import sanitize_url
from casda_mcp.vosi import parse_vosi_availability, parse_vosi_capabilities

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
TAP_ACCEPT = "text/csv"
DATALINK_ACCEPT = "application/x-votable+xml"
UWS_ACCEPT = "application/xml"
BINARY_ACCEPT = "application/octet-stream"


class CasdaClient:
    """Connection-pooled CASDA protocol client with safe retry boundaries."""

    def __init__(self, settings: Settings, *, metrics: Metrics | None = None) -> None:
        self.settings = settings
        self.metrics = metrics or Metrics()
        headers = {"User-Agent": settings.user_agent}
        self._auth: httpx.BasicAuth | None = None
        if settings.username is not None and settings.password is not None:
            self._auth = httpx.BasicAuth(settings.username, settings.password.get_secret_value())
        self._credential_origins = frozenset(
            self._origin(url)
            for url in (settings.login_url, settings.datalink_url, settings.soda_url)
        )
        self._request_semaphore = asyncio.Semaphore(settings.max_concurrent_archive_requests)
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30)
        self.http = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            limits=limits,
            follow_redirects=False,
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
            headers={"Accept": TAP_ACCEPT},
            safe_to_retry=True,
            correlation_id=correlation_id,
        )
        self.metrics.increment("search_result_count", len(response.content.splitlines()) - 1)
        return parse_tap_csv(response.content)

    async def get_availability(self, *, correlation_id: str) -> ArchiveAvailability:
        response = await self.request(
            "GET",
            f"{self.settings.tap_base_url}/availability",
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=True,
            correlation_id=correlation_id,
        )
        return parse_vosi_availability(response.content)

    async def get_capabilities(self, *, correlation_id: str) -> list[Capability]:
        response = await self.request(
            "GET",
            f"{self.settings.tap_base_url}/capabilities",
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=True,
            correlation_id=correlation_id,
        )
        return parse_vosi_capabilities(response.content)

    async def verify_authentication(self, *, correlation_id: str) -> None:
        if not self.settings.has_credentials:
            raise CasdaError(
                "AUTHENTICATION_REQUIRED",
                "This CASDA operation requires configured OPAL credentials.",
            )
        await self.request(
            "GET",
            self.settings.login_url,
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=True,
            authenticated=True,
            correlation_id=correlation_id,
        )

    async def resolve_datalink(self, access_url: str, *, correlation_id: str) -> DatalinkAccess:
        self.validate_archive_url(access_url)
        if sanitize_url(access_url).rstrip("/") != self.settings.datalink_url.rstrip("/"):
            raise CasdaError(
                "UNSAFE_ARCHIVE_URL",
                "CASDA returned an unexpected Datalink endpoint.",
                details={"url": sanitize_url(access_url)},
            )
        response = await self.request(
            "GET",
            access_url,
            headers={"Accept": DATALINK_ACCEPT},
            safe_to_retry=True,
            authenticated=True,
            correlation_id=correlation_id,
        )
        result = parse_datalink_access(response.content)
        self.validate_archive_url(result.service_url)
        if result.service_url.rstrip("/") != self.settings.soda_url.rstrip("/"):
            raise CasdaError(
                "UNSAFE_ARCHIVE_URL",
                "CASDA returned an unexpected asynchronous staging endpoint.",
                details={"url": sanitize_url(result.service_url)},
            )
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
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=False,
            authenticated=True,
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
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=False,
            authenticated=True,
            correlation_id=correlation_id,
        )

    async def get_staging_status(self, job_url: str, *, correlation_id: str) -> UwsStatus:
        self.validate_archive_url(job_url)
        response = await self.request(
            "GET",
            job_url,
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=True,
            authenticated=True,
            correlation_id=correlation_id,
        )
        return parse_uws_status(response.content)

    async def create_tap_job(self, query: str, *, max_records: int, correlation_id: str) -> str:
        """Create one async TAP job. This non-idempotent request is never automatically retried."""

        response = await self.request(
            "POST",
            self.settings.tap_async_url,
            data={
                "REQUEST": "doQuery",
                "LANG": "ADQL",
                "FORMAT": "csv",
                "MAXREC": str(max_records),
                "QUERY": query,
            },
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=False,
            correlation_id=correlation_id,
        )
        job_url = str(response.url).rstrip("/")
        self.validate_archive_url(job_url)
        return job_url

    async def start_tap_job(self, job_url: str, *, correlation_id: str) -> None:
        """Start one async TAP job. This non-idempotent request is never automatically retried."""

        self.validate_archive_url(job_url)
        await self.request(
            "POST",
            f"{job_url}/phase",
            data={"PHASE": "RUN"},
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=False,
            correlation_id=correlation_id,
        )

    async def get_tap_job(self, job_url: str, *, correlation_id: str) -> UwsStatus:
        self.validate_archive_url(job_url)
        response = await self.request(
            "GET",
            job_url,
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=True,
            correlation_id=correlation_id,
        )
        return parse_uws_status(response.content)

    async def get_tap_job_results(
        self, job_url: str, *, correlation_id: str
    ) -> list[dict[str, str | None]]:
        """Fetch async TAP results, preferring CSV from the standard result location."""

        self.validate_archive_url(job_url)
        response = await self.request(
            "GET",
            f"{job_url}/results/result",
            headers={"Accept": TAP_ACCEPT},
            safe_to_retry=True,
            correlation_id=correlation_id,
        )
        return parse_tap_csv(response.content)

    async def abort_tap_job(self, job_url: str, *, correlation_id: str) -> None:
        self.validate_archive_url(job_url)
        await self.request(
            "POST",
            f"{job_url}/phase",
            data={"PHASE": "ABORT"},
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=False,
            correlation_id=correlation_id,
        )

    async def delete_tap_job(self, job_url: str, *, correlation_id: str) -> None:
        """Delete one async TAP job. This non-idempotent request is never automatically retried."""

        self.validate_archive_url(job_url)
        await self.request(
            "DELETE",
            job_url,
            headers={"Accept": UWS_ACCEPT},
            safe_to_retry=False,
            correlation_id=correlation_id,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        safe_to_retry: bool,
        authenticated: bool = False,
        correlation_id: str,
        max_response_bytes: int | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        self.validate_archive_url(url)
        response_limit = (
            self.settings.max_response_bytes if max_response_bytes is None else max_response_bytes
        )
        if response_limit < 1:
            raise ValueError("max_response_bytes must be positive")
        attempts = self.settings.max_retries + 1 if safe_to_retry else 1
        last_error: Exception | None = None
        async with self._request_semaphore:
            for attempt in range(attempts):
                started = time.monotonic()
                try:
                    response = await self._request_following_safe_redirects(
                        method,
                        url,
                        authenticated=authenticated,
                        max_response_bytes=response_limit,
                        **kwargs,
                    )
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

    async def _request_following_safe_redirects(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool,
        max_response_bytes: int,
        **kwargs: Any,
    ) -> httpx.Response:
        initial_origin = self._origin(url)
        if authenticated:
            if self._auth is None:
                raise CasdaError(
                    "AUTHENTICATION_REQUIRED",
                    "This CASDA operation requires configured OPAL credentials.",
                )
            if initial_origin not in self._credential_origins:
                raise CasdaError(
                    "UNSAFE_AUTH_TARGET",
                    "Credentials cannot be sent to this archive origin.",
                    details={"url": sanitize_url(url)},
                )
        current_method = method
        current_url = url
        current_kwargs = dict(kwargs)
        current_kwargs.pop("auth", None)
        for _ in range(6):
            request_kwargs = dict(current_kwargs)
            request = self.http.build_request(current_method, current_url, **request_kwargs)
            request_auth = (
                self._auth
                if authenticated and self._origin(current_url) == initial_origin
                else None
            )
            response = await self.http.send(
                request,
                auth=request_auth,
                stream=True,
                follow_redirects=False,
            )
            if not response.is_redirect:
                return await self._read_bounded_response(response, max_response_bytes)
            location = response.headers.get("Location")
            if not location:
                return await self._read_bounded_response(response, max_response_bytes)
            await response.aclose()
            next_url = urljoin(current_url, location)
            self.validate_archive_url(next_url)
            if response.status_code in {301, 302, 303} and current_method not in {"GET", "HEAD"}:
                current_method = "GET"
                current_kwargs.pop("data", None)
                current_kwargs.pop("params", None)
            current_url = next_url
        raise CasdaError("ARCHIVE_REDIRECT_ERROR", "CASDA returned too many redirects.")

    @staticmethod
    async def _read_bounded_response(
        response: httpx.Response, max_response_bytes: int
    ) -> httpx.Response:
        """Buffer a decoded response only after enforcing its configured byte ceiling."""

        content_length = response.headers.get("Content-Length")
        content_encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        if content_encoding in {"", "identity"}:
            try:
                declared_length = int(content_length) if content_length is not None else None
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length > max_response_bytes:
                await response.aclose()
                raise CasdaError(
                    "ARCHIVE_RESPONSE_TOO_LARGE",
                    "CASDA returned a response larger than the configured metadata limit.",
                    details={
                        "max_response_bytes": max_response_bytes,
                        "declared_bytes": declared_length,
                    },
                )

        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                received_bytes = len(body) + len(chunk)
                if received_bytes > max_response_bytes:
                    raise CasdaError(
                        "ARCHIVE_RESPONSE_TOO_LARGE",
                        "CASDA returned a response larger than the configured metadata limit.",
                        details={
                            "max_response_bytes": max_response_bytes,
                            "received_bytes": received_bytes,
                        },
                    )
                body.extend(chunk)
        finally:
            await response.aclose()

        # This is the same decoded-content cache populated by httpx.Response.aread().
        response._content = bytes(body)
        return response

    @asynccontextmanager
    async def stream_download(
        self,
        url: str,
        *,
        offset: int,
        if_range: str | None,
        correlation_id: str,
    ) -> AsyncIterator[httpx.Response]:
        self.validate_archive_url(url)
        headers = {
            "Accept": BINARY_ACCEPT,
            "Accept-Encoding": "identity",
        }
        if offset:
            if if_range is None:
                raise ValueError("Ranged downloads require an If-Range validator")
            headers["Range"] = f"bytes={offset}-"
            headers["If-Range"] = if_range
        elif if_range is not None:
            raise ValueError("If-Range cannot be sent without a byte offset")
        timeout = httpx.Timeout(self.settings.download_timeout_seconds)
        started = time.monotonic()
        response: httpx.Response | None = None
        current_url = url
        for _ in range(6):
            request = self.http.build_request("GET", current_url, headers=headers, timeout=timeout)
            response = await self.http.send(request, stream=True, follow_redirects=False)
            if not response.is_redirect:
                break
            location = response.headers.get("Location")
            await response.aclose()
            if not location:
                break
            current_url = urljoin(current_url, location)
            self.validate_archive_url(current_url)
        if response is None or response.is_redirect:
            if response is not None:
                await response.aclose()
            raise CasdaError("ARCHIVE_REDIRECT_ERROR", "CASDA returned too many redirects.")
        try:
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
                error = map_http_error(response.status_code, "CASDA download failed.")
                retry_after = self._retry_after(response)
                if retry_after is not None:
                    error.details["retry_after_seconds"] = retry_after
                raise error
            yield response
        finally:
            await response.aclose()

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value:
            is_http_date = False
            try:
                delay = float(value)
            except ValueError:
                is_http_date = True
                try:
                    retry_at = parsedate_to_datetime(value)
                    date_header = response.headers.get("Date")
                    reference = (
                        parsedate_to_datetime(date_header)
                        if date_header is not None
                        else datetime.now(timezone.utc)
                    )
                    if retry_at.tzinfo is None or reference.tzinfo is None:
                        return None
                    delay = (retry_at - reference).total_seconds()
                except (OverflowError, TypeError, ValueError):
                    return None
            if math.isfinite(delay) and (delay >= 0 or is_http_date):
                return min(max(delay, 0.0), 60.0)
        return None

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = CasdaClient._retry_after(response)
        if retry_after is not None:
            return retry_after
        return CasdaClient._backoff(attempt)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return float(min(0.5 * (2**attempt) + random.uniform(0, 0.25), 10.0))  # noqa: S311

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlparse(url)
        return parsed.scheme, parsed.hostname or "", parsed.port
