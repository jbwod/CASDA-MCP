"""Guarded, streamed, resumable download helpers."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiofiles
import httpx

from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError, ValidationError
from casda_mcp.models import ChecksumResult, DownloadResult, Product, ReadyArtifact
from casda_mcp.observability import Metrics

CHECKSUM_RE = re.compile(r"(?i)\b(md5|sha-?1|sha-?256)\s*[:=]\s*([0-9a-f]+)\b")
BARE_CHECKSUM_RE = re.compile(
    r"(?i)(?<![0-9a-f])([0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])"
)
CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.IGNORECASE)
INVALID_FILENAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
CHECKSUM_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ChecksumSpec:
    algorithm: str
    digest: str


def parse_checksum(text: str) -> ChecksumSpec:
    """Parse common CASDA checksum sidecar formats without trusting the filename field."""

    match = CHECKSUM_RE.search(text)
    if match:
        algorithm = match.group(1).lower().replace("-", "")
        digest = match.group(2).lower()
    else:
        bare = BARE_CHECKSUM_RE.search(text)
        if not bare:
            raise CasdaError(
                "CHECKSUM_UNAVAILABLE", "CASDA returned an unrecognised checksum document."
            )
        digest = bare.group(1).lower()
        algorithm = {32: "md5", 40: "sha1", 64: "sha256"}[len(digest)]
    expected_length = {"md5": 32, "sha1": 40, "sha256": 64}[algorithm]
    if len(digest) != expected_length:
        raise CasdaError("CHECKSUM_UNAVAILABLE", "CASDA returned an invalid checksum length.")
    return ChecksumSpec(algorithm=algorithm, digest=digest)


def safe_filename(filename: str) -> str:
    leaf = Path(filename).name
    safe = INVALID_FILENAME.sub("_", leaf).strip(". ")
    if not safe or safe in {".", ".."}:
        raise ValidationError("CASDA metadata does not contain a safe filename.")
    return safe


def resolve_destination(
    base_directory: Path,
    destination: str | None,
    filename: str,
    *,
    allow_overwrite: bool,
) -> Path:
    """Resolve a caller destination while enforcing containment in the configured directory."""

    base = base_directory.resolve()
    requested = Path(destination) if destination else Path(safe_filename(filename))
    target = requested.resolve() if requested.is_absolute() else (base / requested).resolve()
    if target == base or not target.is_relative_to(base):
        raise CasdaError(
            "UNSAFE_DESTINATION",
            "The download destination must be a file inside CASDA_DOWNLOAD_DIR.",
        )
    if target.exists() and not allow_overwrite:
        raise CasdaError(
            "FILE_EXISTS",
            "The destination already exists and overwriting is disabled.",
            details={"destination": str(target)},
        )
    return target


def _hasher(algorithm: str):  # type: ignore[no-untyped-def]
    if algorithm == "md5":
        return hashlib.md5(usedforsecurity=False)  # noqa: S324
    return hashlib.new(algorithm)


class Downloader:
    def __init__(self, settings: Settings, client: CasdaClient, metrics: Metrics) -> None:
        self.settings = settings
        self.client = client
        self.metrics = metrics

    async def download(
        self,
        product: Product,
        artifact: ReadyArtifact,
        *,
        destination: str | None,
        verify_checksum: bool,
        correlation_id: str,
    ) -> DownloadResult:
        if self.settings.download_dir is None:
            raise CasdaError("DOWNLOADS_DISABLED", "Local downloads are not configured.")
        if product.file_size_bytes and product.file_size_bytes > self.settings.max_download_bytes:
            raise CasdaError(
                "DOWNLOAD_TOO_LARGE",
                "The product's estimated size exceeds CASDA_MAX_DOWNLOAD_BYTES.",
                details={"estimated_bytes": product.file_size_bytes},
            )
        target = resolve_destination(
            self.settings.download_dir,
            destination,
            product.filename or product.product_id,
            allow_overwrite=self.settings.allow_overwrite,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        checksum_spec = await self.checksum_spec(
            artifact, verify_checksum=verify_checksum, correlation_id=correlation_id
        )
        hasher = _hasher(checksum_spec.algorithm) if checksum_spec else None
        resumed = False
        expected_total: int | None = None
        try:
            for attempt in range(self.settings.max_retries + 1):
                offset = temporary.stat().st_size if temporary.exists() else 0
                if offset:
                    resumed = True
                try:
                    async with self.client.stream_download(
                        artifact.download_url,
                        offset=offset,
                        correlation_id=correlation_id,
                    ) as response:
                        content_length = self._content_length(response)
                        mode: Literal["ab", "wb"] = "ab"
                        if offset and response.status_code == 206:
                            expected_total = self._content_range_total(response, offset)
                        elif offset and response.status_code == 200:
                            # The archive ignored Range. Restart safely in this same tool call.
                            offset = 0
                            mode = "wb"
                            hasher = _hasher(checksum_spec.algorithm) if checksum_spec else None
                            expected_total = content_length
                        elif response.status_code in {200, 206}:
                            mode = "wb"
                            expected_total = (
                                self._content_range_total(response, 0)
                                if response.status_code == 206
                                else content_length
                            )
                        else:
                            raise CasdaError(
                                "DOWNLOAD_FAILED",
                                "CASDA returned an unsupported response to the download request.",
                            )
                        if expected_total > self.settings.max_download_bytes:
                            raise CasdaError(
                                "DOWNLOAD_TOO_LARGE",
                                "CASDA reported a file larger than CASDA_MAX_DOWNLOAD_BYTES.",
                                details={"content_length": expected_total},
                            )
                        received = 0
                        async with aiofiles.open(temporary, mode) as output:
                            async for chunk in response.aiter_bytes():
                                received += len(chunk)
                                if offset + received > self.settings.max_download_bytes:
                                    raise CasdaError(
                                        "DOWNLOAD_TOO_LARGE",
                                        "The streamed file exceeded CASDA_MAX_DOWNLOAD_BYTES.",
                                    )
                                await output.write(chunk)
                                if hasher:
                                    hasher.update(chunk)
                        if received != content_length:
                            raise CasdaError(
                                "CONTENT_LENGTH_MISMATCH",
                                "Downloaded bytes did not match CASDA's Content-Length.",
                                retryable=True,
                                details={"expected": content_length, "received": received},
                            )
                    break
                except (httpx.ReadError, httpx.TimeoutException, CasdaError) as exc:
                    retryable = not isinstance(exc, CasdaError) or exc.retryable
                    if not retryable or attempt >= self.settings.max_retries:
                        raise
                    continue
            actual_size = temporary.stat().st_size
            if expected_total is None or actual_size != expected_total:
                raise CasdaError(
                    "CONTENT_LENGTH_MISMATCH",
                    "The completed file size did not match the archive response.",
                    details={"expected": expected_total, "actual": actual_size},
                )
            checksum = ChecksumResult()
            if checksum_spec and hasher:
                actual_digest = hasher.hexdigest().lower()
                checksum = ChecksumResult(
                    algorithm=checksum_spec.algorithm,
                    expected=checksum_spec.digest,
                    actual=actual_digest,
                    verified=actual_digest == checksum_spec.digest,
                )
                if not checksum.verified:
                    self.metrics.increment("checksum_failure_count")
                    raise CasdaError(
                        "CHECKSUM_MISMATCH",
                        "The downloaded file did not match CASDA's checksum.",
                        details={"algorithm": checksum_spec.algorithm},
                    )
            os.replace(temporary, target)
            if not target.is_file():
                raise CasdaError(
                    "LOCAL_FILESYSTEM_ERROR", "The completed download file does not exist."
                )
            self.metrics.increment("download_count")
            self.metrics.increment("download_bytes", actual_size)
            return DownloadResult(
                product_id=product.product_id,
                local_path=str(target),
                bytes_downloaded=actual_size,
                content_length_verified=True,
                checksum=checksum,
                resumed=resumed,
                staging_request_id=artifact.request_id,
            )
        except OSError as exc:
            raise CasdaError(
                "LOCAL_FILESYSTEM_ERROR", "The local filesystem operation failed."
            ) from exc
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    async def checksum_spec(
        self,
        artifact: ReadyArtifact,
        *,
        verify_checksum: bool,
        correlation_id: str,
    ) -> ChecksumSpec | None:
        if not verify_checksum or not artifact.checksum_url:
            return None
        try:
            response = await self.client.request(
                "GET",
                artifact.checksum_url,
                headers={"Accept": "text/plain"},
                safe_to_retry=True,
                correlation_id=correlation_id,
                max_response_bytes=CHECKSUM_MAX_BYTES,
            )
        except CasdaError as exc:
            if exc.code != "ARCHIVE_RESPONSE_TOO_LARGE":
                raise
            raise CasdaError(
                "CHECKSUM_UNAVAILABLE", "CASDA returned an oversized checksum document."
            ) from exc
        if len(response.content) > CHECKSUM_MAX_BYTES:
            raise CasdaError(
                "CHECKSUM_UNAVAILABLE", "CASDA returned an oversized checksum document."
            )
        try:
            return parse_checksum(response.content.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise CasdaError(
                "CHECKSUM_UNAVAILABLE", "CASDA returned a non-text checksum document."
            ) from exc

    @staticmethod
    def _content_length(response: httpx.Response) -> int:
        value = response.headers.get("Content-Length")
        try:
            length = int(value or "")
        except ValueError as exc:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA did not provide a valid Content-Length for the download.",
            ) from exc
        if length < 0:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned a negative Content-Length."
            )
        return length

    @staticmethod
    def _content_range_total(response: httpx.Response, expected_start: int) -> int:
        match = CONTENT_RANGE_RE.fullmatch(response.headers.get("Content-Range", ""))
        if not match or int(match.group(1)) != expected_start:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an invalid Content-Range."
            )
        return int(match.group(3))
