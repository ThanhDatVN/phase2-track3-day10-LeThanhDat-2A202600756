"""Cache tests — all must pass when your implementation is correct.

Tests cover: exact match, similarity, TTL, privacy guardrails, false-hit detection.
"""
import time

from reliability_lab.cache import ResponseCache, SharedRedisCache


def test_exact_match_returns_hit() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    cache.set("hello world", "response")
    cached, score = cache.get("hello world")
    assert cached == "response"
    assert score == 1.0


def test_similar_query_returns_hit() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.7)
    cache.set("Summarize the refund policy", "Refund policy summary")
    cached, score = cache.get("Summarize refund policy")
    assert cached is not None
    assert score >= 0.7


def test_dissimilar_query_returns_miss() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.9)
    cache.set("Summarize the refund policy", "Refund policy summary")
    cached, score = cache.get("What is the weather today?")
    assert cached is None


def test_ttl_expiry() -> None:
    cache = ResponseCache(ttl_seconds=1, similarity_threshold=0.5)
    cache.set("hello", "world")
    time.sleep(1.1)
    cached, _ = cache.get("hello")
    assert cached is None


def test_privacy_query_bypasses_cache() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Give me the current account balance for user 123", "Balance: $500")
    cached, _ = cache.get("Give me the current account balance for user 123")
    assert cached is None


def test_privacy_query_not_stored() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("password reset for user 456", "Reset link sent")
    assert len(cache._entries) == 0


def test_false_hit_detection_different_years() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
    cached, _ = cache.get("Summarize refund policy for 2026 deadline")
    assert cached is None
    assert len(cache.false_hit_log) == 1
    assert cache.false_hit_log[0]["reason"] == "date_or_number_mismatch"


def test_false_hit_detection_different_numeric_intent() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize the admission FAQ in 5 bullets", "five bullet summary")
    cached, _ = cache.get("Summarize the admission FAQ in 3 bullets")
    assert cached is None
    assert cache.false_hit_log[-1]["reason"] == "date_or_number_mismatch"


def test_same_year_not_flagged_as_false_hit() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    cache.set("Summarize refund policy for 2024 deadline", "2024 refund policy")
    cached, _ = cache.get("Summarize refund policy for 2024")
    assert cached is not None


def test_ngram_similarity_scores() -> None:
    assert ResponseCache.similarity("hello world", "hello world") == 1.0
    score = ResponseCache.similarity("circuit breaker pattern", "circuit breaker design")
    assert 0.5 < score < 1.0
    score_low = ResponseCache.similarity("hello", "completely different")
    assert score_low < 0.3


def test_cache_upserts_exact_query() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.5)
    cache.set("hello world", "first")
    cache.set("  Hello   World  ", "second")
    cached, score = cache.get("hello world")
    assert cached == "second"
    assert score == 1.0
    assert len(cache._entries) == 1


def test_shared_parameter_cache_across_instances() -> None:
    storage: dict[str, dict[str, object]] = {}
    first = SharedRedisCache("parameter-store", 60, 0.5, prefix="test:", storage=storage)
    second = SharedRedisCache("parameter-store", 60, 0.5, prefix="test:", storage=storage)
    first.set("shared query", "shared response")
    cached, score = second.get("shared query")
    assert cached == "shared response"
    assert score == 1.0


def test_shared_parameter_cache_ttl_expiry() -> None:
    storage: dict[str, dict[str, object]] = {}
    cache = SharedRedisCache("parameter-store", 1, 0.5, prefix="ttl:", storage=storage)
    cache.set("temp query", "temp response")
    time.sleep(1.1)
    cached, _ = cache.get("temp query")
    assert cached is None
