"""Read-only performance checks using retained one-second metrics.

Sustained loop lag names the observation window and ranked candidate causes,
never a diagnosis (dashboard performance spec 2026-09-24 §4.1 and §5).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.metrics.histogram import count_over, is_hist, merge_hists, percentile

OWNER = "dashboard-performance"
CHECK_ID = "perf.sustained_loop_lag"
WINDOW_SECONDS = 180
THRESHOLD_MS = 500.0
MIN_SAMPLES = 120
_ROUTE_MIN_COUNT = 10


def _at(row: dict, *path: str):
    """Read a perf leaf, tolerating older samples and unavailable sections."""
    node = row.get("perf")
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def _merged(rows: list[dict], *path: str) -> dict:
    return merge_hists(_at(row, *path) for row in rows)


def _latest_psi(rows: list[dict], resource: str) -> float | None:
    for row in reversed(rows):
        value = _at(row, "host", "psi", resource, "some_avg10")
        if value is not None:
            return float(value)
    return None


def rank_candidates(rows: list[dict]) -> list[dict]:
    """Rank measured evidence in the same window; rows are in ascending time order."""
    candidates: list[dict] = []
    for metric, cause in (("pool_wait", "db_pool_wait"), ("query", "db_query")):
        p95 = percentile(_merged(rows, "db", metric), 0.95)
        if p95 is not None and p95 > 100:
            candidates.append({"cause": cause, "p95_ms": round(p95, 1)})

    for resource, floor, cause in (
        ("memory", 5.0, "host_memory_pressure"),
        ("io", 20.0, "host_io_pressure"),
        ("cpu", 20.0, "host_cpu_pressure"),
    ):
        value = _latest_psi(rows, resource)
        if value is not None and value >= floor:
            candidates.append({"cause": cause, "some_avg10": value})

    routes: dict[str, list[dict]] = {}
    for row in rows:
        entries = _at(row, "api", "routes")
        if not isinstance(entries, dict):
            continue
        for label, entry in entries.items():
            if isinstance(entry, dict) and is_hist(entry.get("latency")):
                routes.setdefault(label, []).append(entry["latency"])

    slowest: tuple[float, str] | None = None
    for label, hists in routes.items():
        merged = merge_hists(hists)
        if merged["count"] < _ROUTE_MIN_COUNT:
            continue
        p95 = percentile(merged, 0.95)
        if p95 is not None and (slowest is None or p95 > slowest[0]):
            slowest = (p95, label)
    if slowest is not None:
        candidates.append(
            {"cause": "api_route", "route": slowest[1], "p95_ms": round(slowest[0], 1)}
        )

    if not candidates:
        candidates.append(
            {
                "cause": "synchronous_python",
                "hint": "loop lag with low or unavailable pool wait, query time and host pressure: "
                "profile synchronous Python spans (spec §5)",
            }
        )
    return candidates


async def _check_sustained_loop_lag(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(id=CHECK_ID, severity=Severity.INFO, detail="no database handle")

    now = time.time()
    rows = await ctx.db.read_metrics_samples(
        "1s", now - WINDOW_SECONDS, now, limit=WINDOW_SECONDS + 5
    )
    if not rows:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail=f"no 1s samples in the last {WINDOW_SECONDS}s (sampler off or daemon just started)",
        )
    if _at(rows[-1], "enabled") is False:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="performance probes are off (metrics.perf_enabled = false)",
        )

    # A histogram with no observations cannot establish whether the loop is healthy.
    loop_rows = [
        row
        for row in rows
        if is_hist(drift := _at(row, "loop", "drift")) and drift.get("count", 0) > 0
    ]
    data = {"window_seconds": WINDOW_SECONDS, "samples": len(loop_rows)}
    if len(loop_rows) < MIN_SAMPLES:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail=f"only {len(loop_rows)} of {MIN_SAMPLES} loop-lag samples in the window",
            data=data,
        )

    merged = _merged(loop_rows, "loop", "drift")
    p95 = percentile(merged, 0.95)
    data.update(
        {
            "p95_ms": round(p95, 1),
            "max_ms": round(merged["max"], 1),
            "stalls_over_500ms": count_over(merged, THRESHOLD_MS),
        }
    )
    if p95 <= THRESHOLD_MS:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.OK,
            detail=f"event-loop lag p95 {p95:.0f} ms over the last {WINDOW_SECONDS}s",
            data=data,
        )

    since = loop_rows[0]["ts"]
    candidates = rank_candidates(rows)
    data.update({"since_ts": since, "candidates": candidates})
    when = datetime.fromtimestamp(since, tz=UTC).strftime("%H:%M:%SZ")
    names = ", ".join(candidate["cause"] for candidate in candidates)
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"event-loop lag p95 {p95:.0f} ms sustained since {when} "
            f"({data['stalls_over_500ms']} stalls over {THRESHOLD_MS:.0f} ms); "
            f"candidate causes, not a diagnosis: {names}"
        ),
        data=data,
    )


def perf_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id=CHECK_ID, run=_check_sustained_loop_lag, owner=OWNER, timeout_s=10.0)]


CHECKS = {check.id: check for check in perf_checks()}


async def run_check(check_id: str, ctx: DoctorContext, *, repair: bool = False) -> CheckResult:
    """Run a performance check; repair is accepted but no fix is available."""
    return await CHECKS[check_id].run(ctx)
