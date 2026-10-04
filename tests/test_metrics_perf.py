"""The perf registry never raises, bounds its labels, and the probe sees a stall."""

from __future__ import annotations

import asyncio
import time
import threading

import pytest

from src.config import AppConfig, MetricsConfig
from src.metrics.histogram import percentile
from src.metrics.loop_watchdog import LoopWatchdog
from src.metrics.perf import (
    OVERFLOW_LABEL,
    ROUTE_LIMIT,
    LoopLagProbe,
    PerfRegistry,
    install_registry,
    perf_registry,
)

PANE = "GET /api/sessions/{session_id}/pane"


def test_snapshot_returns_fragments_and_resets_counters_but_not_gauges():
    reg = PerfRegistry()
    reg.observe_route("GET /api/x", 12.0, 200)
    reg.observe_route("GET /api/x", 700.0, 503)
    reg.observe_stream_handshake(PANE, 5.0, accepted=True)
    reg.stream_opened(PANE)
    reg.observe_pool_wait(3.0)
    reg.observe_query(250.0)
    first = reg.snapshot()
    route = first["api"]["routes"]["GET /api/x"]
    assert route["latency"]["count"] == 2
    assert route["status"] == {"kind": "sum", "2xx": 1, "5xx": 1}
    assert first["api"]["all"]["count"] == 2
    stream = first["api"]["streams"][PANE]
    assert stream["handshake"]["count"] == 1 and stream["open"] == 1
    assert stream["outcome"] == {"kind": "sum", "accepted": 1, "refused": 0}
    assert first["db"]["pool_wait"]["count"] == 1
    assert first["db"]["counters"]["slow_queries"] == 1  # 250 ms > default 100 ms
    second = reg.snapshot()
    assert second["api"]["all"]["count"] == 0
    assert second["api"]["routes"] == {}
    assert second["db"]["query"]["count"] == 0
    assert second["db"]["counters"] == {"kind": "sum", "slow_queries": 0, "pool_timeouts": 0}
    kept = second["api"]["streams"][PANE]
    assert kept["open"] == 1 and kept["handshake"]["count"] == 0
    assert kept["outcome"] == {"kind": "sum", "accepted": 0, "refused": 0}


def test_errors_timeouts_and_refused_handshakes_are_counted():
    reg = PerfRegistry()
    reg.observe_exception("GET /api/x")
    reg.observe_unmatched()
    reg.observe_unmatched()
    reg.observe_pool_timeout()
    reg.observe_stream_handshake("WS /ws/events", 2.0, accepted=False)
    snap = reg.snapshot()
    assert snap["api"]["errors"] == {"kind": "sum", "exceptions": 1, "unmatched": 2}
    assert snap["db"]["counters"]["pool_timeouts"] == 1
    assert snap["api"]["streams"]["WS /ws/events"]["outcome"]["refused"] == 1


def test_the_slow_query_threshold_is_settable():
    reg = PerfRegistry()
    reg.slow_query_threshold_ms = 500.0
    reg.observe_query(250.0)
    reg.observe_query(600.0)
    assert reg.snapshot()["db"]["counters"]["slow_queries"] == 1


def test_a_stream_gauge_never_goes_negative():
    reg = PerfRegistry()
    reg.stream_opened(PANE)
    reg.stream_closed(PANE)
    reg.stream_closed(PANE)
    assert reg.snapshot()["api"]["streams"][PANE]["open"] == 0


def test_labels_are_bounded_and_overflow_folds_into_other():
    reg = PerfRegistry()
    for i in range(ROUTE_LIMIT + 10):
        reg.observe_route(f"GET /api/r{i}", 1.0, 200)
    routes = reg.snapshot()["api"]["routes"]
    assert len(routes) == ROUTE_LIMIT + 1
    assert routes[OVERFLOW_LABEL]["latency"]["count"] == 10


def test_stream_labels_are_bounded_too():
    reg = PerfRegistry()
    for i in range(ROUTE_LIMIT + 3):
        reg.stream_opened(f"GET /api/s{i}")
    streams = reg.snapshot()["api"]["streams"]
    assert len(streams) == ROUTE_LIMIT + 1
    assert streams[OVERFLOW_LABEL]["open"] == 3


@pytest.mark.parametrize("call", [
    lambda r: r.observe_route(None, "x", "y"),
    lambda r: r.observe_loop_drift(float("nan")),
    lambda r: r.observe_stream_handshake(123, -1, accepted="no"),
    lambda r: r.stream_closed("never-opened"),
    lambda r: r.observe_query(object()),
    lambda r: r.observe_pool_wait("slow"),
    lambda r: r.observe_route("GET /api/x", float("inf"), 200),
])
def test_bad_arguments_never_raise(call):
    reg = PerfRegistry()
    call(reg)
    snap = reg.snapshot()
    assert snap["api"]["all"]["count"] == 0
    assert snap["api"]["routes"] == {} and snap["api"]["streams"] == {}
    assert snap["db"]["query"]["count"] == 0 and snap["db"]["pool_wait"]["count"] == 0


def test_a_disabled_registry_records_nothing_but_still_closes_streams():
    reg = PerfRegistry()
    reg.stream_opened(PANE)
    reg.enabled = False
    reg.observe_route("GET /api/x", 12.0, 200)
    reg.observe_loop_drift(5.0)
    reg.observe_query(250.0)
    reg.observe_pool_wait(3.0)
    reg.observe_pool_timeout()
    reg.observe_exception("GET /api/x")
    reg.observe_unmatched()
    reg.observe_stream_handshake(PANE, 5.0, accepted=True)
    reg.stream_opened(PANE)
    reg.stream_closed(PANE)
    snap = reg.snapshot()
    assert snap["api"]["all"]["count"] == 0 and snap["api"]["routes"] == {}
    assert snap["loop"]["drift"]["count"] == 0
    assert snap["db"]["query"]["count"] == 0 and snap["db"]["pool_wait"]["count"] == 0
    assert snap["db"]["counters"]["pool_timeouts"] == 0
    assert snap["api"]["errors"] == {"kind": "sum", "exceptions": 0, "unmatched": 0}
    assert snap["api"]["streams"][PANE]["open"] == 0


async def test_the_probe_records_a_blocked_loop():
    reg = PerfRegistry()
    probe = LoopLagProbe(reg, interval_ms=20)
    probe.start()
    await asyncio.sleep(0.06)
    time.sleep(0.3)  # noqa: ASYNC251 - block the loop: the next wake-up is ~300 ms late
    await asyncio.sleep(0.06)
    await probe.stop()
    loop = reg.snapshot()["loop"]
    assert loop["probe_interval_ms"] == 20
    assert loop["drift"]["max"] >= 250
    assert percentile(loop["drift"], 0.5) < 50


async def test_the_probe_measures_against_its_injected_clock():
    now = [0.0]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds + (0.4 if len(slept) == 2 else 0.0)
        if len(slept) >= 3:
            await asyncio.Event().wait()  # park until stop() cancels

    reg = PerfRegistry()
    probe = LoopLagProbe(reg, interval_ms=100, sleep=fake_sleep, clock=lambda: now[0])
    probe.start()
    for _ in range(10):
        await asyncio.sleep(0)
    await probe.stop()
    drift = reg.snapshot()["loop"]["drift"]
    assert slept[:3] == [0.1, 0.1, 0.1]
    assert drift["count"] == 2
    assert drift["max"] == pytest.approx(400.0)


async def test_stop_cancels_the_task_and_is_idempotent():
    probe = LoopLagProbe(PerfRegistry(), interval_ms=10)
    probe.start()
    task = probe._task
    watchdog = probe._watchdog
    probe.start()  # a second start does not spawn a second probe
    assert probe._task is task
    await probe.stop()
    await probe.stop()
    assert probe._task is None
    assert task.done()
    assert not watchdog._thread.is_alive()


def test_watchdog_samples_code_locations_and_rate_limits_warnings(monkeypatch, caplog):
    now = [0.0]
    watchdog = LoopWatchdog(threading.get_ident(), clock=lambda: now[0])
    watchdog.touch(0.1)
    assert "test_watchdog_samples_code_locations" in " ".join(watchdog._stack())
    monkeypatch.setattr(watchdog, "_stack", lambda: ("codex.py:581 (_read_new_sync)",))
    watchdog.poll()
    assert not caplog.records
    now[0] = 2.0
    watchdog.poll()
    for _ in range(20):
        watchdog.poll()
    assert len(caplog.records) == 1
    assert "codex.py:581" in caplog.text and watchdog._samples == 21
    now[0] = 32.0
    watchdog.poll()
    assert len(caplog.records) == 2
    watchdog.touch(0.1)
    watchdog.poll()
    assert "recovered" in caplog.text and "22 samples" in caplog.text
    assert not watchdog._stacks and watchdog._samples == 0


def test_watchdog_stack_storage_is_bounded(monkeypatch):
    now = [2.0]
    watchdog = LoopWatchdog(threading.get_ident(), clock=lambda: now[0])
    watchdog._deadline = 0.0
    stack = ["location-0"]
    monkeypatch.setattr(watchdog, "_stack", lambda: tuple(stack))
    for i in range(200):
        stack[0] = f"location-{i}"
        watchdog.poll()
    assert len(watchdog._stacks) <= 64
    assert sum(watchdog._stacks.values()) == 200


def test_watchdog_thread_samples_while_the_loop_thread_is_blocked(monkeypatch):
    sampled = threading.Event()
    loop_thread = threading.get_ident()
    watchdog = LoopWatchdog(loop_thread, threshold=0.01)
    original = watchdog._stack
    observations = []

    def sample():
        observations.append((threading.get_ident(), original()))
        sampled.set()
        return observations[-1][1]

    monkeypatch.setattr(watchdog, "_stack", sample)
    watchdog.start()
    try:
        assert sampled.wait(5), "watchdog must sample without a loop callback"
    finally:
        watchdog.stop()
    assert observations[0][0] != loop_thread
    assert "test_watchdog_thread_samples" in " ".join(observations[0][1])
    assert not watchdog._thread.is_alive()


async def test_a_disabled_probe_does_not_start_a_stack_sampler():
    registry = PerfRegistry()
    registry.enabled = False
    probe = LoopLagProbe(registry)
    probe.start()
    try:
        assert probe._watchdog is None
    finally:
        await probe.stop()


async def test_stop_before_start_is_a_no_op():
    probe = LoopLagProbe(PerfRegistry())
    await probe.stop()
    assert probe._task is None


def test_the_global_registry_is_a_singleton_that_tests_can_replace():
    original = perf_registry()
    assert perf_registry() is original
    fake = PerfRegistry()
    install_registry(fake)
    try:
        assert perf_registry() is fake
    finally:
        install_registry(None)
    assert perf_registry() is not fake


def test_config_defaults_and_validation():
    cfg = AppConfig().metrics
    assert cfg.perf_enabled is True and cfg.perf_loop_probe_ms == 100
    assert cfg.perf_slow_query_ms == 100.0 and cfg.perf_host_budget_ms == 20.0
    assert cfg.perf_relay_poll_seconds == 5.0
    assert cfg.validate() == []
    bad = MetricsConfig(perf_loop_probe_ms=5, perf_slow_query_ms=0, perf_relay_poll_seconds=0.1)
    fields = {e.field for e in bad.validate()}
    assert {"perf_loop_probe_ms", "perf_slow_query_ms", "perf_relay_poll_seconds"} <= fields
    assert "perf_host_budget_ms" in {
        e.field for e in MetricsConfig(perf_host_budget_ms=-1).validate()
    }
    assert "perf_loop_probe_ms" in {
        e.field for e in MetricsConfig(perf_loop_probe_ms=1001).validate()
    }
