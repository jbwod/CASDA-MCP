from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
from casda_mcp.downloads import Downloader, parse_checksum, resolve_destination
from casda_mcp.errors import CasdaError
from casda_mcp.models import Product, ReadyArtifact
from casda_mcp.observability import Metrics


def test_checksum_parser_supports_common_formats() -> None:
    assert parse_checksum("MD5: d41d8cd98f00b204e9800998ecf8427e").algorithm == "md5"
    assert parse_checksum("a" * 40 + " file.fits").algorithm == "sha1"
    assert parse_checksum("sha-256=" + "b" * 64).algorithm == "sha256"
    with pytest.raises(CasdaError):
        parse_checksum("not a checksum")


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


def make_downloader(tmp_path, *, max_bytes: int = 1024, retries: int = 0):
    settings = Settings(
        _env_file=None,
        enable_downloads=True,
        download_dir=tmp_path.resolve(),
        max_download_bytes=max_bytes,
        max_retries=retries,
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
    assert not list(tmp_path.glob("*.part"))


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
            httpx.Response(200, headers={"Content-Length": "6"}, stream=FailingStream()),
            httpx.Response(
                206,
                headers={"Content-Length": "3", "Content-Range": "bytes 3-5/6"},
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


async def test_service_downloads_are_disabled_by_default(settings) -> None:
    from casda_mcp.service import CasdaService

    service = CasdaService(settings)
    try:
        with pytest.raises(CasdaError) as error:
            await service.download_product("cube-1", destination=None, verify_checksum=True)
        assert error.value.code == "DOWNLOADS_DISABLED"
    finally:
        await service.aclose()
