from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from casda_mcp.config import Settings


def test_defaults_are_read_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.enable_staging is False
    assert settings.enable_downloads is False
    assert settings.has_credentials is False
    assert settings.tap_url.startswith("https://")


def test_downloads_require_absolute_directory() -> None:
    with pytest.raises(PydanticValidationError, match="absolute"):
        Settings(_env_file=None, enable_downloads=True, download_dir="relative")


def test_staging_requires_complete_credentials() -> None:
    with pytest.raises(PydanticValidationError, match="requires"):
        Settings(_env_file=None, enable_staging=True)
    with pytest.raises(PydanticValidationError, match="configured together"):
        Settings(_env_file=None, username="researcher@example.test")


@pytest.mark.parametrize("url", ["http://casda.test/tap", "https://user:secret@casda.test/tap"])
def test_archive_endpoints_must_be_safe_https(url: str) -> None:
    with pytest.raises(PydanticValidationError, match="credential-free HTTPS"):
        Settings(_env_file=None, tap_url=url)


def test_secret_values_are_masked() -> None:
    settings = Settings(
        _env_file=None,
        username="researcher@example.test",
        password="top-secret",  # noqa: S106
    )
    assert "top-secret" not in repr(settings)
