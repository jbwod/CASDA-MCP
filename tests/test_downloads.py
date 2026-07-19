from __future__ import annotations

import asyncio
import errno
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
from casda_mcp.downloads import (
    CHECKSUM_MAX_BYTES,
    INTERNAL_DIRECTORY,
    LOCK_DIRECTORY,
    Downloader,
    parse_checksum,
    resolve_destination,
)
from casda_mcp.errors import CasdaError
from casda_mcp.models import Product, ReadyArtifact
from casda_mcp.observability import Metrics


def test_checksum_parser_supports_common_formats() -> None:
    assert parse_checksum("MD5: d41d8cd98f00b204e9800998ecf8427e").algorithm == "md5"
    assert parse_checksum("a" * 40 + " file.fits").algorithm == "sha1"
    assert parse_checksum("sha-256=" + "b" * 64).algorithm == "sha256"
    with pytest.raises(CasdaError):
        parse_checksum("not a checksum")
    with pytest.raises(CasdaError, match="ambiguous"):
        parse_checksum("MD5: " + "a" * 32 + "\nSHA256: " + "b" * 64)


def test_destination_rejects_traversal_absolute_escape_and_existing_file(tmp_path) -> None:
    base = tmp_path.resolve()
    with pytest.raises(CasdaError, match="inside"):
        resolve_destination(base, "../escape.fits", "file.fits", allow_overwrite=False)
    with pytest.raises(CasdaError, match="inside"):
        resolve_destination(
            base,
            str((tmp_path.parent / "escape.fits").resolve()),
            "file.fits",
            allow_overwrite=False,
        )
    existing = base / "exists.fits"
    existing.write_bytes(b"existing")
    with pytest.raises(CasdaError) as error:
        resolve_destination(base, "exists.fits", "file.fits", allow_overwrite=False)
    assert error.value.code == "FILE_EXISTS"


def test_destination_does_not_follow_a_final_symlink(tmp_path) -> None:
    base = tmp_path.resolve()
    victim = base / "victim.fits"
    victim.write_bytes(b"preserve")
    link = base / "link.fits"
    link.symlink_to(victim)

    resolved = resolve_destination(
        base,
        "link.fits",
        "file.fits",
        allow_overwrite=True,
    )

    assert resolved == link


def test_destination_rejects_root_after_configured_symlink_is_retargeted(tmp_path) -> None:
    dedicated = tmp_path / "dedicated"
    dedicated.mkdir()
    configured = tmp_path / "configured"
    configured.symlink_to(dedicated, target_is_directory=True)
    canonical = configured.resolve()

    configured.unlink()
    configured.symlink_to(Path(tmp_path.anchor), target_is_directory=True)

    with pytest.raises(CasdaError, match="filesystem root"):
        resolve_destination(configured, "tmp/escape.fits", "file.fits", allow_overwrite=False)
    assert resolve_destination(
        canonical, "still-contained.fits", "file.fits", allow_overwrite=False
    ) == (dedicated / "still-contained.fits")


@pytest.mark.parametrize(
    "destination",
    [
        f"{INTERNAL_DIRECTORY}/{LOCK_DIRECTORY}/poison.lock",
        f"{INTERNAL_DIRECTORY.upper()}/{LOCK_DIRECTORY}/poison.lock",
        "CON",
        "aux.fits",
        "trailing-dot.",
        "alternate:stream",
    ],
)
def test_destination_rejects_internal_and_nonportable_names(tmp_path, destination) -> None:
    with pytest.raises(CasdaError) as error:
        resolve_destination(
            tmp_path.resolve(),
            destination,
            "file.fits",
            allow_overwrite=False,
        )
    assert error.value.code == "UNSAFE_DESTINATION"


@pytest.fixture(autouse=True)
def disable_download_retry_delay(monkeypatch) -> None:
    monkeypatch.setattr(Downloader, "_download_backoff", staticmethod(lambda attempt: 0.0))


def make_downloader(
    tmp_path,
    *,
    max_bytes: int = 1024,
    retries: int = 0,
    allow_overwrite: bool = False,
):
    settings = Settings(
        _env_file=None,
        enable_downloads=True,
        download_dir=tmp_path.resolve(),
        max_download_bytes=max_bytes,
        max_retries=retries,
        allow_overwrite=allow_overwrite,
    )
    metrics = Metrics()
    client = CasdaClient(settings, metrics=metrics)
    return Downloader(settings, client, metrics), client


def product(size: int = 6) -> Product:
    return Product(product_id="cube-1", filename="cube-1.fits", file_size_bytes=size)


def artifact(checksum: bool = True) -> ReadyArtifact:
    return ReadyArtifact(
        product_id="cube-1",
        request_id="job-1",
        download_url="https://data.csiro.au/download/cube-1.fits?signature=secret",
        checksum_url=(
            "https://data.csiro.au/download/cube-1.fits.checksum?signature=secret"
            if checksum
            else None
        ),
        confirmed_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


class FailingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"abc"
        raise httpx.ReadError("connection interrupted")


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.chunks_read = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class ProtocolFailingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise httpx.RemoteProtocolError("peer closed the response")
        yield b""  # pragma: no cover


class BlockingStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        await self.release.wait()
        yield self.data


@respx.mock
async def test_streamed_download_verifies_length_and_checksum(tmp_path) -> None:
    data = b"abcdef"
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()  # noqa: S324
    respx.get(url__regex=r".*\.checksum.*").mock(
        return_value=httpx.Response(200, content=f"{digest} cube-1.fits")
    )
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, content=data)
    )
    downloader, client = make_downloader(tmp_path)
    try:
        result = await downloader.download(
            product(), artifact(), destination=None, verify_checksum=True, correlation_id="test"
        )
    finally:
        await client.aclose()
    assert result.bytes_downloaded == 6
    assert result.checksum.verified is True
    assert (tmp_path / "cube-1.fits").read_bytes() == data
    assert not list(tmp_path.glob(".*.part"))
    assert not list(tmp_path.glob(".*.casda-download.lock"))


@respx.mock
async def test_checksum_mismatch_removes_incomplete_file(tmp_path) -> None:
    respx.get(url__regex=r".*\.checksum.*").mock(
        return_value=httpx.Response(200, content=("0" * 32) + " cube-1.fits")
    )
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, content=b"abcdef")
    )
    downloader, client = make_downloader(tmp_path)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(), artifact(), destination=None, verify_checksum=True, correlation_id="test"
            )
        assert error.value.code == "CHECKSUM_MISMATCH"
    finally:
        await client.aclose()
    assert not (tmp_path / "cube-1.fits").exists()
    assert not list(tmp_path.glob(".*.part"))


@respx.mock
async def test_checksum_response_is_stopped_at_64_kib_before_buffering(tmp_path) -> None:
    stream = CountingStream([b"a" * CHECKSUM_MAX_BYTES, b"b", b"not-read"])
    route = respx.get(url__regex=r".*\.checksum.*").mock(
        return_value=httpx.Response(200, stream=stream)
    )
    downloader, client = make_downloader(tmp_path)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.checksum_spec(artifact(), verify_checksum=True, correlation_id="test")
        assert error.value.code == "CHECKSUM_UNAVAILABLE"
    finally:
        await client.aclose()
    assert stream.chunks_read == 2
    assert stream.closed is True
    assert route.calls[0].request.headers["Accept"] == "text/plain"


@respx.mock
async def test_download_rejects_archive_size_over_limit_before_writing(tmp_path) -> None:
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "2048"}, content=b"x")
    )
    downloader, client = make_downloader(tmp_path, max_bytes=100)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(size=1),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
        assert error.value.code == "DOWNLOAD_TOO_LARGE"
    finally:
        await client.aclose()
    assert not (tmp_path / "cube-1.fits").exists()


@respx.mock
async def test_download_resumes_with_range_after_transient_read_failure(tmp_path) -> None:
    route = respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"Content-Length": "6", "ETag": '"version-one"'},
                stream=FailingStream(),
            ),
            httpx.Response(
                206,
                headers={
                    "Content-Length": "3",
                    "Content-Range": "bytes 3-5/6",
                    "ETag": '"version-one"',
                },
                content=b"def",
            ),
        ]
    )
    downloader, client = make_downloader(tmp_path, retries=1)
    try:
        result = await downloader.download(
            product(),
            artifact(checksum=False),
            destination=None,
            verify_checksum=False,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert result.resumed is True
    assert (tmp_path / "cube-1.fits").read_bytes() == b"abcdef"
    assert route.calls[1].request.headers["Range"] == "bytes=3-"
    assert route.calls[1].request.headers["If-Range"] == '"version-one"'


@pytest.mark.parametrize(
    "first_headers",
    [
        {},
        {"ETag": 'W/"weak-version"'},
        {"ETag": "not-an-entity-tag"},
        {
            "Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT",
            "Date": "Wed, 21 Oct 2015 07:28:30 GMT",
        },
    ],
)
@respx.mock
async def test_download_without_strong_validator_restarts_from_zero(
    tmp_path,
    first_headers,
) -> None:
    data = b"abcdef"
    digest = hashlib.md5(data, usedforsecurity=False).hexdigest()  # noqa: S324
    respx.get(url__regex=r".*\.checksum.*").mock(
        return_value=httpx.Response(200, content=f"{digest} cube-1.fits")
    )
    headers = {"Content-Length": "6", **first_headers}
    route = respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        side_effect=[
            httpx.Response(200, headers=headers, stream=FailingStream()),
            httpx.Response(200, headers={"Content-Length": "6"}, content=data),
        ]
    )
    downloader, client = make_downloader(tmp_path, retries=1)
    try:
        result = await downloader.download(
            product(), artifact(), destination=None, verify_checksum=True, correlation_id="test"
        )
    finally:
        await client.aclose()

    assert result.resumed is False
    assert result.checksum.verified is True
    assert "Range" not in route.calls[1].request.headers
    assert "If-Range" not in route.calls[1].request.headers
    assert (tmp_path / "cube-1.fits").read_bytes() == data


@respx.mock
async def test_download_uses_last_modified_as_if_range_fallback(tmp_path) -> None:
    modified = "Wed, 21 Oct 2015 07:28:00 GMT"
    response_date = "Wed, 21 Oct 2015 07:30:00 GMT"
    route = respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={
                    "Content-Length": "6",
                    "Last-Modified": modified,
                    "Date": response_date,
                },
                stream=FailingStream(),
            ),
            httpx.Response(
                206,
                headers={
                    "Content-Length": "3",
                    "Content-Range": "bytes 3-5/6",
                    "Last-Modified": modified,
                },
                content=b"def",
            ),
        ]
    )
    downloader, client = make_downloader(tmp_path, retries=1)
    try:
        result = await downloader.download(
            product(),
            artifact(checksum=False),
            destination=None,
            verify_checksum=False,
            correlation_id="test",
        )
    finally:
        await client.aclose()

    assert result.resumed is True
    assert route.calls[1].request.headers["If-Range"] == modified
    assert (tmp_path / "cube-1.fits").read_bytes() == b"abcdef"


@respx.mock
async def test_full_response_after_range_replaces_every_partial_byte(tmp_path) -> None:
    replacement = b"uvwxyz"
    digest = hashlib.sha256(replacement).hexdigest()
    respx.get(url__regex=r".*\.checksum.*").mock(
        return_value=httpx.Response(200, content=f"SHA-256: {digest}")
    )
    route = respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"Content-Length": "6", "ETag": '"version-one"'},
                stream=FailingStream(),
            ),
            httpx.Response(
                200,
                headers={"Content-Length": "6", "ETag": '"version-two"'},
                content=replacement,
            ),
        ]
    )
    downloader, client = make_downloader(tmp_path, retries=1)
    try:
        result = await downloader.download(
            product(),
            artifact(),
            destination=None,
            verify_checksum=True,
            correlation_id="test",
        )
    finally:
        await client.aclose()

    assert route.calls[1].request.headers["Range"] == "bytes=3-"
    assert result.resumed is False
    assert result.checksum.verified is True
    assert (tmp_path / "cube-1.fits").read_bytes() == replacement


@respx.mock
async def test_changed_validator_on_partial_response_is_rejected(tmp_path) -> None:
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"Content-Length": "6", "ETag": '"version-one"'},
                stream=FailingStream(),
            ),
            httpx.Response(
                206,
                headers={
                    "Content-Length": "3",
                    "Content-Range": "bytes 3-5/6",
                    "ETag": '"version-two"',
                },
                content=b"def",
            ),
        ]
    )
    downloader, client = make_downloader(tmp_path, retries=1)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
        assert error.value.code == "DOWNLOAD_VALIDATOR_MISMATCH"
    finally:
        await client.aclose()
    assert not (tmp_path / "cube-1.fits").exists()
    assert not list(tmp_path.glob(".*.part"))
    assert not list(tmp_path.glob(".*.casda-download.lock"))


@respx.mock
async def test_missing_validator_on_partial_response_is_rejected(tmp_path) -> None:
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"Content-Length": "6", "ETag": '"version-one"'},
                stream=FailingStream(),
            ),
            httpx.Response(
                206,
                headers={
                    "Content-Length": "3",
                    "Content-Range": "bytes 3-5/6",
                },
                content=b"def",
            ),
        ]
    )
    downloader, client = make_downloader(tmp_path, retries=1)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
        assert error.value.code == "DOWNLOAD_VALIDATOR_MISMATCH"
    finally:
        await client.aclose()
    assert not (tmp_path / "cube-1.fits").exists()


@respx.mock
async def test_content_range_on_complete_response_is_rejected(tmp_path) -> None:
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Length": "3", "Content-Range": "bytes 0-2/6"},
            content=b"abc",
        )
    )
    downloader, client = make_downloader(tmp_path)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
        assert error.value.code == "MALFORMED_ARCHIVE_RESPONSE"
    finally:
        await client.aclose()
    assert not (tmp_path / "cube-1.fits").exists()


@pytest.mark.parametrize(
    ("content_range", "content_length", "body"),
    [
        ("bytes 2-5/6", "4", b"cdef"),
        ("bytes 3-4/6", "2", b"de"),
        ("bytes 3-5/7", "3", b"def"),
        ("bytes 3-5/6", "2", b"de"),
    ],
)
@respx.mock
async def test_inconsistent_content_range_is_rejected(
    tmp_path,
    content_range,
    content_length,
    body,
) -> None:
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"Content-Length": "6", "ETag": '"version-one"'},
                stream=FailingStream(),
            ),
            httpx.Response(
                206,
                headers={
                    "Content-Length": content_length,
                    "Content-Range": content_range,
                    "ETag": '"version-one"',
                },
                content=body,
            ),
        ]
    )
    downloader, client = make_downloader(tmp_path, retries=1)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
        assert error.value.code == "MALFORMED_ARCHIVE_RESPONSE"
    finally:
        await client.aclose()
    assert not (tmp_path / "cube-1.fits").exists()


@respx.mock
async def test_encoded_download_response_is_rejected(tmp_path) -> None:
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Length": "3", "Content-Encoding": "gzip"},
            stream=CountingStream([b"zip"]),
        )
    )
    downloader, client = make_downloader(tmp_path)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(size=3),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
        assert error.value.code == "UNSUPPORTED_CONTENT_ENCODING"
    finally:
        await client.aclose()


@respx.mock
async def test_checksum_is_computed_over_raw_binary_bytes(tmp_path) -> None:
    data = b"\xff\x00\x80raw"
    digest = hashlib.sha256(data).hexdigest()
    respx.get(url__regex=r".*\.checksum.*").mock(
        return_value=httpx.Response(200, content=f"SHA-256: {digest}")
    )
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": str(len(data))}, content=data)
    )
    downloader, client = make_downloader(tmp_path)
    try:
        result = await downloader.download(
            product(size=len(data)),
            artifact(),
            destination=None,
            verify_checksum=True,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert result.checksum.verified is True
    assert (tmp_path / "cube-1.fits").read_bytes() == data


@respx.mock
async def test_remote_protocol_error_is_retried_with_backoff(tmp_path, monkeypatch) -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(Downloader, "_download_backoff", staticmethod(lambda attempt: 1.25))
    monkeypatch.setattr("casda_mcp.downloads.asyncio.sleep", record_sleep)
    route = respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"Content-Length": "6"},
                stream=ProtocolFailingStream(),
            ),
            httpx.Response(200, headers={"Content-Length": "6"}, content=b"abcdef"),
        ]
    )
    downloader, client = make_downloader(tmp_path, retries=1)
    try:
        result = await downloader.download(
            product(),
            artifact(checksum=False),
            destination=None,
            verify_checksum=False,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert result.bytes_downloaded == 6
    assert route.call_count == 2
    assert sleeps == [1.25]


@respx.mock
async def test_download_retry_honours_bounded_retry_after(tmp_path, monkeypatch) -> None:
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("casda_mcp.downloads.asyncio.sleep", record_sleep)
    route = respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, headers={"Content-Length": "6"}, content=b"abcdef"),
        ]
    )
    downloader, client = make_downloader(tmp_path, retries=1)
    try:
        await downloader.download(
            product(),
            artifact(checksum=False),
            destination=None,
            verify_checksum=False,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert route.call_count == 2
    assert sleeps == [2.0]


@respx.mock
async def test_concurrent_download_is_rejected_before_second_network_request(tmp_path) -> None:
    stream = BlockingStream(b"abcdef")
    route = respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, stream=stream)
    )
    downloader, client = make_downloader(tmp_path)
    first = asyncio.create_task(
        downloader.download(
            product(),
            artifact(checksum=False),
            destination=None,
            verify_checksum=False,
            correlation_id="first",
        )
    )
    await stream.entered.wait()
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="second",
            )
        assert error.value.code == "DOWNLOAD_IN_PROGRESS"
    finally:
        stream.release.set()
        await first
        await client.aclose()
    assert route.call_count == 1
    assert (tmp_path / "cube-1.fits").read_bytes() == b"abcdef"


@respx.mock
async def test_atomic_publish_preserves_file_created_during_download(tmp_path) -> None:
    stream = BlockingStream(b"abcdef")
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, stream=stream)
    )
    downloader, client = make_downloader(tmp_path)
    task = asyncio.create_task(
        downloader.download(
            product(),
            artifact(checksum=False),
            destination=None,
            verify_checksum=False,
            correlation_id="test",
        )
    )
    await stream.entered.wait()
    target = tmp_path / "cube-1.fits"
    target.write_bytes(b"external")
    stream.release.set()
    try:
        with pytest.raises(CasdaError) as error:
            await task
        assert error.value.code == "FILE_EXISTS"
    finally:
        await client.aclose()
    assert target.read_bytes() == b"external"
    assert not list(tmp_path.glob(".*.part"))
    assert not list(tmp_path.glob(".*.casda-download.lock"))


@respx.mock
async def test_temporary_path_substitution_cannot_redirect_writes(tmp_path) -> None:
    stream = BlockingStream(b"abcdef")
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, stream=stream)
    )
    victim = tmp_path / "victim.fits"
    victim.write_bytes(b"preserve")
    downloader, client = make_downloader(tmp_path)
    task = asyncio.create_task(
        downloader.download(
            product(),
            artifact(checksum=False),
            destination=None,
            verify_checksum=False,
            correlation_id="test",
        )
    )
    await stream.entered.wait()
    temporary = next(tmp_path.glob(".casda-*.part"))
    temporary.unlink()
    temporary.symlink_to(victim)
    stream.release.set()
    try:
        with pytest.raises(CasdaError) as error:
            await task
        assert error.value.code == "UNSAFE_DESTINATION"
    finally:
        await client.aclose()
        temporary.unlink(missing_ok=True)
    assert victim.read_bytes() == b"preserve"
    assert not (tmp_path / "cube-1.fits").exists()


@respx.mock
async def test_private_download_directory_permissions_are_required(tmp_path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX mode enforcement is not available")
    tmp_path.chmod(0o770)
    downloader, client = make_downloader(tmp_path)
    try:
        with pytest.raises(CasdaError, match="group- or world-writable"):
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
    finally:
        tmp_path.chmod(0o700)
        await client.aclose()


@respx.mock
async def test_nonsticky_writable_download_ancestor_is_rejected(tmp_path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX mode enforcement is not available")
    unsafe_parent = tmp_path / "shared"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    root = unsafe_parent / "downloads"
    downloader, client = make_downloader(root)
    try:
        with pytest.raises(CasdaError, match="non-sticky"):
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
    finally:
        unsafe_parent.chmod(0o700)
        await client.aclose()
    assert not root.exists()


@respx.mock
async def test_reservation_setup_does_not_delete_replacement_inode(tmp_path, monkeypatch) -> None:
    if os.name != "posix":
        pytest.skip("POSIX reservation permission setup is not available")
    replacement: Path | None = None

    def replace_lock_then_fail(descriptor: int, mode: int) -> None:
        nonlocal replacement
        del descriptor, mode
        replacement = next((tmp_path / INTERNAL_DIRECTORY / LOCK_DIRECTORY).glob("*.lock"))
        replacement.unlink()
        replacement.write_text("external replacement")
        raise OSError("simulated permission failure")

    monkeypatch.setattr("casda_mcp.downloads.os.fchmod", replace_lock_then_fail)
    downloader, client = make_downloader(tmp_path)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
        assert error.value.code == "LOCAL_FILESYSTEM_ERROR"
    finally:
        await client.aclose()
    assert replacement is not None
    assert replacement.read_text() == "external replacement"
    replacement.unlink()


@respx.mock
async def test_internal_names_are_bounded_for_long_destination(tmp_path) -> None:
    destination = "x" * 240
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, content=b"abcdef")
    )
    downloader, client = make_downloader(tmp_path)
    try:
        result = await downloader.download(
            product(),
            artifact(checksum=False),
            destination=destination,
            verify_checksum=False,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert Path(result.local_path).name == destination
    assert not list((tmp_path / INTERNAL_DIRECTORY / LOCK_DIRECTORY).glob("*.lock"))


@respx.mock
async def test_unsupported_no_clobber_filesystem_is_reported(tmp_path, monkeypatch) -> None:
    def unsupported_link(*args, **kwargs) -> None:
        raise OSError(errno.EOPNOTSUPP, "hard links unavailable")

    monkeypatch.setattr("casda_mcp.downloads.os.link", unsupported_link)
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, content=b"abcdef")
    )
    downloader, client = make_downloader(tmp_path)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination=None,
                verify_checksum=False,
                correlation_id="test",
            )
        assert error.value.code == "UNSUPPORTED_DOWNLOAD_FILESYSTEM"
    finally:
        await client.aclose()
    assert not (tmp_path / "cube-1.fits").exists()


@respx.mock
async def test_source_cleanup_failure_does_not_hide_completed_download(
    tmp_path,
    monkeypatch,
) -> None:
    original_unlink = Path.unlink
    failed_once = False

    def fail_first_temporary_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal failed_once
        if path.name.endswith(".part") and not failed_once:
            failed_once = True
            raise OSError("simulated cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_temporary_unlink)
    respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, content=b"abcdef")
    )
    downloader, client = make_downloader(tmp_path)
    try:
        result = await downloader.download(
            product(),
            artifact(checksum=False),
            destination=None,
            verify_checksum=False,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert failed_once is True
    assert Path(result.local_path).read_bytes() == b"abcdef"
    assert not list(tmp_path.glob(".casda-*.part"))


@respx.mock
async def test_explicit_overwrite_replaces_only_a_regular_file(tmp_path) -> None:
    target = tmp_path / "cube-1.fits"
    target.write_bytes(b"old")
    route = respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, content=b"abcdef")
    )
    downloader, client = make_downloader(tmp_path, allow_overwrite=True)
    try:
        await downloader.download(
            product(),
            artifact(checksum=False),
            destination=None,
            verify_checksum=False,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert route.call_count == 1
    assert target.read_bytes() == b"abcdef"


@respx.mock
async def test_explicit_overwrite_rejects_a_symlink_destination(tmp_path) -> None:
    victim = tmp_path / "victim.fits"
    victim.write_bytes(b"preserve")
    link = tmp_path / "link.fits"
    link.symlink_to(victim)
    route = respx.get(url__regex=r".*cube-1\.fits\?signature=secret").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "6"}, content=b"abcdef")
    )
    downloader, client = make_downloader(tmp_path, allow_overwrite=True)
    try:
        with pytest.raises(CasdaError) as error:
            await downloader.download(
                product(),
                artifact(checksum=False),
                destination="link.fits",
                verify_checksum=False,
                correlation_id="test",
            )
        assert error.value.code == "UNSAFE_DESTINATION"
    finally:
        await client.aclose()
    assert route.call_count == 0
    assert victim.read_bytes() == b"preserve"


async def test_service_downloads_are_disabled_by_default(settings) -> None:
    from casda_mcp.service import CasdaService

    service = CasdaService(settings)
    try:
        with pytest.raises(CasdaError) as error:
            await service.download_product("cube-1", destination=None, verify_checksum=True)
        assert error.value.code == "DOWNLOADS_DISABLED"
    finally:
        await service.aclose()
