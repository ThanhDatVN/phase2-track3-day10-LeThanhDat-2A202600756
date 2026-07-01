"""Tests for the shared parameter-backed cache.

The class keeps the starter name SharedRedisCache for interface compatibility,
but the implementation intentionally does not use the Redis Python package.
"""
from __future__ import annotations

import time

import pytest

from reliability_lab.cache import SharedRedisCache


@pytest.fixture
def cache() -> SharedRedisCache:  # type: ignore[misc]
    storage: dict[str, dict[str, object]] = {}
    c = SharedRedisCache(
        redis_url="parameter-store",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:test:",
        storage=storage,
    )
    c.flush()
    yield c  # type: ignore[misc]
    c.flush()
    c.close()


def test_shared_storage_available(cache: SharedRedisCache) -> None:
    """Verifies the injected shared storage is available."""
    assert cache.ping()


def test_set_and_exact_get(cache: SharedRedisCache) -> None:
    cache.set("hello world", "response text")
    cached, score = cache.get("hello world")
    assert cached == "response text"
    assert score == 1.0


def test_ttl_expiry() -> None:
    storage: dict[str, dict[str, object]] = {}
    c = SharedRedisCache(
        redis_url="parameter-store",
        ttl_seconds=1,
        similarity_threshold=0.5,
        prefix="rl:test:ttl:",
        storage=storage,
    )
    c.flush()
    c.set("temp query", "temp response")
    time.sleep(1.5)
    cached, _ = c.get("temp query")
    assert cached is None
    c.flush()
    c.close()


def test_shared_state_across_instances() -> None:
    """Two cache instances with the same storage should see the same data."""
    storage: dict[str, dict[str, object]] = {}
    c1 = SharedRedisCache(
        redis_url="parameter-store",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:test:shared:",
        storage=storage,
    )
    c2 = SharedRedisCache(
        redis_url="parameter-store",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:test:shared:",
        storage=storage,
    )
    c1.flush()
    c1.set("shared query", "shared response")
    cached, _ = c2.get("shared query")
    assert cached == "shared response"
    c1.flush()
    c1.close()
    c2.close()


def test_privacy_query_not_cached(cache: SharedRedisCache) -> None:
    cache.set("account balance for user 123", "Balance: $500")
    cached, _ = cache.get("account balance for user 123")
    assert cached is None


def test_false_hit_different_years(cache: SharedRedisCache) -> None:
    cache.set("refund policy for 2024", "old policy")
    cached, _ = cache.get("refund policy for 2026")
    assert cached is None
    assert len(cache.false_hit_log) >= 1
