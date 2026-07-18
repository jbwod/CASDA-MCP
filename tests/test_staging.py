from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError
from casda_mcp.parsers import DatalinkAccess, UwsStatus
from casda_mcp.service import CasdaService

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
        if not url.startswith("https://"):
            raise AssertionError("unsafe URL")
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
        result_urls=[
            "https://data.csiro.au/download/cube-1.fits.checksum?signature=secret",
            "https://data.csiro.au/download/cube-1.fits?signature=secret",
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
