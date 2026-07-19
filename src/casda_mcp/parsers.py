"""Deterministic parsers for CASDA CSV, VOTable, and UWS responses."""

from __future__ import annotations

import csv
import io
import json
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
from casda_mcp.models import (
    Observation,
    ObservationEvent,
    Product,
    Project,
    StagingState,
    UwsResult,
)

UWS_NS = "{http://www.ivoa.net/xml/UWS/v1.0}"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
SBID_RE = re.compile(r"^ASKAP-(\d+)$")
EVENT_IVORN_RE = re.compile(r"#([^#]+)$")


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
    obs_id = row.get("obs_id")
    if observation_id is None and sbid is None and not obs_id:
        raise CasdaError("MALFORMED_ARCHIVE_RESPONSE", "CASDA observation metadata is incomplete.")
    return Observation(
        id=observation_id,
        sbid=sbid,
        obs_id=obs_id or (f"ASKAP-{sbid}" if sbid is not None else None),
        observation_start=_datetime(row.get("obs_start") or row.get("t_min")),
        observation_end=_datetime(row.get("obs_end") or row.get("t_max")),
        observation_start_mjd=_float(row, "obs_start_mjd") or _float(row, "t_min"),
        observation_end_mjd=_float(row, "obs_end_mjd") or _float(row, "t_max"),
        telescope=row.get("telescope") or row.get("facility_name"),
        observation_program=row.get("obs_program"),
        deposit_state=row.get("deposit_state"),
        facility_name=row.get("facility_name"),
        instrument_name=row.get("instrument_name"),
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


def _event_id_from_ivorn(ivorn: str) -> str:
    match = EVENT_IVORN_RE.search(ivorn.strip())
    if match:
        return match.group(1)
    return ivorn.strip()


def _observation_event_from_mapping(raw: dict[str, Any]) -> ObservationEvent:
    parameters: dict[str, str] = {}
    for key, value in raw.items():
        if key in {
            "event_id",
            "id",
            "ivorn",
            "timestamp",
            "date",
            "description",
            "event_type",
            "event",
            "telescope",
            "scheduling_block_id",
            "project_code",
            "project_name",
            "parameters",
        }:
            continue
        if value is None:
            continue
        parameters[str(key)] = str(value)
    nested = raw.get("parameters")
    if isinstance(nested, dict):
        for key, value in nested.items():
            if value is not None:
                parameters[str(key)] = str(value)
    ivorn = raw.get("ivorn")
    event_id = raw.get("event_id") or raw.get("id")
    if event_id is None and isinstance(ivorn, str) and ivorn:
        event_id = _event_id_from_ivorn(ivorn)
    if event_id is None:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned an observation event without an identifier.",
        )
    timestamp = _datetime(
        raw.get("timestamp") if isinstance(raw.get("timestamp"), str) else None
    ) or _datetime(raw.get("date") if isinstance(raw.get("date"), str) else None)
    sbid_raw = raw.get("scheduling_block_id")
    try:
        scheduling_block_id = int(sbid_raw) if sbid_raw is not None and sbid_raw != "" else None
    except (TypeError, ValueError) as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned an invalid scheduling_block_id in an observation event.",
        ) from exc
    event_type = raw.get("event_type") or raw.get("event")
    return ObservationEvent(
        event_id=str(event_id),
        ivorn=str(ivorn) if ivorn else None,
        timestamp=timestamp,
        description=str(raw["description"]) if raw.get("description") is not None else None,
        event_type=str(event_type) if event_type is not None else None,
        telescope=str(raw["telescope"]) if raw.get("telescope") is not None else None,
        scheduling_block_id=scheduling_block_id,
        project_code=str(raw["project_code"]) if raw.get("project_code") is not None else None,
        project_name=str(raw["project_name"]) if raw.get("project_name") is not None else None,
        parameters=parameters,
    )


def parse_observation_events(content: bytes) -> list[ObservationEvent]:
    """Parse the public CASDA observation-events feed (VOEvent XML list or JSON array)."""

    stripped = content.lstrip()
    if not stripped:
        raise CasdaError("MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an empty events feed.")
    if stripped[:1] in {b"[", b"{"}:
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned invalid events JSON."
            ) from exc
        if isinstance(payload, dict):
            for key in ("events", "items", "results"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
            else:
                payload = [payload]
        if not isinstance(payload, list):
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned an unexpected events JSON document.",
            )
        events: list[ObservationEvent] = []
        for item in payload:
            if not isinstance(item, dict):
                raise CasdaError(
                    "MALFORMED_ARCHIVE_RESPONSE",
                    "CASDA returned a non-object event entry.",
                )
            events.append(_observation_event_from_mapping(item))
        return events
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned invalid events XML."
        ) from exc
    events = []
    nodes = [root] if _local_name(root.tag) == "VOEvent" else list(root)
    for node in nodes:
        if _local_name(node.tag) != "VOEvent":
            continue
        ivorn = (node.attrib.get("ivorn") or "").strip() or None
        timestamp = None
        description = None
        parameters: dict[str, str] = {}
        for child in node.iter():
            local = _local_name(child.tag)
            if local == "Date" and timestamp is None and child.text:
                timestamp = _datetime(child.text.strip())
            elif local == "Description" and description is None and child.text:
                description = child.text.strip() or None
            elif local == "Param" or local == "param":
                name = (child.attrib.get("name") or "").strip()
                value = (child.attrib.get("value") or "").strip()
                if name and value:
                    parameters[name] = value
        mapping: dict[str, Any] = {
            "ivorn": ivorn,
            "timestamp": timestamp.isoformat() if timestamp else None,
            "description": description,
            **parameters,
        }
        events.append(_observation_event_from_mapping(mapping))
    if not events and _local_name(root.tag) not in {"list", "events", "VOEvent"}:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned an unexpected events document.",
        )
    return events


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


def parse_votable_rows(content: bytes) -> list[dict[str, str | None]]:
    """Parse a VOTable into stringified row dictionaries.

    Protocol-specific typed models should be built from these rows so SIA/SCS/SSA
    fields are preserved rather than coerced into ObsCore Product shapes.
    """

    stripped = content.lstrip()
    if not stripped:
        raise CasdaError("MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an empty VOTable.")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned malformed VOTable XML."
        ) from exc
    for info in root.iter():
        if _local_name(info.tag) != "INFO":
            continue
        name = (info.attrib.get("name") or "").upper()
        value = (info.attrib.get("value") or "").upper()
        if name in {"QUERY_STATUS", "ERROR"} and value == "ERROR":
            message = (info.text or info.attrib.get("value") or "").strip()
            raise CasdaError(
                "ARCHIVE_QUERY_ERROR",
                "CASDA rejected the discovery query.",
                details={"archive_message": message[:500]},
            )
        if name == "ERROR" and value and value != "OK":
            raise CasdaError(
                "ARCHIVE_QUERY_ERROR",
                "CASDA rejected the discovery query.",
                details={"archive_message": value[:500]},
            )
    try:
        votable = parse_votable(io.BytesIO(content), verify="warn")
    except Exception as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned an invalid VOTable."
        ) from exc
    rows: list[dict[str, str | None]] = []
    for resource in votable.resources:
        if not resource.tables:
            continue
        table = resource.tables[0]
        names = [field.name for field in table.fields]
        if not names:
            continue
        array = table.array
        if array is None:
            continue
        for result in array:
            row: dict[str, str | None] = {}
            for name in names:
                row[name] = _plain(result[name])
            rows.append(row)
    return rows


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sia1_surveys(content: bytes) -> list[dict[str, str | None]]:
    """Parse the CASDA SIA1 surveys inventory extension document."""

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned invalid SIA surveys XML."
        ) from exc
    if _local_name(root.tag) != "Surveys":
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned an unexpected SIA surveys document.",
        )
    surveys: list[dict[str, str | None]] = []
    for survey in root:
        if _local_name(survey.tag) != "Survey":
            continue
        fields = {_local_name(child.tag): ((child.text or "").strip() or None) for child in survey}
        code = fields.get("Code")
        if not code:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned a survey entry without a Code.",
            )
        surveys.append(
            {
                "code": code,
                "name": fields.get("Name"),
                "description": fields.get("Description"),
                "group": fields.get("Group"),
                "endpoint": fields.get("Endpoint"),
            }
        )
    return surveys


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
