from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError
from casda_mcp.models import StagingItem, StagingRequest, UwsResult
from casda_mcp.parsers import DatalinkAccess, UwsStatus
from casda_mcp.service import CasdaService
from casda_mcp.state import StateStore

PRODUCTS = {
    "cube-1": {
        "obs_publisher_did": "cube-1",
        "filename": "cube-1.fits",
        "access_estsize": "1",
        "access_url": "https://data.csiro.au/datalink?ID=cube-1",
        "obs_release_date": "2020-01-01T00:00:00Z",
    },
    "cube-2": {
        "obs_publisher_did": "cube-2",
        "filename": "cube-2.fits",
        "access_estsize": "2",
        "access_url": "https://data.csiro.au/datalink?ID=cube-2",
        "obs_release_date": "2020-01-01T00:00:00Z",
    },
}


class StagingClient:
    def __init__(self) -> None:
        self.created = 0
        self.started = 0
        self.status = UwsStatus(phase="QUEUED")

    async def tap_query(self, query: str, *, max_records: int, correlation_id: str):
        return [dict(row) for product_id, row in PRODUCTS.items() if f"'{product_id}'" in query]

    async def verify_authentication(self, *, correlation_id: str) -> None:
        return None

    async def resolve_datalink(self, access_url: str, *, correlation_id: str) -> DatalinkAccess:
        return DatalinkAccess(
            service_url="https://casda.csiro.au/casda_data_access/data/async",
            authenticated_id_token=access_url.rsplit("=", 1)[-1] + "-token",
        )

    async def create_staging_job(
        self, service_url: str, tokens: list[str], *, correlation_id: str
    ) -> str:
        self.created += 1
        return f"https://casda.csiro.au/casda_data_access/data/async/job-{self.created}"

    async def start_staging_job(self, job_url: str, *, correlation_id: str) -> None:
        self.started += 1

    async def get_staging_status(self, job_url: str, *, correlation_id: str) -> UwsStatus:
        return self.status

    def validate_archive_url(self, url: str) -> str:
        if not url.startswith(("https://data.csiro.au/", "https://casda.csiro.au/")):
            raise CasdaError("UNSAFE_ARCHIVE_URL", "unsafe URL")
        return url

    async def aclose(self) -> None:
        return None


class BlockingCreateClient(StagingClient):
    def __init__(self) -> None:
        super().__init__()
        self.create_entered = asyncio.Event()
        self.release_create = asyncio.Event()

    async def create_staging_job(
        self, service_url: str, tokens: list[str], *, correlation_id: str
    ) -> str:
        self.created += 1
        job_number = self.created
        self.create_entered.set()
        await self.release_create.wait()
        return f"https://casda.csiro.au/casda_data_access/data/async/job-{job_number}"


class FailingStartClient(StagingClient):
    async def start_staging_job(self, job_url: str, *, correlation_id: str) -> None:
        self.started += 1
        raise CasdaError("ARCHIVE_UNAVAILABLE", "The RUN response was lost.", retryable=True)


class BlockingStartClient(StagingClient):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()

    async def start_staging_job(self, job_url: str, *, correlation_id: str) -> None:
        self.started += 1
        self.start_entered.set()
        await asyncio.Event().wait()


@pytest.fixture
def staging_service(tmp_path) -> CasdaService:
    settings = Settings(
        _env_file=None,
        username="researcher@example.test",
        password="test-password",  # noqa: S106
        enable_staging=True,
        max_stage_products=2,
        max_stage_bytes=10_000,
        download_dir=tmp_path.resolve(),
    )
    return CasdaService(settings, client=StagingClient())  # type: ignore[arg-type]


async def test_staging_is_disabled_by_default(settings) -> None:
    service = CasdaService(settings, client=StagingClient())  # type: ignore[arg-type]
    with pytest.raises(CasdaError) as error:
        await service.stage_products(["cube-1"], idempotency_key=None, allow_duplicate=False)
    assert error.value.code == "STAGING_DISABLED"


async def test_stage_deduplicates_products_and_returns_archive_id(
    staging_service: CasdaService,
) -> None:
    response = await staging_service.stage_products(
        ["cube-1", "cube-1", "cube-2"],
        idempotency_key="research-run-1",
        allow_duplicate=False,
    )
    assert response.request_id == "job-1"
    assert response.idempotency_key == "research-run-1"
    assert response.status == "QUEUED"
    assert [item.product_id for item in response.products] == ["cube-1", "cube-2"]
    assert staging_service.client.created == 1  # type: ignore[attr-defined]
    assert staging_service.client.started == 1  # type: ignore[attr-defined]


async def test_same_idempotency_key_is_reused(staging_service: CasdaService) -> None:
    first = await staging_service.stage_products(
        ["cube-1"], idempotency_key="same-key", allow_duplicate=False
    )
    second = await staging_service.stage_products(
        ["cube-1"], idempotency_key="same-key", allow_duplicate=True
    )
    assert second.request_id == first.request_id
    assert second.reused is True
    assert staging_service.client.created == 1  # type: ignore[attr-defined]


async def test_concurrent_same_idempotency_key_creates_one_archive_job(
    staging_service: CasdaService,
) -> None:
    client = BlockingCreateClient()
    service = CasdaService(staging_service.settings, client=client)  # type: ignore[arg-type]
    first = asyncio.create_task(
        service.stage_products(["cube-1"], idempotency_key="concurrent-key", allow_duplicate=False)
    )
    await client.create_entered.wait()
    second = asyncio.create_task(
        service.stage_products(["cube-1"], idempotency_key="concurrent-key", allow_duplicate=False)
    )
    await asyncio.sleep(0)
    client.release_create.set()

    first_response, second_response = await asyncio.gather(first, second)

    assert client.created == 1
    assert first_response.request_id == second_response.request_id
    assert second_response.reused is True


async def test_concurrent_product_set_is_reused_without_duplicate_opt_in(
    staging_service: CasdaService,
) -> None:
    client = BlockingCreateClient()
    service = CasdaService(staging_service.settings, client=client)  # type: ignore[arg-type]
    first = asyncio.create_task(
        service.stage_products(["cube-1"], idempotency_key="first-key", allow_duplicate=False)
    )
    await client.create_entered.wait()
    second = asyncio.create_task(
        service.stage_products(["cube-1"], idempotency_key="second-key", allow_duplicate=False)
    )
    await asyncio.sleep(0)
    client.release_create.set()

    first_response, second_response = await asyncio.gather(first, second)

    assert client.created == 1
    assert first_response.request_id == second_response.request_id
    assert second_response.idempotency_key == "first-key"
    assert second_response.reused is True


async def test_concurrent_product_set_can_create_two_jobs_with_explicit_opt_in(
    staging_service: CasdaService,
) -> None:
    client = BlockingCreateClient()
    service = CasdaService(staging_service.settings, client=client)  # type: ignore[arg-type]
    first = asyncio.create_task(
        service.stage_products(["cube-1"], idempotency_key="first-copy", allow_duplicate=True)
    )
    await client.create_entered.wait()
    second = asyncio.create_task(
        service.stage_products(["cube-1"], idempotency_key="second-copy", allow_duplicate=True)
    )
    await asyncio.sleep(0)
    client.release_create.set()

    first_response, second_response = await asyncio.gather(first, second)

    assert client.created == 2
    assert first_response.request_id != second_response.request_id
    assert first_response.reused is False
    assert second_response.reused is False


async def test_created_job_is_persisted_before_run_transition(
    staging_service: CasdaService,
) -> None:
    client = FailingStartClient()
    service = CasdaService(staging_service.settings, client=client)  # type: ignore[arg-type]

    with pytest.raises(CasdaError) as error:
        await service.stage_products(
            ["cube-1"], idempotency_key="lost-run-response", allow_duplicate=False
        )

    assert error.value.code == "STAGING_START_UNCONFIRMED"
    assert error.value.details["request_id"] == "job-1"
    persisted = service.state.get_staging_by_idempotency("lost-run-response")
    assert persisted is not None
    assert persisted.request_id == "job-1"
    assert persisted.status == "UNKNOWN"

    reused = await service.stage_products(
        ["cube-1"], idempotency_key="lost-run-response", allow_duplicate=False
    )
    assert reused.request_id == "job-1"
    assert reused.reused is True
    assert client.created == 1


async def test_cancellation_during_run_transition_keeps_created_job(
    staging_service: CasdaService,
) -> None:
    client = BlockingStartClient()
    service = CasdaService(staging_service.settings, client=client)  # type: ignore[arg-type]
    task = asyncio.create_task(
        service.stage_products(["cube-1"], idempotency_key="cancelled-run", allow_duplicate=False)
    )
    await client.start_entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    persisted = service.state.get_staging_by_idempotency("cancelled-run")
    assert persisted is not None
    assert persisted.request_id == "job-1"
    assert persisted.status == "PENDING"
    assert client.created == 1


async def test_active_product_set_reused_unless_explicitly_allowed(
    staging_service: CasdaService,
) -> None:
    first = await staging_service.stage_products(
        ["cube-1"], idempotency_key="key-one", allow_duplicate=False
    )
    reused = await staging_service.stage_products(
        ["cube-1"], idempotency_key="key-two", allow_duplicate=False
    )
    duplicate = await staging_service.stage_products(
        ["cube-1"], idempotency_key="key-three", allow_duplicate=True
    )
    assert reused.request_id == first.request_id
    assert reused.idempotency_key == "key-one"
    assert duplicate.request_id != first.request_id
    assert staging_service.client.created == 2  # type: ignore[attr-defined]


async def test_idempotency_conflict_is_rejected(staging_service: CasdaService) -> None:
    await staging_service.stage_products(
        ["cube-1"], idempotency_key="conflict", allow_duplicate=False
    )
    with pytest.raises(CasdaError) as error:
        await staging_service.stage_products(
            ["cube-2"], idempotency_key="conflict", allow_duplicate=False
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


async def test_stage_limits_count_and_estimated_size(staging_service: CasdaService) -> None:
    staging_service.settings.max_stage_products = 1
    with pytest.raises(CasdaError) as count_error:
        await staging_service.stage_products(
            ["cube-1", "cube-2"], idempotency_key=None, allow_duplicate=False
        )
    assert count_error.value.code == "STAGING_LIMIT_EXCEEDED"
    staging_service.settings.max_stage_products = 2
    staging_service.settings.max_stage_bytes = 1000
    with pytest.raises(CasdaError) as size_error:
        await staging_service.stage_products(
            ["cube-2"], idempotency_key=None, allow_duplicate=False
        )
    assert size_error.value.code == "STAGING_SIZE_LIMIT_EXCEEDED"


async def test_unknown_size_is_rejected(staging_service: CasdaService) -> None:
    original = PRODUCTS["cube-1"]["access_estsize"]
    PRODUCTS["cube-1"]["access_estsize"] = None  # type: ignore[assignment]
    try:
        with pytest.raises(CasdaError) as error:
            await staging_service.stage_products(
                ["cube-1"], idempotency_key=None, allow_duplicate=False
            )
        assert error.value.code == "UNKNOWN_PRODUCT_SIZE"
    finally:
        PRODUCTS["cube-1"]["access_estsize"] = original


async def test_completed_status_records_only_confirmed_product_urls(
    staging_service: CasdaService,
) -> None:
    staged = await staging_service.stage_products(
        ["cube-1", "cube-2"], idempotency_key="complete", allow_duplicate=False
    )
    staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
        phase="COMPLETED",
        destruction=datetime.now(timezone.utc) + timedelta(hours=1),
        results=[
            UwsResult(
                result_id="cube-1.checksum",
                href="https://data.csiro.au/download/cube-1.fits.checksum?signature=secret",
            ),
            UwsResult(
                result_id="cube-1",
                href="https://data.csiro.au/download/cube-1.fits?signature=secret",
            ),
        ],
    )
    result = await staging_service.get_staging_status(staged.request_id or "")
    assert result.status == "COMPLETED"
    assert result.download_ready is False
    assert result.products[0].ready_for_download is True
    assert result.products[0].status_source == "archive_product"
    assert result.products[1].status == "UNKNOWN"
    assert staging_service.state.get_ready("cube-1") is not None
    assert staging_service.state.get_ready("cube-2") is None
    assert result.provenance is not None
    assert "signature" not in result.provenance.endpoint


async def test_completed_initial_status_records_identified_results(
    staging_service: CasdaService,
) -> None:
    staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
        phase="COMPLETED",
        results=[
            UwsResult(
                result_id="cube-1",
                href="https://data.csiro.au/download/archive-name.fits?signature=one",
            )
        ],
    )

    staged = await staging_service.stage_products(
        ["cube-1"], idempotency_key="immediately-complete", allow_duplicate=False
    )

    assert staged.status == "COMPLETED"
    assert staged.products[0].ready_for_download is True
    ready = staging_service.state.get_ready("cube-1")
    assert ready is not None
    assert ready.download_url.endswith("signature=one")


async def test_invalid_initial_completed_results_are_persisted_as_unusable(
    staging_service: CasdaService,
) -> None:
    staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
        phase="COMPLETED",
        results=[
            UwsResult(
                result_id="cube-1",
                href="https://example.test/unsafe.fits",
            )
        ],
    )

    with pytest.raises(CasdaError) as error:
        await staging_service.stage_products(
            ["cube-1"], idempotency_key="invalid-initial-results", allow_duplicate=False
        )

    assert error.value.code == "UNSAFE_ARCHIVE_URL"
    persisted = staging_service.state.get_staging_by_idempotency("invalid-initial-results")
    assert persisted is not None and persisted.status == "COMPLETED"
    assert persisted.products[0].ready_for_download is False
    assert persisted.results == []
    assert persisted.failure_reason is not None
    assert staging_service.state.get_ready("cube-1") is None


async def test_exact_result_ids_override_duplicate_filenames_and_result_basenames(
    staging_service: CasdaService,
) -> None:
    original = PRODUCTS["cube-2"]["filename"]
    PRODUCTS["cube-2"]["filename"] = "cube-1.fits"
    try:
        staged = await staging_service.stage_products(
            ["cube-1", "cube-2"], idempotency_key="duplicate-filenames", allow_duplicate=False
        )
        staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
            phase="COMPLETED",
            results=[
                UwsResult(
                    result_id="cube-1",
                    href="https://data.csiro.au/download/shared.fits?signature=one",
                ),
                UwsResult(
                    result_id="cube-2",
                    href="https://data.csiro.au/download/shared.fits?signature=two",
                ),
            ],
        )

        status = await staging_service.get_staging_status(staged.request_id or "")

        assert status.download_ready is True
        first = staging_service.state.get_ready("cube-1")
        second = staging_service.state.get_ready("cube-2")
        assert first is not None and first.download_url.endswith("signature=one")
        assert second is not None and second.download_url.endswith("signature=two")
    finally:
        PRODUCTS["cube-2"]["filename"] = original


async def test_unique_legacy_result_id_uses_conservative_filename_fallback(
    staging_service: CasdaService,
) -> None:
    staged = await staging_service.stage_products(
        ["cube-1"], idempotency_key="legacy-result", allow_duplicate=False
    )
    staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
        phase="COMPLETED",
        results=[
            UwsResult(
                result_id="encap-123",
                href="https://data.csiro.au/download/cube-1.fits?signature=data",
            ),
            UwsResult(
                result_id="encap-123.checksum",
                href="https://data.csiro.au/download/cube-1.fits.checksum?signature=sum",
            ),
        ],
    )

    status = await staging_service.get_staging_status(staged.request_id or "")

    assert status.download_ready is True
    ready = staging_service.state.get_ready("cube-1")
    assert ready is not None
    assert ready.checksum_url is not None and ready.checksum_url.endswith("signature=sum")


async def test_ambiguous_filename_fallback_does_not_mark_products_ready(
    staging_service: CasdaService,
) -> None:
    original = PRODUCTS["cube-2"]["filename"]
    PRODUCTS["cube-2"]["filename"] = "cube-1.fits"
    try:
        staged = await staging_service.stage_products(
            ["cube-1", "cube-2"], idempotency_key="ambiguous-fallback", allow_duplicate=False
        )
        staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
            phase="COMPLETED",
            results=[
                UwsResult(
                    result_id="legacy-result",
                    href="https://data.csiro.au/download/cube-1.fits",
                )
            ],
        )

        status = await staging_service.get_staging_status(staged.request_id or "")

        assert status.download_ready is False
        assert staging_service.state.get_ready("cube-1") is None
        assert staging_service.state.get_ready("cube-2") is None
    finally:
        PRODUCTS["cube-2"]["filename"] = original


async def test_completed_result_refresh_removes_stale_readiness(
    staging_service: CasdaService,
) -> None:
    staged = await staging_service.stage_products(
        ["cube-1", "cube-2"], idempotency_key="result-refresh", allow_duplicate=False
    )
    staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
        phase="COMPLETED",
        results=[
            UwsResult(
                result_id="cube-1",
                href="https://data.csiro.au/download/cube-1.fits",
            ),
            UwsResult(
                result_id="cube-2",
                href="https://data.csiro.au/download/cube-2.fits",
            ),
        ],
    )
    await staging_service.get_staging_status(staged.request_id or "")
    assert staging_service.state.get_ready("cube-2") is not None

    staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
        phase="COMPLETED",
        results=[
            UwsResult(
                result_id="cube-1",
                href="https://data.csiro.au/download/cube-1.fits",
            )
        ],
    )
    refreshed = await staging_service.get_staging_status(staged.request_id or "")

    assert refreshed.download_ready is False
    assert staging_service.state.get_ready("cube-1") is not None
    assert staging_service.state.get_ready("cube-2") is None


@pytest.mark.parametrize(
    ("result_id", "ready"),
    [("legacy-result", False), ("cube-1", True)],
)
async def test_encoded_path_separator_is_never_used_for_filename_fallback(
    staging_service: CasdaService,
    result_id: str,
    ready: bool,
) -> None:
    staged = await staging_service.stage_products(
        ["cube-1"], idempotency_key=f"encoded-separator-{result_id}", allow_duplicate=False
    )
    staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
        phase="COMPLETED",
        results=[
            UwsResult(
                result_id=result_id,
                href="https://data.csiro.au/download/prefix%2Fcube-1.fits",
            )
        ],
    )

    status = await staging_service.get_staging_status(staged.request_id or "")

    assert status.download_ready is ready
    assert (staging_service.state.get_ready("cube-1") is not None) is ready


async def test_legacy_sqlite_result_urls_are_reconciled_on_idempotent_reuse(
    staging_service: CasdaService,
    tmp_path,
) -> None:
    path = tmp_path / "legacy-state.sqlite3"
    StateStore(path).close()
    legacy = StagingRequest(
        request_id="legacy-job",
        idempotency_key="legacy-idempotency",
        job_url="https://casda.csiro.au/casda_data_access/data/async/legacy-job",
        submitted_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        status="COMPLETED",
        product_ids=["cube-1"],
        filenames={"cube-1": "cube-1.fits"},
        products=[StagingItem(product_id="cube-1", status="UNKNOWN")],
        expiry_time=datetime.now(timezone.utc) + timedelta(hours=1),
        result_urls=["https%3A%2F%2Fdata.csiro.au%2Fdownload%2Fcube-1.fits%3Fsignature%3Dlegacy"],
    )
    payload = legacy.model_dump(mode="json", exclude_none=False)
    payload.pop("results")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO state(kind, key, value_json) VALUES('staging', ?, ?)",
            (legacy.request_id, json.dumps(payload)),
        )
        connection.execute(
            "INSERT INTO state(kind, key, value_json) VALUES('idempotency', ?, ?)",
            (legacy.idempotency_key, json.dumps({"request_id": legacy.request_id})),
        )
        connection.commit()
    finally:
        connection.close()
    state = StateStore(path)
    service = CasdaService(  # type: ignore[arg-type]
        staging_service.settings,
        client=StagingClient(),
        state=state,
    )

    try:
        reused = await service.stage_products(
            ["cube-1"], idempotency_key="legacy-idempotency", allow_duplicate=False
        )

        assert reused.reused is True
        assert reused.products[0].ready_for_download is True
        ready_artifact = state.get_ready("cube-1")
        assert ready_artifact is not None
        assert ready_artifact.download_url == (
            "https://data.csiro.au/download/cube-1.fits?signature=legacy"
        )
        migrated = state.get_staging("legacy-job")
        assert migrated is not None and migrated.result_urls == []
        assert migrated.results[0].href == ready_artifact.download_url
        assert service.client.created == 0  # type: ignore[attr-defined]
    finally:
        await service.aclose()


@pytest.mark.parametrize(
    "results",
    [
        [
            UwsResult(
                result_id="cube-1",
                href="https://data.csiro.au/download/one.fits",
            ),
            UwsResult(
                result_id="cube-1",
                href="https://data.csiro.au/download/two.fits",
            ),
        ],
        [
            UwsResult(
                result_id="cube-1",
                href="https://data.csiro.au/download/shared.fits",
            ),
            UwsResult(
                result_id="cube-2",
                href="https://data.csiro.au/download/shared.fits",
            ),
        ],
        [
            UwsResult(
                result_id="cube-1",
                href="https://data.csiro.au/download/one.fits",
            ),
            UwsResult(result_id="unknown", href="https://example.test/unsafe.fits"),
        ],
    ],
)
async def test_malformed_result_identity_writes_no_ready_artifacts(
    staging_service: CasdaService,
    results: list[UwsResult],
) -> None:
    staged = await staging_service.stage_products(
        ["cube-1", "cube-2"], idempotency_key=str(id(results)), allow_duplicate=False
    )
    staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
        phase="COMPLETED", results=results
    )

    with pytest.raises(CasdaError):
        await staging_service.get_staging_status(staged.request_id or "")

    assert staging_service.state.get_ready("cube-1") is None
    assert staging_service.state.get_ready("cube-2") is None
    persisted = staging_service.state.get_staging(staged.request_id or "")
    assert persisted is not None and persisted.status == "COMPLETED"


async def test_partial_archive_failure_is_returned_per_product(
    staging_service: CasdaService,
) -> None:
    staged = await staging_service.stage_products(
        ["cube-1", "cube-2"], idempotency_key="failed", allow_duplicate=False
    )
    staging_service.client.status = UwsStatus(  # type: ignore[attr-defined]
        phase="ERROR", failure_reason="Tape retrieval failed"
    )
    result = await staging_service.get_staging_status(staged.request_id or "")
    assert result.status == "ERROR"
    assert {item.status for item in result.products} == {"ERROR"}
    assert all(item.failure_reason == "Tape retrieval failed" for item in result.products)


async def test_unknown_staging_request_is_rejected(staging_service: CasdaService) -> None:
    with pytest.raises(CasdaError) as error:
        await staging_service.get_staging_status("not-known")
    assert error.value.code == "STAGING_REQUEST_NOT_FOUND"
