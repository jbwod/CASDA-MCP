"""Validation and safe construction of the allowlisted CASDA ADQL queries."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from casda_mcp.errors import ValidationError

PRODUCT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.:-]{0,127}$")
PROJECT_CODE_RE = re.compile(r"^[A-Z]{2,4}[0-9]{2,6}$")
TEXT_RE = re.compile(r"^[\w .+:/()'-]{1,160}$", re.UNICODE)
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

SORT_FIELDS = {
    "product_id": "o.obs_publisher_did",
    "filename": "o.filename",
    "file_size": "o.access_estsize",
    "observation_start": "o.t_min",
    "release_date": "o.obs_release_date",
    "distance": "distance_deg",
}

PRODUCT_TYPE_CLAUSES = {
    "image": "o.dataproduct_type = 'image'",
    "cube": "o.dataproduct_type = 'cube'",
    "visibility": "o.dataproduct_type = 'visibility'",
    "spectrum": "o.dataproduct_type = 'spectrum'",
    "catalogue": "o.dataproduct_subtype LIKE 'catalogue.%'",
    "weight": (
        "(o.dataproduct_subtype LIKE 'cont.weight.%' "
        "OR o.dataproduct_subtype LIKE 'spectral.weight.%')"
    ),
    "moment_map": "o.dataproduct_subtype LIKE 'spectral.restored.mom%'",
}

PRODUCT_COLUMNS = """o.obs_publisher_did, o.filename, o.dataproduct_type,
o.dataproduct_subtype, o.obs_collection, p.opal_code AS project_code, o.obs_id,
o.target_name, o.access_format, o.access_url, o.access_estsize, o.s_ra, o.s_dec,
o.s_fov, o.s_region, o.s_resolution, o.s_resolution_min, o.s_resolution_max,
o.s_xel1, o.s_xel2, o.t_min, o.t_max, o.t_exptime, o.em_min, o.em_max, o.em_xel,
o.pol_states, o.pol_xel, o.facility_name, o.instrument_name, o.obs_release_date,
o.quality_level, o.calib_level""".replace("\n", " ")


def normalize_product_id(value: str) -> str:
    normalized = value.strip()
    if not PRODUCT_ID_RE.fullmatch(normalized):
        raise ValidationError("Invalid CASDA product identifier.", details={"product_id": value})
    return normalized


def normalize_product_ids(values: list[str]) -> list[str]:
    if not values:
        raise ValidationError("At least one product identifier is required.")
    return list(dict.fromkeys(normalize_product_id(value) for value in values))


def validate_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not IDEMPOTENCY_RE.fullmatch(normalized):
        raise ValidationError("Invalid idempotency key.")
    return normalized


def adql_string(value: str, *, field: str) -> str:
    """Quote validated text as an ADQL string literal."""

    if any(ord(character) < 32 for character in value):
        raise ValidationError(f"Invalid {field}; control characters are not allowed.")
    normalized = value.strip()
    if not TEXT_RE.fullmatch(normalized) or "%" in normalized or "_" in normalized:
        raise ValidationError(
            f"Invalid {field}; wildcards and control characters are not allowed.",
            details={"field": field},
        )
    return "'" + normalized.replace("'", "''") + "'"


def parse_datetime(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO 8601 date or timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def datetime_to_mjd(value: datetime) -> float:
    mjd_epoch = datetime(1858, 11, 17, tzinfo=timezone.utc)
    return (value.astimezone(timezone.utc) - mjd_epoch).total_seconds() / 86400


@dataclass(slots=True)
class SearchCriteria:
    source_name: str | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    radius_deg: float | None = None
    project_code: str | None = None
    scheduling_block_id: int | None = None
    observation_start: str | None = None
    observation_end: str | None = None
    frequency_min_hz: float | None = None
    frequency_max_hz: float | None = None
    product_types: list[str] | None = None
    collection: str | None = None
    released_only: bool = True
    sort_by: str = "product_id"
    sort_order: Literal["asc", "desc"] = "asc"
    page: int = 1
    page_size: int = 25

    def as_parameters(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


class QueryBuilder:
    """Build only predefined queries over known CASDA TAP tables and fields."""

    def __init__(self, *, max_results: int, max_cone_radius_deg: float) -> None:
        self.max_results = max_results
        self.max_cone_radius_deg = max_cone_radius_deg

    def build_search(self, criteria: SearchCriteria) -> tuple[str, int]:
        self._validate_search(criteria)
        clauses: list[str] = []

        if criteria.source_name:
            clauses.append(
                f"o.target_name = {adql_string(criteria.source_name, field='source_name')}"
            )
        if criteria.ra_deg is not None:
            clauses.append(
                "1=INTERSECTS(o.s_region, "
                f"CIRCLE('ICRS',{criteria.ra_deg:.12g},{criteria.dec_deg:.12g},{criteria.radius_deg:.12g}))"
            )
        if criteria.project_code:
            clauses.append(f"p.opal_code = '{criteria.project_code}'")
        if criteria.scheduling_block_id is not None:
            clauses.append(f"o.obs_id = 'ASKAP-{criteria.scheduling_block_id}'")
        if criteria.collection:
            clauses.append(
                f"o.obs_collection = {adql_string(criteria.collection, field='collection')}"
            )
        if criteria.observation_start:
            observation_start = parse_datetime(
                criteria.observation_start, field="observation_start"
            )
            clauses.append(f"o.t_max >= {datetime_to_mjd(observation_start):.12g}")
        if criteria.observation_end:
            observation_end = parse_datetime(criteria.observation_end, field="observation_end")
            clauses.append(f"o.t_min <= {datetime_to_mjd(observation_end):.12g}")
        if criteria.frequency_min_hz is not None or criteria.frequency_max_hz is not None:
            self._append_frequency_clauses(clauses, criteria)
        if criteria.product_types:
            clauses.append(
                "("
                + " OR ".join(PRODUCT_TYPE_CLAUSES[item] for item in criteria.product_types)
                + ")"
            )
        if criteria.released_only:
            clauses.append("o.obs_release_date IS NOT NULL")

        # CASDA's current ADQL dialect does not support CURRENT_TIMESTAMP. For public-only
        # results, retrieve the bounded window and remove future releases locally.
        fetch_count = (
            self.max_results + 1
            if criteria.released_only
            else min(criteria.page * criteria.page_size + 1, self.max_results + 1)
        )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order_field = SORT_FIELDS[criteria.sort_by]
        if criteria.sort_by == "distance":
            if criteria.ra_deg is None:
                raise ValidationError("sort_by='distance' requires a position and radius.")
            distance = (
                "DISTANCE(POINT('ICRS', o.s_ra, o.s_dec), "
                f"POINT('ICRS', {criteria.ra_deg:.12g}, {criteria.dec_deg:.12g})) AS distance_deg, "
            )
        else:
            distance = ""
        order = criteria.sort_order.upper()
        query = (
            f"SELECT TOP {fetch_count} {distance}{PRODUCT_COLUMNS} FROM ivoa.obscore AS o "
            "LEFT OUTER JOIN casda.project AS p ON o.obs_collection = p.short_name"
            f"{where} ORDER BY {order_field} {order}, o.obs_publisher_did ASC"
        )
        return query, fetch_count

    def build_product(self, product_id: str) -> str:
        product_id = normalize_product_id(product_id)
        return (
            f"SELECT TOP 2 {PRODUCT_COLUMNS} FROM ivoa.obscore AS o "
            "LEFT OUTER JOIN casda.project AS p ON o.obs_collection = p.short_name "
            f"WHERE o.obs_publisher_did = '{product_id}'"
        )

    def build_products(self, product_ids: list[str]) -> str:
        normalized = normalize_product_ids(product_ids)
        values = ",".join(f"'{value}'" for value in normalized)
        return (
            f"SELECT TOP {len(normalized) + 1} {PRODUCT_COLUMNS} FROM ivoa.obscore AS o "
            "LEFT OUTER JOIN casda.project AS p ON o.obs_collection = p.short_name "
            f"WHERE o.obs_publisher_did IN ({values}) ORDER BY o.obs_publisher_did ASC"
        )

    @staticmethod
    def build_observation(sbid: int) -> str:
        if sbid <= 0:
            raise ValidationError("scheduling_block_id must be a positive integer.")
        return f"SELECT TOP 2 * FROM casda.observation WHERE sbid = {sbid}"

    @staticmethod
    def build_observation_projects(sbid: int) -> str:
        if sbid <= 0:
            raise ValidationError("scheduling_block_id must be a positive integer.")
        return (
            "SELECT DISTINCT p.id, p.opal_code, p.short_name, p.principal_first_name, "
            "p.principal_last_name FROM casda.project AS p JOIN ivoa.obscore AS o "
            f"ON p.short_name = o.obs_collection WHERE o.obs_id = 'ASKAP-{sbid}'"
        )

    def _validate_search(self, criteria: SearchCriteria) -> None:
        spatial = (criteria.ra_deg, criteria.dec_deg, criteria.radius_deg)
        if any(value is not None for value in spatial) and not all(
            value is not None for value in spatial
        ):
            raise ValidationError("ra_deg, dec_deg, and radius_deg must be supplied together.")
        if criteria.ra_deg is not None and not math.isfinite(criteria.ra_deg):
            raise ValidationError("ra_deg must be finite.")
        if criteria.ra_deg is not None and not 0 <= criteria.ra_deg < 360:
            raise ValidationError("ra_deg must be in the range [0, 360).")
        if criteria.dec_deg is not None and not math.isfinite(criteria.dec_deg):
            raise ValidationError("dec_deg must be finite.")
        if criteria.dec_deg is not None and not -90 <= criteria.dec_deg <= 90:
            raise ValidationError("dec_deg must be in the range [-90, 90].")
        if criteria.radius_deg is not None and (
            not math.isfinite(criteria.radius_deg)
            or not 0 < criteria.radius_deg <= self.max_cone_radius_deg
        ):
            raise ValidationError(
                f"radius_deg must be greater than zero and no more than {self.max_cone_radius_deg}."
            )
        if criteria.project_code:
            criteria.project_code = criteria.project_code.strip().upper()
            if not PROJECT_CODE_RE.fullmatch(criteria.project_code):
                raise ValidationError("project_code is not a valid CASDA/OPAL project code.")
        if criteria.scheduling_block_id is not None and criteria.scheduling_block_id <= 0:
            raise ValidationError("scheduling_block_id must be a positive integer.")
        if criteria.product_types:
            if len(criteria.product_types) > len(PRODUCT_TYPE_CLAUSES):
                raise ValidationError("Too many product types were supplied.")
            criteria.product_types = list(dict.fromkeys(criteria.product_types))
            invalid = sorted(set(criteria.product_types) - PRODUCT_TYPE_CLAUSES.keys())
            if invalid:
                raise ValidationError("Unsupported product type.", details={"unsupported": invalid})
        if criteria.sort_by not in SORT_FIELDS:
            raise ValidationError("Unsupported sort field.", details={"sort_by": criteria.sort_by})
        if criteria.sort_order not in {"asc", "desc"}:
            raise ValidationError(
                "Unsupported sort order.", details={"sort_order": criteria.sort_order}
            )
        if criteria.page < 1 or criteria.page_size < 1:
            raise ValidationError("page and page_size must be positive integers.")
        if (
            criteria.page_size > self.max_results
            or (criteria.page - 1) * criteria.page_size >= self.max_results
        ):
            raise ValidationError("Requested page exceeds the configured result window.")
        start = (
            parse_datetime(criteria.observation_start, field="observation_start")
            if criteria.observation_start
            else None
        )
        end = (
            parse_datetime(criteria.observation_end, field="observation_end")
            if criteria.observation_end
            else None
        )
        if start and end and start > end:
            raise ValidationError("observation_start must not be after observation_end.")
        if criteria.frequency_min_hz is not None and (
            not math.isfinite(criteria.frequency_min_hz) or criteria.frequency_min_hz <= 0
        ):
            raise ValidationError("frequency_min_hz must be finite and greater than zero.")
        if criteria.frequency_max_hz is not None and (
            not math.isfinite(criteria.frequency_max_hz) or criteria.frequency_max_hz <= 0
        ):
            raise ValidationError("frequency_max_hz must be finite and greater than zero.")
        if (
            criteria.frequency_min_hz is not None
            and criteria.frequency_max_hz is not None
            and criteria.frequency_min_hz > criteria.frequency_max_hz
        ):
            raise ValidationError("frequency_min_hz must not exceed frequency_max_hz.")

    @staticmethod
    def _append_frequency_clauses(clauses: list[str], criteria: SearchCriteria) -> None:
        speed_of_light = 299_792_458.0
        if criteria.frequency_min_hz is not None:
            # Overlap at the lower frequency bound maps to maximum wavelength c/f_min.
            clauses.append(f"o.em_min <= {speed_of_light / criteria.frequency_min_hz:.16g}")
        if criteria.frequency_max_hz is not None:
            clauses.append(f"o.em_max >= {speed_of_light / criteria.frequency_max_hz:.16g}")
