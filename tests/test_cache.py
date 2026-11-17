"""Tests for bounded LRU cache and trusted store."""

import time
import pytest

from cache import BoundedCache, TrustedStore, SOURCE_NODE_AGENT, SOURCE_UPSTREAM


class TestBoundedCache:
    def test_put_and_get(self):
        c = BoundedCache(max_entries=100, ttl=60)
        c.put("k1", "v1", SOURCE_UPSTREAM)
        entry = c.get("k1")
        assert entry is not None
        assert entry.value == "v1"
        assert entry.source == SOURCE_UPSTREAM

    def test_miss_returns_none(self):
        c = BoundedCache(max_entries=100, ttl=60)
        assert c.get("missing") is None

    def test_ttl_expiry(self):
        c = BoundedCache(max_entries=100, ttl=0.01)
        c.put("k1", "v1", SOURCE_UPSTREAM)
        time.sleep(0.02)
        assert c.get("k1") is None

    def test_lru_eviction(self):
        c = BoundedCache(max_entries=3, ttl=60)
        c.put("a", 1, SOURCE_UPSTREAM)
        c.put("b", 2, SOURCE_UPSTREAM)
        c.put("c", 3, SOURCE_UPSTREAM)
        assert c.size == 3

        c.put("d", 4, SOURCE_UPSTREAM)
        assert c.size == 3
        assert c.get("a") is None
        assert c.get("d") is not None

    def test_access_refreshes_lru_order(self):
        c = BoundedCache(max_entries=3, ttl=60)
        c.put("a", 1, SOURCE_UPSTREAM)
        c.put("b", 2, SOURCE_UPSTREAM)
        c.put("c", 3, SOURCE_UPSTREAM)
        c.get("a")
        c.put("d", 4, SOURCE_UPSTREAM)
        assert c.get("a") is not None
        assert c.get("b") is None

    def test_overwrite_existing_key(self):
        c = BoundedCache(max_entries=100, ttl=60)
        c.put("k", "old", SOURCE_UPSTREAM)
        c.put("k", "new", SOURCE_NODE_AGENT)
        entry = c.get("k")
        assert entry.value == "new"
        assert entry.source == SOURCE_NODE_AGENT

    def test_invalidate(self):
        c = BoundedCache(max_entries=100, ttl=60)
        c.put("k", "v", SOURCE_UPSTREAM)
        assert c.invalidate("k") is True
        assert c.get("k") is None
        assert c.invalidate("k") is False

    def test_clear(self):
        c = BoundedCache(max_entries=100, ttl=60)
        c.put("a", 1, SOURCE_UPSTREAM)
        c.put("b", 2, SOURCE_UPSTREAM)
        c.clear()
        assert c.size == 0

    def test_provenance_preserved(self):
        c = BoundedCache(max_entries=100, ttl=60)
        c.put("k1", "v1", SOURCE_NODE_AGENT)
        c.put("k2", "v2", SOURCE_UPSTREAM)
        assert c.get("k1").source == SOURCE_NODE_AGENT
        assert c.get("k2").source == SOURCE_UPSTREAM

    def test_max_entries_minimum_one(self):
        c = BoundedCache(max_entries=0, ttl=60)
        c.put("k", "v", SOURCE_UPSTREAM)
        assert c.size == 1


class TestTrustedStore:
    def test_put_and_get(self):
        s = TrustedStore(stale_seconds=60)
        s.put("tip_height", 870000)
        entry = s.get("tip_height")
        assert entry is not None
        assert entry.value == 870000

    def test_staleness(self):
        s = TrustedStore(stale_seconds=0.01)
        s.put("tip_height", 870000)
        time.sleep(0.02)
        assert s.get("tip_height") is None

    def test_get_tip_height(self):
        s = TrustedStore(stale_seconds=60)
        s.put("tip_height", 870123)
        assert s.get_tip_height() == 870123

    def test_get_tip_height_none(self):
        s = TrustedStore(stale_seconds=60)
        assert s.get_tip_height() is None

    def test_is_connected(self):
        s = TrustedStore(stale_seconds=60)
        assert s.is_connected is False
        s.put("k", "v")
        assert s.is_connected is True

    def test_clear(self):
        s = TrustedStore(stale_seconds=60)
        s.put("k", "v")
        s.clear()
        assert s.get("k") is None
        assert s.is_connected is False

    def test_integer_satoshis(self):
        s = TrustedStore(stale_seconds=60)
        s.put("tip_height", 870000)
        val = s.get_tip_height()
        assert isinstance(val, int)
        assert val == 870000

    def test_bounded_max_entries(self):
        s = TrustedStore(stale_seconds=60, max_entries=5)
        for i in range(10):
            s.put(f"key_{i}", i)
        count = sum(1 for i in range(10) if s.get(f"key_{i}") is not None)
        assert count <= 5

    def test_sequence_monotonic(self):
        s = TrustedStore(stale_seconds=60)
        assert s.sequence == 0
        assert s.advance_sequence(1) is True
        assert s.sequence == 1
        assert s.advance_sequence(5) is True
        assert s.sequence == 5

    def test_sequence_rejects_stale(self):
        s = TrustedStore(stale_seconds=60)
        s.advance_sequence(10)
        assert s.advance_sequence(10) is False
        assert s.advance_sequence(5) is False
        assert s.sequence == 10

    def test_sequence_rejects_zero(self):
        s = TrustedStore(stale_seconds=60)
        s.advance_sequence(1)
        assert s.advance_sequence(0) is False

    def test_clear_resets_sequence(self):
        s = TrustedStore(stale_seconds=60)
        s.advance_sequence(42)
        s.clear()
        assert s.sequence == 0
