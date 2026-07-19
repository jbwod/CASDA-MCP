"""Application service coordinating validation, CASDA access, state, and provenance."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from casda_mcp.adql import validate_adql
from casda_mcp.cache import TTLCache
from casda_mcp.client import CasdaClient
from casda_mcp.config import Settings
from casda_mcp.cursor import decode_cursor, encode_cursor, query_hash
from casda_mcp.downloads import Downloader, parse_checksum, resolve_destination
from casda_mcp.errors import CasdaError, ValidationError
from casda_mcp.models import (
    AbortDataJobResponse,
    AbortTapJobResponse,
    AuthStatusResponse,
    BuildAdqlResponse,
    CatalogueRow,
    ChecksumResult,
    CollectionMetadata,
    CollectionSummary,
    ColumnInfo,
    CreateManifestResponse,
    DataJobResult,
    DataJobResultsResponse,
    DatalinkServiceDescriptor,
    DeleteDataJobResponse,
    DeleteTapJobResponse,
    DescribeTableResponse,
    DownloadJobResultsResponse,
    DownloadProductResponse,
    DownloadResult,
    ForeignKeyInfo,
    GetArchiveStatusResponse,
    GetCollectionResponse,
    GetDatalinkResponse,
    GetEventResponse,
    GetObservationResponse,
    GetProductResponse,
    GetProjectResponse,
    ImageRow,
    JobKind,
    ListCapabilitiesResponse,
    ListCataloguesResponse,
    ListEventsResponse,
    ListForeignKeysResponse,
    ListImageSurveysResponse,
    ListSchemasResponse,
    ListTablesResponse,
    Manifest,
    ManifestProduct,
    Pagination,
    Product,
    ReadyArtifact,
    SchemaInfo,
    SearchCatalogueResponse,
    SearchImagesResponse,
    SearchProductsResponse,
    SearchProjectsResponse,
    SearchSpectraResponse,
    SpectrumRow,
    StageProductsResponse,
    StagingItem,
    StagingRequest,
    StagingStatusResponse,
    SubmitTapQueryResponse,
    SurveyInfo,
    TableInfo,
    TapJobRecord,
    TapJobResultsResponse,
    TapJobStatusResponse,
    TapQueryResponse,
    UwsResult,
    ValidateAdqlResponse,
    VerifyFileResponse,
)
from casda_mcp.observability import Metrics
from casda_mcp.parsers import observation_from_row, product_from_row, project_from_row
from casda_mcp.provenance import canonical_hash, make_provenance, utc_now
from casda_mcp.query import (
    PROJECT_CODE_RE,
    QueryBuilder,
    SearchCriteria,
    adql_string,
    normalize_product_id,
    normalize_product_ids,
    validate_catalogue_short_name,
    validate_dec_deg,
    validate_idempotency_key,
    validate_ra_deg,
    validate_radius_deg,
    validate_schema_name,
    validate_table_name,
    validate_vo_param,
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

    def note_archive_availability(self, available: bool, *, detail: str | None = None) -> None:
        self._archive_available = available
        self._archive_status_checked_at = utc_now()
        self._archive_status_detail = detail

    async def get_archive_status(self) -> GetArchiveStatusResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        endpoint = f"{self.settings.tap_base_url}/availability"
        availability = await self.client.get_availability(correlation_id=correlation_id)
        detail = (
            "; ".join(availability.notes)
            if availability.notes
            else ("available" if availability.available else "unavailable")
        )
        self.note_archive_availability(availability.available, detail=detail)
        return GetArchiveStatusResponse(
            availability=availability,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=endpoint,
                parameters={},
                result_count=1,
                correlation_id=correlation_id,
            ),
        )

    async def list_capabilities(self) -> ListCapabilitiesResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        endpoint = f"{self.settings.tap_base_url}/capabilities"
        capabilities = await self.client.get_capabilities(correlation_id=correlation_id)
        return ListCapabilitiesResponse(
            capabilities=capabilities,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=endpoint,
                parameters={},
                result_count=len(capabilities),
                correlation_id=correlation_id,
            ),
        )

    async def list_schemas(
        self,
        *,
        cursor: str | None = None,
        page_size: int = 25,
    ) -> ListSchemasResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        page_size = self._validate_page_size(page_size)
        parameters = {"page_size": page_size}
        bound_hash = query_hash(parameters)
        page = 1
        if cursor:
            offset, page_size = decode_cursor(cursor, expected_query_hash=bound_hash)
            page = (offset // page_size) + 1 if page_size else 1
        else:
            offset = 0
        fetch_count = self.settings.max_results + 1
        rows, cached = await self._cached_tap_query(
            self.queries.build_list_schemas(fetch_count=fetch_count),
            max_records=fetch_count,
            correlation_id=correlation_id,
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        schemas = [
            SchemaInfo(schema_name=name, description=row.get("description"))
            for row in rows
            if (name := row.get("schema_name"))
        ]
        end = min(offset + page_size, self.settings.max_results, len(schemas))
        page_schemas = schemas[offset:end]
        has_more = len(schemas) > end and end < self.settings.max_results
        next_cursor = (
            encode_cursor(query_hash=bound_hash, offset=end, page_size=page_size)
            if has_more
            else None
        )
        return ListSchemasResponse(
            schemas=page_schemas,
            pagination=Pagination(
                page=page,
                page_size=page_size,
                returned=len(page_schemas),
                has_more=has_more,
                max_results=self.settings.max_results,
                next_cursor=next_cursor,
                offset=offset,
            ),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters=parameters,
                result_count=len(page_schemas),
                cached=cached,
                correlation_id=correlation_id,
            ),
        )

    async def list_tables(
        self,
        *,
        schema_name: str | None = None,
        cursor: str | None = None,
        page_size: int = 25,
    ) -> ListTablesResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        page_size = self._validate_page_size(page_size)
        if schema_name is not None:
            schema_name = validate_schema_name(schema_name)
        parameters = {"schema_name": schema_name, "page_size": page_size}
        bound_hash = query_hash(parameters)
        page = 1
        if cursor:
            offset, page_size = decode_cursor(cursor, expected_query_hash=bound_hash)
            page = (offset // page_size) + 1 if page_size else 1
        else:
            offset = 0
        fetch_count = self.settings.max_results + 1
        rows, cached = await self._cached_tap_query(
            self.queries.build_list_tables(schema_name=schema_name, fetch_count=fetch_count),
            max_records=fetch_count,
            correlation_id=correlation_id,
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        tables = [self._table_from_row(row) for row in rows if row.get("table_name")]
        end = min(offset + page_size, self.settings.max_results, len(tables))
        page_tables = tables[offset:end]
        has_more = len(tables) > end and end < self.settings.max_results
        next_cursor = (
            encode_cursor(query_hash=bound_hash, offset=end, page_size=page_size)
            if has_more
            else None
        )
        return ListTablesResponse(
            tables=page_tables,
            pagination=Pagination(
                page=page,
                page_size=page_size,
                returned=len(page_tables),
                has_more=has_more,
                max_results=self.settings.max_results,
                next_cursor=next_cursor,
                offset=offset,
            ),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters={key: value for key, value in parameters.items() if value is not None},
                result_count=len(page_tables),
                cached=cached,
                correlation_id=correlation_id,
            ),
        )

    async def describe_table(self, schema_name: str, table_name: str) -> DescribeTableResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        schema_name = validate_schema_name(schema_name)
        table_name = validate_table_name(table_name)
        fetch_count = self.settings.max_results + 1
        rows, cached = await self._cached_tap_query(
            self.queries.build_describe_table(schema_name, table_name, fetch_count=fetch_count),
            max_records=fetch_count,
            correlation_id=correlation_id,
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        if not rows:
            raise CasdaError(
                "TABLE_NOT_FOUND",
                "No TAP_SCHEMA columns were found for the requested table.",
                details={"schema_name": schema_name, "table_name": table_name},
            )
        columns = [self._column_from_row(row) for row in rows if row.get("column_name")]
        return DescribeTableResponse(
            schema_name=schema_name,
            table_name=table_name,
            columns=columns,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters={"schema_name": schema_name, "table_name": table_name},
                result_count=len(columns),
                cached=cached,
                correlation_id=correlation_id,
            ),
        )

    async def list_foreign_keys(self, schema_name: str, table_name: str) -> ListForeignKeysResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        schema_name = validate_schema_name(schema_name)
        table_name = validate_table_name(table_name)
        fetch_count = self.settings.max_results + 1
        rows, cached = await self._cached_tap_query(
            self.queries.build_list_foreign_keys(schema_name, table_name, fetch_count=fetch_count),
            max_records=fetch_count,
            correlation_id=correlation_id,
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        foreign_keys = [self._foreign_key_from_row(row) for row in rows if row.get("key_id")]
        return ListForeignKeysResponse(
            schema_name=schema_name,
            table_name=table_name,
            foreign_keys=foreign_keys,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters={"schema_name": schema_name, "table_name": table_name},
                result_count=len(foreign_keys),
                cached=cached,
                correlation_id=correlation_id,
            ),
        )

    async def search_images(
        self,
        *,
        pos_type: str,
        ra_deg: float | None = None,
        dec_deg: float | None = None,
        radius_deg: float | None = None,
        ra_min_deg: float | None = None,
        ra_max_deg: float | None = None,
        dec_min_deg: float | None = None,
        dec_max_deg: float | None = None,
        polygon: list[tuple[float, float]] | None = None,
        band: str | None = None,
        time: str | None = None,
        pol: str | None = None,
        max_records: int | None = None,
    ) -> SearchImagesResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        max_records = self._bound_max_records(max_records)
        params = self._build_sia2_params(
            pos_type=pos_type,
            ra_deg=ra_deg,
            dec_deg=dec_deg,
            radius_deg=radius_deg,
            ra_min_deg=ra_min_deg,
            ra_max_deg=ra_max_deg,
            dec_min_deg=dec_min_deg,
            dec_max_deg=dec_max_deg,
            polygon=polygon,
            band=band,
            time=time,
            pol=pol,
            max_records=max_records,
        )
        rows = await self.client.sia2_query(params, correlation_id=correlation_id)
        self.note_archive_availability(True, detail="SIA2 query succeeded")
        images = [self._image_from_row(row) for row in rows[:max_records]]
        return SearchImagesResponse(
            images=images,
            max_records=max_records,
            returned=len(images),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.sia2_url,
                parameters=dict(params),
                result_count=len(images),
                correlation_id=correlation_id,
            ),
        )

    async def list_image_surveys(self) -> ListImageSurveysResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        rows = await self.client.sia1_surveys(correlation_id=correlation_id)
        self.note_archive_availability(True, detail="SIA1 surveys inventory succeeded")
        surveys = [
            SurveyInfo(
                code=code,
                name=row.get("name"),
                description=row.get("description"),
                group=row.get("group"),
                endpoint=row.get("endpoint"),
            )
            for row in rows
            if (code := row.get("code"))
        ]
        return ListImageSurveysResponse(
            surveys=surveys,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.sia1_surveys_url,
                parameters={},
                result_count=len(surveys),
                correlation_id=correlation_id,
            ),
        )

    async def search_survey_images(
        self,
        *,
        survey: str,
        ra_deg: float,
        dec_deg: float,
        size_deg: float,
        max_records: int | None = None,
    ) -> SearchImagesResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        max_records = self._bound_max_records(max_records)
        survey = validate_vo_param(survey, field="survey")
        ra_deg = validate_ra_deg(ra_deg)
        dec_deg = validate_dec_deg(dec_deg)
        size_deg = validate_radius_deg(
            size_deg, maximum=self.settings.max_cone_radius_deg, field="size_deg"
        )
        params = [
            ("POS", f"{ra_deg:.12g},{dec_deg:.12g}"),
            ("SIZE", f"{size_deg:.12g}"),
            ("SURVEY", survey),
            ("MAXREC", str(max_records)),
        ]
        rows = await self.client.sia1_query(params, correlation_id=correlation_id)
        self.note_archive_availability(True, detail="SIA1 query succeeded")
        images = [self._image_from_row(row) for row in rows[:max_records]]
        return SearchImagesResponse(
            images=images,
            max_records=max_records,
            returned=len(images),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.sia1_url,
                parameters=dict(params),
                result_count=len(images),
                correlation_id=correlation_id,
            ),
        )

    async def list_catalogues(
        self,
        *,
        cursor: str | None = None,
        page_size: int = 25,
    ) -> ListCataloguesResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        page_size = self._validate_page_size(page_size)
        parameters = {"page_size": page_size}
        bound_hash = query_hash(parameters)
        page = 1
        if cursor:
            offset, page_size = decode_cursor(cursor, expected_query_hash=bound_hash)
            page = (offset // page_size) + 1 if page_size else 1
        else:
            offset = 0
        fetch_count = self.settings.max_results + 1
        rows, cached = await self._cached_tap_query(
            self.queries.build_list_catalogues(fetch_count=fetch_count),
            max_records=fetch_count,
            correlation_id=correlation_id,
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        catalogues = [self._catalogue_inventory_from_row(row) for row in rows]
        end = min(offset + page_size, self.settings.max_results, len(catalogues))
        page_catalogues = catalogues[offset:end]
        has_more = len(catalogues) > end and end < self.settings.max_results
        next_cursor = (
            encode_cursor(query_hash=bound_hash, offset=end, page_size=page_size)
            if has_more
            else None
        )
        return ListCataloguesResponse(
            catalogues=page_catalogues,
            pagination=Pagination(
                page=page,
                page_size=page_size,
                returned=len(page_catalogues),
                has_more=has_more,
                max_results=self.settings.max_results,
                next_cursor=next_cursor,
                offset=offset,
            ),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters=parameters,
                result_count=len(page_catalogues),
                cached=cached,
                correlation_id=correlation_id,
            ),
        )

    async def search_catalogue(
        self,
        *,
        catalogue: str,
        ra_deg: float,
        dec_deg: float,
        radius_deg: float,
        max_records: int | None = None,
    ) -> SearchCatalogueResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        catalogue = validate_catalogue_short_name(catalogue)
        ra_deg = validate_ra_deg(ra_deg)
        dec_deg = validate_dec_deg(dec_deg)
        radius_deg = validate_radius_deg(
            radius_deg, maximum=self.settings.max_cone_radius_deg, field="radius_deg"
        )
        max_records = self._bound_max_records(max_records)
        params = [
            ("RA", f"{ra_deg:.12g}"),
            ("DEC", f"{dec_deg:.12g}"),
            ("SR", f"{radius_deg:.12g}"),
            ("MAXREC", str(max_records)),
        ]
        endpoint = f"{self.settings.scs_base_url}/{catalogue}"
        rows = await self.client.scs_query(catalogue, params, correlation_id=correlation_id)
        self.note_archive_availability(True, detail="SCS query succeeded")
        catalogue_rows = [self._catalogue_row_from_scs(row) for row in rows[:max_records]]
        return SearchCatalogueResponse(
            catalogue=catalogue,
            rows=catalogue_rows,
            max_records=max_records,
            returned=len(catalogue_rows),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=endpoint,
                parameters={"catalogue": catalogue, **dict(params)},
                result_count=len(catalogue_rows),
                correlation_id=correlation_id,
            ),
        )

    async def search_spectra(
        self,
        *,
        ra_deg: float,
        dec_deg: float,
        size_deg: float,
        band: str | None = None,
        time: str | None = None,
        max_records: int | None = None,
    ) -> SearchSpectraResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        ra_deg = validate_ra_deg(ra_deg)
        dec_deg = validate_dec_deg(dec_deg)
        size_deg = validate_radius_deg(
            size_deg, maximum=self.settings.max_cone_radius_deg, field="size_deg"
        )
        max_records = self._bound_max_records(max_records)
        params: list[tuple[str, str]] = [
            ("REQUEST", "queryData"),
            ("POS", f"{ra_deg:.12g},{dec_deg:.12g}"),
            ("SIZE", f"{size_deg:.12g}"),
            ("MAXREC", str(max_records)),
        ]
        if band is not None:
            params.append(("BAND", validate_vo_param(band, field="band")))
        if time is not None:
            params.append(("TIME", validate_vo_param(time, field="time")))
        rows = await self.client.ssa_query(params, correlation_id=correlation_id)
        self.note_archive_availability(True, detail="SSA query succeeded")
        spectra = [self._spectrum_from_row(row) for row in rows[:max_records]]
        return SearchSpectraResponse(
            spectra=spectra,
            max_records=max_records,
            returned=len(spectra),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.ssa_url,
                parameters=dict(params),
                result_count=len(spectra),
                correlation_id=correlation_id,
            ),
        )

    def _bound_max_records(self, max_records: int | None) -> int:
        if max_records is None:
            return self.settings.max_results
        if max_records < 1:
            raise ValidationError("max_records must be a positive integer.")
        return min(max_records, self.settings.max_results, self.settings.max_tap_rows)

    def _build_sia2_params(
        self,
        *,
        pos_type: str,
        ra_deg: float | None,
        dec_deg: float | None,
        radius_deg: float | None,
        ra_min_deg: float | None,
        ra_max_deg: float | None,
        dec_min_deg: float | None,
        dec_max_deg: float | None,
        polygon: list[tuple[float, float]] | None,
        band: str | None,
        time: str | None,
        pol: str | None,
        max_records: int,
    ) -> list[tuple[str, str]]:
        normalized = pos_type.strip().upper()
        if normalized == "CIRCLE":
            if ra_deg is None or dec_deg is None or radius_deg is None:
                raise ValidationError("CIRCLE position requires ra_deg, dec_deg, and radius_deg.")
            ra_deg = validate_ra_deg(ra_deg)
            dec_deg = validate_dec_deg(dec_deg)
            radius_deg = validate_radius_deg(
                radius_deg, maximum=self.settings.max_cone_radius_deg, field="radius_deg"
            )
            pos = f"CIRCLE {ra_deg:.12g} {dec_deg:.12g} {radius_deg:.12g}"
        elif normalized == "RANGE":
            values = (ra_min_deg, ra_max_deg, dec_min_deg, dec_max_deg)
            if any(value is None for value in values):
                raise ValidationError(
                    "RANGE position requires ra_min_deg, ra_max_deg, dec_min_deg, and dec_max_deg."
                )
            assert ra_min_deg is not None and ra_max_deg is not None
            assert dec_min_deg is not None and dec_max_deg is not None
            for field_name, value in (
                ("ra_min_deg", ra_min_deg),
                ("ra_max_deg", ra_max_deg),
            ):
                if not math.isfinite(value) or not 0 <= value < 360:
                    raise ValidationError(f"{field_name} must be finite and in [0, 360).")
            for field_name, value in (
                ("dec_min_deg", dec_min_deg),
                ("dec_max_deg", dec_max_deg),
            ):
                if not math.isfinite(value) or not -90 <= value <= 90:
                    raise ValidationError(f"{field_name} must be finite and in [-90, 90].")
            if ra_min_deg > ra_max_deg or dec_min_deg > dec_max_deg:
                raise ValidationError("RANGE bounds must be ordered min <= max.")
            pos = f"RANGE {ra_min_deg:.12g} {ra_max_deg:.12g} {dec_min_deg:.12g} {dec_max_deg:.12g}"
        elif normalized == "POLYGON":
            if not polygon or len(polygon) < 3:
                raise ValidationError("POLYGON position requires at least three vertices.")
            if len(polygon) > 64:
                raise ValidationError("POLYGON position accepts at most 64 vertices.")
            parts: list[str] = []
            for index, (vertex_ra, vertex_dec) in enumerate(polygon):
                try:
                    parts.append(f"{validate_ra_deg(vertex_ra):.12g}")
                    parts.append(f"{validate_dec_deg(vertex_dec):.12g}")
                except ValidationError as exc:
                    raise ValidationError(
                        f"Invalid POLYGON vertex at index {index}.",
                        details=exc.details,
                    ) from exc
            pos = "POLYGON " + " ".join(parts)
        else:
            raise ValidationError(
                "pos_type must be CIRCLE, RANGE, or POLYGON.",
                details={"pos_type": pos_type},
            )
        params: list[tuple[str, str]] = [("POS", pos), ("MAXREC", str(max_records))]
        if band is not None:
            params.append(("BAND", validate_vo_param(band, field="band")))
        if time is not None:
            params.append(("TIME", validate_vo_param(time, field="time")))
        if pol is not None:
            params.append(("POL", validate_vo_param(pol, field="pol")))
        return params

    @staticmethod
    def _optional_float(row: dict[str, str | None], key: str) -> float | None:
        value = row.get(key)
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @staticmethod
    def _optional_int(row: dict[str, str | None], key: str) -> int | None:
        value = row.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _image_from_row(row: dict[str, str | None]) -> ImageRow:
        known = {
            "obs_publisher_did",
            "access_url",
            "access_format",
            "dataproduct_type",
            "obs_collection",
            "obs_id",
            "target_name",
            "survey",
            "image_title",
            "s_ra",
            "s_dec",
            "s_fov",
            "distance",
        }
        extras = {key: value for key, value in row.items() if key not in known}
        return ImageRow(
            obs_publisher_did=row.get("obs_publisher_did"),
            access_url=row.get("access_url"),
            access_format=row.get("access_format"),
            dataproduct_type=row.get("dataproduct_type"),
            obs_collection=row.get("obs_collection"),
            obs_id=row.get("obs_id"),
            target_name=row.get("target_name"),
            survey=row.get("survey"),
            image_title=row.get("image_title"),
            s_ra=CasdaService._optional_float(row, "s_ra"),
            s_dec=CasdaService._optional_float(row, "s_dec"),
            s_fov=CasdaService._optional_float(row, "s_fov"),
            distance=CasdaService._optional_float(row, "distance"),
            **extras,
        )

    @staticmethod
    def _catalogue_inventory_from_row(row: dict[str, str | None]) -> CatalogueRow:
        known = {
            "id",
            "filename",
            "format",
            "observation_id",
            "project_id",
            "image_id",
            "freq_ref",
            "time_obs",
            "time_obs_mjd",
            "quality_level",
            "released_date",
            "name",
            "ra",
            "dec",
        }
        extras = {key: value for key, value in row.items() if key not in known}
        return CatalogueRow(
            id=CasdaService._optional_int(row, "id"),
            filename=row.get("filename"),
            format=row.get("format"),
            observation_id=CasdaService._optional_int(row, "observation_id"),
            project_id=CasdaService._optional_int(row, "project_id"),
            image_id=CasdaService._optional_int(row, "image_id"),
            freq_ref=CasdaService._optional_float(row, "freq_ref"),
            time_obs=row.get("time_obs"),
            time_obs_mjd=CasdaService._optional_float(row, "time_obs_mjd"),
            quality_level=row.get("quality_level"),
            released_date=row.get("released_date"),
            **extras,
        )

    @staticmethod
    def _catalogue_row_from_scs(row: dict[str, str | None]) -> CatalogueRow:
        known = {
            "id",
            "filename",
            "format",
            "observation_id",
            "project_id",
            "image_id",
            "freq_ref",
            "time_obs",
            "time_obs_mjd",
            "quality_level",
            "released_date",
            "name",
            "ra",
            "dec",
        }
        extras = {key: value for key, value in row.items() if key not in known}
        return CatalogueRow(
            id=CasdaService._optional_int(row, "id"),
            name=row.get("name"),
            ra=CasdaService._optional_float(row, "ra"),
            dec=CasdaService._optional_float(row, "dec"),
            **extras,
        )

    @staticmethod
    def _spectrum_from_row(row: dict[str, str | None]) -> SpectrumRow:
        known = {
            "obs_publisher_id",
            "title",
            "access_url",
            "access_format",
            "access_estsize",
            "spectrum_type",
            "obs_collection",
            "s_ra",
            "s_dec",
            "num_chan",
        }
        extras = {key: value for key, value in row.items() if key not in known}
        return SpectrumRow(
            obs_publisher_id=row.get("obs_publisher_id"),
            title=row.get("title"),
            access_url=row.get("access_url"),
            access_format=row.get("access_format"),
            access_estsize=CasdaService._optional_int(row, "access_estsize"),
            spectrum_type=row.get("spectrum_type"),
            obs_collection=row.get("obs_collection"),
            s_ra=CasdaService._optional_float(row, "s_ra"),
            s_dec=CasdaService._optional_float(row, "s_dec"),
            num_chan=CasdaService._optional_int(row, "num_chan"),
            **extras,
        )

    def _validate_page_size(self, page_size: int) -> int:
        if page_size < 1:
            raise ValidationError("page_size must be a positive integer.")
        if page_size > self.settings.max_results:
            raise ValidationError(
                "page_size exceeds the configured result window.",
                details={"page_size": page_size, "maximum": self.settings.max_results},
            )
        return page_size

    @staticmethod
    def _table_from_row(row: dict[str, str | None]) -> TableInfo:
        schema_name = (row.get("schema_name") or "").strip()
        raw_table = (row.get("table_name") or "").strip()
        if not schema_name or not raw_table:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned incomplete TAP_SCHEMA table metadata.",
            )
        prefix = f"{schema_name}."
        table_name = raw_table[len(prefix) :] if raw_table.startswith(prefix) else raw_table
        return TableInfo(
            schema_name=schema_name,
            table_name=table_name,
            table_type=row.get("table_type"),
            description=row.get("description"),
        )

    @staticmethod
    def _column_from_row(row: dict[str, str | None]) -> ColumnInfo:
        column_name = row.get("column_name")
        if not column_name:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned a TAP_SCHEMA column without a name.",
            )
        size_text = row.get("size")
        try:
            size = int(size_text) if size_text is not None else None
        except ValueError as exc:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned an invalid TAP_SCHEMA column size.",
            ) from exc
        return ColumnInfo(
            column_name=column_name,
            datatype=row.get("datatype"),
            ucd=row.get("ucd"),
            unit=row.get("unit"),
            utype=row.get("utype"),
            description=row.get("description"),
            size=size,
            principal=CasdaService._tap_bool(row.get("principal")),
            indexed=CasdaService._tap_bool(row.get("indexed")),
            std=CasdaService._tap_bool(row.get("std")),
        )

    @staticmethod
    def _foreign_key_from_row(row: dict[str, str | None]) -> ForeignKeyInfo:
        key_id = row.get("key_id")
        from_table = row.get("from_table")
        target_table = row.get("target_table")
        from_column = row.get("from_column")
        target_column = row.get("target_column")
        if not key_id or not from_table or not target_table or not from_column or not target_column:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned incomplete TAP_SCHEMA foreign-key metadata.",
            )
        from_schema, from_name = CasdaService._split_qualified_name(from_table)
        target_schema, target_name = CasdaService._split_qualified_name(target_table)
        return ForeignKeyInfo(
            key_id=key_id,
            from_schema=from_schema,
            from_table=from_name,
            target_schema=target_schema,
            target_table=target_name,
            from_column=from_column,
            target_column=target_column,
            description=row.get("description"),
        )

    @staticmethod
    def _split_qualified_name(value: str) -> tuple[str, str]:
        if "." not in value:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned an unqualified TAP_SCHEMA table name.",
                details={"table_name": value},
            )
        schema_name, table_name = value.split(".", 1)
        if not schema_name or not table_name:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned an invalid qualified TAP_SCHEMA table name.",
                details={"table_name": value},
            )
        return schema_name, table_name

    @staticmethod
    def _tap_bool(value: str | None) -> bool | None:
        if value is None:
            return None
        lowered = value.strip().lower()
        if lowered in {"1", "true"}:
            return True
        if lowered in {"0", "false"}:
            return False
        return None

    def build_adql(self, criteria: SearchCriteria) -> BuildAdqlResponse:
        """Build the allowlisted search ADQL without contacting CASDA."""

        requested_at = utc_now()
        query, max_records = self.queries.build_search(criteria)
        parameters = criteria.as_parameters()
        return BuildAdqlResponse(
            query=query,
            max_records=max_records,
            parameters=parameters,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint="local://adql/build",
                parameters=parameters,
                result_count=1,
                correlation_id=str(uuid.uuid4()),
            ),
        )

    def validate_adql_query(self, query: str) -> ValidateAdqlResponse:
        """Validate ADQL against the SELECT-only policy without contacting CASDA."""

        requested_at = utc_now()
        validated = validate_adql(
            query,
            max_length=self.settings.max_adql_length,
            max_rows=self.settings.max_tap_rows,
        )
        return ValidateAdqlResponse(
            valid=True,
            query=validated.query,
            max_rows=validated.max_rows,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint="local://adql/validate",
                parameters={"query": validated.query, "max_rows": validated.max_rows},
                result_count=1,
                correlation_id=str(uuid.uuid4()),
            ),
        )

    async def tap_query(self, query: str) -> TapQueryResponse:
        """Run one validated sync TAP query when advanced ADQL is enabled."""

        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        if not self.settings.enable_advanced_adql:
            raise CasdaError(
                "ADVANCED_ADQL_DISABLED",
                "Advanced ADQL is disabled by server configuration.",
            )
        validated = validate_adql(
            query,
            max_length=self.settings.max_adql_length,
            max_rows=self.settings.max_tap_rows,
        )
        rows = await self.client.tap_query(
            validated.query,
            max_records=validated.max_rows,
            correlation_id=correlation_id,
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        return TapQueryResponse(
            rows=rows,
            max_rows=validated.max_rows,
            returned=len(rows),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters={"query": validated.query, "max_rows": validated.max_rows},
                result_count=len(rows),
                correlation_id=correlation_id,
            ),
        )

    async def submit_tap_query(self, query: str) -> SubmitTapQueryResponse:
        """Create and start one async TAP job for a validated SELECT-only query."""

        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        if not self.settings.enable_advanced_adql:
            raise CasdaError(
                "ADVANCED_ADQL_DISABLED",
                "Advanced ADQL is disabled by server configuration.",
            )
        validated = validate_adql(
            query,
            max_length=self.settings.max_adql_length,
            max_rows=self.settings.max_tap_rows,
        )
        job_url = await self.client.create_tap_job(
            validated.query,
            max_records=validated.max_rows,
            correlation_id=correlation_id,
        )
        request_id = unquote(urlparse(job_url).path.rstrip("/").rsplit("/", 1)[-1])
        validate_idempotency_key(request_id)
        await self.client.start_tap_job(job_url, correlation_id=correlation_id)
        record = TapJobRecord(
            request_id=request_id,
            job_url=job_url,
            query_hash=canonical_hash({"query": validated.query, "max_rows": validated.max_rows}),
            created_at=requested_at,
            phase="QUEUED",
        )
        self.state.put_tap_job(record)
        self.note_archive_availability(True, detail="TAP async job submitted")
        return SubmitTapQueryResponse(
            request_id=request_id,
            status=record.phase,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_async_url,
                parameters={"query": validated.query, "max_rows": validated.max_rows},
                result_count=1,
                request_id=request_id,
                correlation_id=correlation_id,
            ),
        )

    async def get_tap_job_status(self, request_id: str) -> TapJobStatusResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        record = self._require_tap_job(request_id)
        archive = await self.client.get_tap_job(record.job_url, correlation_id=correlation_id)
        record.phase = archive.phase
        self.state.put_tap_job(record)
        return TapJobStatusResponse(
            request_id=record.request_id,
            status=archive.phase,
            failure_reason=archive.failure_reason,
            expiry_time=archive.destruction,
            results=archive.results,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=record.job_url,
                parameters={"request_id": record.request_id, "cache_bypassed": True},
                result_count=len(archive.results),
                request_id=record.request_id,
                correlation_id=correlation_id,
            ),
        )

    async def get_tap_results(self, request_id: str) -> TapJobResultsResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        record = self._require_tap_job(request_id)
        rows = await self.client.get_tap_job_results(record.job_url, correlation_id=correlation_id)
        max_rows = self.settings.max_tap_rows
        if len(rows) > max_rows:
            rows = rows[:max_rows]
        return TapJobResultsResponse(
            request_id=record.request_id,
            rows=rows,
            max_rows=max_rows,
            returned=len(rows),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=f"{record.job_url}/results/result",
                parameters={"request_id": record.request_id, "max_rows": max_rows},
                result_count=len(rows),
                request_id=record.request_id,
                correlation_id=correlation_id,
            ),
        )

    async def abort_tap_job(self, request_id: str) -> AbortTapJobResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        record = self._require_tap_job(request_id)
        await self.client.abort_tap_job(record.job_url, correlation_id=correlation_id)
        archive = await self.client.get_tap_job(record.job_url, correlation_id=correlation_id)
        record.phase = archive.phase
        self.state.put_tap_job(record)
        return AbortTapJobResponse(
            request_id=record.request_id,
            status=record.phase,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=f"{record.job_url}/phase",
                parameters={"request_id": record.request_id, "phase": "ABORT"},
                result_count=1,
                request_id=record.request_id,
                correlation_id=correlation_id,
            ),
        )

    async def delete_tap_job(self, request_id: str) -> DeleteTapJobResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        record = self._require_tap_job(request_id)
        await self.client.delete_tap_job(record.job_url, correlation_id=correlation_id)
        self.state.delete_tap_job(record.request_id)
        return DeleteTapJobResponse(
            request_id=record.request_id,
            deleted=True,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=record.job_url,
                parameters={"request_id": record.request_id, "action": "DELETE"},
                result_count=0,
                request_id=record.request_id,
                correlation_id=correlation_id,
            ),
        )

    def _require_tap_job(self, request_id: str) -> TapJobRecord:
        request_id = validate_idempotency_key(request_id)
        record = self.state.get_tap_job(request_id)
        if record is None:
            raise CasdaError(
                "TAP_JOB_NOT_FOUND",
                "The TAP job is not known to this server instance or configured state database.",
                details={"request_id": request_id},
            )
        return record

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

    async def search_projects(
        self,
        *,
        project_code: str | None = None,
        short_name: str | None = None,
        cursor: str | None = None,
        page_size: int = 25,
    ) -> SearchProjectsResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        page_size = self._validate_page_size(page_size)
        if short_name is not None:
            adql_string(short_name, field="short_name")
        parameters = {
            "project_code": project_code,
            "short_name": short_name,
            "page_size": page_size,
        }
        bound_hash = query_hash(parameters)
        page = 1
        if cursor:
            offset, page_size = decode_cursor(cursor, expected_query_hash=bound_hash)
            page = (offset // page_size) + 1 if page_size else 1
        else:
            offset = 0
        fetch_count = self.settings.max_results + 1
        rows, cached = await self._cached_tap_query(
            self.queries.build_search_projects(
                project_code=project_code,
                short_name=short_name,
                fetch_count=fetch_count,
            ),
            max_records=fetch_count,
            correlation_id=correlation_id,
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        projects = [project_from_row(row) for row in rows]
        end = min(offset + page_size, self.settings.max_results, len(projects))
        page_projects = projects[offset:end]
        has_more = len(projects) > end and end < self.settings.max_results
        next_cursor = (
            encode_cursor(query_hash=bound_hash, offset=end, page_size=page_size)
            if has_more
            else None
        )
        return SearchProjectsResponse(
            projects=page_projects,
            pagination=Pagination(
                page=page,
                page_size=page_size,
                returned=len(page_projects),
                has_more=has_more,
                max_results=self.settings.max_results,
                next_cursor=next_cursor,
                offset=offset,
            ),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters=parameters,
                result_count=len(page_projects),
                cached=cached,
                correlation_id=correlation_id,
            ),
        )

    async def get_project(self, project_code: str) -> GetProjectResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        rows, cached = await self._cached_tap_query(
            self.queries.build_get_project(project_code),
            max_records=2,
            correlation_id=correlation_id,
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        if not rows:
            raise CasdaError(
                "PROJECT_NOT_FOUND",
                "No CASDA project matches the requested project code.",
                details={"project_code": project_code.strip().upper()},
            )
        if len(rows) > 1:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned duplicate project identifiers.",
            )
        return GetProjectResponse(
            project=project_from_row(rows[0]),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters={"project_code": project_code.strip().upper()},
                result_count=1,
                cached=cached,
                correlation_id=correlation_id,
            ),
        )

    async def get_collection(self, collection: str) -> GetCollectionResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        adql_string(collection, field="collection")
        summary_rows, type_rows, facility_rows = await asyncio.gather(
            self.client.tap_query(
                self.queries.build_collection_summary(collection),
                max_records=2,
                correlation_id=correlation_id,
            ),
            self.client.tap_query(
                self.queries.build_collection_product_types(
                    collection, fetch_count=self.settings.max_results
                ),
                max_records=self.settings.max_results,
                correlation_id=correlation_id,
            ),
            self.client.tap_query(
                self.queries.build_collection_facilities(
                    collection, fetch_count=self.settings.max_results
                ),
                max_records=self.settings.max_results,
                correlation_id=correlation_id,
            ),
        )
        self.note_archive_availability(True, detail="TAP sync query succeeded")
        if not summary_rows:
            raise CasdaError(
                "COLLECTION_NOT_FOUND",
                "No CASDA ObsCore products match the requested collection.",
                details={"collection": collection.strip()},
            )
        if len(summary_rows) > 1:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned duplicate collection aggregates.",
            )
        summary = summary_rows[0]
        obs_collection = summary.get("obs_collection") or collection.strip()
        product_count = self._optional_int(summary, "product_count") or 0
        product_types = sorted(
            value for row in type_rows if (value := row.get("dataproduct_type")) is not None
        )
        facility_names = sorted(
            value for row in facility_rows if (value := row.get("facility_name")) is not None
        )
        return GetCollectionResponse(
            collection=CollectionSummary(
                obs_collection=obs_collection,
                product_count=product_count,
                product_types=product_types,
                facility_names=facility_names,
                release_date_min=self._optional_datetime(summary.get("release_date_min")),
                release_date_max=self._optional_datetime(summary.get("release_date_max")),
            ),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.tap_url,
                parameters={"collection": obs_collection},
                result_count=1,
                correlation_id=correlation_id,
            ),
        )

    async def list_events(
        self,
        *,
        cursor: str | None = None,
        page_size: int = 25,
        project_code: str | None = None,
        event_type: str | None = None,
    ) -> ListEventsResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        page_size = self._validate_page_size(page_size)
        if project_code is not None:
            project_code = project_code.strip().upper()
            if not PROJECT_CODE_RE.fullmatch(project_code):
                raise ValidationError("project_code is not a valid CASDA/OPAL project code.")
        if event_type is not None:
            event_type = validate_vo_param(event_type, field="event_type").upper()
        parameters = {
            "page_size": page_size,
            "project_code": project_code,
            "event_type": event_type,
        }
        bound_hash = query_hash(parameters)
        page = 1
        if cursor:
            offset, page_size = decode_cursor(cursor, expected_query_hash=bound_hash)
            page = (offset // page_size) + 1 if page_size else 1
        else:
            offset = 0
        events = await self.client.get_events(correlation_id=correlation_id)
        self.note_archive_availability(True, detail="Observation events feed succeeded")
        if project_code is not None:
            events = [
                event for event in events if (event.project_code or "").upper() == project_code
            ]
        if event_type is not None:
            events = [event for event in events if (event.event_type or "").upper() == event_type]
        end = min(offset + page_size, self.settings.max_results, len(events))
        page_events = events[offset:end]
        has_more = len(events) > end and end < self.settings.max_results
        next_cursor = (
            encode_cursor(query_hash=bound_hash, offset=end, page_size=page_size)
            if has_more
            else None
        )
        return ListEventsResponse(
            events=page_events,
            pagination=Pagination(
                page=page,
                page_size=page_size,
                returned=len(page_events),
                has_more=has_more,
                max_results=self.settings.max_results,
                next_cursor=next_cursor,
                offset=offset,
            ),
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.events_url,
                parameters=parameters,
                result_count=len(page_events),
                correlation_id=correlation_id,
            ),
        )

    async def get_event(self, event_id: str) -> GetEventResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        event_id = validate_idempotency_key(event_id)
        events = await self.client.get_events(correlation_id=correlation_id)
        self.note_archive_availability(True, detail="Observation events feed succeeded")
        matches = [event for event in events if event.event_id == event_id]
        if not matches:
            raise CasdaError(
                "EVENT_NOT_FOUND",
                "No observation event matches the requested identifier in the current feed.",
                details={"event_id": event_id},
            )
        if len(matches) > 1:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned duplicate observation event identifiers.",
            )
        return GetEventResponse(
            event=matches[0],
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.events_url,
                parameters={"event_id": event_id},
                result_count=1,
                correlation_id=correlation_id,
            ),
        )

    @staticmethod
    def _optional_datetime(value: str | None) -> datetime | None:
        if value is None or value == "":
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (OverflowError, TypeError, ValueError) as exc:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned an invalid datetime value.",
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

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
        active = self.state.find_active_staging(normalized, job_kind="full_file")
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
                    product.access_url or "",
                    correlation_id=correlation_id,
                    service_name="async_service",
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
            job_kind="full_file",
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
        """Alias of get_data_job for full-file and general data jobs."""

        return await self.get_data_job(request_id)

    async def get_data_job(self, request_id: str) -> StagingStatusResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        request_id = validate_idempotency_key(request_id)
        request = self.state.get_staging(request_id)
        if request is None:
            raise CasdaError(
                "STAGING_REQUEST_NOT_FOUND",
                "The data job is not known to this server instance or configured state database.",
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
                "Call casda_get_data_job again later. This server does not poll automatically."
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
            job_kind=request.job_kind,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=request.job_url,
                parameters={
                    "request_id": request.request_id,
                    "job_kind": request.job_kind,
                    "cache_bypassed": True,
                },
                request_id=request.request_id,
                result_count=len(request.products),
                correlation_id=correlation_id,
            ),
        )

    async def get_auth_status(self) -> AuthStatusResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        configured = self.settings.has_credentials
        authenticated = False
        if configured:
            try:
                await self.client.verify_authentication(correlation_id=correlation_id)
                authenticated = True
            except CasdaError as exc:
                if exc.code not in {"AUTHENTICATION_FAILED", "AUTHORISATION_FAILED"}:
                    raise
                authenticated = False
        return AuthStatusResponse(
            credentials_configured=configured,
            authenticated=authenticated,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=self.settings.login_url,
                parameters={"credentials_configured": configured},
                result_count=1,
                correlation_id=correlation_id,
            ),
        )

    async def get_datalink(self, product_id: str) -> GetDatalinkResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        product_id = normalize_product_id(product_id)
        products = await self.get_products([product_id], correlation_id=correlation_id)
        product = products[0]
        if not product.access_url:
            raise CasdaError(
                "PRODUCT_UNAVAILABLE",
                "CASDA did not provide Datalink access metadata for this product.",
                details={"product_id": product_id},
            )
        await self.client.verify_authentication(correlation_id=correlation_id)
        descriptors = await self.client.inspect_datalink(
            product.access_url, correlation_id=correlation_id
        )
        services = [
            DatalinkServiceDescriptor(
                service_name=item.service_name,
                service_url=item.service_url,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                authenticated_id_present=item.authenticated_id_present,
            )
            for item in descriptors
        ]
        return GetDatalinkResponse(
            product_id=product_id,
            services=services,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=product.access_url,
                parameters={"product_id": product_id},
                result_count=len(services),
                correlation_id=correlation_id,
            ),
        )

    async def create_cutout(
        self,
        product_id: str,
        *,
        circle: str | None = None,
        polygon: str | None = None,
        band: str | None = None,
        channel: str | None = None,
        pol: str | None = None,
        coord: str | None = None,
        idempotency_key: str | None = None,
    ) -> StageProductsResponse:
        return await self._create_soda_data_job(
            product_id,
            job_kind="cutout",
            service_name="cutout_service",
            soda_params=self._soda_cutout_params(
                circle=circle,
                polygon=polygon,
                band=band,
                channel=channel,
                pol=pol,
                coord=coord,
            ),
            idempotency_key=idempotency_key,
        )

    async def create_spectrum(
        self,
        product_id: str,
        *,
        circle: str | None = None,
        polygon: str | None = None,
        band: str | None = None,
        channel: str | None = None,
        pol: str | None = None,
        coord: str | None = None,
        idempotency_key: str | None = None,
    ) -> StageProductsResponse:
        return await self._create_soda_data_job(
            product_id,
            job_kind="spectrum",
            service_name="spectrum_generation_service",
            soda_params=self._soda_cutout_params(
                circle=circle,
                polygon=polygon,
                band=band,
                channel=channel,
                pol=pol,
                coord=coord,
            ),
            idempotency_key=idempotency_key,
        )

    async def get_data_job_results(self, request_id: str) -> DataJobResultsResponse:
        status = await self.get_data_job(request_id)
        request = self.state.get_staging(request_id)
        assert request is not None
        results = [
            DataJobResult(
                result_id=result.result_id,
                mime_type=result.mime_type,
                size_bytes=result.size_bytes,
            )
            for result in request.results
        ]
        return DataJobResultsResponse(
            request_id=request.request_id,
            status=request.status,
            results=results,
            provenance=status.provenance,
        )

    async def abort_data_job(self, request_id: str) -> AbortDataJobResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        request = self._require_data_job(request_id)
        await self.client.abort_data_job(request.job_url, correlation_id=correlation_id)
        archive = await self.client.get_staging_status(
            request.job_url, correlation_id=correlation_id
        )
        request.status = archive.phase
        request.failure_reason = archive.failure_reason
        request.expiry_time = archive.destruction
        request.results = archive.results
        request.products = self._staging_items(request, request.status, request.failure_reason)
        self.state.put_staging(request)
        return AbortDataJobResponse(
            request_id=request.request_id,
            status=request.status,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=f"{request.job_url}/phase",
                parameters={"request_id": request.request_id, "phase": "ABORT"},
                result_count=1,
                request_id=request.request_id,
                correlation_id=correlation_id,
            ),
        )

    async def delete_data_job(self, request_id: str) -> DeleteDataJobResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        request = self._require_data_job(request_id)
        await self.client.delete_data_job(request.job_url, correlation_id=correlation_id)
        self.state.delete_staging(request.request_id)
        return DeleteDataJobResponse(
            request_id=request.request_id,
            deleted=True,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=request.job_url,
                parameters={"request_id": request.request_id, "action": "DELETE"},
                result_count=0,
                request_id=request.request_id,
                correlation_id=correlation_id,
            ),
        )

    async def download_job_results(
        self,
        request_id: str,
        *,
        verify_checksum: bool = True,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadJobResultsResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        if not self.settings.enable_downloads:
            raise CasdaError(
                "DOWNLOADS_DISABLED", "Local downloads are disabled by server configuration."
            )
        status = await self.get_data_job(request_id)
        request = self.state.get_staging(request_id)
        assert request is not None
        ready_ids = [item.product_id for item in request.products if item.ready_for_download]
        if not ready_ids:
            raise CasdaError(
                "PRODUCT_NOT_READY",
                "No ready results are available for this data job.",
                retryable=status.status in {"PENDING", "QUEUED", "EXECUTING", "SUSPENDED"},
                details={"request_id": request_id, "status": status.status},
            )
        downloaded: list[DownloadResult] = []
        failed_product_id: str | None = None
        failure_reason: str | None = None
        for index, product_id in enumerate(ready_ids):
            try:

                async def _progress(
                    current: int, total: int | None, *, _index: int = index
                ) -> None:
                    if progress_callback is None:
                        return
                    # Report overall batch progress as completed files + in-file fraction.
                    if total and total > 0:
                        overall = _index + (current / total)
                        await progress_callback(int(overall * 1000), len(ready_ids) * 1000)
                    else:
                        await progress_callback(_index, len(ready_ids))

                response = await self.download_product(
                    product_id,
                    destination=None,
                    verify_checksum=verify_checksum,
                    progress_callback=_progress if progress_callback is not None else None,
                )
                if response.result is None:
                    raise CasdaError(
                        "INTERNAL_ERROR",
                        "Download completed without a result payload.",
                        details={"product_id": product_id},
                    )
                downloaded.append(response.result)
            except CasdaError as exc:
                failed_product_id = product_id
                failure_reason = exc.message
                break
        return DownloadJobResultsResponse(
            request_id=request_id,
            results=downloaded,
            failed_product_id=failed_product_id,
            failure_reason=failure_reason,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint="local://download/job-results",
                parameters={
                    "request_id": request_id,
                    "verify_checksum": verify_checksum,
                    "ready_count": len(ready_ids),
                    "downloaded_count": len(downloaded),
                },
                result_count=len(downloaded),
                request_id=request_id,
                correlation_id=correlation_id,
            ),
        )

    async def verify_file(self, path: str) -> VerifyFileResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        if self.settings.download_dir is None:
            raise CasdaError("DOWNLOADS_DISABLED", "Local downloads are not configured.")
        download_root = self.settings.download_dir
        target = resolve_destination(
            download_root,
            path,
            Path(path).name,
            allow_overwrite=True,
        )
        if not target.exists() or not target.is_file() or target.is_symlink():
            return VerifyFileResponse(
                local_path=str(target),
                exists=False,
                provenance=make_provenance(
                    request_timestamp=requested_at,
                    endpoint="local://verify-file",
                    parameters={"path": path},
                    result_count=0,
                    correlation_id=correlation_id,
                ),
            )
        size_bytes = target.stat().st_size
        checksum = ChecksumResult()
        checksum_path = Path(str(target) + ".checksum")
        if checksum_path.is_file() and not checksum_path.is_symlink():
            try:
                spec = parse_checksum(checksum_path.read_text(encoding="utf-8"))
            except CasdaError:
                checksum = ChecksumResult(verified=False)
            else:
                hasher = hashlib.new(spec.algorithm)
                with target.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        hasher.update(chunk)
                actual = hasher.hexdigest().lower()
                checksum = ChecksumResult(
                    algorithm=spec.algorithm,
                    expected=spec.digest,
                    actual=actual,
                    verified=hmac.compare_digest(actual, spec.digest),
                )
                if not checksum.verified:
                    raise CasdaError(
                        "CHECKSUM_MISMATCH",
                        "The local file did not match its checksum sidecar.",
                        details={"algorithm": spec.algorithm, "path": str(target)},
                    )
        return VerifyFileResponse(
            local_path=str(target),
            exists=True,
            size_bytes=size_bytes,
            content_length_verified=True,
            checksum=checksum,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint="local://verify-file",
                parameters={"path": path},
                result_count=1,
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
            collection_metadata = CollectionMetadata(
                obs_collection=product.collection,
                facility_name=product.facility_name,
                release_date_min=product.release_date,
                release_date_max=product.release_date,
            )
            manifest_products.append(
                ManifestProduct(
                    product=product,
                    checksum=checksum,
                    checksum_algorithm=checksum_algorithm,
                    staging_request_id=ready.request_id if ready else None,
                    download_url=download_url,
                    collection_metadata=collection_metadata,
                )
            )
        collections = self._manifest_collections(manifest_products)
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
            "collections": [item.model_dump(mode="json") for item in collections],
            "original_search_criteria": search_criteria,
        }
        manifest = Manifest(
            manifest_id=f"sha256-{canonical_hash(identity)}",
            created_at=utc_now(),
            source_name=source_name,
            workflow_name=workflow_name,
            products=manifest_products,
            collections=collections,
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
            job_kind=request.job_kind,
            provenance=make_provenance(
                request_timestamp=requested_at,
                endpoint=request.job_url,
                parameters={
                    "product_ids": request.product_ids,
                    "idempotency_key": request.idempotency_key,
                    "job_kind": request.job_kind,
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

        # Cutout/spectrum jobs often return a single generated result whose ID is
        # not the ObsCore publisher DID. Assign it only when uniqueness is clear.
        if request.job_kind in {"cutout", "spectrum"} and len(request.product_ids) == 1:
            product_id = request.product_ids[0]
            if product_id not in assignments:
                candidates = [
                    result
                    for result in results
                    if result.result_id not in claimed_ids
                    and not result.result_id.endswith(".checksum")
                ]
                if len(candidates) == 1:
                    data_result = candidates[0]
                    checksum_result = by_id.get(f"{data_result.result_id}.checksum")
                    if checksum_result is not None and checksum_result.result_id in claimed_ids:
                        checksum_result = None
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

    @staticmethod
    def _manifest_collections(
        products: list[ManifestProduct],
    ) -> list[CollectionMetadata]:
        by_collection: dict[str, CollectionMetadata] = {}
        for item in products:
            meta = item.collection_metadata
            if meta is None or not meta.obs_collection:
                continue
            existing = by_collection.get(meta.obs_collection)
            if existing is None:
                by_collection[meta.obs_collection] = CollectionMetadata(
                    obs_collection=meta.obs_collection,
                    facility_name=meta.facility_name,
                    release_date_min=meta.release_date_min,
                    release_date_max=meta.release_date_max,
                )
                continue
            if existing.facility_name is None:
                existing.facility_name = meta.facility_name
            elif meta.facility_name is not None and existing.facility_name != meta.facility_name:
                existing.facility_name = None
            if meta.release_date_min is not None and (
                existing.release_date_min is None
                or meta.release_date_min < existing.release_date_min
            ):
                existing.release_date_min = meta.release_date_min
            if meta.release_date_max is not None and (
                existing.release_date_max is None
                or meta.release_date_max > existing.release_date_max
            ):
                existing.release_date_max = meta.release_date_max
        return [by_collection[key] for key in sorted(by_collection)]

    def _require_data_job(self, request_id: str) -> StagingRequest:
        request_id = validate_idempotency_key(request_id)
        request = self.state.get_staging(request_id)
        if request is None:
            raise CasdaError(
                "STAGING_REQUEST_NOT_FOUND",
                "The data job is not known to this server instance or configured state database.",
                details={"request_id": request_id},
            )
        return request

    @staticmethod
    def _soda_cutout_params(
        *,
        circle: str | None,
        polygon: str | None,
        band: str | None,
        channel: str | None,
        pol: str | None,
        coord: str | None,
    ) -> list[tuple[str, str]]:
        params: list[tuple[str, str]] = []
        if circle is not None:
            params.append(("CIRCLE", validate_vo_param(circle, field="CIRCLE")))
        if polygon is not None:
            params.append(("POLYGON", validate_vo_param(polygon, field="POLYGON")))
        if band is not None:
            params.append(("BAND", validate_vo_param(band, field="BAND")))
        if channel is not None:
            params.append(("CHANNEL", validate_vo_param(channel, field="CHANNEL")))
        if pol is not None:
            params.append(("POL", validate_vo_param(pol, field="POL")))
        if coord is not None:
            params.append(("COORD", validate_vo_param(coord, field="COORD")))
        if not params:
            raise ValidationError(
                "At least one SODA constraint is required "
                "(CIRCLE, POLYGON, BAND, CHANNEL, POL, or COORD)."
            )
        return params

    async def _create_soda_data_job(
        self,
        product_id: str,
        *,
        job_kind: JobKind,
        service_name: str,
        soda_params: list[tuple[str, str]],
        idempotency_key: str | None,
    ) -> StageProductsResponse:
        requested_at = utc_now()
        correlation_id = str(uuid.uuid4())
        if not self.settings.enable_staging:
            raise CasdaError(
                "STAGING_DISABLED", "Archive-side staging is disabled by server configuration."
            )
        normalized = normalize_product_ids([product_id])
        key = validate_idempotency_key(idempotency_key) if idempotency_key else str(uuid.uuid4())
        param_fingerprint = canonical_hash({"service_name": service_name, "params": soda_params})
        async with self._staging_submission_lock:
            existing = self.state.get_staging_by_idempotency(key)
            if existing:
                if (
                    set(existing.product_ids) != set(normalized)
                    or existing.job_kind != job_kind
                    or existing.param_fingerprint != param_fingerprint
                ):
                    raise CasdaError(
                        "IDEMPOTENCY_CONFLICT",
                        "The idempotency key was already used with different job parameters.",
                    )
                self._reconcile_completed_staging(existing)
                return self._stage_response(existing, requested_at=requested_at, reused=True)
            active = self.state.find_active_staging(
                normalized, job_kind=job_kind, param_fingerprint=param_fingerprint
            )
            if active:
                return self._stage_response(active, requested_at=requested_at, reused=True)

            products = await self.get_products(normalized, correlation_id=correlation_id)
            product = products[0]
            if not product.access_url:
                raise CasdaError(
                    "PRODUCT_UNAVAILABLE",
                    "CASDA did not provide Datalink access metadata for this product.",
                    details={"product_id": product.product_id},
                )
            await self.client.verify_authentication(correlation_id=correlation_id)
            access = await self.client.resolve_datalink(
                product.access_url,
                correlation_id=correlation_id,
                service_name=service_name,
            )
            job_url = await self.client.create_soda_job(
                access.service_url,
                [access.authenticated_id_token],
                extra_params=soda_params,
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
                filenames={product.product_id: product.filename},
                products=[
                    StagingItem(
                        product_id=product.product_id,
                        status="PENDING",
                        status_source="archive_request",
                    )
                ],
                job_kind=job_kind,
                param_fingerprint=param_fingerprint,
            )
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
                request.products = self._staging_items(
                    request, request.status, request.failure_reason
                )
                self.state.put_staging(request)
                raise CasdaError(
                    "STAGING_START_UNCONFIRMED",
                    "The CASDA data job was created, but starting it could not be confirmed. "
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
                request.products = self._staging_items(
                    request, request.status, request.failure_reason
                )
                self.state.put_staging(request)
            return self._stage_response(
                request, requested_at=requested_at, correlation_id=correlation_id
            )

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
