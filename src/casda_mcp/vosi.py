"""Parsers for VOSI availability and capabilities documents."""

from __future__ import annotations

from typing import Any

from defusedxml import ElementTree

from casda_mcp.errors import CasdaError
from casda_mcp.models import ArchiveAvailability, Capability, TapExample

XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(parent: Any, name: str) -> str | None:
    for child in parent:
        if _local_name(child.tag) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def _child_texts(parent: Any, name: str) -> list[str]:
    values: list[str] = []
    for child in parent:
        if _local_name(child.tag) == name:
            text = (child.text or "").strip()
            if text:
                values.append(text)
    return values


def parse_vosi_availability(content: bytes) -> ArchiveAvailability:
    """Parse a VOSI availability document into a stable model."""

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned invalid VOSI availability XML."
        ) from exc
    if _local_name(root.tag) != "availability":
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned an unexpected VOSI availability document.",
        )
    available_text = (_child_text(root, "available") or "").lower()
    if available_text not in {"true", "false", "1", "0"}:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned availability XML without a usable available flag.",
        )
    return ArchiveAvailability(
        available=available_text in {"true", "1"},
        notes=_child_texts(root, "note"),
        up_since=_child_text(root, "upSince"),
    )


def parse_vosi_capabilities(content: bytes) -> list[Capability]:
    """Parse a VOSI capabilities document into stable capability rows."""

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned invalid VOSI capabilities XML."
        ) from exc
    if _local_name(root.tag) != "capabilities":
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE",
            "CASDA returned an unexpected VOSI capabilities document.",
        )
    capabilities: list[Capability] = []
    for capability in root:
        if _local_name(capability.tag) != "capability":
            continue
        standard_id = (capability.attrib.get("standardID") or "").strip()
        if not standard_id:
            raise CasdaError(
                "MALFORMED_ARCHIVE_RESPONSE",
                "CASDA returned a capability without a standardID.",
            )
        interface_url: str | None = None
        interface_type: str | None = None
        interface_version: str | None = None
        for interface in capability:
            if _local_name(interface.tag) != "interface":
                continue
            raw_type = interface.attrib.get(XSI_TYPE) or interface.attrib.get("type") or ""
            interface_type = raw_type.strip() or None
            interface_version = (interface.attrib.get("version") or "").strip() or None
            for child in interface:
                if _local_name(child.tag) == "accessURL":
                    interface_url = (child.text or "").strip() or None
                    break
            break
        capabilities.append(
            Capability(
                standard_id=standard_id,
                interface_url=interface_url,
                interface_type=interface_type,
                interface_version=interface_version,
            )
        )
    return capabilities


def parse_tap_examples(content: bytes) -> list[TapExample]:
    """Parse a DALI/TAP examples document into named ADQL examples when possible."""

    stripped = content.lstrip()
    if not stripped.startswith(b"<") and not stripped.startswith(b"<?xml"):
        text = content.decode("utf-8", errors="replace").strip()
        if not text:
            return []
        return [TapExample(name="document", description=None, query=text[:8000])]
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise CasdaError(
            "MALFORMED_ARCHIVE_RESPONSE", "CASDA returned malformed TAP examples XML."
        ) from exc
    examples: list[TapExample] = []
    for example in root.iter():
        if _local_name(example.tag) != "example":
            continue
        name = (
            (example.attrib.get("name") or "").strip()
            or _child_text(example, "name")
            or _child_text(example, "title")
        )
        description = _child_text(example, "description") or _child_text(example, "info")
        query = _child_text(example, "query") or _child_text(example, "adql")
        if query is None:
            for child in example:
                if _local_name(child.tag) in {"query", "adql"}:
                    query = (child.text or "").strip() or None
                    break
        if name or description or query:
            examples.append(TapExample(name=name, description=description, query=query))
    if examples:
        return examples
    # Fall back to a single document capture when the schema is unfamiliar.
    text = ElementTree.tostring(root, encoding="unicode")
    return [TapExample(name="document", description=None, query=text[:8000])]
