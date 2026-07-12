"""Small in-memory TTL cache for read-only metadata queries."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from copy import deepcopy
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    def __init__(self, *, ttl_seconds: int, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._values: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> T | None:
        if self.ttl_seconds == 0 or self.max_entries == 0:
            return None
        async with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= time.monotonic():
                del self._values[key]
                return None
            self._values.move_to_end(key)
            return deepcopy(value)

    async def set(self, key: str, value: T) -> None:
        if self.ttl_seconds == 0 or self.max_entries == 0:
            return
        async with self._lock:
            self._values[key] = (time.monotonic() + self.ttl_seconds, deepcopy(value))
            self._values.move_to_end(key)
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)

    async def clear(self) -> None:
        async with self._lock:
            self._values.clear()
