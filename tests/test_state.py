from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone

from casda_mcp.models import ReadyArtifact, StagingItem, StagingRequest
from casda_mcp.state import StateStore


def test_sqlite_state_round_trips_idempotency_job_and_ready_url(tmp_path) -> None:
    path = tmp_path / "casda-state.sqlite3"
    staging = StagingRequest(
        request_id="job-1",
        idempotency_key="run-1",
        job_url="https://casda.csiro.au/casda_data_access/data/async/job-1",
        submitted_at=datetime.now(timezone.utc),
        status="QUEUED",
        product_ids=["cube-1"],
        filenames={"cube-1": "cube-1.fits"},
        products=[StagingItem(product_id="cube-1", status="QUEUED")],
    )
    ready = ReadyArtifact(
        product_id="cube-1",
        request_id="job-1",
        download_url="https://data.csiro.au/download/cube-1.fits?signature=opaque",
        confirmed_at=datetime.now(timezone.utc),
    )
    first = StateStore(path)
    first.put_staging(staging)
    first.put_ready(ready)
    first.close()

    second = StateStore(path)
    try:
        restored = second.get_staging_by_idempotency("run-1")
        assert restored is not None
        assert restored.job_url.endswith("/job-1")
        assert second.find_active_staging(["cube-1"]) is not None
        restored_ready = second.get_ready("cube-1")
        assert restored_ready is not None
        assert restored_ready.download_url.endswith("signature=opaque")
    finally:
        second.close()


def test_sqlite_state_file_is_created_with_private_permissions(tmp_path) -> None:
    private_dir = tmp_path / "private"
    path = private_dir / "casda-state.sqlite3"
    previous_umask = os.umask(0)
    try:
        store = StateStore(path)
        store.close()
    finally:
        os.umask(previous_umask)
    if os.name == "posix":
        assert stat.S_IMODE(private_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_sqlite_state_repairs_existing_file_permissions(tmp_path) -> None:
    path = tmp_path / "casda-state.sqlite3"
    path.touch(mode=0o644)
    path.chmod(0o644)
    store = StateStore(path)
    store.close()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_expired_ready_artifacts_are_pruned() -> None:
    store = StateStore()
    store.put_ready(
        ReadyArtifact(
            product_id="cube-1",
            request_id="job-1",
            download_url="https://data.csiro.au/file.fits",
            confirmed_at=datetime.now(timezone.utc) - timedelta(hours=2),
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    assert store.get_ready("cube-1") is None


def test_expired_staging_request_is_not_reused() -> None:
    store = StateStore()
    store.put_staging(
        StagingRequest(
            request_id="job-expired",
            idempotency_key="run-expired",
            job_url="https://casda.csiro.au/casda_data_access/data/async/job-expired",
            submitted_at=datetime.now(timezone.utc) - timedelta(hours=2),
            status="QUEUED",
            product_ids=["cube-1"],
            products=[StagingItem(product_id="cube-1", status="QUEUED")],
            expiry_time=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    assert store.find_active_staging(["cube-1"]) is None
