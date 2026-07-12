from __future__ import annotations

from datetime import datetime, timezone

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
