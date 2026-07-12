"""Validated server configuration loaded from environment variables."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """CASDA MCP configuration with conservative defaults."""

    model_config = SettingsConfigDict(env_prefix="CASDA_", extra="ignore", case_sensitive=False)

    base_url: str = "https://casda.csiro.au"
    tap_url: str = "https://casda.csiro.au/casda_vo_tools/tap/sync"
    datalink_url: str = "https://data.csiro.au/casda_vo_proxy/vo/datalink/links"
    soda_url: str = "https://casda.csiro.au/casda_data_access/data/async"
    login_url: str = "https://data.csiro.au/casda_vo_proxy/vo/tap/availability"

    username: str | None = None
    password: SecretStr | None = None
    token: SecretStr | None = None

    enable_staging: bool = False
    enable_downloads: bool = False
    download_dir: Path | None = None
    allow_overwrite: bool = False
    allow_unknown_stage_size: bool = False

    max_results: int = Field(default=100, ge=1, le=1000)
    max_cone_radius_deg: float = Field(default=5.0, gt=0, le=90)
    max_stage_products: int = Field(default=20, ge=1, le=500)
    max_stage_bytes: int = Field(default=100 * 1024**3, ge=1)
    max_manifest_products: int = Field(default=100, ge=1, le=1000)
    max_download_bytes: int = Field(default=50 * 1024**3, ge=1)
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    download_timeout_seconds: float = Field(default=300.0, gt=0, le=86400)
    max_retries: int = Field(default=3, ge=0, le=10)
    cache_ttl_seconds: int = Field(default=60, ge=0, le=86400)
    cache_max_entries: int = Field(default=256, ge=0, le=10000)
    state_db: Path | None = None
    user_agent: str = "casda-mcp/0.1.0 (+https://github.com/csiro-rds)"

    @field_validator("base_url", "tap_url", "datalink_url", "soda_url", "login_url")
    @classmethod
    def validate_archive_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("archive endpoints must be credential-free HTTPS URLs")
        return value.rstrip("/")

    @field_validator("username")
    @classmethod
    def empty_username_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("download_dir", "state_db", mode="before")
    @classmethod
    def empty_path_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_state_changing_configuration(self) -> Settings:
        if self.enable_downloads:
            if self.download_dir is None:
                raise ValueError("CASDA_DOWNLOAD_DIR is required when downloads are enabled")
            if not self.download_dir.is_absolute():
                raise ValueError("CASDA_DOWNLOAD_DIR must be an absolute path")
        if self.enable_staging and not self.has_credentials:
            raise ValueError("staging requires CASDA_TOKEN or CASDA_USERNAME and CASDA_PASSWORD")
        if (self.username is None) != (self.password is None):
            raise ValueError("CASDA_USERNAME and CASDA_PASSWORD must be configured together")
        return self

    @property
    def has_credentials(self) -> bool:
        return self.token is not None or (self.username is not None and self.password is not None)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        configured = {
            urlparse(url).hostname
            for url in (
                self.base_url,
                self.tap_url,
                self.datalink_url,
                self.soda_url,
                self.login_url,
            )
        }
        # CASDA currently returns staged files from these archive-controlled hosts.
        configured.update({"ingest.pawsey.org", "ingest.pawsey.org.au"})
        return frozenset(host for host in configured if host)
