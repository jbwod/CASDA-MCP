from __future__ import annotations

import gzip

import httpx
import pytest
import respx

from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError


class ChunkedStream(httpx.AsyncByteStream):
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


@pytest.fixture
def client() -> CasdaClient:
    return CasdaClient(Settings(_env_file=None, max_retries=0))


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
@respx.mock
async def test_client_rejects_chunked_response_over_decoded_limit(
    headers: dict[str, str],
) -> None:
    client = CasdaClient(Settings(_env_file=None, max_retries=0, max_response_bytes=1024))
    stream = ChunkedStream([b"a" * 700, b"b" * 325, b"not-read"])
    respx.get("https://casda.csiro.au/metadata").mock(
        return_value=httpx.Response(200, headers=headers, stream=stream)
    )
    try:
        with pytest.raises(CasdaError) as error:
            await client.request(
                "GET",
                "https://casda.csiro.au/metadata",
                safe_to_retry=True,
                correlation_id="test",
            )
        assert error.value.code == "ARCHIVE_RESPONSE_TOO_LARGE"
        assert error.value.details["received_bytes"] == 1025
        assert stream.chunks_read == 2
        assert stream.closed is True
    finally:
        await client.aclose()


@respx.mock
async def test_client_applies_limit_to_decompressed_response_body() -> None:
    client = CasdaClient(Settings(_env_file=None, max_retries=0, max_response_bytes=1024))
    compressed = gzip.compress(b"x" * 1025)
    assert len(compressed) < 1024
    stream = ChunkedStream([compressed])
    respx.get("https://casda.csiro.au/compressed").mock(
        return_value=httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
            },
            stream=stream,
        )
    )
    try:
        with pytest.raises(CasdaError) as error:
            await client.request(
                "GET",
                "https://casda.csiro.au/compressed",
                safe_to_retry=True,
                correlation_id="test",
            )
        assert error.value.code == "ARCHIVE_RESPONSE_TOO_LARGE"
        assert error.value.details["received_bytes"] == 1025
        assert stream.closed is True
    finally:
        await client.aclose()


@respx.mock
async def test_client_follows_only_allowlisted_redirects(client: CasdaClient) -> None:
    respx.get("https://data.csiro.au/start").mock(
        return_value=httpx.Response(302, headers={"Location": "https://casda.csiro.au/final"})
    )
    respx.get("https://casda.csiro.au/final").mock(return_value=httpx.Response(200, text="ok"))
    try:
        response = await client.request(
            "GET",
            "https://data.csiro.au/start",
            safe_to_retry=True,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert response.text == "ok"
    assert str(response.url) == "https://casda.csiro.au/final"


@respx.mock
async def test_client_rejects_redirect_to_untrusted_host(client: CasdaClient) -> None:
    respx.get("https://data.csiro.au/start").mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example/file"})
    )
    try:
        with pytest.raises(CasdaError) as error:
            await client.request(
                "GET",
                "https://data.csiro.au/start",
                safe_to_retry=True,
                correlation_id="test",
            )
        assert error.value.code == "UNSAFE_ARCHIVE_URL"
    finally:
        await client.aclose()


@respx.mock
async def test_client_maps_authentication_failure(client: CasdaClient) -> None:
    respx.get("https://data.csiro.au/private").mock(return_value=httpx.Response(401))
    try:
        with pytest.raises(CasdaError) as error:
            await client.request(
                "GET",
                "https://data.csiro.au/private",
                safe_to_retry=True,
                correlation_id="test",
            )
        assert error.value.code == "AUTHENTICATION_FAILED"
        assert error.value.retryable is False
    finally:
        await client.aclose()


def test_client_rejects_caller_controlled_host_before_network(client: CasdaClient) -> None:
    with pytest.raises(CasdaError) as error:
        client.validate_archive_url("https://example.com/not-casda")
    assert error.value.code == "UNSAFE_ARCHIVE_URL"


async def test_client_rejects_unexpected_datalink_path_before_network(
    client: CasdaClient,
) -> None:
    try:
        with pytest.raises(CasdaError) as error:
            await client.resolve_datalink(
                "https://data.csiro.au/unexpected?ID=cube-1", correlation_id="test"
            )
        assert error.value.code == "UNSAFE_ARCHIVE_URL"
    finally:
        await client.aclose()


@respx.mock
async def test_non_idempotent_stage_creation_redirect_is_followed_without_reposting() -> None:
    client = CasdaClient(
        Settings(
            _env_file=None,
            username="researcher@example.test",
            password="top-secret",  # noqa: S106
            max_retries=0,
        )
    )
    create = respx.post("https://casda.csiro.au/casda_data_access/data/async").mock(
        return_value=httpx.Response(
            303, headers={"Location": "/casda_data_access/data/async/job-1"}
        )
    )
    status = respx.get("https://casda.csiro.au/casda_data_access/data/async/job-1").mock(
        return_value=httpx.Response(200, text="<job/>")
    )
    try:
        job_url = await client.create_staging_job(
            "https://casda.csiro.au/casda_data_access/data/async",
            ["opaque-token"],  # noqa: S106
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert job_url.endswith("/job-1")
    assert create.call_count == 1
    assert status.call_count == 1


@respx.mock
async def test_credentials_are_not_sent_to_pawsey_download_hosts() -> None:
    client = CasdaClient(
        Settings(
            _env_file=None,
            username="researcher@example.test",
            password="top-secret",  # noqa: S106
            max_retries=0,
        )
    )
    route = respx.get("https://ingest.pawsey.org/archive/file.fits").mock(
        return_value=httpx.Response(200, text="ok")
    )
    try:
        await client.request(
            "GET",
            "https://ingest.pawsey.org/archive/file.fits",
            safe_to_retry=True,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
async def test_authenticated_redirect_strips_credentials_on_origin_change() -> None:
    client = CasdaClient(
        Settings(
            _env_file=None,
            username="researcher@example.test",
            password="top-secret",  # noqa: S106
            max_retries=0,
        )
    )
    start = respx.get("https://data.csiro.au/private").mock(
        return_value=httpx.Response(302, headers={"Location": "https://casda.csiro.au/final"})
    )
    final = respx.get("https://casda.csiro.au/final").mock(
        return_value=httpx.Response(200, text="ok")
    )
    try:
        await client.request(
            "GET",
            "https://data.csiro.au/private",
            safe_to_retry=True,
            authenticated=True,
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert start.calls[0].request.headers["authorization"].startswith("Basic ")
    assert "authorization" not in final.calls[0].request.headers


@respx.mock
async def test_protocol_requests_send_endpoint_specific_accept_headers() -> None:
    client = CasdaClient(
        Settings(
            _env_file=None,
            username="researcher@example.test",
            password="top-secret",  # noqa: S106
            max_retries=0,
        )
    )
    tap = respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(
        return_value=httpx.Response(200, content=b"column\r\nvalue\r\n")
    )
    datalink = respx.get("https://data.csiro.au/casda_vo_proxy/vo/datalink/links?ID=cube-1").mock(
        return_value=httpx.Response(
            200,
            content=b"""<VOTABLE xmlns='http://www.ivoa.net/xml/VOTable/v1.3' version='1.3'>
              <RESOURCE type='results'><TABLE>
                <FIELD datatype='char' arraysize='*' name='service_def'/>
                <FIELD datatype='char' arraysize='*' name='authenticated_id_token'/>
                <DATA><TABLEDATA><TR><TD>async_service</TD><TD>token</TD></TR></TABLEDATA></DATA>
              </TABLE></RESOURCE>
              <RESOURCE ID='async_service' type='meta'>
                <PARAM datatype='char' arraysize='*' name='accessURL'
                  value='https://casda.csiro.au/casda_data_access/data/async'/>
              </RESOURCE>
            </VOTABLE>""",
        )
    )
    status = respx.get("https://casda.csiro.au/casda_data_access/data/async/job-1").mock(
        return_value=httpx.Response(
            200,
            content=b"<uws:job xmlns:uws='http://www.ivoa.net/xml/UWS/v1.0'>"
            b"<uws:phase>COMPLETED</uws:phase></uws:job>",
        )
    )
    try:
        await client.tap_query("SELECT 1", max_records=1, correlation_id="test")
        await client.resolve_datalink(
            "https://data.csiro.au/casda_vo_proxy/vo/datalink/links?ID=cube-1",
            correlation_id="test",
        )
        await client.get_staging_status(
            "https://casda.csiro.au/casda_data_access/data/async/job-1",
            correlation_id="test",
        )
    finally:
        await client.aclose()
    assert tap.calls[0].request.headers["Accept"] == "text/csv"
    assert datalink.calls[0].request.headers["Accept"] == "application/x-votable+xml"
    assert status.calls[0].request.headers["Accept"] == "application/xml"


@respx.mock
async def test_binary_download_requests_identity_encoding() -> None:
    client = CasdaClient(Settings(_env_file=None, max_retries=0))
    route = respx.get("https://data.csiro.au/download/cube-1.fits").mock(
        return_value=httpx.Response(206, content=b"data")
    )
    try:
        async with client.stream_download(
            "https://data.csiro.au/download/cube-1.fits",
            offset=8,
            correlation_id="test",
        ):
            pass
    finally:
        await client.aclose()
    headers = route.calls[0].request.headers
    assert headers["Accept"] == "application/octet-stream"
    assert headers["Accept-Encoding"] == "identity"
    assert headers["Range"] == "bytes=8-"
