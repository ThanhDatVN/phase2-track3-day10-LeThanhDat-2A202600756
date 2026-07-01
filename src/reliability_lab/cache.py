from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from threading import RLock
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit[ -]?card|ssn|social[ -]?security|"
    r"user\s*\d+|account\s*\d+|api[ -]?key|token|secret|employee\s*\d+)\b"
    r"|[\w.+-]+@[\w-]+\.[\w.-]+",
    re.IGNORECASE,
)

VOLATILE_TERMS = re.compile(r"\b(current|today|latest|now|deadline|fee|price|rate)\b", re.IGNORECASE)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True when similar text is likely asking for a different fact."""
    nums_q = set(re.findall(r"\b\d+\b", query))
    nums_c = set(re.findall(r"\b\d+\b", cached_key))
    if nums_q and nums_c and nums_q != nums_c:
        return True

    # Time-sensitive wording is safer to miss than to serve stale cached text.
    if query.lower() != cached_key.lower() and (
        VOLATILE_TERMS.search(query) or VOLATILE_TERMS.search(cached_key)
    ):
        years_q = set(re.findall(r"\b20\d{2}\b", query))
        years_c = set(re.findall(r"\b20\d{2}\b", cached_key))
        if years_q != years_c:
            return True

    return False


def _normalize_query(query: str) -> str:
    """Canonical cache key used for exact-match upserts."""
    return re.sub(r"\s+", " ", query.strip().lower())


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache skeleton.

    Provides semantic similarity and false-hit guardrails.
    Use the module-level _is_uncacheable() and _looks_like_false_hit() helpers in your
    get() and set() methods.  For production, replace with SharedRedisCache.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity.

        1. Return (None, 0.0) if _is_uncacheable(query) — privacy check
        2. Evict expired entries (compare time.time() - created_at vs ttl_seconds)
        3. Find best matching entry using self.similarity(query, entry.key)
        4. If best_score >= similarity_threshold:
           a. Check _looks_like_false_hit(query, best_key) — if true, log to
              self.false_hit_log and return (None, best_score)
           b. Otherwise return (best_value, best_score)
        5. Return (None, best_score) if no match above threshold

        You'll need a self.false_hit_log: list[dict[str, object]] attribute
        (add it in __init__).
        """
        if _is_uncacheable(query):
            return None, 0.0

        self._evict_expired()

        best_entry: CacheEntry | None = None
        best_score = 0.0
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_entry = entry
                best_score = score

        if best_entry is None or best_score < self.similarity_threshold:
            return None, best_score

        if _looks_like_false_hit(query, best_entry.key):
            self.false_hit_log.append(
                {
                    "query": query,
                    "cached_key": best_entry.key,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                }
            )
            return None, best_score

        return best_entry.value, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in cache.

        1. Return immediately if _is_uncacheable(query)
        2. Append a CacheEntry to self._entries
        """
        if _is_uncacheable(query):
            return
        entry = CacheEntry(
            key=query,
            value=value,
            created_at=time.time(),
            metadata=metadata or {},
        )
        normalized = _normalize_query(query)
        for index, existing in enumerate(self._entries):
            if _normalize_query(existing.key) == normalized:
                self._entries[index] = entry
                return
        self._entries.append(entry)

    def _evict_expired(self) -> None:
        now = time.time()
        self._entries = [
            entry for entry in self._entries if now - entry.created_at <= self.ttl_seconds
        ]

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute semantic similarity between two strings.

        The naive token-overlap (Jaccard) approach loses too much information.

        Suggested approach:
        1. If a == b, return 1.0
        2. Tokenize both strings: split into words + character n-grams (n=3)
           e.g., "hello world" → ["hello", "world", "hel", "ell", "llo", "wor", "orl", "rld"]
        3. Build Counter (bag-of-words) vectors from these tokens
        4. Compute cosine similarity: dot(a,b) / (|a| * |b|)

        Hint: Use collections.Counter and math.sqrt.
        Import them at the top of the file.
        """
        if a == b or _normalize_query(a) == _normalize_query(b):
            return 1.0

        left = Counter(ResponseCache._tokens(a))
        right = Counter(ResponseCache._tokens(b))
        if not left or not right:
            return 0.0

        dot = sum(count * right[token] for token, count in left.items())
        left_norm = math.sqrt(sum(count * count for count in left.values()))
        right_norm = math.sqrt(sum(count * count for count in right.values()))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)

    @staticmethod
    def _tokens(text: str) -> list[str]:
        normalized = _normalize_query(text)
        words = re.findall(r"\b\w+\b", normalized)
        compact = re.sub(r"\s+", "", normalized)
        ngrams = [compact[i : i + 3] for i in range(max(0, len(compact) - 2))]
        return words + ngrams


# ---------------------------------------------------------------------------
# Shared parameter cache (keeps the original lab class name)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Parameter-backed shared cache for multi-instance deployments.

    The starter interface keeps its original name, but this implementation does
    not import or depend on the Redis client library. Instances with the same
    redis_url parameter share the same process-level store, and callers may pass
    an explicit storage dict.

    Data model:
        Key    = "{prefix}{query_hash}"
        Value  = dict with fields: "query", "response", "expires_at"
        TTL    = enforced during lookup

    For similarity lookup, scan all keys with self.prefix and compute
    similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for cache key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
        storage: dict[str, dict[str, object]] | None = None,
    ):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._storage_key = redis_url
        if storage is None:
            storage = _SHARED_STORES.setdefault(redis_url, {})
        self._storage = storage
        self._lock: Any = _SHARED_LOCKS.setdefault(redis_url, RLock())

    def ping(self) -> bool:
        """Check shared storage availability."""
        return self._storage is not None

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from shared storage.

        1. Return (None, 0.0) if _is_uncacheable(query)
        2. Build exact-match key: f"{self.prefix}{self._query_hash(query)}"
        3. Check the shared storage key; if found return (response, 1.0)
        4. Otherwise iterate keys with this prefix
        5. For each key, HGET "query" field and compute
           ResponseCache.similarity(query, cached_query)
        6. Track best match that is >= self.similarity_threshold
        7. Before returning a match, check _looks_like_false_hit(); if true,
           append to self.false_hit_log and return (None, best_score)
        """
        if _is_uncacheable(query):
            return None, 0.0

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        with self._lock:
            self._evict_expired()
            exact_entry = self._storage.get(exact_key)
            if exact_entry is not None:
                exact_response = exact_entry.get("response")
                return str(exact_response), 1.0

            best_key: str | None = None
            best_query: str | None = None
            best_response: str | None = None
            best_score = 0.0
            for key, entry in self._storage.items():
                if not key.startswith(self.prefix):
                    continue
                cached_query = entry.get("query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, str(cached_query))
                if score > best_score:
                    best_key = key
                    best_query = str(cached_query)
                    best_response = str(entry.get("response"))
                    best_score = score

        if best_key is None or best_query is None or best_response is None:
            return None, best_score
        if best_score < self.similarity_threshold:
            return None, best_score
        if _looks_like_false_hit(query, best_query):
            self.false_hit_log.append(
                {
                    "query": query,
                    "cached_key": best_query,
                    "cache_key": best_key,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                }
            )
            return None, best_score
        return str(best_response), best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in shared storage with TTL.

        1. Return immediately if _is_uncacheable(query)
        2. Build key: f"{self.prefix}{self._query_hash(query)}"
        3. Store the response in the shared storage dict
        4. Store an expires_at timestamp for TTL enforcement
        """
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        with self._lock:
            self._storage[key] = {
                "query": query,
                "response": value,
                "metadata": metadata or {},
                "expires_at": time.time() + self.ttl_seconds,
            }

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        with self._lock:
            for key in list(self._storage):
                if key.startswith(self.prefix):
                    del self._storage[key]

    def close(self) -> None:
        """No-op close method kept for interface compatibility."""
        return None

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(_normalize_query(query).encode()).hexdigest()[:12]

    def _evict_expired(self) -> None:
        now = time.time()
        for key, entry in list(self._storage.items()):
            expires_at = entry.get("expires_at")
            if isinstance(expires_at, (float, int)) and expires_at <= now:
                del self._storage[key]


_SHARED_STORES: dict[str, dict[str, dict[str, object]]] = {}
_SHARED_LOCKS: dict[str, Any] = {}
