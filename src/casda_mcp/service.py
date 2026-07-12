"""Application service coordinating validation, CASDA access, state, and provenance."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from casda_mcp.cache import TTLCache
from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
from casda_mcp.errors import CasdaError
from casda_mcp.models import (
    GetObservationResponse,
    GetProductResponse,
    Pagination,
    SearchProductsResponse,
)
from casda_mcp.observability import Metrics
from casda_mcp.parsers import observation_from_row, product_from_row, project_from_row
from casda_mcp.provenance import canonical_hash, make_provenance, utc_now
from casda_mcp.query import (
    QueryBuilder,
    SearchCriteria,
    normalize_product_id,
    normalize_product_ids,
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

    async def get_products(self, product_ids: list[str], *, correlation_id: str) -> list[Any]:
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
