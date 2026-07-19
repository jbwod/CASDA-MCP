"""Public DOI normalisation and DataCite/CSL parsing helpers (read-only)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

from casda_mcp.errors import ValidationError
from casda_mcp.models import DoiRecord

CSIRO_DAP_DOI_PREFIX = "10.25919"
_DOI_RE = re.compile(
    r"^10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+$",
    re.ASCII,
)


def normalize_doi(value: str) -> str:
    """Normalise a DOI or doi.org URL to the canonical ``prefix/suffix`` form."""

    text = value.strip()
    if not text:
        raise ValidationError("doi must not be empty.")
    lowered = text.lower()
    if lowered.startswith("https://doi.org/") or lowered.startswith("http://doi.org/"):
        path = unquote(urlparse(text).path).lstrip("/")
        text = path
    elif lowered.startswith("doi:"):
        text = text[4:].strip()
    text = text.strip().strip("/")
    if not _DOI_RE.fullmatch(text):
        raise ValidationError(
            "doi must be a well-formed DOI (for example 10.25919/example-id).",
            details={"doi": value},
        )
    return text


def doi_record_from_datacite(payload: dict[str, Any], *, doi: str) -> DoiRecord:
    """Build a DoiRecord from a DataCite REST API JSON document."""

    raw_data = payload.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else payload
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        raise ValidationError("DataCite response did not include DOI attributes.")
    resolved_doi = str(attributes.get("doi") or data.get("id") or doi)
    titles = attributes.get("titles") or []
    title = None
    if isinstance(titles, list) and titles:
        first = titles[0]
        if isinstance(first, dict):
            title = first.get("title")
        elif isinstance(first, str):
            title = first
    creators_raw = attributes.get("creators") or []
    creators: list[str] = []
    if isinstance(creators_raw, list):
        for item in creators_raw:
            if isinstance(item, dict):
                name = item.get("name") or " ".join(
                    part
                    for part in (item.get("givenName"), item.get("familyName"))
                    if isinstance(part, str) and part
                )
                if name:
                    creators.append(str(name))
            elif isinstance(item, str) and item:
                creators.append(item)
    publisher = attributes.get("publisher")
    if isinstance(publisher, dict):
        publisher = publisher.get("name")
    year = attributes.get("publicationYear")
    publication_year = int(year) if isinstance(year, int) else None
    url = attributes.get("url")
    related: list[str] = []
    for item in attributes.get("relatedIdentifiers") or []:
        if isinstance(item, dict) and item.get("relatedIdentifier"):
            related.append(str(item["relatedIdentifier"]))
    prefix = resolved_doi.split("/", 1)[0]
    return DoiRecord(
        doi=resolved_doi,
        prefix=prefix,
        is_csiro_dap=prefix == CSIRO_DAP_DOI_PREFIX,
        title=str(title) if title else None,
        creators=creators,
        publisher=str(publisher) if publisher else None,
        publication_year=publication_year,
        url=str(url) if url else None,
        related_identifiers=related,
        source="datacite",
    )


def doi_record_from_csl(payload: dict[str, Any], *, doi: str) -> DoiRecord:
    """Build a DoiRecord from CSL-JSON (doi.org Accept header fallback)."""

    resolved_doi = str(payload.get("DOI") or payload.get("doi") or doi)
    authors = payload.get("author") or []
    creators: list[str] = []
    if isinstance(authors, list):
        for item in authors:
            if isinstance(item, dict):
                name = item.get("literal") or " ".join(
                    part
                    for part in (item.get("given"), item.get("family"))
                    if isinstance(part, str) and part
                )
                if name:
                    creators.append(str(name))
            elif isinstance(item, str) and item:
                creators.append(item)
    year = None
    issued = payload.get("issued")
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            try:
                year = int(parts[0][0])
            except (TypeError, ValueError):
                year = None
    prefix = resolved_doi.split("/", 1)[0]
    title = payload.get("title")
    publisher = payload.get("publisher")
    url = payload.get("URL") or payload.get("url")
    return DoiRecord(
        doi=resolved_doi,
        prefix=prefix,
        is_csiro_dap=prefix == CSIRO_DAP_DOI_PREFIX,
        title=str(title) if title else None,
        creators=creators,
        publisher=str(publisher) if publisher else None,
        publication_year=year,
        url=str(url) if url else None,
        related_identifiers=[],
        source="doi_org_csl",
    )
