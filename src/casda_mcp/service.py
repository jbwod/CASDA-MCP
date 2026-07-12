"""Application service coordinating validation, CASDA access, state, and provenance."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from casda_mcp.cache import TTLCache
from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
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
        self.downloader = Downloader(self.settings, self.client, self.metrics)

    async def aclose(self) -> None:
        await self.client.aclose()
        self.state.close()

    async def search_products(self, criteria: SearchCriteria) -> SearchProductsResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        query, max_records = self.queries.build_search(criteria)
        rows, cached = await self._cached_tap_query(
            query, max_records=max_records, correlation_id=correlation_id
        )
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
        start = (criteria.page - 1) * criteria.page_size
        end = min(start + criteria.page_size, self.settings.max_results)
        products = all_products[start:end]
        parameters = criteria.as_parameters()
        for product in products:
            self.state.put_search(product.product_id, parameters)
        self.metrics.increment("search_request_count")
        if cached:
            self.metrics.increment("cache_hit_count")
        else:
            self.metrics.increment("cache_miss_count")
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
                page=criteria.page,
                page_size=criteria.page_size,
                returned=len(products),
                has_more=len(all_products) > end and end < self.settings.max_results,
                max_results=self.settings.max_results,
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

    async def get_observation(self, scheduling_block_id: int) -> GetObservationResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        observation_query = self.queries.build_observation(scheduling_block_id)
        projects_query = self.queries.build_observation_projects(scheduling_block_id)
        product_criteria = SearchCriteria(
            scheduling_block_id=scheduling_block_id,
            page_size=self.settings.max_results,
            released_only=False,
        )
        products_query, product_limit = self.queries.build_search(product_criteria)
        observation_rows, project_rows, product_rows = await asyncio.gather(
            self.client.tap_query(observation_query, max_records=2, correlation_id=correlation_id),
            self.client.tap_query(projects_query, max_records=50, correlation_id=correlation_id),
            self.client.tap_query(
                products_query, max_records=product_limit, correlation_id=correlation_id
            ),
        )
        if not observation_rows:
            raise CasdaError(
                "OBSERVATION_NOT_FOUND",
                "No CASDA observation has the requested scheduling block identifier.",
                details={"scheduling_block_id": scheduling_block_id},
            )
        if len(observation_rows) > 1:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned duplicate observation identifiers."
            )
        products = [
            product_from_row(
                row, ready=self.state.get_ready(row.get("obs_publisher_did") or "") is not None
            )
            for row in product_rows[: self.settings.max_results]
        ]
        return GetObservationResponse(
            observation=observation_from_row(observation_rows[0]),
            projects=[project_from_row(row) for row in project_rows],
            products=products,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters={"scheduling_block_id": scheduling_block_id},
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
        normalized = normalize_product_ids(product_ids)
        if len(normalized) > self.settings.max_stage_products:
            raise CasdaError(
                "STAGING_LIMIT_EXCEEDED",
                "The request exceeds CASDA_MAX_STAGE_PRODUCTS.",
                details={"requested": len(normalized), "maximum": self.settings.max_stage_products},
            )
        key = validate_idempotency_key(idempotency_key) if idempotency_key else str(uuid.uuid4())
        existing = self.state.get_staging_by_idempotency(key)
        if existing:
            if set(existing.product_ids) != set(normalized):
                raise CasdaError(
                    "IDEMPOTENCY_CONFLICT",
                    "The idempotency key was already used with different product identifiers.",
                )
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
        await self.client.start_staging_job(job_url, correlation_id=correlation_id)
        status = "UNKNOWN"
        failure_reason: str | None = None
        expiry = None
        try:
            archive_status = await self.client.get_staging_status(
                job_url, correlation_id=correlation_id
            )
            status = archive_status.phase
            failure_reason = archive_status.failure_reason
            expiry = archive_status.destruction
        except CasdaError:
            failure_reason = (
                "The job was submitted, but its initial archive status was unavailable."
            )
        request = StagingRequest(
            request_id=request_id,
            idempotency_key=key,
            job_url=job_url,
            submitted_at=utc_now(),
            status=status,
            product_ids=normalized,
            filenames={product.product_id: product.filename for product in products},
            products=[
                StagingItem(
                    product_id=product.product_id,
                    status=status,
                    failure_reason=failure_reason,
                    status_source="archive_request" if status != "UNKNOWN" else "local",
                )
                for product in products
            ],
            expiry_time=expiry,
            failure_reason=failure_reason,
        )
        self.state.put_staging(request)
        self.metrics.increment("staging_submission_count")
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
        archive = await self.client.get_staging_status(
            request.job_url, correlation_id=correlation_id
        )
        request.status = archive.phase
        request.expiry_time = archive.destruction
        request.failure_reason = archive.failure_reason
        request.result_urls = archive.result_urls
        request.products = self._staging_items(request, archive.phase, archive.failure_reason)
        if archive.phase == "COMPLETED":
            self._record_ready_artifacts(request)
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
                if urlparse(ready.download_url).query:
                    warnings.append(
                        f"A short-lived or signed URL was omitted for product {product.product_id}."
                    )
                else:
                    download_url = ready.download_url
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
    ) -> list[StagingItem]:
        items: list[StagingItem] = []
        for product_id in request.product_ids:
            ready = self.state.get_ready(product_id)
            if ready and ready.request_id == request.request_id:
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

    def _record_ready_artifacts(self, request: StagingRequest) -> None:
        by_filename = {
            unquote(urlparse(url).path.rsplit("/", 1)[-1]): url for url in request.result_urls
        }
        for product_id, filename in request.filenames.items():
            if not filename:
                continue
            expected = Path(filename).name
            download_url = by_filename.get(expected)
            if not download_url:
                continue
            self.client.validate_archive_url(download_url)
            checksum_url = by_filename.get(f"{expected}.checksum")
            if checksum_url:
                self.client.validate_archive_url(checksum_url)
            self.state.put_ready(
                ReadyArtifact(
                    product_id=product_id,
                    request_id=request.request_id,
                    download_url=download_url,
                    checksum_url=checksum_url,
                    confirmed_at=utc_now(),
                    expires_at=request.expiry_time,
                )
            )

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
