"""Local idempotency, provenance, manifest, and staged-download state."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from casda_mcp.models import Manifest, ReadyArtifact, StagingRequest, TapJobRecord

ModelT = TypeVar("ModelT", bound=BaseModel)


class StateStore:
    """A small key-value state store; process-local unless SQLite is explicitly configured."""

    def __init__(self, path: Path | None = None) -> None:
        self._memory: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        if path is not None:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.parent.is_symlink() or not path.parent.is_dir():
                raise ValueError("CASDA_STATE_DB parent must be a real directory")
            self._prepare_private_database(path)
            self._connection = sqlite3.connect(path, check_same_thread=False)
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS state (kind TEXT NOT NULL, key TEXT NOT NULL, "
                "value_json TEXT NOT NULL, PRIMARY KEY(kind, key))"
            )
            self._connection.commit()

    @staticmethod
    def _prepare_private_database(path: Path) -> None:
        """Create a regular SQLite file without group/other access."""

        if path.is_symlink():
            raise ValueError("CASDA_STATE_DB must not be a symbolic link")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("CASDA_STATE_DB must be a regular file")
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _put(self, kind: str, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            if self._connection is None:
                self._memory[(kind, key)] = value
                return
            self._connection.execute(
                "INSERT INTO state(kind, key, value_json) VALUES(?, ?, ?) "
                "ON CONFLICT(kind, key) DO UPDATE SET value_json=excluded.value_json",
                (kind, key, json.dumps(value, separators=(",", ":"), default=str)),
            )
            self._connection.commit()

    def _get(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            if self._connection is None:
                return self._memory.get((kind, key))
            row = self._connection.execute(
                "SELECT value_json FROM state WHERE kind=? AND key=?", (kind, key)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def _all(self, kind: str) -> list[dict[str, Any]]:
        with self._lock:
            if self._connection is None:
                return [
                    value for (item_kind, _), value in self._memory.items() if item_kind == kind
                ]
            rows = self._connection.execute(
                "SELECT value_json FROM state WHERE kind=?", (kind,)
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def _delete(self, kind: str, key: str) -> None:
        with self._lock:
            if self._connection is None:
                self._memory.pop((kind, key), None)
                return
            self._connection.execute("DELETE FROM state WHERE kind=? AND key=?", (kind, key))
            self._connection.commit()

    def put_staging(self, request: StagingRequest) -> None:
        value = request.model_dump(mode="json", exclude_none=False)
        pointer = {"request_id": request.request_id}
        with self._lock:
            if self._connection is None:
                self._memory[("staging", request.request_id)] = value
                self._memory[("idempotency", request.idempotency_key)] = pointer
                return
            with self._connection:
                self._connection.execute(
                    "INSERT INTO state(kind, key, value_json) VALUES(?, ?, ?) "
                    "ON CONFLICT(kind, key) DO UPDATE SET value_json=excluded.value_json",
                    (
                        "staging",
                        request.request_id,
                        json.dumps(value, separators=(",", ":"), default=str),
                    ),
                )
                self._connection.execute(
                    "INSERT INTO state(kind, key, value_json) VALUES(?, ?, ?) "
                    "ON CONFLICT(kind, key) DO UPDATE SET value_json=excluded.value_json",
                    (
                        "idempotency",
                        request.idempotency_key,
                        json.dumps(pointer, separators=(",", ":")),
                    ),
                )

    def get_staging(self, request_id: str) -> StagingRequest | None:
        value = self._get("staging", request_id)
        return StagingRequest.model_validate(value) if value else None

    def get_staging_by_idempotency(self, key: str) -> StagingRequest | None:
        pointer = self._get("idempotency", key)
        return self.get_staging(pointer["request_id"]) if pointer else None

    def find_active_staging(
        self,
        product_ids: list[str],
        *,
        job_kind: str = "full_file",
        param_fingerprint: str | None = None,
    ) -> StagingRequest | None:
        requested = set(product_ids)
        for value in self._all("staging"):
            staging = StagingRequest.model_validate(value)
            expiry = self._as_utc(staging.expiry_time)
            if (
                staging.status in {"PENDING", "QUEUED", "EXECUTING", "SUSPENDED", "UNKNOWN"}
                and (expiry is None or expiry > datetime.now(timezone.utc))
                and set(staging.product_ids) == requested
                and staging.job_kind == job_kind
                and staging.param_fingerprint == param_fingerprint
            ):
                return staging
        return None

    def delete_staging(self, request_id: str) -> None:
        request = self.get_staging(request_id)
        if request is None:
            return
        with self._lock:
            if self._connection is None:
                self._memory.pop(("staging", request_id), None)
                self._memory.pop(("idempotency", request.idempotency_key), None)
                for product_id in request.product_ids:
                    ready = self._memory.get(("ready", product_id))
                    if ready is not None and ready.get("request_id") == request_id:
                        self._memory.pop(("ready", product_id), None)
                return
            with self._connection:
                self._connection.execute(
                    "DELETE FROM state WHERE kind='staging' AND key=?", (request_id,)
                )
                self._connection.execute(
                    "DELETE FROM state WHERE kind='idempotency' AND key=?",
                    (request.idempotency_key,),
                )
                for product_id in request.product_ids:
                    row = self._connection.execute(
                        "SELECT value_json FROM state WHERE kind='ready' AND key=?",
                        (product_id,),
                    ).fetchone()
                    if row is not None and json.loads(row[0]).get("request_id") == request_id:
                        self._connection.execute(
                            "DELETE FROM state WHERE kind='ready' AND key=?",
                            (product_id,),
                        )

    def put_ready(self, artifact: ReadyArtifact) -> None:
        self.put_ready_many([artifact])

    def put_ready_many(self, artifacts: list[ReadyArtifact]) -> None:
        """Persist a validated group of ready artifacts atomically."""

        values = [
            (
                artifact.product_id,
                artifact.model_dump(mode="json", exclude_none=False),
            )
            for artifact in artifacts
        ]
        with self._lock:
            if self._connection is None:
                for product_id, value in values:
                    self._memory[("ready", product_id)] = value
                return
            with self._connection:
                self._connection.executemany(
                    "INSERT INTO state(kind, key, value_json) VALUES('ready', ?, ?) "
                    "ON CONFLICT(kind, key) DO UPDATE SET value_json=excluded.value_json",
                    [
                        (
                            product_id,
                            json.dumps(value, separators=(",", ":"), default=str),
                        )
                        for product_id, value in values
                    ],
                )

    def put_completed_staging(
        self,
        request: StagingRequest,
        artifacts: list[ReadyArtifact],
    ) -> None:
        """Atomically persist a completed request and its current ready results."""

        if request.status != "COMPLETED":
            raise ValueError("Only completed staging requests can own ready artifacts")
        requested = set(request.product_ids)
        artifact_ids = [artifact.product_id for artifact in artifacts]
        if (
            len(artifact_ids) != len(set(artifact_ids))
            or any(artifact.product_id not in requested for artifact in artifacts)
            or any(artifact.request_id != request.request_id for artifact in artifacts)
        ):
            raise ValueError("Ready artifacts must uniquely belong to the staging request")
        request_value = request.model_dump(mode="json", exclude_none=False)
        pointer = {"request_id": request.request_id}
        values = [
            (
                artifact.product_id,
                artifact.model_dump(mode="json", exclude_none=False),
            )
            for artifact in artifacts
        ]
        with self._lock:
            if self._connection is None:
                for product_id in requested:
                    current = self._memory.get(("ready", product_id))
                    if current is not None and current.get("request_id") == request.request_id:
                        self._memory.pop(("ready", product_id), None)
                for product_id, value in values:
                    self._memory[("ready", product_id)] = value
                self._memory[("staging", request.request_id)] = request_value
                self._memory[("idempotency", request.idempotency_key)] = pointer
                return
            with self._connection:
                self._connection.execute(
                    "INSERT INTO state(kind, key, value_json) VALUES('staging', ?, ?) "
                    "ON CONFLICT(kind, key) DO UPDATE SET value_json=excluded.value_json",
                    (
                        request.request_id,
                        json.dumps(request_value, separators=(",", ":"), default=str),
                    ),
                )
                self._connection.execute(
                    "INSERT INTO state(kind, key, value_json) VALUES('idempotency', ?, ?) "
                    "ON CONFLICT(kind, key) DO UPDATE SET value_json=excluded.value_json",
                    (
                        request.idempotency_key,
                        json.dumps(pointer, separators=(",", ":")),
                    ),
                )
                for product_id in requested:
                    row = self._connection.execute(
                        "SELECT value_json FROM state WHERE kind='ready' AND key=?",
                        (product_id,),
                    ).fetchone()
                    if (
                        row is not None
                        and json.loads(row[0]).get("request_id") == request.request_id
                    ):
                        self._connection.execute(
                            "DELETE FROM state WHERE kind='ready' AND key=?",
                            (product_id,),
                        )
                self._connection.executemany(
                    "INSERT INTO state(kind, key, value_json) VALUES('ready', ?, ?) "
                    "ON CONFLICT(kind, key) DO UPDATE SET value_json=excluded.value_json",
                    [
                        (
                            product_id,
                            json.dumps(value, separators=(",", ":"), default=str),
                        )
                        for product_id, value in values
                    ],
                )

    def get_ready(self, product_id: str) -> ReadyArtifact | None:
        value = self._get("ready", product_id)
        artifact = ReadyArtifact.model_validate(value) if value else None
        if artifact is not None:
            expiry = self._as_utc(artifact.expires_at)
            if expiry is not None and expiry <= datetime.now(timezone.utc):
                self._delete("ready", product_id)
                return None
        return artifact

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def put_manifest(self, manifest: Manifest) -> None:
        self._put(
            "manifest", manifest.manifest_id, manifest.model_dump(mode="json", exclude_none=False)
        )

    def get_manifest(self, manifest_id: str) -> Manifest | None:
        value = self._get("manifest", manifest_id)
        return Manifest.model_validate(value) if value else None

    def put_search(self, product_id: str, criteria: dict[str, Any]) -> None:
        self._put("search", product_id, criteria)

    def get_search(self, product_id: str) -> dict[str, Any] | None:
        return self._get("search", product_id)

    def put_tap_job(self, job: TapJobRecord) -> None:
        self._put("tap_job", job.request_id, job.model_dump(mode="json", exclude_none=False))

    def get_tap_job(self, request_id: str) -> TapJobRecord | None:
        value = self._get("tap_job", request_id)
        return TapJobRecord.model_validate(value) if value else None

    def delete_tap_job(self, request_id: str) -> None:
        self._delete("tap_job", request_id)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
