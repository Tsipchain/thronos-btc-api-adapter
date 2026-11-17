"""Bounded LRU cache with source provenance tracking."""

import time
from collections import OrderedDict
from typing import Any, Optional, Tuple

SOURCE_NODE_AGENT = "node_agent"
SOURCE_UPSTREAM = "upstream"


class CacheEntry:
    __slots__ = ("value", "source", "timestamp")

    def __init__(self, value: Any, source: str, timestamp: float):
        self.value = value
        self.source = source
        self.timestamp = timestamp

    def is_expired(self, ttl: float) -> bool:
        return (time.time() - self.timestamp) >= ttl


class BoundedCache:
    def __init__(self, max_entries: int = 10_000, ttl: float = 30.0):
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_entries = max(1, max_entries)
        self._ttl = ttl

    @property
    def size(self) -> int:
        return len(self._store)

    def get(self, key: str) -> Optional[CacheEntry]:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired(self._ttl):
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return entry

    def put(self, key: str, value: Any, source: str) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = CacheEntry(value, source, time.time())
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    def invalidate(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    def clear(self) -> None:
        self._store.clear()


class TrustedStore:
    """Separate store for node-agent pushed data with its own staleness TTL.

    Supports monotonic sequence enforcement: updates with a sequence number
    at or below the current high-water mark are rejected as stale/replayed.
    """

    def __init__(self, stale_seconds: float = 120.0, max_entries: int = 5000):
        self._data: dict[str, CacheEntry] = {}
        self._stale_seconds = stale_seconds
        self._max_entries = max(1, max_entries)
        self._last_seen: float = 0.0
        self._sequence: int = 0

    @property
    def last_seen(self) -> float:
        return self._last_seen

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def is_connected(self) -> bool:
        if self._last_seen == 0.0:
            return False
        return (time.time() - self._last_seen) < self._stale_seconds * 2

    def advance_sequence(self, seq: int) -> bool:
        """Try to advance to the given sequence. Returns False if stale/replay."""
        if seq <= self._sequence:
            return False
        self._sequence = seq
        return True

    def commit(self, seq: int, items: dict[str, Any]) -> bool:
        """Atomically advance the sequence and publish a validated batch."""
        if seq <= self._sequence:
            return False
        now = time.time()
        self._sequence = seq
        for key, value in items.items():
            self._data[key] = CacheEntry(value, SOURCE_NODE_AGENT, now)
        self._last_seen = now
        while len(self._data) > self._max_entries:
            oldest_key = next(iter(self._data))
            del self._data[oldest_key]
        return True

    def put(self, key: str, value: Any) -> None:
        self._data[key] = CacheEntry(value, SOURCE_NODE_AGENT, time.time())
        self._last_seen = time.time()
        while len(self._data) > self._max_entries:
            oldest_key = next(iter(self._data))
            del self._data[oldest_key]

    def get(self, key: str) -> Optional[CacheEntry]:
        entry = self._data.get(key)
        if entry is None:
            return None
        if (time.time() - entry.timestamp) >= self._stale_seconds:
            del self._data[key]
            return None
        return entry

    def get_tip_height(self) -> Optional[int]:
        entry = self.get("tip_height")
        if entry is None:
            return None
        try:
            return int(entry.value)
        except (ValueError, TypeError):
            return None

    def clear(self) -> None:
        self._data.clear()
        self._last_seen = 0.0
        self._sequence = 0
