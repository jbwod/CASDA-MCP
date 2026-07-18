"""MCP protocol registration for CASDA tools and resources."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar, cast

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from casda_mcp import __version__
from casda_mcp.errors import CasdaError
from casda_mcp.models import (
    CreateManifestResponse,
    DownloadProductResponse,
    ErrorInfo,
    GetObservationResponse,
    GetProductResponse,
    SearchProductsResponse,
    StageProductsResponse,
    StagingStatusResponse,
)
from casda_mcp.query import SearchCriteria
from casda_mcp.service import CasdaService

LOGGER = logging.getLogger(__name__)
ResponseT = TypeVar("ResponseT", bound=BaseModel)
ProductType = Literal[
    "image", "cube", "visibility", "spectrum", "catalogue", "weight", "moment_map"
]
ProductSort = Literal[
    "product_id", "filename", "file_size", "observation_start", "release_date", "distance"
]


def _error(response_type: type[ResponseT], exc: CasdaError) -> ResponseT:
    return response_type(error=ErrorInfo(**exc.as_dict()))


def _internal_error(response_type: type[ResponseT], exc: Exception) -> ResponseT:
    LOGGER.exception(
        "unhandled_tool_error", extra={"fields": {"exception_type": type(exc).__name__}}
    )
    return response_type(
        error=ErrorInfo(
            code="INTERNAL_ERROR",
            message="The CASDA MCP server could not complete the operation.",
            retryable=False,
        )
    )


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
            "Staging and downloads "
            "are separate guarded operations and are disabled by default."
        ),
        host=host,
        port=port,
        json_response=True,
        lifespan=lifespan,
    )
    cast(Any, mcp).casda_service = service

    @mcp.tool()
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
                    "weight, moment_map."
                ),
                max_length=7,
            ),
        ] = None,
        collection: Annotated[
            str | None, Field(description="Exact ObsCore collection name.")
        ] = None,
        released_only: Annotated[
            bool, Field(description="Exclude unreleased products when true.")
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
                    released_only=released_only,
                    sort_by=sort_by,
                    sort_order=sort_order,
                    page=page,
                    page_size=page_size,
                )
            )
        except CasdaError as exc:
            return _error(SearchProductsResponse, exc)
        except Exception as exc:
            return _internal_error(SearchProductsResponse, exc)

    @mcp.tool()
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
            return _error(GetProductResponse, exc)
        except Exception as exc:
            return _internal_error(GetProductResponse, exc)

    @mcp.tool()
    async def casda_get_observation(
        scheduling_block_id: Annotated[
            int, Field(description="Positive ASKAP scheduling block identifier.", gt=0)
        ],
    ) -> GetObservationResponse:
        """Retrieve an observation, projects, and bounded products for an ASKAP SBID."""
        try:
            return await service.get_observation(scheduling_block_id)
        except CasdaError as exc:
            return _error(GetObservationResponse, exc)
        except Exception as exc:
            return _internal_error(GetObservationResponse, exc)

    @mcp.tool()
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
            return _error(StageProductsResponse, exc)
        except Exception as exc:
            return _internal_error(StageProductsResponse, exc)

    @mcp.tool()
    async def casda_get_staging_status(
        request_id: Annotated[
            str, Field(description="Archive staging request identifier returned by this server.")
        ],
    ) -> StagingStatusResponse:
        """Perform one uncached status check for a known CASDA staging request.

        This tool does not continue polling after the call returns and does not download files.
        """
        try:
            return await service.get_staging_status(request_id)
        except CasdaError as exc:
            return _error(StagingStatusResponse, exc)
        except Exception as exc:
            return _internal_error(StagingStatusResponse, exc)

    @mcp.tool()
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
    ) -> DownloadProductResponse:
        """Download one confirmed-ready product using guarded, streamed local filesystem writes.

        This operation requires CASDA_ENABLE_DOWNLOADS and a restricted CASDA_DOWNLOAD_DIR.
        It never overwrites by default, enforces the byte limit, verifies Content-Length, resumes
        within the call when Range is supported, and removes incomplete files after failure.
        """
        try:
            return await service.download_product(
                product_id,
                destination=destination,
                verify_checksum=verify_checksum,
            )
        except CasdaError as exc:
            return _error(DownloadProductResponse, exc)
        except Exception as exc:
            return _internal_error(DownloadProductResponse, exc)

    @mcp.tool()
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
            return _error(CreateManifestResponse, exc)
        except Exception as exc:
            return _internal_error(CreateManifestResponse, exc)

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
    """Create the Streamable HTTP app with a non-sensitive health endpoint."""

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

    app = server.streamable_http_app()
    app.routes.insert(0, Route("/healthz", healthz, methods=["GET"]))
    return app


mcp = create_mcp_server()
