from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError
from casda_mcp.models import ReadyArtifact, StagingItem, StagingRequest, UwsResult
from casda_mcp.parsers import parse_datalink_access, parse_datalink_descriptors
from casda_mcp.service import CasdaService
from casda_mcp.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"

PRODUCT_ROW = {
    "obs_publisher_did": "cube-1",
    "filename": "cube-1.fits",
    "access_estsize": "100",
    "access_url": "https://data.csiro.au/casda_vo_proxy/vo/datalink/links?ID=cube-1",
    "obs_collection": "WALLABY",
    "facility_name": "ASKAP",
    "obs_release_date": "2020-01-01T00:00:00Z",
}


def test_parse_datalink_lists_all_access_descriptors() -> None:
    descriptors = parse_datalink_descriptors((FIXTURES / "datalink_cutout.xml").read_bytes())
    names = [item.service_name for item in descriptors]
    assert names == [
        "async_service",
        "cutout_service",
        "pawsey_async_service",
        "spectrum_generation_service",
    ]
    cutout = next(item for item in descriptors if item.service_name == "cutout_service")
    assert cutout.authenticated_id_present is True
    assert cutout.authenticated_id_token == "secret-cutout-token"  # noqa: S105
    assert cutout.content_type == "application/fits"
    assert cutout.size_bytes == 256
    # Inspection must be able to select cutout without requiring async_service.
    access = parse_datalink_access(
        (FIXTURES / "datalink_cutout.xml").read_bytes(), service_name="cutout_service"
    )
    assert access.service_url.endswith("/cutout")
    assert access.authenticated_id_token == "secret-cutout-token"  # noqa: S105


@respx.mock
async def test_get_auth_status_and_datalink_redacts_tokens(tmp_path) -> None:
    login = respx.get("https://data.csiro.au/casda_vo_proxy/vo/tap/availability").mock(
        return_value=httpx.Response(200, content=b"<availability><available>true</available>")
    )
    respx.get("https://data.csiro.au/casda_vo_proxy/vo/datalink/links").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "datalink_cutout.xml").read_bytes())
    )
    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(
        return_value=httpx.Response(
            200,
            content=(
                b"obs_publisher_did,filename,access_estsize,access_url,obs_collection,"
                b"facility_name,obs_release_date\n"
                b"cube-1,cube-1.fits,100,"
                b"https://data.csiro.au/casda_vo_proxy/vo/datalink/links?ID=cube-1,"
                b"WALLABY,ASKAP,2020-01-01T00:00:00Z\n"
            ),
        )
    )
    settings = Settings(
        _env_file=None,
        username="researcher@example.test",
        password="test-password",  # noqa: S106
        enable_staging=True,
        download_dir=tmp_path.resolve(),
        max_retries=0,
    )
    service = CasdaService(settings)
    try:
        auth = await service.get_auth_status()
        assert auth.credentials_configured is True
        assert auth.authenticated is True
        assert login.called

        datalink = await service.get_datalink("cube-1")
        payload = datalink.model_dump_json()
        assert "secret-cutout-token" not in payload
        assert "secret-async-token" not in payload
        assert {item.service_name for item in datalink.services} >= {
            "async_service",
            "cutout_service",
            "spectrum_generation_service",
            "pawsey_async_service",
        }
        cutout = next(item for item in datalink.services if item.service_name == "cutout_service")
        assert cutout.authenticated_id_present is True
        assert cutout.size_bytes == 256
    finally:
        await service.aclose()


@respx.mock
async def test_create_cutout_job_and_data_job_lifecycle(tmp_path) -> None:
    respx.get("https://data.csiro.au/casda_vo_proxy/vo/tap/availability").mock(
        return_value=httpx.Response(200, content=b"<ok/>")
    )
    respx.get("https://data.csiro.au/casda_vo_proxy/vo/datalink/links").mock(
        return_value=httpx.Response(200, content=(FIXTURES / "datalink_cutout.xml").read_bytes())
    )
    create = respx.post("https://casda.csiro.au/casda_data_access/data/cutout").mock(
        return_value=httpx.Response(
            303,
            headers={"Location": "https://casda.csiro.au/casda_data_access/data/cutout/job-cut-1"},
        )
    )
    respx.post("https://casda.csiro.au/casda_data_access/data/cutout/job-cut-1/phase").mock(
        return_value=httpx.Response(200, content=b"<ok/>")
    )
    status_route = respx.get("https://casda.csiro.au/casda_data_access/data/cutout/job-cut-1").mock(
        return_value=httpx.Response(
            200, content=(FIXTURES / "uws_cutout_completed.xml").read_bytes()
        )
    )
    respx.delete("https://casda.csiro.au/casda_data_access/data/cutout/job-cut-1").mock(
        return_value=httpx.Response(200, content=b"<ok/>")
    )
    respx.post("https://casda.csiro.au/casda_vo_tools/tap/sync").mock(
        return_value=httpx.Response(
            200,
            content=(
                b"obs_publisher_did,filename,access_estsize,access_url,obs_collection,"
                b"facility_name,obs_release_date\n"
                b"cube-1,cube-1.fits,100,"
                b"https://data.csiro.au/casda_vo_proxy/vo/datalink/links?ID=cube-1,"
                b"WALLABY,ASKAP,2020-01-01T00:00:00Z\n"
            ),
        )
    )

    settings = Settings(
        _env_file=None,
        username="researcher@example.test",
        password="test-password",  # noqa: S106
        enable_staging=True,
        enable_downloads=True,
        download_dir=tmp_path.resolve(),
        max_retries=0,
    )
    service = CasdaService(settings)
    try:
        created = await service.create_cutout(
            "cube-1",
            circle="187.5 -60.0 0.1",
            band="0.2 0.3",
            idempotency_key="cutout-run-1",
        )
        assert created.request_id == "job-cut-1"
        assert created.job_kind == "cutout"
        assert create.called
        params = parse_qs(urlparse(str(create.calls[0].request.url)).query)
        assert params["ID"] == ["secret-cutout-token"]
        assert params["CIRCLE"] == ["187.5 -60.0 0.1"]
        assert params["BAND"] == ["0.2 0.3"]

        status = await service.get_data_job("job-cut-1")
        assert status.status == "COMPLETED"
        assert status.job_kind == "cutout"
        assert status.download_ready is True
        assert status_route.call_count >= 1

        results = await service.get_data_job_results("job-cut-1")
        assert [item.result_id for item in results.results] == [
            "cutout-result-1",
            "cutout-result-1.checksum",
        ]
        results_json = results.model_dump_json(exclude={"provenance"})
        assert "cutout-result-1.fits" not in results_json
        assert "secret-" not in results_json

        alias = await service.get_staging_status("job-cut-1")
        assert alias.request_id == "job-cut-1"

        deleted = await service.delete_data_job("job-cut-1")
        assert deleted.deleted is True
        assert service.state.get_staging("job-cut-1") is None
    finally:
        await service.aclose()


@respx.mock
async def test_create_spectrum_requires_constraint(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        username="researcher@example.test",
        password="test-password",  # noqa: S106
        enable_staging=True,
        download_dir=tmp_path.resolve(),
        max_retries=0,
    )
    service = CasdaService(settings)
    try:
        with pytest.raises(CasdaError) as error:
            await service.create_spectrum("cube-1")
        assert error.value.code == "VALIDATION_ERROR"
    finally:
        await service.aclose()


@respx.mock
async def test_download_job_results_stops_on_first_failure(tmp_path) -> None:
    from datetime import datetime, timezone

    download_root = tmp_path.resolve()
    settings = Settings(
        _env_file=None,
        username="researcher@example.test",
        password="test-password",  # noqa: S106
        enable_staging=True,
        enable_downloads=True,
        download_dir=download_root,
        max_retries=0,
    )
    service = CasdaService(settings)
    request = StagingRequest(
        request_id="job-1",
        idempotency_key="batch-1",
        job_url="https://casda.csiro.au/casda_data_access/data/async/job-1",
        submitted_at=datetime.now(timezone.utc),
        status="COMPLETED",
        product_ids=["cube-1", "cube-2"],
        filenames={"cube-1": "cube-1.fits", "cube-2": "cube-2.fits"},
        products=[
            StagingItem(product_id="cube-1", status="COMPLETED", ready_for_download=True),
            StagingItem(product_id="cube-2", status="COMPLETED", ready_for_download=True),
        ],
        results=[
            UwsResult(result_id="cube-1", href="https://data.csiro.au/cube-1.fits", size_bytes=4),
            UwsResult(result_id="cube-2", href="https://data.csiro.au/cube-2.fits", size_bytes=4),
        ],
        job_kind="full_file",
    )
    service.state.put_completed_staging(
        request,
        [
            ReadyArtifact(
                product_id="cube-1",
                request_id="job-1",
                download_url="https://data.csiro.au/cube-1.fits",
                confirmed_at=datetime.now(timezone.utc),
            ),
            ReadyArtifact(
                product_id="cube-2",
                request_id="job-1",
                download_url="https://data.csiro.au/cube-2.fits",
                confirmed_at=datetime.now(timezone.utc),
            ),
        ],
    )

    async def tap_query(query: str, *, max_records: int, correlation_id: str):
        rows = []
        for product_id in ("cube-1", "cube-2"):
            if f"'{product_id}'" in query:
                rows.append(
                    {
                        "obs_publisher_did": product_id,
                        "filename": f"{product_id}.fits",
                        "access_estsize": "1",
                        "access_url": (
                            "https://data.csiro.au/casda_vo_proxy/vo/datalink/links"
                            f"?ID={product_id}"
                        ),
                        "obs_release_date": "2020-01-01T00:00:00Z",
                    }
                )
        return rows

    service.client.tap_query = tap_query  # type: ignore[method-assign]
    respx.get("https://casda.csiro.au/casda_data_access/data/async/job-1").mock(
        return_value=httpx.Response(
            200,
            content=b"""<uws:job xmlns:uws='http://www.ivoa.net/xml/UWS/v1.0'
              xmlns:xlink='http://www.w3.org/1999/xlink'>
              <uws:phase>COMPLETED</uws:phase>
              <uws:results>
                <uws:result id='cube-1' xlink:href='https://data.csiro.au/cube-1.fits'/>
                <uws:result id='cube-2' xlink:href='https://data.csiro.au/cube-2.fits'/>
              </uws:results>
            </uws:job>""",
        )
    )
    respx.get("https://data.csiro.au/cube-1.fits").mock(
        return_value=httpx.Response(
            200, content=b"data", headers={"Content-Length": "4", "ETag": '"v1"'}
        )
    )
    respx.get("https://data.csiro.au/cube-2.fits").mock(
        return_value=httpx.Response(500, content=b"fail")
    )

    try:
        response = await service.download_job_results("job-1", verify_checksum=False)
        assert len(response.results) == 1
        assert response.results[0].product_id == "cube-1"
        assert response.failed_product_id == "cube-2"
        assert response.failure_reason is not None
        assert (download_root / "cube-1.fits").exists()
    finally:
        await service.aclose()


async def test_verify_file_checks_checksum_sidecar(tmp_path) -> None:
    download_root = tmp_path.resolve()
    target = download_root / "cube-1.fits"
    target.write_bytes(b"abcd")
    (download_root / "cube-1.fits.checksum").write_text(
        "MD5: e2fc714c4727ee9395f324cd2e7f331f\n", encoding="utf-8"
    )
    settings = Settings(
        _env_file=None,
        enable_downloads=True,
        download_dir=download_root,
        max_retries=0,
    )
    service = CasdaService(settings)
    try:
        ok = await service.verify_file("cube-1.fits")
        assert ok.exists is True
        assert ok.size_bytes == 4
        assert ok.checksum.verified is True
        assert ok.checksum.algorithm == "md5"
    finally:
        await service.aclose()


async def test_manifest_includes_collection_metadata(tmp_path) -> None:
    class ManifestClient:
        async def tap_query(self, query: str, *, max_records: int, correlation_id: str):
            return [dict(PRODUCT_ROW)]

        async def aclose(self) -> None:
            return None

    settings = Settings(_env_file=None, download_dir=tmp_path.resolve(), max_retries=0)
    service = CasdaService(settings, client=ManifestClient())  # type: ignore[arg-type]
    try:
        response = await service.create_manifest(
            ["cube-1"],
            source_name=None,
            workflow_name=None,
            include_download_urls=False,
        )
        assert response.manifest is not None
        assert response.manifest.collections[0].obs_collection == "WALLABY"
        assert response.manifest.collections[0].facility_name == "ASKAP"
        assert response.manifest.products[0].collection_metadata is not None
        assert response.manifest.products[0].collection_metadata.obs_collection == "WALLABY"
    finally:
        await service.aclose()


async def test_state_store_delete_staging_removes_ready(tmp_path) -> None:
    from datetime import datetime, timezone

    store = StateStore(tmp_path / "state.db")
    request = StagingRequest(
        request_id="job-del",
        idempotency_key="key-del",
        job_url="https://casda.csiro.au/casda_data_access/data/async/job-del",
        submitted_at=datetime.now(timezone.utc),
        status="COMPLETED",
        product_ids=["cube-1"],
        filenames={"cube-1": "cube-1.fits"},
        products=[StagingItem(product_id="cube-1", status="COMPLETED", ready_for_download=True)],
        job_kind="full_file",
    )
    artifact = ReadyArtifact(
        product_id="cube-1",
        request_id="job-del",
        download_url="https://data.csiro.au/cube-1.fits",
        confirmed_at=datetime.now(timezone.utc),
    )
    store.put_completed_staging(request, [artifact])
    store.delete_staging("job-del")
    assert store.get_staging("job-del") is None
    assert store.get_ready("cube-1") is None
    store.close()
