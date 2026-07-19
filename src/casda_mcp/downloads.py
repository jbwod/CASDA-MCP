"""Guarded, streamed, resumable download helpers."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import hmac
import os
import random
import re
import stat
import tempfile
import time
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal

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
STRONG_ETAG_RE = re.compile(r'^"[\x21\x23-\x7e\x80-\xff]*"$')
INVALID_FILENAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
CHECKSUM_MAX_BYTES = 64 * 1024
INTERNAL_DIRECTORY = ".casda-mcp"
LOCK_DIRECTORY = "locks"
WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ChecksumSpec:
    algorithm: str
    digest: str


@dataclass(frozen=True, slots=True)
class DownloadValidator:
    header_name: Literal["ETag", "Last-Modified"]
    value: str


def parse_checksum(text: str) -> ChecksumSpec:
    """Parse common CASDA checksum sidecar formats without trusting the filename field."""

    labelled = list(CHECKSUM_RE.finditer(text))
    if labelled:
        identified = {
            (match.group(1).lower().replace("-", ""), match.group(2).lower()) for match in labelled
        }
        if len(identified) != 1:
            raise CasdaError(
                "CHECKSUM_UNAVAILABLE", "CASDA returned an ambiguous checksum document."
            )
        algorithm, digest = identified.pop()
        bare_digests = {match.group(1).lower() for match in BARE_CHECKSUM_RE.finditer(text)}
        if bare_digests - {digest}:
            raise CasdaError(
                "CHECKSUM_UNAVAILABLE", "CASDA returned an ambiguous checksum document."
            )
    else:
        bare = list(BARE_CHECKSUM_RE.finditer(text))
        identified_digests = {match.group(1).lower() for match in bare}
        if len(identified_digests) != 1:
            raise CasdaError(
                "CHECKSUM_UNAVAILABLE", "CASDA returned an unrecognised checksum document."
            )
        digest = identified_digests.pop()
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
    if base.parent == base:
        raise CasdaError(
            "UNSAFE_DESTINATION",
            "CASDA_DOWNLOAD_DIR must be a dedicated directory, not a filesystem root.",
        )
    requested = Path(destination) if destination else Path(safe_filename(filename))
    unresolved = requested if requested.is_absolute() else base / requested
    if unresolved.name in {"", ".", ".."}:
        raise CasdaError(
            "UNSAFE_DESTINATION",
            "The download destination must name a file inside CASDA_DOWNLOAD_DIR.",
        )
    target = unresolved.parent.resolve() / unresolved.name
    if target == base or not target.is_relative_to(base):
        raise CasdaError(
            "UNSAFE_DESTINATION",
            "The download destination must be a file inside CASDA_DOWNLOAD_DIR.",
        )
    relative_parts = target.relative_to(base).parts
    reserved_name = unicodedata.normalize("NFC", INTERNAL_DIRECTORY).casefold()
    if any(
        unicodedata.normalize("NFC", part).casefold() == reserved_name for part in relative_parts
    ):
        raise CasdaError(
            "UNSAFE_DESTINATION",
            "The download destination uses CASDA's reserved internal namespace.",
        )
    for component in relative_parts:
        _validate_portable_component(component)
    if os.path.lexists(target) and not allow_overwrite:
        raise CasdaError(
            "FILE_EXISTS",
            "The destination already exists and overwriting is disabled.",
            details={"destination": str(target)},
        )
    return target


def _validate_portable_component(component: str) -> None:
    """Reject aliases and device names that are unsafe on supported host filesystems."""

    if component != component.rstrip(". ") or INVALID_FILENAME.search(component):
        raise CasdaError(
            "UNSAFE_DESTINATION",
            "The download destination contains a non-portable path component.",
        )
    device_stem = component.split(".", 1)[0].upper()
    if device_stem in WINDOWS_RESERVED_NAMES:
        raise CasdaError(
            "UNSAFE_DESTINATION",
            "The download destination uses a reserved device name.",
        )


def _hasher(algorithm: str):  # type: ignore[no-untyped-def]
    if algorithm == "md5":
        return hashlib.md5(usedforsecurity=False)  # noqa: S324
    return hashlib.new(algorithm)


class Downloader:
    def __init__(self, settings: Settings, client: CasdaClient, metrics: Metrics) -> None:
        self.settings = settings
        self.client = client
        self.metrics = metrics
        self._download_root_identity: tuple[int, int] | None = None

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
        temporary: Path | None = None
        temporary_fd: int | None = None
        temporary_identity: tuple[int, int] | None = None
        reservation_path: Path | None = None
        reservation_identity: tuple[int, int] | None = None
        try:
            download_root = self._prepare_download_root()
            target = resolve_destination(
                download_root,
                destination,
                product.filename or product.product_id,
                allow_overwrite=self.settings.allow_overwrite,
            )
            self._prepare_target_parent(download_root, target.parent)
            self._verify_download_root(download_root)
            reservation_path, reservation_identity = self._reserve_target(download_root, target)
            self._ensure_target_available(target)
            target_digest = hashlib.sha256(
                os.fsencode(str(target.relative_to(download_root)))
            ).hexdigest()[:16]
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".casda-{target_digest}-", suffix=".part", dir=target.parent
            )
            temporary = Path(temporary_name)
            temporary_fd = descriptor
            details = os.fstat(temporary_fd)
            if not stat.S_ISREG(details.st_mode):
                raise CasdaError(
                    "LOCAL_FILESYSTEM_ERROR", "The download temporary file is not regular."
                )
            temporary_identity = (details.st_dev, details.st_ino)
            checksum_spec = await self.checksum_spec(
                artifact, verify_checksum=verify_checksum, correlation_id=correlation_id
            )
            actual_size, resumed, hasher = await self._transfer(
                temporary_fd,
                artifact,
                checksum_spec=checksum_spec,
                correlation_id=correlation_id,
            )
            checksum = ChecksumResult()
            if checksum_spec and hasher:
                actual_digest = hasher.hexdigest().lower()
                checksum = ChecksumResult(
                    algorithm=checksum_spec.algorithm,
                    expected=checksum_spec.digest,
                    actual=actual_digest,
                    verified=hmac.compare_digest(actual_digest, checksum_spec.digest),
                )
                if not checksum.verified:
                    self.metrics.increment("checksum_failure_count")
                    raise CasdaError(
                        "CHECKSUM_MISMATCH",
                        "The downloaded file did not match CASDA's checksum.",
                        details={"algorithm": checksum_spec.algorithm},
                    )
            os.fsync(temporary_fd)
            self._require_path_identity(temporary, temporary_identity, "temporary file")
            if os.name == "nt":
                # Windows commonly denies link/rename/unlink while the source
                # handle is open. The private directory and inode recheck retain
                # the same boundary after the descriptor-backed transfer ends.
                os.close(temporary_fd)
                temporary_fd = None
                self._require_path_identity(temporary, temporary_identity, "temporary file")
            self._publish(temporary, temporary_identity, target)
            self._sync_directory(target.parent)
            if target.is_symlink() or not target.is_file():
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
            if temporary_fd is not None:
                try:
                    os.close(temporary_fd)
                except OSError:
                    self.metrics.increment("download_cleanup_failure_count")
            if temporary is not None and temporary_identity is not None:
                self._unlink_if_owned(temporary, temporary_identity)
            if reservation_path is not None and reservation_identity is not None:
                self._unlink_if_owned(reservation_path, reservation_identity)

    async def _transfer(
        self,
        temporary_fd: int,
        artifact: ReadyArtifact,
        *,
        checksum_spec: ChecksumSpec | None,
        correlation_id: str,
    ) -> tuple[int, bool, Any | None]:
        hasher = _hasher(checksum_spec.algorithm) if checksum_spec else None
        expected_total: int | None = None
        validator: DownloadValidator | None = None
        resumed = False
        for attempt in range(self.settings.max_retries + 1):
            offset = os.fstat(temporary_fd).st_size
            if offset and (validator is None or expected_total is None or offset >= expected_total):
                os.ftruncate(temporary_fd, 0)
                os.lseek(temporary_fd, 0, os.SEEK_SET)
                offset = 0
                expected_total = None
                validator = None
                resumed = False
                hasher = _hasher(checksum_spec.algorithm) if checksum_spec else None
            try:
                async with self.client.stream_download(
                    artifact.download_url,
                    offset=offset,
                    if_range=validator.value if offset and validator is not None else None,
                    correlation_id=correlation_id,
                ) as response:
                    self._require_identity_encoding(response)
                    content_length = self._content_length(response)
                    if response.status_code == 200 and "Content-Range" in response.headers:
                        raise CasdaError(
                            "MALFORMED_ARCHIVE_RESPONSE",
                            "CASDA returned Content-Range with a complete download response.",
                        )
                    mode: Literal["ab", "wb"]
                    appended = False
                    if offset and response.status_code == 206:
                        total = self._content_range_total(
                            response,
                            expected_start=offset,
                            content_length=content_length,
                            expected_total=expected_total,
                        )
                        if validator is None:
                            raise CasdaError(
                                "MALFORMED_ARCHIVE_RESPONSE",
                                "CASDA returned a ranged response without an If-Range validator.",
                            )
                        self._require_matching_validator(response, validator)
                        expected_total = total
                        mode = "ab"
                        appended = True
                    elif offset and response.status_code == 200:
                        # If-Range either failed or Range was ignored. Replace every
                        # partial byte with the new complete representation.
                        offset = 0
                        mode = "wb"
                        expected_total = content_length
                        validator = self._response_validator(response)
                        resumed = False
                        hasher = _hasher(checksum_spec.algorithm) if checksum_spec else None
                    elif not offset and response.status_code == 206:
                        mode = "wb"
                        expected_total = self._content_range_total(
                            response,
                            expected_start=0,
                            content_length=content_length,
                            expected_total=None,
                        )
                        validator = self._response_validator(response)
                    elif not offset and response.status_code == 200:
                        mode = "wb"
                        expected_total = content_length
                        validator = self._response_validator(response)
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
                    if mode == "wb":
                        os.ftruncate(temporary_fd, 0)
                        os.lseek(temporary_fd, 0, os.SEEK_SET)
                    else:
                        os.lseek(temporary_fd, 0, os.SEEK_END)
                    output_fd = os.dup(temporary_fd)
                    try:
                        async with aiofiles.open(output_fd, mode, closefd=True) as output:
                            async for chunk in response.aiter_raw():
                                received += len(chunk)
                                if offset + received > self.settings.max_download_bytes:
                                    raise CasdaError(
                                        "DOWNLOAD_TOO_LARGE",
                                        "The streamed file exceeded CASDA_MAX_DOWNLOAD_BYTES.",
                                    )
                                await output.write(chunk)
                                if hasher:
                                    hasher.update(chunk)
                    finally:
                        with suppress(OSError):
                            os.close(output_fd)
                    if received != content_length:
                        raise CasdaError(
                            "CONTENT_LENGTH_MISMATCH",
                            "Downloaded bytes did not match CASDA's Content-Length.",
                            retryable=True,
                            details={"expected": content_length, "received": received},
                        )
                    actual_size = os.fstat(temporary_fd).st_size
                    if actual_size != expected_total:
                        raise CasdaError(
                            "CONTENT_LENGTH_MISMATCH",
                            "The completed file size did not match the archive response.",
                            retryable=True,
                            details={"expected": expected_total, "actual": actual_size},
                        )
                    if appended:
                        resumed = True
                    return actual_size, resumed, hasher
            except (httpx.TransportError, CasdaError) as exc:
                retryable = not isinstance(exc, CasdaError) or exc.retryable
                if not retryable or attempt >= self.settings.max_retries:
                    raise
                self.metrics.increment("download_retry_count")
                delay = self._download_backoff(attempt)
                if isinstance(exc, CasdaError):
                    archive_delay = exc.details.get("retry_after_seconds")
                    if isinstance(archive_delay, (int, float)) and archive_delay >= 0:
                        delay = min(float(archive_delay), 60.0)
                await asyncio.sleep(delay)
        raise RuntimeError("download retry loop exited without a result")

    def _prepare_download_root(self) -> Path:
        if self.settings.download_dir is None:  # pragma: no cover - guarded by caller
            raise CasdaError("DOWNLOADS_DISABLED", "Local downloads are not configured.")
        root = self.settings.download_dir
        self._require_safe_ancestors(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._require_safe_ancestors(root)
        details = self._require_private_directory(root)
        identity = (details.st_dev, details.st_ino)
        if self._download_root_identity is None:
            self._download_root_identity = identity
        elif identity != self._download_root_identity:
            raise CasdaError(
                "UNSAFE_DESTINATION",
                "CASDA_DOWNLOAD_DIR changed after the server established its write boundary.",
            )
        return root

    def _prepare_target_parent(self, root: Path, parent: Path) -> None:
        try:
            components = parent.relative_to(root).parts
        except ValueError as exc:  # pragma: no cover - resolve_destination already guards this
            raise CasdaError(
                "UNSAFE_DESTINATION", "The target directory is outside CASDA_DOWNLOAD_DIR."
            ) from exc
        current = root
        for component in components:
            current /= component
            current.mkdir(mode=0o700, exist_ok=True)
            self._require_private_directory(current)

    @staticmethod
    def _require_private_directory(directory: Path) -> os.stat_result:
        details = os.lstat(directory)
        if not stat.S_ISDIR(details.st_mode):
            raise CasdaError(
                "UNSAFE_DESTINATION",
                "CASDA download directories must not be symbolic links or special files.",
            )
        if os.name == "posix":
            if details.st_uid != os.geteuid():
                raise CasdaError(
                    "UNSAFE_DESTINATION",
                    "CASDA download directories must be owned by the server account.",
                )
            if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise CasdaError(
                    "UNSAFE_DESTINATION",
                    "CASDA download directories must not be group- or world-writable.",
                )
        return details

    @staticmethod
    def _require_safe_ancestors(directory: Path) -> None:
        """Reject path ancestors another OS account could rename or replace."""

        current = directory
        while not os.path.lexists(current) and current.parent != current:
            current = current.parent
        while True:
            details = os.lstat(current)
            if not stat.S_ISDIR(details.st_mode):
                raise CasdaError(
                    "UNSAFE_DESTINATION",
                    "CASDA_DOWNLOAD_DIR ancestors must not be symbolic links or special files.",
                )
            if os.name == "posix" and details.st_uid not in {0, os.geteuid()}:
                raise CasdaError(
                    "UNSAFE_DESTINATION",
                    "CASDA_DOWNLOAD_DIR ancestors must be owned by root or the server account.",
                )
            if (
                os.name == "posix"
                and details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                and not details.st_mode & stat.S_ISVTX
            ):
                raise CasdaError(
                    "UNSAFE_DESTINATION",
                    "CASDA_DOWNLOAD_DIR has a non-sticky group- or world-writable ancestor.",
                )
            if current.parent == current:
                return
            current = current.parent

    def _verify_download_root(self, root: Path) -> None:
        self._require_safe_ancestors(root)
        details = self._require_private_directory(root)
        identity = (details.st_dev, details.st_ino)
        if self._download_root_identity != identity:
            raise CasdaError(
                "UNSAFE_DESTINATION",
                "CASDA_DOWNLOAD_DIR changed during the download operation.",
            )

    def _reserve_target(self, root: Path, target: Path) -> tuple[Path, tuple[int, int]]:
        internal = root / INTERNAL_DIRECTORY
        locks = internal / LOCK_DIRECTORY
        for directory in (internal, locks):
            directory.mkdir(mode=0o700, exist_ok=True)
            self._require_private_directory(directory)
        relative = str(target.relative_to(root))
        lock_key = unicodedata.normalize("NFC", os.path.normcase(relative)).casefold()
        lock_name = f"{hashlib.sha256(os.fsencode(lock_key)).hexdigest()}.lock"
        reservation = locks / lock_name
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(reservation, flags, 0o600)
        except FileExistsError as exc:
            raise CasdaError(
                "DOWNLOAD_IN_PROGRESS",
                "Another download currently owns this destination.",
                details={"destination": str(target)},
            ) from exc
        identity: tuple[int, int] | None = None
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise CasdaError(
                    "UNSAFE_DESTINATION", "The download reservation is not a regular file."
                )
            identity = (details.st_dev, details.st_ino)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            metadata = f"pid={os.getpid()}\ntarget={relative}\ncreated={time.time():.6f}\n".encode()
            while metadata:
                written = os.write(descriptor, metadata)
                if written <= 0:  # pragma: no cover - defensive OS contract check
                    raise OSError("could not write the download reservation")
                metadata = metadata[written:]
            os.fsync(descriptor)
        except Exception:
            with suppress(OSError):
                os.close(descriptor)
            if identity is not None:
                self._unlink_if_owned(reservation, identity)
            raise
        try:
            os.close(descriptor)
        except OSError:
            self.metrics.increment("download_cleanup_failure_count")
        return reservation, identity

    def _ensure_target_available(self, target: Path) -> None:
        if not os.path.lexists(target):
            return
        if not self.settings.allow_overwrite:
            raise CasdaError(
                "FILE_EXISTS",
                "The destination already exists and overwriting is disabled.",
                details={"destination": str(target)},
            )
        if target.is_symlink() or not target.is_file():
            raise CasdaError(
                "UNSAFE_DESTINATION",
                "Only an existing regular file can be replaced.",
            )

    def _publish(
        self,
        temporary: Path,
        temporary_identity: tuple[int, int],
        target: Path,
    ) -> None:
        self._require_path_identity(temporary, temporary_identity, "temporary file")
        if self.settings.allow_overwrite:
            self._ensure_target_available(target)
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise CasdaError(
                    "FILE_EXISTS",
                    "The destination was created while the download was running.",
                    details={"destination": str(target)},
                ) from exc
            except OSError as exc:
                unsupported_errors = {
                    errno.EPERM,
                    errno.EXDEV,
                    getattr(errno, "ENOTSUP", errno.EINVAL),
                    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
                }
                if exc.errno in unsupported_errors:
                    raise CasdaError(
                        "UNSUPPORTED_DOWNLOAD_FILESYSTEM",
                        "The download filesystem does not support atomic no-clobber publication.",
                    ) from exc
                raise
            try:
                temporary.unlink()
            except OSError:
                # The verified target is already complete. Final cleanup retries
                # after the still-open temporary descriptor is closed.
                self.metrics.increment("download_cleanup_failure_count")
        self._require_path_identity(target, temporary_identity, "completed download")

    @staticmethod
    def _require_path_identity(
        path: Path,
        identity: tuple[int, int],
        description: str,
    ) -> None:
        try:
            details = os.lstat(path)
        except FileNotFoundError as exc:
            raise CasdaError(
                "LOCAL_FILESYSTEM_ERROR", f"The {description} disappeared unexpectedly."
            ) from exc
        if not stat.S_ISREG(details.st_mode) or (details.st_dev, details.st_ino) != identity:
            raise CasdaError(
                "UNSAFE_DESTINATION", f"The {description} changed during the download operation."
            )

    def _unlink_if_owned(self, path: Path, identity: tuple[int, int]) -> None:
        try:
            details = os.lstat(path)
            if (details.st_dev, details.st_ino) == identity:
                path.unlink()
            else:
                self.metrics.increment("download_cleanup_failure_count")
        except FileNotFoundError:
            pass
        except OSError:
            self.metrics.increment("download_cleanup_failure_count")

    def _sync_directory(self, directory: Path) -> None:
        if os.name != "posix":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(directory, flags)
            os.fsync(descriptor)
        except OSError:
            self.metrics.increment("download_directory_sync_failure_count")
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    self.metrics.increment("download_cleanup_failure_count")

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
        if value is None or not value.isascii() or not value.isdigit():
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA did not provide a valid Content-Length for the download.",
            )
        try:
            length = int(value)
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
    def _content_range_total(
        response: httpx.Response,
        *,
        expected_start: int,
        content_length: int,
        expected_total: int | None,
    ) -> int:
        match = CONTENT_RANGE_RE.fullmatch(response.headers.get("Content-Range", ""))
        if not match:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an invalid Content-Range."
            )
        start, end, total = (int(match.group(index)) for index in range(1, 4))
        if (
            start != expected_start
            or end < start
            or total <= end
            or end != total - 1
            or content_length != end - start + 1
            or (expected_total is not None and total != expected_total)
        ):
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an inconsistent Content-Range."
            )
        return total

    @staticmethod
    def _require_identity_encoding(response: httpx.Response) -> None:
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in {"", "identity"}:
            raise CasdaError(
                "UNSUPPORTED_CONTENT_ENCODING",
                "CASDA encoded a ranged download despite identity encoding being required.",
            )

    @staticmethod
    def _response_validator(response: httpx.Response) -> DownloadValidator | None:
        etag = response.headers.get("ETag", "").strip()
        if STRONG_ETAG_RE.fullmatch(etag):
            return DownloadValidator("ETag", etag)
        last_modified = response.headers.get("Last-Modified", "").strip()
        response_date = response.headers.get("Date", "").strip()
        if not last_modified or not response_date:
            return None
        try:
            modified_at = parsedate_to_datetime(last_modified)
            response_at = parsedate_to_datetime(response_date)
        except (TypeError, ValueError):
            return None
        if modified_at.tzinfo is None or response_at.tzinfo is None:
            return None
        # RFC 9110 permits a client to treat Last-Modified as strong only when
        # the response Date is at least 60 seconds later. This protects range
        # retries from joining two changes made within timestamp resolution.
        try:
            age_seconds = (response_at - modified_at).total_seconds()
        except OverflowError:
            return None
        if age_seconds < 60:
            return None
        return DownloadValidator("Last-Modified", last_modified)

    @staticmethod
    def _require_matching_validator(
        response: httpx.Response,
        validator: DownloadValidator,
    ) -> None:
        current = response.headers.get(validator.header_name)
        if current is None or current.strip() != validator.value:
            raise CasdaError(
                "DOWNLOAD_VALIDATOR_MISMATCH",
                "CASDA changed the file validator during a ranged download.",
            )

    @staticmethod
    def _download_backoff(attempt: int) -> float:
        return float(min(0.5 * (2**attempt) + random.uniform(0, 0.25), 10.0))  # noqa: S311
