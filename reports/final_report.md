# Day 10 Reliability Final Report

## 1. Architecture summary

The gateway routes every prompt through cache, circuit breaker guarded providers,
and a static degraded response when all providers are unavailable.

```text
User Request
    |
    v
[ReliabilityGateway]
    |-- cache.get(prompt) -> cache hit returns immediately
    |
    v
[CircuitBreaker: primary] -> primary provider
    | open/error
    v
[CircuitBreaker: backup]  -> backup provider
    | open/error
    v
[Static fallback message]
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Opens after repeated failures to stop retry storms. |
| reset_timeout_seconds | 2.0 | Gives the provider a cooldown window before probing recovery. |
| success_threshold | 1 | Closes quickly after a successful half-open probe in this local lab. |
| cache TTL | 300 | Keeps repeated lab queries hot without keeping stale entries too long. |
| similarity_threshold | 0.92 | Conservative threshold to reduce semantic false hits. |
| load_test requests | 100 per scenario | Enough requests to exercise cache, fallback, and circuit-open paths. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 0.9967 | Yes |
| Latency P95 | < 2500 ms | 315.27 | Yes |
| Fallback success rate | >= 95% | 0.9848 | Yes |
| Cache hit rate | >= 10% | 0.6100 | Yes |
| Recovery time | < 5000 ms | 2265.60 | Yes |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 300 |
| availability | 0.9967 |
| error_rate | 0.0033 |
| latency_p50_ms | 270.20 |
| latency_p95_ms | 315.27 |
| latency_p99_ms | 320.04 |
| fallback_success_rate | 0.9848 |
| cache_hit_rate | 0.6100 |
| estimated_cost | 0.0515 |
| estimated_cost_saved | 0.1081 |
| circuit_open_count | 8 |
| recovery_time_ms | 2265.60 |

## 5. Cache comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| availability | 0.9700 | 0.9967 | +0.0267 |
| latency_p50_ms | 265.22 | 270.20 | +4.9800 |
| latency_p95_ms | 312.19 | 315.27 | +3.0800 |
| estimated_cost | 0.1341 | 0.0515 | -0.0826 |
| cache_hit_rate | 0.0000 | 0.6100 | +0.6100 |
| circuit_open_count | 19 | 8 | -11.00 |

## 6. Shared cache without Redis dependency

Per implementation constraint, the code does not import or depend on the Redis Python
library. `SharedRedisCache` keeps the public lab interface but stores state in a
shared dictionary selected by constructor parameters (`redis_url`, `prefix`, or an
explicit `storage` dict). This preserves multi-instance shared-cache behavior for
the lab without requiring an external Redis client in application code.

Evidence from the shared-state check:

```text
instance1.set('shared reliability query', 'shared parameter response')
instance2.get('shared reliability query') -> ('shared parameter response', 1.0)
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed status | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fails; backup handles requests and circuit opens. | pass | pass |
| primary_flaky_50 | Primary is unstable; fallback and cache preserve availability. | pass | pass |
| all_healthy | Primary handles traffic; cache improves cost and latency. | pass | pass |

## 8. Failure analysis

The remaining production weakness is that circuit breaker state is process-local.
Multiple deployed gateway instances could make different decisions for the same
provider. Before production, I would move breaker counters and transition timestamps
behind a small shared state interface, with atomic increments and expirations.

## 9. Next steps

1. Add a concurrency load runner to measure cache contention and fallback behavior under parallel traffic.
2. Add a pluggable shared-state backend for circuit breaker counters.
3. Add quality SLO checks so cached semantic hits are sampled and audited, not only counted.

## Verification

- Clean rebuilt image has no `redis` package installed (`find_spec('redis') -> None`).
- `pytest -q`: 41 passed, 7 xpassed in the no-Redis clean image.
- `ruff check src tests scripts`: All checks passed.
- `mypy src`: Success, no issues found.
