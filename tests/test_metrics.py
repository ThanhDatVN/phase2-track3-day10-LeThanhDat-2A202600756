from reliability_lab.metrics import RunMetrics, percentile


def test_percentile() -> None:
    values = [10, 20, 30, 40, 50]
    assert percentile(values, 50) == 30
    assert percentile(values, 95) >= 40


def test_report_dict_contains_required_metrics() -> None:
    m = RunMetrics(total_requests=2, successful_requests=1, failed_requests=1, latencies_ms=[100, 200])
    report = m.to_report_dict()
    for key in ["availability", "error_rate", "latency_p50_ms", "latency_p95_ms", "cache_hit_rate"]:
        assert key in report


def test_report_dict_keeps_raw_counters() -> None:
    m = RunMetrics(
        total_requests=3,
        successful_requests=2,
        failed_requests=1,
        fallback_successes=1,
        static_fallbacks=1,
        cache_hits=1,
    )
    report = m.to_report_dict()
    assert report["successful_requests"] == 2
    assert report["failed_requests"] == 1
    assert report["fallback_successes"] == 1
    assert report["static_fallbacks"] == 1
    assert report["cache_hits"] == 1
