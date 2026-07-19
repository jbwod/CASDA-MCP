"""MCP protocol registration for CASDA tools and resources."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, NoReturn, TypeVar, cast

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from casda_mcp import __version__
from casda_mcp.errors import CasdaError
from casda_mcp.models import (
    BuildAdqlResponse,
    CreateManifestResponse,
    DescribeTableResponse,
    DownloadProductResponse,
    GetArchiveStatusResponse,
    GetObservationResponse,
    GetProductResponse,
    ListCapabilitiesResponse,
    ListForeignKeysResponse,
    ListSchemasResponse,
    ListTablesResponse,
    SearchProductsResponse,
    StageProductsResponse,
    StagingStatusResponse,
    TapQueryResponse,
    ValidateAdqlResponse,
)
from casda_mcp.query import SearchCriteria
from casda_mcp.service import CasdaService
from casda_mcp.skills_loader import get_skill, skills_index

LOGGER = logging.getLogger(__name__)
ResponseT = TypeVar("ResponseT", bound=BaseModel)
ProductType = Literal[
    "image",
    "cube",
    "visibility",
    "spectrum",
    "catalogue",
    "weight",
    "moment_map",
    "cubelet",
    "evaluation",
    "scan",
]
ProductSort = Literal[
    "product_id", "filename", "file_size", "observation_start", "release_date", "distance"
]

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
_LOCAL_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_STATE_CHANGING = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
_IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


def _raise_tool_error(exc: CasdaError) -> NoReturn:
    raise ToolError(json.dumps(exc.as_dict(), separators=(",", ":"))) from exc


def _raise_internal_error(exc: Exception) -> NoReturn:
    LOGGER.exception(
        "unhandled_tool_error", extra={"fields": {"exception_type": type(exc).__name__}}
    )
    raise ToolError(
        json.dumps(
            {
                "code": "INTERNAL_ERROR",
                "message": "The CASDA MCP server could not complete the operation.",
                "retryable": False,
                "details": {},
            },
            separators=(",", ":"),
        )
    ) from exc


def create_mcp_server(
    service: CasdaService | None = None, *, host: str = "127.0.0.1", port: int = 8000
) -> FastMCP:
    service = service or CasdaService()

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[dict[str, object]]:
        try:
            yield {}
        finally:
            await service.aclose()

    mcp = FastMCP(
        "CASDA",
        instructions=(
            "Structured, auditable access to the CSIRO ASKAP Science Data Archive. "
            "Search and inspect before selecting explicit product identifiers. "
            "Staging and downloads are separate guarded operations and are disabled by default. "
            "Use registered prompts for guided workflows and read casda://skills for "
            "procedural agent guidance. Prefer allowlisted discovery tools; advanced ADQL "
            "requires CASDA_ENABLE_ADVANCED_ADQL. Do not scrape DAP administrative flows."
        ),
        host=host,
        port=port,
        json_response=True,
        lifespan=lifespan,
    )
    cast(Any, mcp).casda_service = service

    @mcp.tool(
        title="Search CASDA products",
        annotations=_READ_ONLY,
    )
    async def casda_search_products(
        source_name: Annotated[
            str | None,
            Field(description="Exact CASDA target name; this tool does not resolve names."),
        ] = None,
        ra_deg: Annotated[
            float | None, Field(description="ICRS right ascension in degrees, in [0, 360).")
        ] = None,
        dec_deg: Annotated[
            float | None, Field(description="ICRS declination in degrees, in [-90, 90].")
        ] = None,
        radius_deg: Annotated[
            float | None,
            Field(description="Cone radius in degrees; bounded by server configuration."),
        ] = None,
        project_code: Annotated[
            str | None, Field(description="Exact OPAL/CASDA project code, such as AS102.")
        ] = None,
        scheduling_block_id: Annotated[
            int | None, Field(description="Positive ASKAP scheduling block identifier.")
        ] = None,
        observation_start: Annotated[
            str | None, Field(description="Earliest overlapping observation date/time in ISO 8601.")
        ] = None,
        observation_end: Annotated[
            str | None, Field(description="Latest overlapping observation date/time in ISO 8601.")
        ] = None,
        frequency_min_hz: Annotated[
            float | None, Field(description="Lower overlapping spectral frequency in hertz.")
        ] = None,
        frequency_max_hz: Annotated[
            float | None, Field(description="Upper overlapping spectral frequency in hertz.")
        ] = None,
        product_types: Annotated[
            list[ProductType] | None,
            Field(
                description=(
                    "Allowlisted types: image, cube, visibility, spectrum, catalogue, "
                    "weight, moment_map, cubelet, evaluation, scan."
                ),
                max_length=10,
            ),
        ] = None,
        collection: Annotated[
            str | None, Field(description="Exact ObsCore collection name.")
        ] = None,
        facility_name: Annotated[
            str | None, Field(description="Exact ObsCore facility_name filter.")
        ] = None,
        instrument_name: Annotated[
            str | None, Field(description="Exact ObsCore instrument_name filter.")
        ] = None,
        released_only: Annotated[
            bool,
            Field(
                description=(
                    "When true, require a non-null obs_release_date and exclude restricted "
                    "access rows after the TAP fetch."
                )
            ),
        ] = True,
        sort_by: Annotated[
            ProductSort,
            Field(
                description=(
                    "Allowlisted sort field: product_id, filename, file_size, "
                    "observation_start, release_date, distance."
                )
            ),
        ] = "product_id",
        sort_order: Annotated[
            Literal["asc", "desc"], Field(description="Sort direction: asc or desc.")
        ] = "asc",
        page: Annotated[
            int,
            Field(description="One-based page within the configured bounded result window.", ge=1),
        ] = 1,
        page_size: Annotated[
            int,
            Field(
                description="Number of results to return; bounded by server configuration.", ge=1
            ),
        ] = 25,
        cursor: Annotated[
            str | None,
            Field(description="Opaque next-page cursor from a previous search response."),
        ] = None,
    ) -> SearchProductsResponse:
        """Search product metadata with a safely generated, bounded TAP/ADQL query.

        This read-only tool performs no staging, download, filesystem write, source-name
        resolution, or unrestricted ADQL.
        Spatial coordinates are ICRS degrees and frequencies are hertz.
        """
        try:
            return await service.search_products(
                SearchCriteria(
                    source_name=source_name,
                    ra_deg=ra_deg,
                    dec_deg=dec_deg,
                    radius_deg=radius_deg,
                    project_code=project_code,
                    scheduling_block_id=scheduling_block_id,
                    observation_start=observation_start,
                    observation_end=observation_end,
                    frequency_min_hz=frequency_min_hz,
                    frequency_max_hz=frequency_max_hz,
                    product_types=list(product_types) if product_types is not None else None,
                    collection=collection,
                    facility_name=facility_name,
                    instrument_name=instrument_name,
                    released_only=released_only,
                    sort_by=sort_by,
                    sort_order=sort_order,
                    page=page,
                    page_size=page_size,
                    cursor=cursor,
                )
            )
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Get CASDA product", annotations=_READ_ONLY)
    async def casda_get_product(
        product_id: Annotated[
            str, Field(description="Exact CASDA obs_publisher_did product identifier.")
        ],
    ) -> GetProductResponse:
        """Retrieve supported ObsCore metadata for one explicit product identifier.

        Access state is reported conservatively; this tool does not stage or download the product.
        """
        try:
            return await service.get_product(product_id)
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Get CASDA archive status", annotations=_READ_ONLY)
    async def casda_get_archive_status() -> GetArchiveStatusResponse:
        """Read VOSI availability for the configured public CASDA TAP service.

        This is archive availability, not local process liveness. Prefer /healthz for the
        MCP server itself.
        """
        try:
            return await service.get_archive_status()
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="List CASDA capabilities", annotations=_READ_ONLY)
    async def casda_list_capabilities() -> ListCapabilitiesResponse:
        """List VOSI capabilities advertised by the configured public CASDA TAP service."""
        try:
            return await service.list_capabilities()
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="List CASDA schemas", annotations=_READ_ONLY)
    async def casda_list_schemas(
        page_size: Annotated[
            int,
            Field(
                description="Number of schemas to return; bounded by server configuration.",
                ge=1,
            ),
        ] = 25,
        cursor: Annotated[
            str | None,
            Field(description="Opaque next-page cursor from a previous schema list response."),
        ] = None,
    ) -> ListSchemasResponse:
        """List TAP_SCHEMA schemas with bounded cursor pagination."""
        try:
            return await service.list_schemas(cursor=cursor, page_size=page_size)
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="List CASDA tables", annotations=_READ_ONLY)
    async def casda_list_tables(
        schema_name: Annotated[
            str | None,
            Field(
                description=(
                    "Optional TAP schema filter such as ivoa, casda, TAP_SCHEMA, or AS102."
                )
            ),
        ] = None,
        page_size: Annotated[
            int,
            Field(
                description="Number of tables to return; bounded by server configuration.",
                ge=1,
            ),
        ] = 25,
        cursor: Annotated[
            str | None,
            Field(description="Opaque next-page cursor from a previous table list response."),
        ] = None,
    ) -> ListTablesResponse:
        """List TAP_SCHEMA tables, optionally filtered by schema, with cursor pagination."""
        try:
            return await service.list_tables(
                schema_name=schema_name, cursor=cursor, page_size=page_size
            )
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Describe CASDA table", annotations=_READ_ONLY)
    async def casda_describe_table(
        schema_name: Annotated[
            str, Field(description="TAP schema name such as ivoa, casda, or AS102.")
        ],
        table_name: Annotated[
            str, Field(description="Unqualified TAP table name such as obscore or catalogue.")
        ],
    ) -> DescribeTableResponse:
        """Describe columns for one TAP_SCHEMA table identified by schema and table name."""
        try:
            return await service.describe_table(schema_name, table_name)
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="List CASDA foreign keys", annotations=_READ_ONLY)
    async def casda_list_foreign_keys(
        schema_name: Annotated[
            str, Field(description="TAP schema name such as ivoa, casda, or AS102.")
        ],
        table_name: Annotated[
            str, Field(description="Unqualified TAP table name such as catalogue.")
        ],
    ) -> ListForeignKeysResponse:
        """List TAP_SCHEMA foreign keys that originate from the requested table."""
        try:
            return await service.list_foreign_keys(schema_name, table_name)
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Build CASDA ADQL", annotations=_LOCAL_READ_ONLY)
    async def casda_build_adql(
        source_name: Annotated[
            str | None,
            Field(description="Exact CASDA target name; this tool does not resolve names."),
        ] = None,
        ra_deg: Annotated[
            float | None, Field(description="ICRS right ascension in degrees, in [0, 360).")
        ] = None,
        dec_deg: Annotated[
            float | None, Field(description="ICRS declination in degrees, in [-90, 90].")
        ] = None,
        radius_deg: Annotated[
            float | None,
            Field(description="Cone radius in degrees; bounded by server configuration."),
        ] = None,
        project_code: Annotated[
            str | None, Field(description="Exact OPAL/CASDA project code, such as AS102.")
        ] = None,
        scheduling_block_id: Annotated[
            int | None, Field(description="Positive ASKAP scheduling block identifier.")
        ] = None,
        observation_start: Annotated[
            str | None, Field(description="Earliest overlapping observation date/time in ISO 8601.")
        ] = None,
        observation_end: Annotated[
            str | None, Field(description="Latest overlapping observation date/time in ISO 8601.")
        ] = None,
        frequency_min_hz: Annotated[
            float | None, Field(description="Lower overlapping spectral frequency in hertz.")
        ] = None,
        frequency_max_hz: Annotated[
            float | None, Field(description="Upper overlapping spectral frequency in hertz.")
        ] = None,
        product_types: Annotated[
            list[ProductType] | None,
            Field(
                description=(
                    "Allowlisted types: image, cube, visibility, spectrum, catalogue, "
                    "weight, moment_map, cubelet, evaluation, scan."
                ),
                max_length=10,
            ),
        ] = None,
        collection: Annotated[
            str | None, Field(description="Exact ObsCore collection name.")
        ] = None,
        facility_name: Annotated[
            str | None, Field(description="Exact ObsCore facility_name filter.")
        ] = None,
        instrument_name: Annotated[
            str | None, Field(description="Exact ObsCore instrument_name filter.")
        ] = None,
        released_only: Annotated[
            bool,
            Field(description="When true, require a non-null obs_release_date in the ADQL."),
        ] = True,
        sort_by: Annotated[
            ProductSort,
            Field(
                description=(
                    "Allowlisted sort field: product_id, filename, file_size, "
                    "observation_start, release_date, distance."
                )
            ),
        ] = "product_id",
        sort_order: Annotated[
            Literal["asc", "desc"], Field(description="Sort direction: asc or desc.")
        ] = "asc",
        page: Annotated[
            int,
            Field(description="One-based page within the configured bounded result window.", ge=1),
        ] = 1,
        page_size: Annotated[
            int,
            Field(
                description="Number of results to return; bounded by server configuration.", ge=1
            ),
        ] = 25,
    ) -> BuildAdqlResponse:
        """Build the allowlisted ObsCore search ADQL string without contacting CASDA."""
        try:
            return service.build_adql(
                SearchCriteria(
                    source_name=source_name,
                    ra_deg=ra_deg,
                    dec_deg=dec_deg,
                    radius_deg=radius_deg,
                    project_code=project_code,
                    scheduling_block_id=scheduling_block_id,
                    observation_start=observation_start,
                    observation_end=observation_end,
                    frequency_min_hz=frequency_min_hz,
                    frequency_max_hz=frequency_max_hz,
                    product_types=list(product_types) if product_types is not None else None,
                    collection=collection,
                    facility_name=facility_name,
                    instrument_name=instrument_name,
                    released_only=released_only,
                    sort_by=sort_by,
                    sort_order=sort_order,
                    page=page,
                    page_size=page_size,
                )
            )
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Validate CASDA ADQL", annotations=_LOCAL_READ_ONLY)
    async def casda_validate_adql(
        query: Annotated[
            str,
            Field(description="Candidate ADQL SELECT statement to validate locally."),
        ],
    ) -> ValidateAdqlResponse:
        """Validate ADQL against the SELECT-only policy without contacting CASDA."""
        try:
            return service.validate_adql_query(query)
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Run CASDA TAP query", annotations=_READ_ONLY)
    async def casda_tap_query(
        query: Annotated[
            str,
            Field(description="SELECT-only ADQL validated and executed via sync TAP."),
        ],
    ) -> TapQueryResponse:
        """Execute one validated, bounded sync TAP query.

        Requires CASDA_ENABLE_ADVANCED_ADQL. Rejects mutations, comments, multi-statement
        input, and non-allowlisted table references.
        """
        try:
            return await service.tap_query(query)
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Get CASDA observation", annotations=_READ_ONLY)
    async def casda_get_observation(
        scheduling_block_id: Annotated[
            int | None, Field(description="Positive ASKAP scheduling block identifier.", gt=0)
        ] = None,
        observation_id: Annotated[
            str | None,
            Field(description="Exact ObsCore obs_id when not using the ASKAP SBID convenience."),
        ] = None,
    ) -> GetObservationResponse:
        """Retrieve an observation, projects, and bounded products for an ASKAP SBID or obs_id."""
        try:
            return await service.get_observation(
                scheduling_block_id=scheduling_block_id, observation_id=observation_id
            )
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Stage CASDA products", annotations=_STATE_CHANGING)
    async def casda_stage_products(
        product_ids: Annotated[
            list[str],
            Field(
                description=(
                    "Explicit CASDA product identifiers. Empty input is rejected; "
                    "duplicates are removed."
                ),
                min_length=1,
                max_length=500,
            ),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Caller-supplied idempotency key; a UUID is generated when omitted."),
        ] = None,
        allow_duplicate: Annotated[
            bool,
            Field(
                description=(
                    "Allow a new request for the same active product set when a new "
                    "idempotency key is used."
                )
            ),
        ] = False,
    ) -> StageProductsResponse:
        """Submit one guarded archive-side staging request for explicit products.

        This state-changing network operation requires authentication and CASDA_ENABLE_STAGING.
        It enforces configured product-count and estimated-size limits. It starts the confirmed
        archive job but does not poll continuously or write local files.
        """
        try:
            return await service.stage_products(
                product_ids,
                idempotency_key=idempotency_key,
                allow_duplicate=allow_duplicate,
            )
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Get CASDA staging status", annotations=_READ_ONLY)
    async def casda_get_staging_status(
        request_id: Annotated[
            str, Field(description="Archive staging request identifier returned by this server.")
        ],
    ) -> StagingStatusResponse:
        """Perform one uncached status check for a known CASDA staging request.

        This tool does not continue polling after the call returns and does not download files.
        Alias of casda_get_data_job for full-file staging jobs.
        """
        try:
            return await service.get_staging_status(request_id)
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Download CASDA product", annotations=_IDEMPOTENT_WRITE)
    async def casda_download_product(
        product_id: Annotated[
            str,
            Field(description="One explicit CASDA product identifier confirmed ready by staging."),
        ],
        destination: Annotated[
            str | None,
            Field(
                description=(
                    "Optional path constrained to CASDA_DOWNLOAD_DIR; defaults to the "
                    "archive filename."
                )
            ),
        ] = None,
        verify_checksum: Annotated[
            bool,
            Field(description="Verify the archive checksum when a checksum sidecar is available."),
        ] = True,
        ctx: Context[Any, Any, Any] | None = None,
    ) -> DownloadProductResponse:
        """Download one confirmed-ready product using guarded, streamed local filesystem writes.

        This operation requires CASDA_ENABLE_DOWNLOADS and a restricted CASDA_DOWNLOAD_DIR.
        It never overwrites by default, enforces the byte limit, verifies Content-Length, resumes
        within the call when Range is supported, and removes incomplete files after failure.
        """
        try:

            async def _progress(current: int, total: int | None) -> None:
                if ctx is not None:
                    await ctx.report_progress(current, total)

            return await service.download_product(
                product_id,
                destination=destination,
                verify_checksum=verify_checksum,
                progress_callback=_progress if ctx is not None else None,
            )
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.tool(title="Create CASDA manifest", annotations=_IDEMPOTENT_WRITE)
    async def casda_create_manifest(
        product_ids: Annotated[
            list[str],
            Field(
                description="Explicit CASDA product identifiers to record reproducibly.",
                min_length=1,
                max_length=1000,
            ),
        ],
        source_name: Annotated[
            str | None, Field(description="Optional source label to record in the manifest.")
        ] = None,
        workflow_name: Annotated[
            str | None, Field(description="Optional downstream workflow label.")
        ] = None,
        include_download_urls: Annotated[
            bool,
            Field(
                description=(
                    "Request URL inclusion. CASDA artifact URLs are never persisted because "
                    "opaque paths may be bearer credentials; true records an omission warning."
                )
            ),
        ] = False,
    ) -> CreateManifestResponse:
        """Create and retain a versioned, machine-readable manifest for explicit products.

        This tool performs bounded metadata and optional checksum reads. It does not stage,
        download, or write the manifest to the caller's filesystem. Signed and short-lived URLs
        are omitted even when URL inclusion is requested.
        """
        try:
            return await service.create_manifest(
                product_ids,
                source_name=source_name,
                workflow_name=workflow_name,
                include_download_urls=include_download_urls,
            )
        except CasdaError as exc:
            _raise_tool_error(exc)
        except Exception as exc:
            _raise_internal_error(exc)

    @mcp.prompt(
        name="find-and-inspect-products",
        title="Find and inspect CASDA products",
        description=(
            "Search bounded ObsCore metadata, present candidates, then inspect selected "
            "products or ASKAP observations. Does not stage or download."
        ),
    )
    def find_and_inspect_products(
        ra_deg: float | None = None,
        dec_deg: float | None = None,
        radius_deg: float | None = None,
        project_code: str | None = None,
        product_types: str | None = None,
    ) -> str:
        criteria: list[str] = []
        if ra_deg is not None:
            criteria.append(f"ra_deg={ra_deg}")
        if dec_deg is not None:
            criteria.append(f"dec_deg={dec_deg}")
        if radius_deg is not None:
            criteria.append(f"radius_deg={radius_deg}")
        if project_code:
            criteria.append(f'project_code="{project_code}"')
        if product_types:
            criteria.append(f"product_types hint: {product_types}")
        criteria_text = ", ".join(criteria) if criteria else "criteria supplied by the user in chat"
        return (
            "Use the CASDA MCP safely for discovery only.\n\n"
            f"1. Call casda_search_products with explicit bounded filters ({criteria_text}). "
            "Coordinates are ICRS degrees; frequencies are hertz; dates are ISO 8601. "
            "Do not resolve source names inside this server.\n"
            "2. Present stable product_id values, file sizes, release state, and access fields.\n"
            "3. For selected identifiers call casda_get_product. For a known ASKAP SBID call "
            "casda_get_observation.\n"
            "4. Do not stage or download unless the user explicitly asks next.\n"
            "5. Read casda://skills/casda-find-and-inspect if procedural detail is needed."
        )

    @mcp.prompt(
        name="stage-and-download",
        title="Stage and download CASDA products",
        description=(
            "Stage explicit products, check status once per wait, then download ready files "
            "under the configured download directory."
        ),
    )
    def stage_and_download(
        product_ids: str,
        destination: str | None = None,
    ) -> str:
        destination_text = (
            f' Use destination hint "{destination}" only if it stays under CASDA_DOWNLOAD_DIR.'
            if destination
            else ""
        )
        return (
            "Use the CASDA MCP for authenticated full-file staging and guarded download.\n\n"
            f"Selected product_ids (comma-separated or JSON list text): {product_ids}\n"
            "1. Confirm OPAL credentials and that staging/downloads are enabled; "
            "treat STAGING_DISABLED or DOWNLOADS_DISABLED as configuration issues.\n"
            "2. Inspect sizes with casda_get_product for each ID.\n"
            "3. Call casda_stage_products with those explicit IDs and a stable idempotency_key.\n"
            "4. Later call casda_get_staging_status once for the returned request_id; "
            "do not assume background polling.\n"
            "5. Only after products are ready, call casda_download_product one product at a "
            f"time with verify_checksum=true.{destination_text}\n"
            "6. Cutouts and spectrum generation are not available—do not invent those calls.\n"
            "7. Read casda://skills/casda-stage-and-download for safety details."
        )

    @mcp.prompt(
        name="build-reproducible-selection",
        title="Build a reproducible CASDA selection",
        description=(
            "Inspect explicit products and create a versioned manifest without persisting "
            "artifact download URLs."
        ),
    )
    def build_reproducible_selection(
        product_ids: str,
        source_name: str | None = None,
        workflow_name: str | None = None,
    ) -> str:
        labels: list[str] = []
        if source_name:
            labels.append(f'source_name="{source_name}"')
        if workflow_name:
            labels.append(f'workflow_name="{workflow_name}"')
        label_text = f" Include {', '.join(labels)}." if labels else ""
        return (
            "Record an explicit CASDA product selection for reproducibility.\n\n"
            f"product_ids: {product_ids}\n"
            "1. Confirm the IDs with the user after search/inspect.\n"
            "2. Optionally call casda_get_product for each ID.\n"
            f"3. Call casda_create_manifest with those IDs and include_download_urls=false."
            f"{label_text}\n"
            "4. Never persist archive artifact URLs; opaque paths may be short-lived credentials.\n"
            "5. Re-read via casda://manifests/{{manifest_id}} when needed.\n"
            "6. Read casda://skills/casda-reproducible-manifest for details."
        )

    @mcp.prompt(
        name="query-catalogue",
        title="Query CASDA catalogue products",
        description=(
            "Search ObsCore catalogue products with bounded filters. Dedicated SCS endpoints "
            "are not exposed."
        ),
    )
    def query_catalogue(
        ra_deg: float | None = None,
        dec_deg: float | None = None,
        radius_deg: float | None = None,
        project_code: str | None = None,
    ) -> str:
        criteria: list[str] = ['product_types=["catalogue"]']
        if ra_deg is not None:
            criteria.append(f"ra_deg={ra_deg}")
        if dec_deg is not None:
            criteria.append(f"dec_deg={dec_deg}")
        if radius_deg is not None:
            criteria.append(f"radius_deg={radius_deg}")
        if project_code:
            criteria.append(f'project_code="{project_code}"')
        return (
            "Search CASDA catalogue products only through the bounded ObsCore search tool.\n\n"
            f"1. Call casda_search_products with {', '.join(criteria)}.\n"
            "2. Present catalogue product_id values and inspect selected rows with "
            "casda_get_product.\n"
            "3. Do not call SCS, invent catalogue-specific endpoints, or write raw ADQL.\n"
            "4. Dedicated Simple Cone Search catalogue endpoints remain planned, not exposed."
        )

    @mcp.prompt(
        name="make-cutout",
        title="Make a CASDA cutout (unsupported)",
        description=(
            "Explains that spatial/spectral cutouts are not exposed by this server and must "
            "not be invented."
        ),
    )
    def make_cutout() -> str:
        return (
            "CASDA cutouts are not available through this MCP server.\n\n"
            "There is no cutout tool to call. Do not invent SODA cutout parameters, scrape the "
            "Data Access Portal, or claim cutout staging succeeded.\n"
            "Spatial/spectral cutouts and spectrum generation remain planned/authenticated "
            "CASDA capabilities outside the current MCP surface.\n"
            "Offer full-file search, inspect, stage, download, or manifest workflows instead, "
            "or direct the user to supported CASDA VO/DAP cutout paths outside this server."
        )

    @mcp.prompt(
        name="monitor-releases",
        title="Monitor CASDA release state",
        description=(
            "Check product release fields via search and get_product. The observation-events "
            "feed is not exposed yet."
        ),
    )
    def monitor_releases(
        project_code: str | None = None,
        scheduling_block_id: int | None = None,
        collection: str | None = None,
    ) -> str:
        criteria: list[str] = []
        if project_code:
            criteria.append(f'project_code="{project_code}"')
        if scheduling_block_id is not None:
            criteria.append(f"scheduling_block_id={scheduling_block_id}")
        if collection:
            criteria.append(f'collection="{collection}"')
        criteria_text = ", ".join(criteria) if criteria else "filters supplied by the user"
        return (
            "Check CASDA release state using existing metadata tools only.\n\n"
            f"1. Call casda_search_products with {criteria_text}. Consider released_only=false "
            "when the user wants unreleased rows, and sort_by=release_date when useful.\n"
            "2. Inspect selected products with casda_get_product and report release_date / "
            "release-related fields honestly.\n"
            "3. Do not claim continuous monitoring. The public observation-events feed is "
            "planned and not exposed as an MCP tool or resource yet.\n"
            "4. Do not scrape the DAP for release notices."
        )

    @mcp.resource("casda://archive/status", mime_type="application/json")
    async def archive_status_resource() -> str:
        """Read-only VOSI availability for the configured public CASDA TAP service."""
        try:
            return (await service.get_archive_status()).model_dump_json(exclude_none=True)
        except CasdaError as exc:
            return json.dumps({"error": exc.as_dict()}, separators=(",", ":"))

    @mcp.resource("casda://archive/capabilities", mime_type="application/json")
    async def archive_capabilities_resource() -> str:
        """Read-only VOSI capabilities for the configured public CASDA TAP service."""
        try:
            return (await service.list_capabilities()).model_dump_json(exclude_none=True)
        except CasdaError as exc:
            return json.dumps({"error": exc.as_dict()}, separators=(",", ":"))

    @mcp.resource("casda://skills", mime_type="application/json")
    async def skills_index_resource() -> str:
        """Read-only JSON index of packaged agent skills."""
        try:
            return json.dumps(skills_index(), separators=(",", ":"), sort_keys=True)
        except CasdaError as exc:
            return json.dumps({"error": exc.as_dict()}, separators=(",", ":"))

    @mcp.resource("casda://skills/{skill_name}", mime_type="text/markdown")
    async def skill_resource(skill_name: str) -> str:
        """Read-only SKILL.md guidance for one packaged agent skill."""
        try:
            return get_skill(skill_name).markdown
        except CasdaError as exc:
            return json.dumps({"error": exc.as_dict()}, separators=(",", ":"))

    @mcp.resource("casda://products/{product_id}", mime_type="application/json")
    async def product_resource(product_id: str) -> str:
        """Read-only JSON metadata for one exact CASDA product identifier."""
        try:
            return (await service.get_product(product_id)).model_dump_json(exclude_none=True)
        except CasdaError as exc:
            return json.dumps({"error": exc.as_dict()}, separators=(",", ":"))

    @mcp.resource("casda://observations/{scheduling_block_id}", mime_type="application/json")
    async def observation_resource(scheduling_block_id: int) -> str:
        """Read-only JSON metadata and relationships for one ASKAP scheduling block."""
        try:
            return (await service.get_observation(scheduling_block_id)).model_dump_json(
                exclude_none=True
            )
        except CasdaError as exc:
            return json.dumps({"error": exc.as_dict()}, separators=(",", ":"))

    @mcp.resource("casda://staging/{request_id}", mime_type="application/json")
    async def staging_resource(request_id: str) -> str:
        """One uncached JSON status read for a staging request known to this server."""
        try:
            return (await service.get_staging_status(request_id)).model_dump_json(exclude_none=True)
        except CasdaError as exc:
            return json.dumps({"error": exc.as_dict()}, separators=(",", ":"))

    @mcp.resource("casda://manifests/{manifest_id}", mime_type="application/json")
    async def manifest_resource(manifest_id: str) -> str:
        """Read-only JSON representation of a manifest created by this server."""
        manifest = service.state.get_manifest(manifest_id)
        if manifest is None:
            error = CasdaError("MANIFEST_NOT_FOUND", "The requested manifest is not known.")
            return json.dumps({"error": error.as_dict()}, separators=(",", ":"))
        return manifest.model_dump_json(exclude_none=True)

    @mcp.resource("casda://server/status", mime_type="application/json")
    async def server_status_resource() -> str:
        """Read-only local server version, safety flags, and process-local counters."""
        return json.dumps(
            {
                "server": "casda-mcp",
                "version": __version__,
                "staging_enabled": service.settings.enable_staging,
                "downloads_enabled": service.settings.enable_downloads,
                "metrics": service.metrics.snapshot(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    return mcp


def create_http_app(server: FastMCP) -> Starlette:
    """Create the Streamable HTTP app with liveness and readiness endpoints."""

    service = cast(CasdaService, cast(Any, server).casda_service)

    async def healthz(_: object) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "server": "casda-mcp",
                "version": __version__,
                "staging_enabled": service.settings.enable_staging,
                "downloads_enabled": service.settings.enable_downloads,
            }
        )

    async def readyz(_: object) -> JSONResponse:
        readiness = service.readiness_snapshot()
        archive_available = readiness.get("archive_available")
        ready = archive_available is not False
        payload = {
            "status": "ready" if ready else "not_ready",
            "server": "casda-mcp",
            "version": __version__,
            **readiness,
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    app = server.streamable_http_app()
    app.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
    app.routes.insert(1, Route("/readyz", readyz, methods=["GET"]))
    return app


mcp = create_mcp_server()
