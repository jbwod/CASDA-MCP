from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
import pytest
import respx

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError
from casda_mcp.models import TapJobRecord
from casda_mcp.service import CasdaService
from casda_mcp.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"
ASYNC_URL = "https://casda.csiro.au/casda_vo_tools/tap/async"
JOB_URL = f"{ASYNC_URL}/tap-job-1"
QUERY = "SELECT TOP 2 obs_publisher_did FROM ivoa.obscore"


def _enabled_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "enable_advanced_adql": True,
        "max_retries": 3,
        "max_tap_rows": 100,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


async def test_submit_tap_query_disabled_by_default(settings) -> None:
    service = CasdaService(settings)
    try:
        with pytest.raises(CasdaError) as error:
            await service.submit_tap_query(QUERY)
        assert error.value.code == "ADVANCED_ADQL_DISABLED"
    finally:
        await service.aclose()


async def test_unknown_tap_job_is_rejected(settings) -> None:
    service = CasdaService(settings)
    try:
        with pytest.raises(CasdaError) as error:
            await service.get_tap_job_status("unknown-job")
        assert error.value.code == "TAP_JOB_NOT_FOUND"
        with pytest.raises(CasdaError) as results_error:
            await service.get_tap_results("unknown-job")
        assert results_error.value.code == "TAP_JOB_NOT_FOUND"
        with pytest.raises(CasdaError) as abort_error:
            await service.abort_tap_job("unknown-job")
        assert abort_error.value.code == "TAP_JOB_NOT_FOUND"
        with pytest.raises(CasdaError) as delete_error:
            await service.delete_tap_job("unknown-job")
        assert delete_error.value.code == "TAP_JOB_NOT_FOUND"
    finally:
        await service.aclose()


@respx.mock
async def test_submit_start_status_results_abort_delete_lifecycle() -> None:
    create = respx.post(ASYNC_URL).mock(
        return_value=httpx.Response(
            303, headers={"Location": "/casda_vo_tools/tap/async/tap-job-1"}
        )
    )
    job_get = respx.get(JOB_URL).mock(
        side_effect=[
            httpx.Response(200, content=(FIXTURES / "uws_tap_job_queued.xml").read_bytes()),
            httpx.Response(200, content=(FIXTURES / "uws_tap_job_completed.xml").read_bytes()),
            httpx.Response(200, content=(FIXTURES / "uws_tap_job_aborted.xml").read_bytes()),
        ]
    )
    phase = respx.post(f"{JOB_URL}/phase").mock(return_value=httpx.Response(200, content=b""))
    results = respx.get(f"{JOB_URL}/results/result").mock(
        return_value=httpx.Response(200, content=b"obs_publisher_did\ncube-1\ncube-2\n")
    )
    delete = respx.delete(JOB_URL).mock(return_value=httpx.Response(204))

    service = CasdaService(_enabled_settings())
    try:
        submitted = await service.submit_tap_query(QUERY)
        assert submitted.request_id == "tap-job-1"
        assert submitted.status == "QUEUED"
        assert create.call_count == 1
        assert phase.call_count == 1
        create_body = unquote_plus(create.calls[0].request.content.decode())
        assert "SELECT TOP 2" in create_body
        start_body = unquote_plus(phase.calls[0].request.content.decode())
        assert "PHASE=RUN" in start_body

        status = await service.get_tap_job_status("tap-job-1")
        assert status.status == "COMPLETED"
        assert status.results[0].result_id == "result"

        tap_results = await service.get_tap_results("tap-job-1")
        assert tap_results.returned == 2
        assert tap_results.rows[0]["obs_publisher_did"] == "cube-1"
        assert results.call_count == 1

        aborted = await service.abort_tap_job("tap-job-1")
        assert aborted.status == "ABORTED"
        assert phase.call_count == 2
        abort_body = unquote_plus(phase.calls[1].request.content.decode())
        assert "PHASE=ABORT" in abort_body

        deleted = await service.delete_tap_job("tap-job-1")
        assert deleted.deleted is True
        assert delete.call_count == 1
        with pytest.raises(CasdaError) as error:
            await service.get_tap_job_status("tap-job-1")
        assert error.value.code == "TAP_JOB_NOT_FOUND"
        assert job_get.call_count == 3
    finally:
        await service.aclose()


@respx.mock
async def test_create_and_start_are_never_retried() -> None:
    create = respx.post(ASYNC_URL).mock(return_value=httpx.Response(503))
    start = respx.post(f"{JOB_URL}/phase").mock(return_value=httpx.Response(503))

    service = CasdaService(_enabled_settings(max_retries=3))
    try:
        with pytest.raises(CasdaError) as create_error:
            await service.submit_tap_query(QUERY)
        assert create_error.value.http_status == 503
        assert create.call_count == 1
        assert start.call_count == 0

        with pytest.raises(CasdaError) as start_error:
            await service.client.start_tap_job(JOB_URL, correlation_id="test")
        assert start_error.value.http_status == 503
        assert start.call_count == 1
    finally:
        await service.aclose()


def test_tap_job_state_round_trip() -> None:
    store = StateStore()
    record = TapJobRecord(
        request_id="tap-job-1",
        job_url=JOB_URL,
        query_hash="abc",
        created_at=datetime.now(timezone.utc),
        phase="QUEUED",
    )
    store.put_tap_job(record)
    restored = store.get_tap_job("tap-job-1")
    assert restored is not None
    assert restored.job_url == JOB_URL
    store.delete_tap_job("tap-job-1")
    assert store.get_tap_job("tap-job-1") is None
    store.close()
