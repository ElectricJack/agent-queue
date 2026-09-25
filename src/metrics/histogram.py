"""Fixed-bucket latency histograms that merge by addition.

The roll-up averages gauges (``aggregate_samples``); a percentile cannot be
averaged, so a latency series is stored as bucket counts and the percentile
is derived wherever it is read (spec 2026-09-24 dashboard performance §4.1).
Bounds are milliseconds and fixed for every series, so any two histograms in
the store merge without inspecting each other's edges.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from typing import Any

HIST_KIND = "hist"
SUM_KIND = "sum"

#: Upper edges in milliseconds.  Bucket ``i`` is ``(BOUNDS_MS[i-1], BOUNDS_MS[i]]``;
#: the last bucket is ``(BOUNDS_MS[-1], +inf)``.
BOUNDS_MS: tuple[float, ...] = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)


def new_hist() -> dict[str, Any]:
    return {"kind": HIST_KIND, "counts": [0] * (len(BOUNDS_MS) + 1), "count": 0, "sum": 0.0, "max": 0.0}


def observe(hist: dict[str, Any], value_ms: Any) -> None:
    if isinstance(value_ms, bool) or not isinstance(value_ms, (int, float)):
        return
    value = float(value_ms)
    if not math.isfinite(value) or value < 0:
        return
    index = bisect_left(BOUNDS_MS, value)
    hist["counts"][index] += 1
    hist["count"] += 1
    hist["sum"] += value
    hist["max"] = max(hist["max"], value)


def is_hist(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("kind") == HIST_KIND
        and isinstance(value.get("counts"), list)
        and len(value["counts"]) == len(BOUNDS_MS) + 1
    )


def merge_hists(hists: Iterable[Any]) -> dict[str, Any]:
    out = new_hist()
    for hist in hists:
        if not is_hist(hist):
            continue
        for i, count in enumerate(hist["counts"]):
            out["counts"][i] += count
        out["count"] += hist.get("count", 0)
        out["sum"] += hist.get("sum", 0.0)
        out["max"] = max(out["max"], float(hist.get("max", 0.0) or 0.0))
    return out


def percentile(hist: Mapping[str, Any], q: float) -> float | None:
    if not is_hist(hist) or hist["count"] <= 0:
        return None
    target = q * hist["count"]
    seen = 0
    for i, count in enumerate(hist["counts"]):
        if count <= 0:
            continue
        if seen + count >= target:
            lower = BOUNDS_MS[i - 1] if i > 0 else 0.0
            upper = BOUNDS_MS[i] if i < len(BOUNDS_MS) else max(float(hist["max"]), lower)
            fraction = (target - seen) / count
            return lower + (upper - lower) * min(1.0, max(0.0, fraction))
        seen += count
    return float(hist["max"])


def count_over(hist: Mapping[str, Any], threshold_ms: float) -> int:
    if not is_hist(hist):
        return 0
    total = 0
    for i, count in enumerate(hist["counts"]):
        lower = BOUNDS_MS[i - 1] if i > 0 else 0.0
        if lower >= threshold_ms:
            total += count
    return total


def new_sum(**counters: float) -> dict[str, Any]:
    return {"kind": SUM_KIND, **counters}


def is_sum(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("kind") == SUM_KIND


def merge_sums(sums: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": SUM_KIND}
    for entry in sums:
        if not is_sum(entry):
            continue
        for key, value in entry.items():
            if key == "kind" or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            out[key] = out.get(key, 0) + value
    return out
