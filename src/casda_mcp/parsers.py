"""Deterministic parsers for CASDA CSV, VOTable, and UWS responses."""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote, urlparse

import numpy as np
from astropy.io.votable import parse as parse_votable
from defusedxml import ElementTree

from casda_mcp.errors import CasdaError
from casda_mcp.models import Observation, Product, Project, StagingState, UwsResult

UWS_NS = "{http://www.ivoa.net/xml/UWS/v1.0}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
SBID_RE = re.compile(r"^ASKAP-(\d+)$")


def parse_tap_csv(content: bytes) -> list[dict[str, str | None]]:
    """Parse a TAP CSV response, detecting XML error documents explicitly."""

    stripped = content.lstrip()
    if stripped.startswith(b"<") or stripped.startswith(b"<?xml"):
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise CasdaError("MALFORMED_ARCHIVE_RESPONSE", "CASDA returned malformed XML.") from exc
        for info in root.iter():
            if (
                info.tag.rsplit("}", 1)[-1] == "INFO"
                and info.attrib.get("name") == "QUERY_STATUS"
                and info.attrib.get("value") == "ERROR"
            ):
                raise CasdaError(
                    "ARCHIVE_QUERY_ERROR",
                    "CASDA rejected the generated metadata query.",
                    details={"archive_message": (info.text or "").strip()[:500]},
                )
        raise CasdaError("MALFORMED_ARCHIVE_RESPONSE", "CASDA returned XML when CSV was requested.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned non-UTF-8 table data."
        ) from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise CasdaError("MALFORMED_ARCHIVE_RESPONSE", "CASDA returned a table without columns.")
    return [{key: value if value != "" else None for key, value in row.items()} for row in reader]


def _float(row: dict[str, str | None], key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned an invalid numeric metadata value.",
            details={"field": key},
        ) from exc
    if not math.isfinite(parsed):
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned a non-finite numeric metadata value.",
            details={"field": key},
        )
    return parsed


def _int(row: dict[str, str | None], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned an invalid integer metadata value.",
            details={"field": key},
        ) from exc
    if not -(2**63) <= parsed < 2**63:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned an out-of-range integer metadata value.",
            details={"field": key},
        )
    return parsed


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, TypeError, ValueError) as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an invalid datetime value."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mjd_datetime(value: float | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime(1858, 11, 17, tzinfo=timezone.utc) + timedelta(days=value)
    except OverflowError as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an out-of-range MJD value."
        ) from exc


def product_from_row(row: dict[str, str | None], *, ready: bool = False) -> Product:
    product_id = row.get("obs_publisher_did")
    if not product_id:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA product metadata omitted obs_publisher_did.",
        )
    em_min = _float(row, "em_min")
    em_max = _float(row, "em_max")
    speed_of_light = 299_792_458.0
    release_date = _datetime(row.get("obs_release_date"))
    if release_date is not None and release_date > datetime.now(timezone.utc):
        access_state = "RESTRICTED"
    elif ready:
        access_state = "READY"
    elif row.get("access_url"):
        access_state = "STAGING_REQUIRED"
    else:
        access_state = "UNKNOWN"
    observation_id = row.get("obs_id")
    sbid_match = SBID_RE.fullmatch(observation_id or "")
    known = {
        "obs_publisher_did",
        "filename",
        "dataproduct_type",
        "dataproduct_subtype",
        "obs_collection",
        "project_code",
        "obs_id",
        "target_name",
        "access_format",
        "access_url",
        "access_estsize",
        "s_ra",
        "s_dec",
        "s_fov",
        "s_region",
        "s_resolution",
        "s_resolution_min",
        "s_resolution_max",
        "s_xel1",
        "s_xel2",
        "t_min",
        "t_max",
        "t_exptime",
        "em_min",
        "em_max",
        "em_xel",
        "pol_states",
        "pol_xel",
        "facility_name",
        "instrument_name",
        "obs_release_date",
        "quality_level",
        "calib_level",
    }
    extras = {key: value for key, value in row.items() if key not in known}
    start_mjd = _float(row, "t_min")
    end_mjd = _float(row, "t_max")
    size_kib = _int(row, "access_estsize")
    return Product(
        product_id=product_id,
        filename=row.get("filename"),
        product_type=row.get("dataproduct_type")
        or (
            "catalogue" if (row.get("dataproduct_subtype") or "").startswith("catalogue.") else None
        ),
        product_subtype=row.get("dataproduct_subtype"),
        collection=row.get("obs_collection"),
        project_code=row.get("project_code"),
        observation_id=observation_id,
        sbid=int(sbid_match.group(1)) if sbid_match else None,
        target_name=row.get("target_name"),
        access_format=row.get("access_format"),
        access_url=row.get("access_url"),
        file_size_bytes=size_kib * 1024 if size_kib is not None else None,
        ra_deg=_float(row, "s_ra"),
        dec_deg=_float(row, "s_dec"),
        field_of_view_deg=_float(row, "s_fov"),
        spatial_region=row.get("s_region"),
        spatial_resolution_arcsec=_float(row, "s_resolution"),
        spatial_resolution_min_arcsec=_float(row, "s_resolution_min"),
        spatial_resolution_max_arcsec=_float(row, "s_resolution_max"),
        spatial_pixels_x=_int(row, "s_xel1"),
        spatial_pixels_y=_int(row, "s_xel2"),
        observation_start_mjd=start_mjd,
        observation_end_mjd=end_mjd,
        observation_start=_mjd_datetime(start_mjd),
        observation_end=_mjd_datetime(end_mjd),
        exposure_seconds=_float(row, "t_exptime"),
        frequency_min_hz=speed_of_light / em_max if em_max and em_max > 0 else None,
        frequency_max_hz=speed_of_light / em_min if em_min and em_min > 0 else None,
        spectral_channels=_int(row, "em_xel"),
        polarisation_states=row.get("pol_states"),
        polarisation_samples=_int(row, "pol_xel"),
        facility_name=row.get("facility_name"),
        instrument_name=row.get("instrument_name"),
        release_date=release_date,
        quality_level=row.get("quality_level"),
        calibration_level=_int(row, "calib_level"),
        access_state=access_state,
        **extras,
    )


def observation_from_row(row: dict[str, str | None]) -> Observation:
    observation_id = _int(row, "id")
    sbid = _int(row, "sbid")
    if observation_id is None or sbid is None:
        raise CasdaError("MALFORMED_ARCHIVE_RESPONSE", "CASDA observation metadata is incomplete.")
    return Observation(
        id=observation_id,
        sbid=sbid,
        observation_start=_datetime(row.get("obs_start")),
        observation_end=_datetime(row.get("obs_end")),
        observation_start_mjd=_float(row, "obs_start_mjd"),
        observation_end_mjd=_float(row, "obs_end_mjd"),
        telescope=row.get("telescope"),
        observation_program=row.get("obs_program"),
        deposit_state=row.get("deposit_state"),
    )


def project_from_row(row: dict[str, str | None]) -> Project:
    project_id = _int(row, "id")
    project_code = row.get("opal_code")
    if project_id is None or project_code is None:
        raise CasdaError("MALFORMED_ARCHIVE_RESPONSE", "CASDA project metadata is incomplete.")
    names = [row.get("principal_first_name"), row.get("principal_last_name")]
    principal = " ".join(name for name in names if name) or None
    return Project(
        id=project_id,
        project_code=project_code,
        short_name=row.get("short_name"),
        principal_investigator=principal,
    )


@dataclass(slots=True)
class DatalinkAccess:
    service_url: str
    authenticated_id_token: str


def _plain(value: Any) -> str | None:
    if value is None or np.ma.is_masked(value):
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def parse_datalink_access(content: bytes, service_name: str = "async_service") -> DatalinkAccess:
    try:
        votable = parse_votable(io.BytesIO(content), verify="warn")
    except Exception as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an invalid Datalink VOTable."
        ) from exc
    service_url: str | None = None
    token: str | None = None
    for resource in votable.resources:
        if resource.type == "meta" and service_name == resource.ID:
            for param in resource.params:
                if param.name == "accessURL":
                    service_url = _plain(param.value)
        if resource.type == "results" and resource.tables:
            names = {field.name for field in resource.tables[0].fields}
            if {"service_def", "authenticated_id_token"}.issubset(names):
                for result in resource.tables[0].array:
                    if _plain(result["service_def"]) == service_name:
                        token = _plain(result["authenticated_id_token"])
                        break
    if not service_url or not token:
        raise CasdaError(
            "AUTHORISATION_FAILED",
            "CASDA did not provide an authorised asynchronous staging service for this product.",
        )
    return DatalinkAccess(service_url=service_url, authenticated_id_token=token)


@dataclass(slots=True)
class UwsStatus:
    phase: StagingState
    destruction: datetime | None = None
    failure_reason: str | None = None
    result_urls: list[str] = field(default_factory=list)
    results: list[UwsResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.results and self.result_urls:
            if self.result_urls != [result.href for result in self.results]:
                raise ValueError("UWS result URLs conflict with structured results")
        elif self.results:
            self.result_urls = [result.href for result in self.results]
        elif self.result_urls:
            self.results = [
                UwsResult(
                    result_id=f"legacy-{index}",
                    href=_decode_uws_href(href),
                )
                for index, href in enumerate(self.result_urls)
            ]
            self.result_urls = [result.href for result in self.results]


def _decode_uws_href(raw_href: str) -> str:
    if urlparse(raw_href).scheme.lower() in {"http", "https"}:
        return raw_href
    return unquote(raw_href)


def parse_uws_status(content: bytes) -> UwsStatus:
    try:
        root = ElementTree.fromstring(content)
    except Exception as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned invalid UWS status XML."
        ) from exc
    if root.tag != f"{UWS_NS}job":
        raise CasdaError("MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an unexpected UWS document.")
    phase_node = root.find(f"{UWS_NS}phase")
    phase_text = (phase_node.text or "").upper() if phase_node is not None else ""
    valid = {"PENDING", "QUEUED", "EXECUTING", "SUSPENDED", "COMPLETED", "ERROR", "ABORTED"}
    if phase_text not in valid:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an invalid or missing UWS phase."
        )
    phase: StagingState = phase_text  # type: ignore[assignment]
    destruction_node = root.find(f"{UWS_NS}destruction")
    error_node = root.find(f"{UWS_NS}errorSummary/{UWS_NS}message")
    results: list[UwsResult] = []
    result_ids: set[str] = set()
    results_node = root.find(f"{UWS_NS}results")
    if results_node is not None:
        for result in results_node.findall(f"{UWS_NS}result"):
            result_id = (result.attrib.get("id") or "").strip()
            raw_href = (result.attrib.get(XLINK_HREF) or "").strip()
            if not result_id or not raw_href:
                raise CasdaError(
                    "MALFORMED_ARCHIVE_RESPONSE",
                    "CASDA returned a UWS result without its required id or href.",
                )
            if result_id in result_ids:
                raise CasdaError(
                    "MALFORMED_ARCHIVE_RESPONSE",
                    "CASDA returned duplicate UWS result identifiers.",
                )
            result_ids.add(result_id)
            size_text = result.attrib.get("size")
            try:
                size_bytes = int(size_text) if size_text is not None else None
            except ValueError as exc:
                raise CasdaError(
                    "MALFORMED_ARCHIVE_RESPONSE",
                    "CASDA returned an invalid UWS result size.",
                ) from exc
            if size_bytes is not None and size_bytes < 0:
                raise CasdaError(
                    "MALFORMED_ARCHIVE_RESPONSE",
                    "CASDA returned an invalid UWS result size.",
                )
            # CASDA commonly percent-encodes the entire absolute result URL. Decode
            # that representation once, but preserve escaping inside an URL that is
            # already absolute so signed path/query semantics are not changed.
            href = _decode_uws_href(raw_href)
            results.append(
                UwsResult(
                    result_id=result_id,
                    href=href,
                    mime_type=result.attrib.get("mime-type"),
                    size_bytes=size_bytes,
                )
            )
    return UwsStatus(
        phase=phase,
        destruction=_datetime(destruction_node.text) if destruction_node is not None else None,
        failure_reason=(error_node.text or "").strip()[:500] if error_node is not None else None,
        results=results,
    )
