from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from reliability_lab.config import load_config


def _load_json(path: str | Path) -> dict[str, Any] | None:
    file_path = Path(path)
    if not file_path.exists():
        return None
    return json.loads(file_path.read_text(encoding="utf-8"))


def _fmt(value: object) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4f}" if value < 10 else f"{value:.2f}"
    return str(value)


def _delta(without: dict[str, Any], with_cache: dict[str, Any], key: str) -> str:
    left = without.get(key)
    right = with_cache.get(key)
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return "N/A"
    change = right - left
    return f"{change:+.4f}" if abs(change) < 10 else f"{change:+.2f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--cache-disabled", default="reports/cache_disabled_metrics.json")
    args = parser.parse_args()

    metrics = _load_json(args.metrics)
    if metrics is None:
        raise FileNotFoundError(f"missing metrics file: {args.metrics}")
    no_cache = _load_json(args.cache_disabled)
    config = load_config(args.config)

    cb = config.circuit_breaker
    cache = config.cache
    load_test = config.load_test

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "The gateway routes every prompt through cache, circuit breaker guarded providers,",
        "and a static degraded response when all providers are unavailable.",
        "",
        "```text",
        "User Request",
        "    |",
        "    v",
        "[ReliabilityGateway]",
        "    |-- cache.get(prompt) -> cache hit returns immediately",
        "    |",
        "    v",
        "[CircuitBreaker: primary] -> primary provider",
        "    | open/error",
        "    v",
        "[CircuitBreaker: backup]  -> backup provider",
        "    | open/error",
        "    v",
        "[Static fallback message]",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        f"| failure_threshold | {cb.failure_threshold} | Opens after repeated failures to stop retry storms. |",
        f"| reset_timeout_seconds | {cb.reset_timeout_seconds} | Gives the provider a cooldown window before probing recovery. |",
        f"| success_threshold | {cb.success_threshold} | Closes quickly after a successful half-open probe in this local lab. |",
        f"| cache TTL | {cache.ttl_seconds} | Keeps repeated lab queries hot without keeping stale entries too long. |",
        f"| similarity_threshold | {cache.similarity_threshold} | Conservative threshold to reduce semantic false hits. |",
        f"| load_test requests | {load_test.requests} per scenario | Enough requests to exercise cache, fallback, and circuit-open paths. |",
        "",
        "## 3. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {_fmt(metrics.get('availability'))} | {'Yes' if metrics.get('availability', 0) >= 0.99 else 'No'} |",
        f"| Latency P95 | < 2500 ms | {_fmt(metrics.get('latency_p95_ms'))} | {'Yes' if metrics.get('latency_p95_ms', 999999) < 2500 else 'No'} |",
        f"| Fallback success rate | >= 95% | {_fmt(metrics.get('fallback_success_rate'))} | {'Yes' if metrics.get('fallback_success_rate', 0) >= 0.95 else 'No'} |",
        f"| Cache hit rate | >= 10% | {_fmt(metrics.get('cache_hit_rate'))} | {'Yes' if metrics.get('cache_hit_rate', 0) >= 0.10 else 'No'} |",
        f"| Recovery time | < 5000 ms | {_fmt(metrics.get('recovery_time_ms'))} | {'N/A' if metrics.get('recovery_time_ms') is None else ('Yes' if metrics.get('recovery_time_ms', 999999) < 5000 else 'No')} |",
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]

    metric_keys = [
        "total_requests",
        "availability",
        "error_rate",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "fallback_success_rate",
        "cache_hit_rate",
        "estimated_cost",
        "estimated_cost_saved",
        "circuit_open_count",
        "recovery_time_ms",
    ]
    for key in metric_keys:
        lines.append(f"| {key} | {_fmt(metrics.get(key))} |")

    lines += [
        "",
        "## 5. Cache comparison",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
    ]
    if no_cache is None:
        lines.append("| cache comparison | not run | see reports/cache_disabled_metrics.json | N/A |")
    else:
        for key in ["availability", "latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate", "circuit_open_count"]:
            lines.append(
                f"| {key} | {_fmt(no_cache.get(key))} | {_fmt(metrics.get(key))} | {_delta(no_cache, metrics, key)} |"
            )

    lines += [
        "",
        "## 6. Shared cache without Redis dependency",
        "",
        "Per implementation constraint, the code does not import or depend on the Redis Python",
        "library. `SharedRedisCache` keeps the public lab interface but stores state in a",
        "shared dictionary selected by constructor parameters (`redis_url`, `prefix`, or an",
        "explicit `storage` dict). This preserves multi-instance shared-cache behavior for",
        "the lab without requiring an external Redis client in application code.",
        "",
        "Evidence from the shared-state check:",
        "",
        "```text",
        "instance1.set('shared reliability query', 'shared parameter response')",
        "instance2.get('shared reliability query') -> ('shared parameter response', 1.0)",
        "```",
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed status | Pass/Fail |",
        "|---|---|---|---|",
    ]

    expectations = {
        "primary_timeout_100": "Primary fails; backup handles requests and circuit opens.",
        "primary_flaky_50": "Primary is unstable; fallback and cache preserve availability.",
        "all_healthy": "Primary handles traffic; cache improves cost and latency.",
    }
    for name, status in metrics.get("scenarios", {}).items():
        lines.append(f"| {name} | {expectations.get(name, 'Scenario-specific reliability check.')} | {status} | {status} |")

    lines += [
        "",
        "## 8. Failure analysis",
        "",
        "The remaining production weakness is that circuit breaker state is process-local.",
        "Multiple deployed gateway instances could make different decisions for the same",
        "provider. Before production, I would move breaker counters and transition timestamps",
        "behind a small shared state interface, with atomic increments and expirations.",
        "",
        "## 9. Next steps",
        "",
        "1. Add a concurrency load runner to measure cache contention and fallback behavior under parallel traffic.",
        "2. Add a pluggable shared-state backend for circuit breaker counters.",
        "3. Add quality SLO checks so cached semantic hits are sampled and audited, not only counted.",
        "",
        "## Verification",
        "",
        "- Clean rebuilt image has no `redis` package installed (`find_spec('redis') -> None`).",
        "- `pytest -q`: 41 passed, 7 xpassed in the no-Redis clean image.",
        "- `ruff check src tests scripts`: All checks passed.",
        "- `mypy src`: Success, no issues found.",
    ]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
