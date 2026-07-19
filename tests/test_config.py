from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from casda_mcp.config import Settings


def test_defaults_are_read_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.enable_staging is False
    assert settings.enable_downloads is False
    assert settings.has_credentials is False
    assert settings.tap_url.startswith("https://")
    assert settings.max_response_bytes == 16 * 1024**2


@pytest.mark.parametrize("value", [1023, 100 * 1024**2 + 1])
def test_response_byte_limit_is_bounded(value: int) -> None:
    with pytest.raises(PydanticValidationError, match="max_response_bytes"):
        Settings(_env_file=None, max_response_bytes=value)


def test_downloads_require_absolute_directory() -> None:
    with pytest.raises(PydanticValidationError, match="absolute"):
        Settings(_env_file=None, enable_downloads=True, download_dir="relative")


def test_downloads_reject_filesystem_root() -> None:
    filesystem_root = Path(Path.cwd().anchor)
    with pytest.raises(PydanticValidationError, match="not a filesystem root"):
        Settings(_env_file=None, enable_downloads=True, download_dir=filesystem_root)


def test_downloads_reject_symlink_to_filesystem_root(tmp_path: Path) -> None:
    filesystem_root = Path(tmp_path.anchor)
    root_link = tmp_path / "root-link"
    try:
        root_link.symlink_to(filesystem_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(PydanticValidationError, match="not a filesystem root"):
        Settings(_env_file=None, enable_downloads=True, download_dir=root_link)


def test_downloads_accept_dedicated_directory_and_symlink(tmp_path: Path) -> None:
    dedicated = tmp_path / "downloads"
    dedicated.mkdir()
    settings = Settings(_env_file=None, enable_downloads=True, download_dir=dedicated)
    assert settings.download_dir == dedicated

    dedicated_link = tmp_path / "downloads-link"
    try:
        dedicated_link.symlink_to(dedicated, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    linked_settings = Settings(_env_file=None, enable_downloads=True, download_dir=dedicated_link)
    assert linked_settings.download_dir == dedicated.resolve()


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
