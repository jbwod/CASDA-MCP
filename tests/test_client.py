from __future__ import annotations

import httpx
import pytest
import respx

from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError


@pytest.fixture
def client() -> CasdaClient:
    return CasdaClient(Settings(_env_file=None, max_retries=0))


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


@respx.mock
async def test_non_idempotent_stage_creation_redirect_is_followed_without_reposting(
    client: CasdaClient,
) -> None:
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
