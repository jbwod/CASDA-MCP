"""SELECT-only ADQL validation for advanced TAP query tools."""

from __future__ import annotations

import re
from dataclasses import dataclass

from casda_mcp.errors import ValidationError
from casda_mcp.query import IDENTIFIER_RE, PROJECT_CODE_RE

MUTATION_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "MERGE",
        "GRANT",
        "REVOKE",
        "INTO",
    }
)
FIXED_SCHEMAS = frozenset({"ivoa", "casda", "tap_schema", "internal"})
TOP_RE = re.compile(r"\bTOP\s+(\d+)\b", re.IGNORECASE)
TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
UNQUALIFIED_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)\b(?!\s*\.)",
    re.IGNORECASE,
)
KEYWORD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
COMMENT_LINE_RE = re.compile(r"--")
COMMENT_BLOCK_RE = re.compile(r"/\*|\*/")


@dataclass(slots=True, frozen=True)
class ValidatedAdql:
    """Normalized ADQL accepted by the SELECT-only policy."""

    query: str
    max_rows: int


def validate_adql(query: str, *, max_length: int, max_rows: int) -> ValidatedAdql:
    """Validate a single SELECT statement against the advanced ADQL policy."""

    if max_length < 1:
        raise ValidationError("max_length must be a positive integer.")
    if max_rows < 1:
        raise ValidationError("max_rows must be a positive integer.")
    if not isinstance(query, str) or not query.strip():
        raise ValidationError("ADQL query must be a non-empty string.")

    normalized = query.strip()
    if len(normalized) > max_length:
        raise ValidationError(
            "ADQL query exceeds the configured maximum length.",
            details={"length": len(normalized), "maximum": max_length},
        )

    masked = _mask_string_literals(normalized)
    if ";" in masked:
        raise ValidationError(
            "ADQL must be a single statement; semicolons are not allowed.",
            details={"code": "ADQL_MULTI_STATEMENT"},
        )
    if COMMENT_LINE_RE.search(masked) or COMMENT_BLOCK_RE.search(masked):
        raise ValidationError(
            "ADQL comments are not allowed.",
            details={"code": "ADQL_COMMENT"},
        )

    if not re.match(r"(?is)^SELECT\b", normalized):
        raise ValidationError(
            "ADQL must start with SELECT.",
            details={"code": "ADQL_NOT_SELECT"},
        )

    for match in KEYWORD_RE.finditer(masked):
        token = match.group(1).upper()
        if token in MUTATION_KEYWORDS:
            raise ValidationError(
                f"ADQL must not contain the keyword {token}.",
                details={"code": "ADQL_MUTATION", "keyword": token},
            )

    for match in TABLE_REF_RE.finditer(masked):
        schema_name, table_name = match.group(1), match.group(2)
        if not _schema_allowed(schema_name):
            raise ValidationError(
                "ADQL table reference uses a schema outside the allowlist.",
                details={
                    "code": "ADQL_TABLE_DENY",
                    "schema_name": schema_name,
                    "table_name": table_name,
                },
            )
        if not IDENTIFIER_RE.fullmatch(table_name):
            raise ValidationError(
                "ADQL table name must be a simple identifier.",
                details={"code": "ADQL_TABLE_DENY", "table_name": table_name},
            )

    for match in UNQUALIFIED_TABLE_RE.finditer(masked):
        # Skip subquery openers: FROM (SELECT ...)
        start = match.end()
        remainder = masked[start:].lstrip()
        if remainder.startswith("("):
            continue
        token = match.group(1)
        if token.upper() in {"SELECT", "LATERAL"}:
            continue
        raise ValidationError(
            "ADQL table references must be schema-qualified allowlisted names.",
            details={"code": "ADQL_TABLE_DENY", "table_name": token},
        )

    top_match = TOP_RE.search(masked)
    if top_match is not None:
        top_rows = int(top_match.group(1))
        if top_rows < 1:
            raise ValidationError(
                "ADQL TOP must be a positive integer.",
                details={"code": "ADQL_TOP_INVALID", "top": top_rows},
            )
        if top_rows > max_rows:
            raise ValidationError(
                "ADQL TOP exceeds the configured row limit.",
                details={"code": "ADQL_TOP_LIMIT", "top": top_rows, "maximum": max_rows},
            )
        effective_rows = top_rows
    else:
        effective_rows = max_rows

    return ValidatedAdql(query=normalized, max_rows=effective_rows)


def _schema_allowed(schema_name: str) -> bool:
    if schema_name.lower() in FIXED_SCHEMAS:
        return True
    return PROJECT_CODE_RE.fullmatch(schema_name) is not None


def _mask_string_literals(query: str) -> str:
    """Replace ADQL string literals with spaces so keyword checks ignore them."""

    chars: list[str] = []
    index = 0
    length = len(query)
    while index < length:
        character = query[index]
        if character != "'":
            chars.append(character)
            index += 1
            continue
        chars.append(" ")
        index += 1
        while index < length:
            if query[index] == "'":
                chars.append(" ")
                index += 1
                if index < length and query[index] == "'":
                    chars.append(" ")
                    index += 1
                    continue
                break
            chars.append(" ")
            index += 1
        else:
            raise ValidationError(
                "ADQL string literal is not terminated.",
                details={"code": "ADQL_BAD_STRING"},
            )
    return "".join(chars)
