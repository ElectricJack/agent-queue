"""Sustained loop lag reports a timestamp and candidate causes, never a diagnosis."""

from __future__ import annotations

import sys

import pytest

from src.config import AppConfig
from src.doctor import DoctorContext, Severity, default_registry
from src.doctor.perf_checks import CHECK_ID, MIN_SAMPLES, WINDOW_SECONDS, rank_candidates, run_check
from src.metrics.histogram import new_hist, observe

perf_checks = sys.modules["src.doctor.perf_checks"]
NOW = 10_000.0


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(perf_checks.time, "time", lambda: NOW)


def hist(*values):
    histogram = new_hist()
    for value in values:
        observe(histogram, value)
    return histogram


def sample(ts, *, drift=(5,), pool=(1,), query=(1,), psi=None, routes=None, enabled=True):
    perf = {"enabled": enabled}
    if enabled:
        perf.update(
            {
                "loop": {"drift": hist(*drift)},
                "db": {"pool_wait": hist(*pool), "query": hist(*query)},
                "host": {
                    "psi": psi
                    or {"cpu": None, "io": None, "memory": None, "reason": "psi_unavailable"}
                },
                "api": {"routes": routes or {}},
            }
        )
    return {"ts": ts, "perf": perf}


class FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.reads = []

    async def read_metrics_samples(self, resolution, from_ts, to_ts, *, limit=10_000):
        self.reads.append((resolution, from_ts, to_ts, limit))
        return sorted(
            (row for row in self.rows if from_ts <= row["ts"] <= to_ts),
            key=lambda row: row["ts"],
        )[:limit]


def ctx(rows) -> DoctorContext:
    return DoctorContext(config=AppConfig(), db=FakeDb(rows))


def window_rows(**kwargs):
    return [sample(NOW - i, **kwargs) for i in range(MIN_SAMPLES)]


def test_the_check_is_registered_and_read_only():
    registry = default_registry()
    assert CHECK_ID in registry.ids()
    assert perf_checks.CHECKS[CHECK_ID].fix is None
    assert perf_checks.CHECKS[CHECK_ID].owner == "dashboard-performance"


async def test_no_database_is_info():
    result = await run_check(CHECK_ID, DoctorContext(config=AppConfig()))
    assert result.severity is Severity.INFO
    assert "no database" in result.detail


async def test_no_samples_is_info():
    result = await run_check(CHECK_ID, ctx([]))
    assert result.severity is Severity.INFO


async def test_reads_only_the_last_180_seconds_of_one_second_samples():
    context = ctx([sample(NOW - WINDOW_SECONDS - 1, drift=(900,)), sample(NOW + 1)])
    result = await run_check(CHECK_ID, context)
    assert result.severity is Severity.INFO
    assert context.db.reads == [("1s", NOW - WINDOW_SECONDS, NOW, WINDOW_SECONDS + 5)]


async def test_probes_disabled_is_info_even_with_old_lag():
    rows = window_rows(drift=(900,)) + [sample(NOW + 0.0, enabled=False)]
    result = await run_check(CHECK_ID, ctx(rows))
    assert result.severity is Severity.INFO
    assert "perf_enabled" in result.detail


async def test_too_few_samples_is_info():
    result = await run_check(CHECK_ID, ctx(window_rows()[:-1]))
    assert result.severity is Severity.INFO
    assert result.data["samples"] == MIN_SAMPLES - 1


@pytest.mark.parametrize("perf", [None, {}, {"enabled": True, "loop": None}])
async def test_missing_loop_telemetry_is_info(perf):
    rows = [{"ts": NOW - i, "perf": perf} for i in range(MIN_SAMPLES)]
    result = await run_check(CHECK_ID, ctx(rows))
    assert result.severity is Severity.INFO
    assert result.data["samples"] == 0


async def test_empty_histograms_do_not_report_a_healthy_loop():
    result = await run_check(CHECK_ID, ctx(window_rows(drift=())))
    assert result.severity is Severity.INFO
    assert result.data["samples"] == 0


async def test_a_quiet_loop_is_ok_at_the_sample_minimum():
    result = await run_check(CHECK_ID, ctx(window_rows()))
    assert result.severity is Severity.OK
    assert result.data["samples"] == MIN_SAMPLES
    assert result.data["p95_ms"] < 50


@pytest.mark.parametrize("drift", [(500,), (500,) * 19 + (1000,)])
async def test_a_p95_at_or_below_500ms_is_ok(drift):
    result = await run_check(CHECK_ID, ctx(window_rows(drift=drift)))
    assert result.severity is Severity.OK
    assert result.data["p95_ms"] <= 500


async def test_sustained_lag_warns_with_since_and_ranked_candidates():
    rows = window_rows(drift=(900,), pool=(400,))
    result = await run_check(CHECK_ID, ctx(rows), repair=True)
    assert result.severity is Severity.WARN
    assert result.data == {
        "window_seconds": 180,
        "samples": MIN_SAMPLES,
        "p95_ms": 975.0,
        "max_ms": 900.0,
        "stalls_over_500ms": MIN_SAMPLES,
        "since_ts": min(row["ts"] for row in rows),
        "candidates": [{"cause": "db_pool_wait", "p95_ms": 485.0}],
    }
    assert "candidate" in result.detail and "diagnosis" in result.detail
    assert "02:44:41Z" in result.detail
    assert not result.fixable and not result.fix_applied


async def test_p95_is_computed_from_merged_observations_not_averaged_percentiles():
    rows = [sample(NOW - i, drift=(5,) * 100) for i in range(MIN_SAMPLES - 1)]
    rows.append(sample(NOW - MIN_SAMPLES + 1, drift=(900,)))
    result = await run_check(CHECK_ID, ctx(rows))
    assert result.severity is Severity.OK
    assert result.data["p95_ms"] < 5
    assert result.data["max_ms"] == 900
    assert result.data["stalls_over_500ms"] == 1


async def test_older_disabled_and_legacy_samples_do_not_count_or_set_since():
    rows = window_rows(drift=(900,)) + [
        sample(NOW - 130, enabled=False),
        {"ts": NOW - 140},
    ]
    result = await run_check(CHECK_ID, ctx(rows))
    assert result.severity is Severity.WARN
    assert result.data["samples"] == MIN_SAMPLES
    assert result.data["since_ts"] == NOW - MIN_SAMPLES + 1


def test_rank_candidates_falls_back_to_synchronous_python():
    candidates = rank_candidates([sample(1.0, drift=(900,))])
    assert candidates[0]["cause"] == "synchronous_python"
    assert "profile synchronous Python" in candidates[0]["hint"]


def test_unavailable_candidate_sections_are_tolerated():
    rows = [{"perf": {"db": None, "host": {"psi": None}, "api": {"routes": None}}}]
    assert rank_candidates(rows)[0]["cause"] == "synchronous_python"


def test_rank_candidates_orders_pool_query_host_then_route():
    routes = {"POST /api/pool/status": {"latency": hist(*([700] * 12))}}
    rows = [
        sample(
            1.0,
            drift=(900,),
            pool=(300,),
            query=(300,),
            psi={
                "memory": {"some_avg10": 5.0},
                "io": {"some_avg10": 20.0},
                "cpu": {"some_avg10": 20.0, "full_avg10": None},
                "reason": None,
            },
            routes=routes,
        )
    ]
    candidates = rank_candidates(rows)
    assert [candidate["cause"] for candidate in candidates] == [
        "db_pool_wait",
        "db_query",
        "host_memory_pressure",
        "host_io_pressure",
        "host_cpu_pressure",
        "api_route",
    ]
    assert candidates[-1]["route"] == "POST /api/pool/status"


def test_pool_and_query_candidates_require_p95_above_100ms():
    assert rank_candidates([sample(1, pool=(100,), query=(100,))])[0]["cause"] == (
        "synchronous_python"
    )


def test_host_pressure_uses_latest_available_value_per_resource():
    rows = [
        sample(1, psi={"memory": {"some_avg10": 8}, "cpu": {"some_avg10": 30}}),
        sample(2, psi={"memory": {"some_avg10": 1}, "cpu": None}),
    ]
    assert rank_candidates(rows) == [{"cause": "host_cpu_pressure", "some_avg10": 30.0}]


def test_routes_merge_counts_and_choose_slowest_with_at_least_ten_observations():
    routes = {
        "GET /api/tasks": {"latency": hist(*([700] * 5))},
        "GET /api/agents": {"latency": hist(*([200] * 6))},
        "GET /api/sparse": {"latency": hist(*([5000] * 4))},
    }
    candidates = rank_candidates([sample(1, routes=routes), sample(2, routes=routes)])
    assert candidates == [{"cause": "api_route", "route": "GET /api/tasks", "p95_ms": 975.0}]


def test_sparse_routes_do_not_displace_the_fallback():
    candidates = rank_candidates(
        [sample(1, routes={"GET /api/tasks": {"latency": hist(*([900] * 9))}})]
    )
    assert candidates[0]["cause"] == "synchronous_python"
