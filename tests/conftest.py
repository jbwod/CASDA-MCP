from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from casda_mcp.config import Settings
from casda_mcp.service import CasdaService


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        cache_ttl_seconds=60,
        cache_max_entries=16,
        state_db=None,
        max_results=100,
        download_dir=tmp_path.resolve(),
    )


@pytest.fixture
async def close_services() -> AsyncIterator[list[CasdaService]]:
    services: list[CasdaService] = []
    yield services
    for service in services:
        await service.aclose()
