"""MCP protocol registration for CASDA tools and resources."""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypeVar, cast

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from casda_mcp.errors import CasdaError
from casda_mcp.models import (
    ErrorInfo,
    GetObservationResponse,
    GetProductResponse,
    SearchProductsResponse,
)
from casda_mcp.query import SearchCriteria
from casda_mcp.service import CasdaService

LOGGER = logging.getLogger(__name__)
ResponseT = TypeVar("ResponseT", bound=BaseModel)


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
            list[str] | None,
            Field(
                description=(
                    "Allowlisted types: image, cube, visibility, spectrum, catalogue, "
                    "weight, moment_map."
                )
            ),
        ] = None,
        collection: Annotated[
            str | None, Field(description="Exact ObsCore collection name.")
        ] = None,
        released_only: Annotated[
            bool, Field(description="Exclude unreleased products when true.")
        ] = True,
        sort_by: Annotated[
            str,
            Field(
                description=(
                    "Allowlisted sort field: product_id, filename, file_size, "
                    "observation_start, release_date, distance."
                )
            ),
        ] = "product_id",
        sort_order: Annotated[str, Field(description="Sort direction: asc or desc.")] = "asc",
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
                    product_types=product_types,
                    collection=collection,
                    released_only=released_only,
                    sort_by=sort_by,
                    sort_order=sort_order,  # type: ignore[arg-type]
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

    return mcp


mcp = create_mcp_server()
