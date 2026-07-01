from reliability_lab.chaos import run_scenario
from reliability_lab.config import (
    CacheConfig,
    CircuitBreakerConfig,
    LabConfig,
    LoadTestConfig,
    ProviderConfig,
    ScenarioConfig,
)


def _config() -> LabConfig:
    return LabConfig(
        providers=[
            ProviderConfig(
                name="primary",
                fail_rate=0.2,
                base_latency_ms=1,
                cost_per_1k_tokens=0.01,
            ),
            ProviderConfig(
                name="backup",
                fail_rate=0.0,
                base_latency_ms=1,
                cost_per_1k_tokens=0.005,
            ),
        ],
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=2,
            reset_timeout_seconds=1,
            success_threshold=1,
        ),
        cache=CacheConfig(enabled=True, backend="memory", ttl_seconds=60, similarity_threshold=0.95),
        load_test=LoadTestConfig(requests=8),
        scenarios=[],
    )


def test_run_scenario_is_reproducible_for_same_config() -> None:
    scenario = ScenarioConfig(
        name="deterministic",
        provider_overrides={"primary": 0.5, "backup": 0.0},
    )
    queries = ["query one", "query two", "query three"]

    first = run_scenario(_config(), queries, scenario)
    second = run_scenario(_config(), queries, scenario)

    assert first.total_requests == second.total_requests
    assert first.successful_requests == second.successful_requests
    assert first.failed_requests == second.failed_requests
    assert first.cache_hits == second.cache_hits
    assert first.circuit_open_count == second.circuit_open_count
