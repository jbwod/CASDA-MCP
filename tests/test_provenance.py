from __future__ import annotations

from casda_mcp.provenance import REDACTED, canonical_hash, redact, sanitize_url


def test_redacts_nested_secrets() -> None:
    value = {"token": "secret", "nested": {"password": "secret", "safe": "ok"}}
    assert redact(value) == {
        "token": REDACTED,
        "nested": {"password": REDACTED, "safe": "ok"},
    }


def test_sanitize_url_removes_userinfo_query_and_fragment() -> None:
    assert (
        sanitize_url("https://user:pass@example.test/path?signature=secret#fragment")
        == "https://example.test/path"
    )


def test_canonical_hash_is_stable_across_key_order() -> None:
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
