"""Local idempotency, provenance, manifest, and staged-download state."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from casda_mcp.models import Manifest, ReadyArtifact, StagingRequest

ModelT = TypeVar("ModelT", bound=BaseModel)


class StateStore:
    """A small key-value state store; process-local unless SQLite is explicitly configured."""

    def __init__(self, path: Path | None = None) -> None:
        self._memory: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(path, check_same_thread=False)
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS state (kind TEXT NOT NULL, key TEXT NOT NULL, "
                "value_json TEXT NOT NULL, PRIMARY KEY(kind, key))"
            )
            self._connection.commit()

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

    def put_staging(self, request: StagingRequest) -> None:
        value = request.model_dump(mode="json", exclude_none=False)
        self._put("staging", request.request_id, value)
        self._put("idempotency", request.idempotency_key, {"request_id": request.request_id})

    def get_staging(self, request_id: str) -> StagingRequest | None:
        value = self._get("staging", request_id)
        return StagingRequest.model_validate(value) if value else None

    def get_staging_by_idempotency(self, key: str) -> StagingRequest | None:
        pointer = self._get("idempotency", key)
        return self.get_staging(pointer["request_id"]) if pointer else None

    def find_active_staging(self, product_ids: list[str]) -> StagingRequest | None:
        requested = set(product_ids)
        for value in self._all("staging"):
            staging = StagingRequest.model_validate(value)
            if (
                staging.status in {"PENDING", "QUEUED", "EXECUTING", "SUSPENDED", "UNKNOWN"}
                and set(staging.product_ids) == requested
            ):
                return staging
        return None

    def put_ready(self, artifact: ReadyArtifact) -> None:
        self._put(
            "ready", artifact.product_id, artifact.model_dump(mode="json", exclude_none=False)
        )

    def get_ready(self, product_id: str) -> ReadyArtifact | None:
        value = self._get("ready", product_id)
        return ReadyArtifact.model_validate(value) if value else None

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

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
