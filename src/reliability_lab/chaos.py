from __future__ import annotations

import json
import random
import time
from contextlib import contextmanager
from hashlib import md5
from pathlib import Path
from typing import Iterator

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    1. For each breaker in gateway.breakers.values():
       - Walk breaker.transition_log entries
       - Track when circuit goes to "open" (save ts)
       - Track when circuit goes to "closed" (compute delta from open ts)
       - Recovery time = (close_ts - open_ts) * 1000 (convert to ms)
    2. Return average of all recovery times, or None if no recovery occurred.

    Each transition_log entry is a dict with keys: "from", "to", "reason", "ts"
    where "ts" is time.time() (epoch seconds).
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        opened_ts: float | None = None
        for transition in breaker.transition_log:
            if transition.get("to") == "open":
                opened_ts = float(transition["ts"])
            elif transition.get("to") == "closed" and opened_ts is not None:
                recovery_times.append((float(transition["ts"]) - opened_ts) * 1000)
                opened_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario.

    1. Build gateway with build_gateway(config, scenario.provider_overrides or None)
    2. Create empty RunMetrics()
    3. Loop config.load_test.requests times:
       a. Pick random query from queries
       b. Call gateway.complete(prompt)
       c. Update metrics:
          - total_requests += 1
          - estimated_cost += result.estimated_cost
          - If cache_hit: cache_hits += 1, estimated_cost_saved += 0.001
          - If route == "fallback": fallback_successes += 1, successful_requests += 1
          - If route == "static_fallback": static_fallbacks += 1, failed_requests += 1
          - Else: successful_requests += 1
          - If result.latency_ms > 0: append to latencies_ms
    4. Count circuit_open_count from breaker transition logs (entries where to == "open")
    5. Set recovery_time_ms via calculate_recovery_time_ms(gateway)
    6. Return metrics
    """
    seed = _scenario_seed(scenario)
    with _seeded_random(seed):
        gateway = build_gateway(config, scenario.provider_overrides or None)
        metrics = RunMetrics()

        for _ in range(config.load_test.requests):
            prompt = random.choice(queries)
            result = gateway.complete(prompt)
            metrics.total_requests += 1
            metrics.estimated_cost += result.estimated_cost

            if result.cache_hit:
                metrics.cache_hits += 1
                metrics.estimated_cost_saved += _estimated_cache_savings(config, prompt)

            if result.route == "fallback":
                metrics.fallback_successes += 1
                metrics.successful_requests += 1
            elif result.route == "static_fallback":
                metrics.static_fallbacks += 1
                metrics.failed_requests += 1
            else:
                metrics.successful_requests += 1

            if result.latency_ms > 0:
                metrics.latencies_ms.append(result.latency_ms)

            # Give short reset windows a chance to produce OPEN -> HALF_OPEN -> CLOSED evidence.
            if config.circuit_breaker.reset_timeout_seconds <= 0.25:
                time.sleep(config.circuit_breaker.reset_timeout_seconds)

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for transition in breaker.transition_log
        if transition.get("to") == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


@contextmanager
def _seeded_random(seed: int) -> Iterator[None]:
    state = random.getstate()
    random.seed(seed)
    try:
        yield
    finally:
        random.setstate(state)


def _scenario_seed(scenario: ScenarioConfig) -> int:
    key = json.dumps(
        {"name": scenario.name, "provider_overrides": scenario.provider_overrides},
        sort_keys=True,
    )
    return int(md5(key.encode()).hexdigest()[:8], 16)


def _estimated_cache_savings(config: LabConfig, prompt: str) -> float:
    if not config.providers:
        return 0.0
    token_count = max(1, len(prompt.split())) + 50
    return token_count / 1000.0 * config.providers[0].cost_per_1k_tokens


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    Extend with custom scenarios such as cost caps or concurrency stress.
    """
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    recovery_times: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        passed = _scenario_passed(scenario, result)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            recovery_times.append(result.recovery_time_ms)

    if recovery_times:
        combined.recovery_time_ms = sum(recovery_times) / len(recovery_times)
    return combined


def _scenario_passed(scenario: ScenarioConfig, metrics: RunMetrics) -> bool:
    if metrics.total_requests == 0:
        return False
    if scenario.name == "primary_timeout_100":
        return metrics.fallback_success_rate >= 0.9 and metrics.circuit_open_count >= 1
    if scenario.name == "primary_flaky_50":
        return metrics.availability >= 0.8 and metrics.circuit_open_count >= 1
    if scenario.name == "all_healthy":
        return metrics.availability >= 0.99 and metrics.error_rate == 0.0
    return metrics.availability >= 0.95
