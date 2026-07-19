"""Application service coordinating validation, CASDA access, state, and provenance."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from casda_mcp.cache import TTLCache
from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
from casda_mcp.cursor import decode_cursor, encode_cursor, query_hash
from casda_mcp.downloads import Downloader
from casda_mcp.errors import CasdaError
from casda_mcp.models import (
    CreateManifestResponse,
    DownloadProductResponse,
    GetObservationResponse,
    GetProductResponse,
    Manifest,
    ManifestProduct,
    Pagination,
    Product,
    ReadyArtifact,
    SearchProductsResponse,
    StageProductsResponse,
    StagingItem,
    StagingRequest,
    StagingStatusResponse,
    UwsResult,
)
from casda_mcp.observability import Metrics
from casda_mcp.parsers import observation_from_row, product_from_row, project_from_row
from casda_mcp.provenance import canonical_hash, make_provenance, utc_now
from casda_mcp.query import (
    QueryBuilder,
    SearchCriteria,
    normalize_product_id,
    normalize_product_ids,
    validate_idempotency_key,
)
from casda_mcp.state import StateStore

ProgressCallback = Callable[[int, int | None], Awaitable[None]]


class CasdaService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: CasdaClient | None = None,
        state: StateStore | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.metrics = metrics or Metrics()
        self.client = client or CasdaClient(self.settings, metrics=self.metrics)
        self.state = state or StateStore(self.settings.state_db)
        self.queries = QueryBuilder(
            max_results=self.settings.max_results,
            max_cone_radius_deg=self.settings.max_cone_radius_deg,
        )
        self.cache: TTLCache[list[dict[str, str | None]]] = TTLCache(
            ttl_seconds=self.settings.cache_ttl_seconds,
            max_entries=self.settings.cache_max_entries,
        )
        self._staging_submission_lock = asyncio.Lock()
        self.downloader = Downloader(self.settings, self.client, self.metrics)
        self._archive_available: bool | None = None
        self._archive_status_checked_at: datetime | None = None
        self._archive_status_detail: str | None = None

    async def aclose(self) -> None:
        await self.client.aclose()
        self.state.close()

    def readiness_snapshot(self) -> dict[str, object]:
        """Last-known archive availability for /readyz; never blocks on a live check."""

        return {
            "archive_available": self._archive_available,
            "checked_at": (
                self._archive_status_checked_at.isoformat().replace("+00:00", "Z")
                if self._archive_status_checked_at
                else None
            ),
            "detail": self._archive_status_detail,
        }

    def note_archive_availability(
        self, available: bool, *, detail: str | None = None
    ) -> None:
        self._archive_available = available
        self._archive_status_checked_at = utc_now()
        self._archive_status_detail = detail

    async def search_products(self, criteria: SearchCriteria) -> SearchProductsResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        parameters = criteria.as_parameters()
        bound_hash = query_hash(parameters)
        page = criteria.page
        page_size = criteria.page_size
        if criteria.cursor:
            offset, page_size = decode_cursor(criteria.cursor, expected_query_hash=bound_hash)
            page = (offset // page_size) + 1 if page_size else 1
            start = offset
        else:
            start = (page - 1) * page_size
        query, max_records = self.queries.build_search(criteria)
        rows, cached = await self._cached_tap_query(
            query, max_records=max_records, correlation_id=correlation_id
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        all_products = [
            product_from_row(
                row, ready=self.state.get_ready(row.get("obs_publisher_did") or "") is not None
            )
            for row in rows
        ]
        if criteria.released_only:
            all_products = [
                product
                for product in all_products
                if product.release_date is not None and product.access_state != "RESTRICTED"
            ]
        end = min(start + page_size, self.settings.max_results)
        products = all_products[start:end]
        for product in products:
            self.state.put_search(product.product_id, parameters)
        self.metrics.increment("search_request_count")
        if cached:
            self.metrics.increment("cache_hit_count")
        else:
            self.metrics.increment("cache_miss_count")
        has_more = len(all_products) > end and end < self.settings.max_results
        next_cursor = (
            encode_cursor(query_hash=bound_hash, offset=end, page_size=page_size)
            if has_more
            else None
        )
        provenance = make_provenance(
            request_timestamp=requested_at,
            endpoint=self.settings.tap_url,
            parameters=parameters,
            result_count=len(products),
            cached=cached,
            correlation_id=correlation_id,
        )
        return SearchProductsResponse(
            products=products,
            pagination=Pagination(
                page=page,
                page_size=page_size,
                returned=len(products),
                has_more=has_more,
                max_results=self.settings.max_results,
                next_cursor=next_cursor,
                offset=start,
            ),
            provenance=provenance,
        )

    async def get_product(self, product_id: str) -> GetProductResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        product_id = normalize_product_id(product_id)
        rows, cached = await self._cached_tap_query(
            self.queries.build_product(product_id), max_records=2, correlation_id=correlation_id
        )
        if not rows:
            raise CasdaError(
                "PRODUCT_NOT_FOUND",
                "No CASDA product has the requested identifier.",
                details={"product_id": product_id},
            )
        if len(rows) > 1:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned duplicate product identifiers."
            )
        product = product_from_row(rows[0], ready=self.state.get_ready(product_id) is not None)
        return GetProductResponse(
            product=product,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters={"product_id": product_id},
                result_count=1,
                cached=cached,
                correlation_id=correlation_id,
            ),
        )

    async def get_observation(
        self,
        scheduling_block_id: int | None = None,
        *,
        observation_id: str | None = None,
    ) -> GetObservationResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        if (scheduling_block_id is None) == (observation_id is None):
            raise CasdaError(
                "VALIDATION_ERROR",
                "Provide exactly one of scheduling_block_id or observation_id.",
            )
        resolved_obs_id: str
        sbid: int | None = scheduling_block_id
        if observation_id is not None:
            from casda_mcp.query import adql_string

            resolved_obs_id = observation_id.strip()
            adql_string(resolved_obs_id, field="observation_id")
            if resolved_obs_id.startswith("ASKAP-"):
                try:
                    sbid = int(resolved_obs_id.removeprefix("ASKAP-"))
                except ValueError:
                    sbid = None
        else:
            assert scheduling_block_id is not None
            resolved_obs_id = f"ASKAP-{scheduling_block_id}"

        products_query, product_limit = self.queries.build_observation_products(resolved_obs_id)
        projects_query = self.queries.build_observation_projects(resolved_obs_id)
        if sbid is not None and sbid > 0:
            observation_rows, project_rows, product_rows = await asyncio.gather(
                self.client.tap_query(
                    self.queries.build_observation(sbid),
                    max_records=2,
                    correlation_id=correlation_id,
                ),
                self.client.tap_query(
                    projects_query, max_records=50, correlation_id=correlation_id
                ),
                self.client.tap_query(
                    products_query, max_records=product_limit, correlation_id=correlation_id
                ),
            )
            if observation_rows:
                if len(observation_rows) > 1:
                    raise CasdaError(
                        "MALFORMED_ARCHIVE_RESPONSE",
                        "CASDA returned duplicate observation identifiers.",
                    )
                observation = observation_from_row(observation_rows[0]).model_copy(
                    update={"obs_id": resolved_obs_id}
                )
            else:
                observation = None
        else:
            observation_rows = []
            project_rows, product_rows = await asyncio.gather(
                self.client.tap_query(
                    projects_query, max_records=50, correlation_id=correlation_id
                ),
                self.client.tap_query(
                    products_query, max_records=product_limit, correlation_id=correlation_id
                ),
            )
            observation = None

        if not product_rows and observation is None:
            raise CasdaError(
                "OBSERVATION_NOT_FOUND",
                "No CASDA observation matches the requested identifier.",
                details={
                    "scheduling_block_id": scheduling_block_id,
                    "observation_id": resolved_obs_id,
                },
            )
        if observation is None and product_rows:
            seed = dict(product_rows[0])
            seed["obs_id"] = resolved_obs_id
            observation = observation_from_row(seed)
        products = [
            product_from_row(
                row, ready=self.state.get_ready(row.get("obs_publisher_did") or "") is not None
            )
            for row in product_rows[: self.settings.max_results]
        ]
        return GetObservationResponse(
            observation=observation,
            projects=[project_from_row(row) for row in project_rows],
            products=products,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters={
                    "scheduling_block_id": scheduling_block_id,
                    "observation_id": resolved_obs_id,
                },
                result_count=len(products),
                correlation_id=correlation_id,
            ),
        )

    async def stage_products(
        self,
        product_ids: list[str],
        *,
        idempotency_key: str | None,
        allow_duplicate: bool,
    ) -> StageProductsResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        if not self.settings.enable_staging:
            raise CasdaError(
                "STAGING_DISABLED", "Archive-side staging is disabled by server configuration."
            )
        if len(product_ids) > 500:
            raise CasdaError(
                "STAGING_LIMIT_EXCEEDED",
                "The request exceeds the hard input limit of 500 product identifiers.",
                details={"requested": len(product_ids), "maximum": 500},
            )
        normalized = normalize_product_ids(product_ids)
        if len(normalized) > self.settings.max_stage_products:
            raise CasdaError(
                "STAGING_LIMIT_EXCEEDED",
                "The request exceeds CASDA_MAX_STAGE_PRODUCTS.",
                details={"requested": len(normalized), "maximum": self.settings.max_stage_products},
            )
        key = validate_idempotency_key(idempotency_key) if idempotency_key else str(uuid.uuid4())
        async with self._staging_submission_lock:
            return await self._stage_products_locked(
                normalized,
                key=key,
                allow_duplicate=allow_duplicate,
                requested_at=requested_at,
                correlation_id=correlation_id,
            )

    async def _stage_products_locked(
        self,
        normalized: list[str],
        *,
        key: str,
        allow_duplicate: bool,
        requested_at: datetime,
        correlation_id: str,
    ) -> StageProductsResponse:
        """Create at most one archive job while holding the process submission lock."""

        existing = self.state.get_staging_by_idempotency(key)
        if existing:
            if set(existing.product_ids) != set(normalized):
                raise CasdaError(
                    "IDEMPOTENCY_CONFLICT",
                    "The idempotency key was already used with different product identifiers.",
                )
            self._reconcile_completed_staging(existing)
            return self._stage_response(existing, requested_at=requested_at, reused=True)
        active = self.state.find_active_staging(normalized)
        if active and not allow_duplicate:
            return self._stage_response(active, requested_at=requested_at, reused=True)

        products = await self.get_products(normalized, correlation_id=correlation_id)
        unknown_size = [
            product.product_id for product in products if product.file_size_bytes is None
        ]
        if unknown_size and not self.settings.allow_unknown_stage_size:
            raise CasdaError(
                "UNKNOWN_PRODUCT_SIZE",
                "CASDA did not provide sizes for all products; the staging size limit "
                "cannot be enforced.",
                details={"product_ids": unknown_size},
            )
        total_size = sum(product.file_size_bytes or 0 for product in products)
        if total_size > self.settings.max_stage_bytes:
            raise CasdaError(
                "STAGING_SIZE_LIMIT_EXCEEDED",
                "The estimated request size exceeds CASDA_MAX_STAGE_BYTES.",
                details={"estimated_bytes": total_size, "maximum": self.settings.max_stage_bytes},
            )
        missing_access = [product.product_id for product in products if not product.access_url]
        if missing_access:
            raise CasdaError(
                "PRODUCT_UNAVAILABLE",
                "CASDA did not provide Datalink access metadata for all products.",
                details={"product_ids": missing_access},
            )
        await self.client.verify_authentication(correlation_id=correlation_id)
        datalinks = await asyncio.gather(
            *(
                self.client.resolve_datalink(
                    product.access_url or "", correlation_id=correlation_id
                )
                for product in products
            )
        )
        service_urls = {access.service_url.rstrip("/") for access in datalinks}
        if len(service_urls) != 1:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned inconsistent staging service endpoints.",
            )
        job_url = await self.client.create_staging_job(
            service_urls.pop(),
            [access.authenticated_id_token for access in datalinks],
            correlation_id=correlation_id,
        )
        request_id = unquote(urlparse(job_url).path.rstrip("/").rsplit("/", 1)[-1])
        validate_idempotency_key(request_id)
        request = StagingRequest(
            request_id=request_id,
            idempotency_key=key,
            job_url=job_url,
            submitted_at=utc_now(),
            status="PENDING",
            product_ids=normalized,
            filenames={product.product_id: product.filename for product in products},
            products=[
                StagingItem(
                    product_id=product.product_id,
                    status="PENDING",
                    status_source="archive_request",
                )
                for product in products
            ],
        )
        # Store the confirmed archive job before requesting the RUN transition. If the
        # transition response is lost, callers can reconcile the known job instead of
        # accidentally creating another one.
        self.state.put_staging(request)
        self.metrics.increment("staging_submission_count")
        try:
            await self.client.start_staging_job(job_url, correlation_id=correlation_id)
        except Exception as exc:
            archive_code = exc.code if isinstance(exc, CasdaError) else "INTERNAL_ERROR"
            request.status = "UNKNOWN"
            request.failure_reason = (
                "The archive job was created, but its RUN transition could not be confirmed."
            )
            request.products = self._staging_items(request, request.status, request.failure_reason)
            self.state.put_staging(request)
            raise CasdaError(
                "STAGING_START_UNCONFIRMED",
                "The CASDA staging job was created, but starting it could not be confirmed. "
                "Inspect the stored request before submitting another archive job.",
                retryable=True,
                details={
                    "request_id": request_id,
                    "idempotency_key": key,
                    "archive_error": archive_code,
                },
            ) from exc

        try:
            archive_status = await self.client.get_staging_status(
                job_url, correlation_id=correlation_id
            )
            request.status = archive_status.phase
            request.failure_reason = archive_status.failure_reason
            request.expiry_time = archive_status.destruction
            request.results = archive_status.results
            request.result_urls = []
        except CasdaError:
            request.status = "UNKNOWN"
            request.failure_reason = (
                "The job was submitted, but its initial archive status was unavailable."
            )
        if request.status == "COMPLETED":
            self._store_completed_staging(request)
        else:
            request.products = self._staging_items(request, request.status, request.failure_reason)
            self.state.put_staging(request)
        return self._stage_response(
            request, requested_at=requested_at, correlation_id=correlation_id
        )

    async def get_staging_status(self, request_id: str) -> StagingStatusResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        request_id = validate_idempotency_key(request_id)
        request = self.state.get_staging(request_id)
        if request is None:
            raise CasdaError(
                "STAGING_REQUEST_NOT_FOUND",
                "The staging request is not known to this server instance or configured "
                "state database.",
                details={"request_id": request_id},
            )
        self._reconcile_completed_staging(request)
        archive = await self.client.get_staging_status(
            request.job_url, correlation_id=correlation_id
        )
        request.status = archive.phase
        request.expiry_time = archive.destruction
        request.failure_reason = archive.failure_reason
        request.results = archive.results
        request.result_urls = []
        if archive.phase == "COMPLETED":
            self._store_completed_staging(request)
        else:
            request.products = self._staging_items(request, archive.phase, archive.failure_reason)
            self.state.put_staging(request)
        if archive.phase in {"ERROR", "ABORTED"}:
            self.metrics.increment("staging_failure_count")
        active = archive.phase in {"PENDING", "QUEUED", "EXECUTING", "SUSPENDED"}
        missing_results = archive.phase == "COMPLETED" and not all(
            item.ready_for_download for item in request.products
        )
        retry_guidance = None
        if active:
            retry_guidance = (
                "Call casda_get_staging_status again later. This server does not poll "
                "automatically."
            )
        elif missing_results:
            retry_guidance = (
                "CASDA completed the request but did not return a matching file URL for "
                "every product."
            )
        return StagingStatusResponse(
            request_id=request.request_id,
            status=request.status,
            products=request.products,
            failure_reason=request.failure_reason,
            expiry_time=request.expiry_time,
            download_ready=bool(request.products)
            and all(item.ready_for_download for item in request.products),
            retry_guidance=retry_guidance,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=request.job_url,
                parameters={"request_id": request.request_id, "cache_bypassed": True},
                request_id=request.request_id,
                result_count=len(request.products),
                correlation_id=correlation_id,
            ),
        )

    async def download_product(
        self,
        product_id: str,
        *,
        destination: str | None,
        verify_checksum: bool,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadProductResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        if not self.settings.enable_downloads:
            raise CasdaError(
                "DOWNLOADS_DISABLED", "Local downloads are disabled by server configuration."
            )
        product_id = normalize_product_id(product_id)
        artifact = self.state.get_ready(product_id)
        if artifact is None or (artifact.expires_at and artifact.expires_at <= utc_now()):
            raise CasdaError(
                "PRODUCT_NOT_READY",
                "The requested product is not confirmed ready for download.",
                retryable=artifact is None,
                details={"product_id": product_id, "staging_required": True},
            )
        products = await self.get_products([product_id], correlation_id=correlation_id)
        result = await self.downloader.download(
            products[0],
            artifact,
            destination=destination,
            verify_checksum=verify_checksum,
            correlation_id=correlation_id,
            progress_callback=progress_callback,
        )
        return DownloadProductResponse(
            result=result,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=artifact.download_url,
                parameters={
                    "product_id": product_id,
                    "destination": destination,
                    "verify_checksum": verify_checksum,
                },
                request_id=artifact.request_id,
                result_count=1,
                correlation_id=correlation_id,
            ),
        )

    async def create_manifest(
        self,
        product_ids: list[str],
        *,
        source_name: str | None,
        workflow_name: str | None,
        include_download_urls: bool,
    ) -> CreateManifestResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        if len(product_ids) > 1000:
            raise CasdaError(
                "MANIFEST_LIMIT_EXCEEDED",
                "The request exceeds the hard input limit of 1000 product identifiers.",
                details={"requested": len(product_ids), "maximum": 1000},
            )
        normalized = normalize_product_ids(product_ids)
        if len(normalized) > self.settings.max_manifest_products:
            raise CasdaError(
                "MANIFEST_LIMIT_EXCEEDED",
                "The request exceeds CASDA_MAX_MANIFEST_PRODUCTS.",
                details={
                    "requested": len(normalized),
                    "maximum": self.settings.max_manifest_products,
                },
            )
        source_name = self._manifest_label(source_name, "source_name")
        workflow_name = self._manifest_label(workflow_name, "workflow_name")
        products = await self.get_products(normalized, correlation_id=correlation_id)
        manifest_products: list[ManifestProduct] = []
        warnings: list[str] = []
        search_criteria: list[dict[str, object]] = []
        seen_criteria: set[str] = set()
        for product in products:
            criteria = self.state.get_search(product.product_id)
            if criteria:
                criteria_hash = canonical_hash(criteria)
                if criteria_hash not in seen_criteria:
                    seen_criteria.add(criteria_hash)
                    search_criteria.append(criteria)
            ready = self.state.get_ready(product.product_id)
            checksum = None
            checksum_algorithm = None
            download_url = None
            if ready and ready.checksum_url:
                try:
                    checksum_spec = await self.downloader.checksum_spec(
                        ready,
                        verify_checksum=True,
                        correlation_id=correlation_id,
                    )
                    if checksum_spec:
                        checksum = checksum_spec.digest
                        checksum_algorithm = checksum_spec.algorithm
                except CasdaError:
                    warnings.append(
                        f"Checksum metadata was unavailable for product {product.product_id}."
                    )
            if include_download_urls and ready:
                warnings.append(
                    f"The archive artifact URL was omitted for product {product.product_id}; "
                    "CASDA URLs may be bearer credentials even when they have no query string."
                )
            manifest_products.append(
                ManifestProduct(
                    product=product,
                    checksum=checksum,
                    checksum_algorithm=checksum_algorithm,
                    staging_request_id=ready.request_id if ready else None,
                    download_url=download_url,
                )
            )
        parameters = {
            "product_ids": normalized,
            "source_name": source_name,
            "workflow_name": workflow_name,
            "include_download_urls": include_download_urls,
        }
        provenance = make_provenance(
            request_timestamp=requested_at,
            endpoint=self.settings.tap_url,
            parameters=parameters,
            result_count=len(manifest_products),
            correlation_id=correlation_id,
        )
        identity = {
            "schema_version": "1.0",
            "source_name": source_name,
            "workflow_name": workflow_name,
            "products": [item.model_dump(mode="json") for item in manifest_products],
            "original_search_criteria": search_criteria,
        }
        manifest = Manifest(
            manifest_id=f"sha256-{canonical_hash(identity)}",
            created_at=utc_now(),
            source_name=source_name,
            workflow_name=workflow_name,
            products=manifest_products,
            original_search_criteria=search_criteria,
            provenance=provenance,
            server_version=provenance.server_version,
            warnings=warnings,
        )
        self.state.put_manifest(manifest)
        return CreateManifestResponse(manifest=manifest, provenance=provenance)

    async def get_products(self, product_ids: list[str], *, correlation_id: str) -> list[Product]:
        normalized = normalize_product_ids(product_ids)
        rows = await self.client.tap_query(
            self.queries.build_products(normalized),
            max_records=len(normalized) + 1,
            correlation_id=correlation_id,
        )
        products = [
            product_from_row(
                row, ready=self.state.get_ready(row.get("obs_publisher_did") or "") is not None
            )
            for row in rows
        ]
        found = {product.product_id for product in products}
        missing = [product_id for product_id in normalized if product_id not in found]
        if missing:
            raise CasdaError(
                "PRODUCT_NOT_FOUND",
                "One or more CASDA products were not found.",
                details={"product_ids": missing},
            )
        if len(products) != len(normalized):
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned duplicate product identifiers."
            )
        by_id = {product.product_id: product for product in products}
        return [by_id[product_id] for product_id in normalized]

    def _stage_response(
        self,
        request: StagingRequest,
        *,
        requested_at: datetime,
        reused: bool = False,
        correlation_id: str | None = None,
    ) -> StageProductsResponse:
        return StageProductsResponse(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            submitted_at=request.submitted_at,
            status=request.status,
            products=request.products,
            reused=reused,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=request.job_url,
                parameters={
                    "product_ids": request.product_ids,
                    "idempotency_key": request.idempotency_key,
                    "reused": reused,
                },
                request_id=request.request_id,
                result_count=len(request.products),
                correlation_id=correlation_id,
            ),
        )

    def _staging_items(
        self,
        request: StagingRequest,
        phase: str,
        failure_reason: str | None,
        *,
        ready_product_ids: set[str] | None = None,
    ) -> list[StagingItem]:
        items: list[StagingItem] = []
        for product_id in request.product_ids:
            if ready_product_ids is None:
                ready = self.state.get_ready(product_id)
                is_ready = ready is not None and ready.request_id == request.request_id
            else:
                is_ready = product_id in ready_product_ids
            if is_ready:
                items.append(
                    StagingItem(
                        product_id=product_id,
                        status="COMPLETED",
                        ready_for_download=True,
                        status_source="archive_product",
                    )
                )
            elif phase == "COMPLETED":
                items.append(
                    StagingItem(
                        product_id=product_id,
                        status="UNKNOWN",
                        failure_reason=(
                            "CASDA completed the request without a matching result URL for "
                            "this product."
                        ),
                        status_source="archive_request",
                    )
                )
            else:
                items.append(
                    StagingItem(
                        product_id=product_id,
                        status=phase,
                        failure_reason=failure_reason,
                        status_source="archive_request",
                    )
                )
        return items

    def _store_completed_staging(self, request: StagingRequest) -> None:
        try:
            artifacts = self._build_ready_artifacts(request)
        except CasdaError:
            request.results = []
            request.result_urls = []
            request.failure_reason = (
                "CASDA completed the job, but returned unusable result metadata."
            )
            request.products = self._staging_items(
                request,
                "COMPLETED",
                request.failure_reason,
                ready_product_ids=set(),
            )
            self.state.put_completed_staging(request, [])
            raise
        request.products = self._staging_items(
            request,
            "COMPLETED",
            request.failure_reason,
            ready_product_ids={artifact.product_id for artifact in artifacts},
        )
        self.state.put_completed_staging(request, artifacts)

    def _reconcile_completed_staging(self, request: StagingRequest) -> None:
        if request.status == "COMPLETED" and (request.results or request.result_urls):
            self._store_completed_staging(request)

    def _build_ready_artifacts(self, request: StagingRequest) -> list[ReadyArtifact]:
        expiry = request.expiry_time
        if expiry is not None:
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= utc_now():
                return []
        results = list(request.results)
        migrated_legacy_results = not results and bool(request.result_urls)
        if not results:
            results = [
                UwsResult(
                    result_id=f"legacy-{index}",
                    href=(
                        url if urlparse(url).scheme.lower() in {"http", "https"} else unquote(url)
                    ),
                )
                for index, url in enumerate(request.result_urls)
            ]

        by_id: dict[str, UwsResult] = {}
        for result in results:
            if not result.result_id or result.result_id in by_id:
                raise CasdaError(
                    "MALFORMED_ARCHIVE_RESPONSE",
                    "CASDA returned duplicate or empty UWS result identifiers.",
                )
            self.client.validate_archive_url(result.href)
            by_id[result.result_id] = result

        def result_basename(result: UwsResult) -> str | None:
            encoded_segment = urlparse(result.href).path.rsplit("/", 1)[-1]
            decoded_segment = unquote(encoded_segment)
            if "/" in decoded_segment or "\\" in decoded_segment:
                return None
            return decoded_segment

        requested_basenames = Counter(
            Path(filename).name for filename in request.filenames.values() if filename
        )
        result_basenames = Counter(
            basename for result in results if (basename := result_basename(result)) is not None
        )
        claimed_ids: set[str] = set()
        assignments: dict[str, tuple[UwsResult, UwsResult | None]] = {}

        # CASDA defines the authoritative pair as <product-id> and
        # <product-id>.checksum. Filenames are not identifiers and may collide.
        for product_id in request.product_ids:
            data_result = by_id.get(product_id)
            if data_result is None:
                continue
            checksum_result = by_id.get(f"{product_id}.checksum")
            assignments[product_id] = (data_result, checksum_result)
            claimed_ids.add(data_result.result_id)
            if checksum_result is not None:
                claimed_ids.add(checksum_result.result_id)

        # Older persisted jobs, and historical evaluation-file result IDs, may not
        # align with the requested publisher DID. Permit a filename fallback only
        # when both sides are globally unique and every selected result is unclaimed.
        for product_id in request.product_ids:
            if product_id in assignments:
                continue
            filename = request.filenames.get(product_id)
            if not filename:
                continue
            expected = Path(filename).name
            if requested_basenames[expected] != 1 or result_basenames[expected] != 1:
                continue
            candidates = [
                result
                for result in results
                if result.result_id not in claimed_ids
                and not result.result_id.endswith(".checksum")
                and result_basename(result) == expected
            ]
            if len(candidates) != 1:
                continue
            data_result = candidates[0]
            checksum_result = by_id.get(f"{data_result.result_id}.checksum")
            if checksum_result is not None and checksum_result.result_id in claimed_ids:
                checksum_result = None
            if checksum_result is None:
                checksum_name = f"{expected}.checksum"
                checksum_candidates = [
                    result
                    for result in results
                    if result.result_id not in claimed_ids
                    and result_basename(result) == checksum_name
                ]
                if len(checksum_candidates) == 1:
                    checksum_result = checksum_candidates[0]
            assignments[product_id] = (data_result, checksum_result)
            claimed_ids.add(data_result.result_id)
            if checksum_result is not None:
                claimed_ids.add(checksum_result.result_id)

        selected_hrefs = [
            result.href
            for data_result, checksum_result in assignments.values()
            for result in (data_result, checksum_result)
            if result is not None
        ]
        if len(selected_hrefs) != len(set(selected_hrefs)):
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA assigned one UWS result location to multiple staged artifacts.",
            )

        confirmed_at = utc_now()
        artifacts = [
            ReadyArtifact(
                product_id=product_id,
                request_id=request.request_id,
                download_url=data_result.href,
                checksum_url=checksum_result.href if checksum_result is not None else None,
                confirmed_at=confirmed_at,
                expires_at=request.expiry_time,
            )
            for product_id, (data_result, checksum_result) in assignments.items()
        ]
        if migrated_legacy_results:
            request.results = results
            request.result_urls = []
        return artifacts

    @staticmethod
    def _manifest_label(value: str | None, field: str) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 160 or any(ord(char) < 32 for char in value):
            raise CasdaError("VALIDATION_ERROR", f"{field} must be 1 to 160 printable characters.")
        return normalized

    async def _cached_tap_query(
        self, query: str, *, max_records: int, correlation_id: str
    ) -> tuple[list[dict[str, str | None]], bool]:
        key = canonical_hash({"query": query, "max_records": max_records})
        cached = await self.cache.get(key)
        if cached is not None:
            return cached, True
        rows = await self.client.tap_query(
            query, max_records=max_records, correlation_id=correlation_id
        )
        await self.cache.set(key, rows)
        return rows, False
