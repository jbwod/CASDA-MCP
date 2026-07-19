"""Typed models exposed at the MCP boundary and used internally."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class Provenance(BaseModel):
    server_version: str
    archive: Literal["CASDA"] = "CASDA"
    request_timestamp: datetime
    response_timestamp: datetime
    query_id: str
    request_id: str | None = None
    endpoint: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    result_count: int = 0
    cached: bool = False
    correlation_id: str


class Product(BaseModel):
    """Stable CASDA product representation derived from ObsCore metadata."""

    model_config = ConfigDict(extra="allow")

    product_id: str
    filename: str | None = None
    product_type: str | None = None
    product_subtype: str | None = None
    collection: str | None = None
    project_code: str | None = None
    observation_id: str | None = None
    sbid: int | None = None
    target_name: str | None = None
    access_format: str | None = None
    access_url: str | None = None
    file_size_bytes: int | None = None
    file_size_is_estimate: bool = True
    ra_deg: float | None = None
    dec_deg: float | None = None
    field_of_view_deg: float | None = None
    spatial_region: str | None = None
    spatial_resolution_arcsec: float | None = None
    spatial_resolution_min_arcsec: float | None = None
    spatial_resolution_max_arcsec: float | None = None
    spatial_pixels_x: int | None = None
    spatial_pixels_y: int | None = None
    observation_start_mjd: float | None = None
    observation_end_mjd: float | None = None
    observation_start: datetime | None = None
    observation_end: datetime | None = None
    exposure_seconds: float | None = None
    frequency_min_hz: float | None = None
    frequency_max_hz: float | None = None
    spectral_channels: int | None = None
    polarisation_states: str | None = None
    polarisation_samples: int | None = None
    facility_name: str | None = None
    instrument_name: str | None = None
    release_date: datetime | None = None
    quality_level: str | None = None
    calibration_level: int | None = None
    access_state: Literal["STAGING_REQUIRED", "READY", "RESTRICTED", "UNKNOWN"] = "UNKNOWN"
    authorisation_state: Literal["AUTHORISED", "DENIED", "UNKNOWN"] = "UNKNOWN"


class Observation(BaseModel):
    id: int | None = None
    sbid: int | None = None
    obs_id: str | None = None
    observation_start: datetime | None = None
    observation_end: datetime | None = None
    observation_start_mjd: float | None = None
    observation_end_mjd: float | None = None
    telescope: str | None = None
    observation_program: str | None = None
    deposit_state: str | None = None
    facility_name: str | None = None
    instrument_name: str | None = None


class Project(BaseModel):
    id: int
    project_code: str
    short_name: str | None = None
    principal_investigator: str | None = None


class Pagination(BaseModel):
    page: int
    page_size: int
    returned: int
    has_more: bool
    max_results: int
    next_cursor: str | None = None
    offset: int | None = None


StagingState = Literal[
    "PENDING", "QUEUED", "EXECUTING", "SUSPENDED", "COMPLETED", "ERROR", "ABORTED", "UNKNOWN"
]


class StagingItem(BaseModel):
    product_id: str
    status: StagingState
    ready_for_download: bool = False
    failure_reason: str | None = None
    status_source: Literal["archive_product", "archive_request", "local"] = "local"


class UwsResult(BaseModel):
    """One identified UWS result reference returned by CASDA."""

    result_id: str
    href: str = Field(repr=False)
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)


class StagingRequest(BaseModel):
    request_id: str
    idempotency_key: str
    job_url: str = Field(repr=False)
    submitted_at: datetime
    status: StagingState
    product_ids: list[str]
    filenames: dict[str, str | None] = Field(default_factory=dict, repr=False)
    products: list[StagingItem]
    expiry_time: datetime | None = None
    failure_reason: str | None = None
    results: list[UwsResult] = Field(default_factory=list, repr=False)
    # Retained only so state written by releases before structured UWS results can
    # still be reconciled through the conservative filename fallback.
    result_urls: list[str] = Field(default_factory=list, repr=False)
    reused: bool = False


class ReadyArtifact(BaseModel):
    product_id: str
    request_id: str
    download_url: str = Field(repr=False)
    checksum_url: str | None = Field(default=None, repr=False)
    confirmed_at: datetime
    expires_at: datetime | None = None


class ChecksumResult(BaseModel):
    algorithm: str | None = None
    expected: str | None = None
    actual: str | None = None
    verified: bool = False


class DownloadResult(BaseModel):
    product_id: str
    local_path: str
    bytes_downloaded: int
    content_length_verified: bool
    checksum: ChecksumResult
    resumed: bool = False
    staging_request_id: str | None = None


class ManifestProduct(BaseModel):
    product: Product
    checksum: str | None = None
    checksum_algorithm: str | None = None
    staging_request_id: str | None = None
    download_url: str | None = None


class Manifest(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_id: str
    created_at: datetime
    source_name: str | None = None
    workflow_name: str | None = None
    products: list[ManifestProduct]
    original_search_criteria: list[dict[str, Any]] = Field(default_factory=list)
    provenance: Provenance
    server_version: str
    warnings: list[str] = Field(default_factory=list)


class SearchProductsResponse(BaseModel):
    products: list[Product] = Field(default_factory=list)
    pagination: Pagination | None = None
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class GetProductResponse(BaseModel):
    product: Product | None = None
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class GetObservationResponse(BaseModel):
    observation: Observation | None = None
    projects: list[Project] = Field(default_factory=list)
    products: list[Product] = Field(default_factory=list)
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class StageProductsResponse(BaseModel):
    request_id: str | None = None
    idempotency_key: str | None = None
    submitted_at: datetime | None = None
    status: StagingState | None = None
    products: list[StagingItem] = Field(default_factory=list)
    reused: bool = False
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class StagingStatusResponse(BaseModel):
    request_id: str | None = None
    status: StagingState | None = None
    products: list[StagingItem] = Field(default_factory=list)
    failure_reason: str | None = None
    expiry_time: datetime | None = None
    download_ready: bool = False
    retry_guidance: str | None = None
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class DownloadProductResponse(BaseModel):
    result: DownloadResult | None = None
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class CreateManifestResponse(BaseModel):
    manifest: Manifest | None = None
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class ArchiveAvailability(BaseModel):
    available: bool
    notes: list[str] = Field(default_factory=list)
    up_since: str | None = None


class Capability(BaseModel):
    standard_id: str
    interface_url: str | None = None
    interface_type: str | None = None
    interface_version: str | None = None


class SchemaInfo(BaseModel):
    schema_name: str
    description: str | None = None


class TableInfo(BaseModel):
    schema_name: str
    table_name: str
    table_type: str | None = None
    description: str | None = None


class ColumnInfo(BaseModel):
    column_name: str
    datatype: str | None = None
    ucd: str | None = None
    unit: str | None = None
    utype: str | None = None
    description: str | None = None
    size: int | None = None
    principal: bool | None = None
    indexed: bool | None = None
    std: bool | None = None


class ForeignKeyInfo(BaseModel):
    key_id: str
    from_schema: str
    from_table: str
    target_schema: str
    target_table: str
    from_column: str
    target_column: str
    description: str | None = None


class GetArchiveStatusResponse(BaseModel):
    availability: ArchiveAvailability | None = None
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class ListCapabilitiesResponse(BaseModel):
    capabilities: list[Capability] = Field(default_factory=list)
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class ListSchemasResponse(BaseModel):
    schemas: list[SchemaInfo] = Field(default_factory=list)
    pagination: Pagination | None = None
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class ListTablesResponse(BaseModel):
    tables: list[TableInfo] = Field(default_factory=list)
    pagination: Pagination | None = None
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class DescribeTableResponse(BaseModel):
    schema_name: str | None = None
    table_name: str | None = None
    columns: list[ColumnInfo] = Field(default_factory=list)
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class ListForeignKeysResponse(BaseModel):
    schema_name: str | None = None
    table_name: str | None = None
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class BuildAdqlResponse(BaseModel):
    query: str
    max_records: int
    parameters: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class ValidateAdqlResponse(BaseModel):
    valid: bool = True
    query: str
    max_rows: int
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class TapQueryResponse(BaseModel):
    rows: list[dict[str, str | None]] = Field(default_factory=list)
    max_rows: int
    returned: int
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class TapJobRecord(BaseModel):
    request_id: str
    job_url: str = Field(repr=False)
    query_hash: str
    created_at: datetime
    phase: StagingState


class SubmitTapQueryResponse(BaseModel):
    request_id: str
    status: StagingState
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class TapJobStatusResponse(BaseModel):
    request_id: str
    status: StagingState
    failure_reason: str | None = None
    expiry_time: datetime | None = None
    results: list[UwsResult] = Field(default_factory=list)
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class TapJobResultsResponse(BaseModel):
    request_id: str
    rows: list[dict[str, str | None]] = Field(default_factory=list)
    max_rows: int
    returned: int
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class AbortTapJobResponse(BaseModel):
    request_id: str
    status: StagingState
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class DeleteTapJobResponse(BaseModel):
    request_id: str
    deleted: bool = True
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class SurveyInfo(BaseModel):
    """One CASDA SIA1 survey inventory entry."""

    code: str
    name: str | None = None
    description: str | None = None
    group: str | None = None
    endpoint: str | None = None


class ImageRow(BaseModel):
    """Protocol-specific image/cube discovery row from SIA 1 or SIA 2."""

    model_config = ConfigDict(extra="allow")

    obs_publisher_did: str | None = None
    access_url: str | None = None
    access_format: str | None = None
    dataproduct_type: str | None = None
    obs_collection: str | None = None
    obs_id: str | None = None
    target_name: str | None = None
    survey: str | None = None
    image_title: str | None = None
    s_ra: float | None = None
    s_dec: float | None = None
    s_fov: float | None = None
    distance: float | None = None


class CatalogueRow(BaseModel):
    """Catalogue inventory row (casda.catalogue) or SCS science row."""

    model_config = ConfigDict(extra="allow")

    id: int | None = None
    filename: str | None = None
    format: str | None = None
    observation_id: int | None = None
    project_id: int | None = None
    image_id: int | None = None
    freq_ref: float | None = None
    time_obs: str | None = None
    time_obs_mjd: float | None = None
    quality_level: str | None = None
    released_date: str | None = None
    name: str | None = None
    ra: float | None = None
    dec: float | None = None


class SpectrumRow(BaseModel):
    """Protocol-specific spectrum discovery row from SSA."""

    model_config = ConfigDict(extra="allow")

    obs_publisher_id: str | None = None
    title: str | None = None
    access_url: str | None = None
    access_format: str | None = None
    access_estsize: int | None = None
    spectrum_type: str | None = None
    obs_collection: str | None = None
    s_ra: float | None = None
    s_dec: float | None = None
    num_chan: int | None = None


class SearchImagesResponse(BaseModel):
    images: list[ImageRow] = Field(default_factory=list)
    max_records: int
    returned: int
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class ListImageSurveysResponse(BaseModel):
    surveys: list[SurveyInfo] = Field(default_factory=list)
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class ListCataloguesResponse(BaseModel):
    catalogues: list[CatalogueRow] = Field(default_factory=list)
    pagination: Pagination | None = None
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class SearchCatalogueResponse(BaseModel):
    catalogue: str
    rows: list[CatalogueRow] = Field(default_factory=list)
    max_records: int
    returned: int
    provenance: Provenance | None = None
    error: ErrorInfo | None = None


class SearchSpectraResponse(BaseModel):
    spectra: list[SpectrumRow] = Field(default_factory=list)
    max_records: int
    returned: int
    provenance: Provenance | None = None
    error: ErrorInfo | None = None
