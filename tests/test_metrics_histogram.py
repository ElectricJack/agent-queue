"""Fixed-bucket histograms: placement, merge by addition, derived percentiles."""

from __future__ import annotations

import math

import pytest

from src.metrics.histogram import (
    BOUNDS_MS,
    count_over,
    is_hist,
    is_sum,
    merge_hists,
    merge_sums,
    new_hist,
    new_sum,
    observe,
    percentile,
)
from src.metrics.sampler import aggregate_samples


def test_bounds_are_ascending_and_the_open_bucket_exists():
    assert list(BOUNDS_MS) == sorted(BOUNDS_MS)
    assert len(new_hist()["counts"]) == len(BOUNDS_MS) + 1


@pytest.mark.parametrize(
    "value, bucket",
    [(0.0, 0), (1.0, 0), (1.01, 1), (500.0, 8), (500.5, 9), (10000.0, 12), (10001.0, 13)],
)
def test_observe_places_a_value_in_the_half_open_bucket(value, bucket):
    h = new_hist()
    observe(h, value)
    assert h["counts"][bucket] == 1
    assert h["count"] == 1 and h["sum"] == value and h["max"] == value


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "12"])
def test_observe_ignores_unusable_values(bad):
    h = new_hist()
    observe(h, bad)  # type: ignore[arg-type]
    assert h["count"] == 0


def test_merge_adds_counts_and_keeps_the_largest_max():
    a, b = new_hist(), new_hist()
    for v in (3, 30, 300):
        observe(a, v)
    for v in (7, 700):
        observe(b, v)
    merged = merge_hists([a, b, {"kind": "not-a-hist"}, None])
    assert merged["count"] == 5 and merged["sum"] == 1040 and merged["max"] == 700
    assert sum(merged["counts"]) == 5
    assert is_hist(merged)


def test_percentile_is_derived_from_buckets_not_averaged():
    h = new_hist()
    for _ in range(95):
        observe(h, 15)  # (10, 20]
    for _ in range(5):
        observe(h, 1500)  # (1000, 2000]
    p95 = percentile(h, 0.95)
    assert 10 <= p95 <= 20
    assert percentile(h, 0.99) > 1000
    assert percentile(new_hist(), 0.5) is None


def test_count_over_counts_only_strictly_above_the_threshold():
    h = new_hist()
    for v in (499, 500, 501, 900, 5000):
        observe(h, v)
    assert count_over(h, 500) == 3


def test_sum_counters_merge_by_addition():
    merged = merge_sums([new_sum(a=1, b=2), new_sum(a=3), {"kind": "sum", "c": 1.5}])
    assert merged == {"kind": "sum", "a": 4, "b": 2, "c": 1.5}
    assert is_sum(merged) and not is_sum({"a": 1})


def test_aggregate_samples_merges_hist_and_sum_leaves_and_still_averages_gauges():
    first = {"perf": {"loop": {"drift": new_hist()}, "api": {"errors": new_sum(x=1)}, "g": 2}}
    second = {"perf": {"loop": {"drift": new_hist()}, "api": {"errors": new_sum(x=2)}, "g": 4}}
    observe(first["perf"]["loop"]["drift"], 10)
    observe(second["perf"]["loop"]["drift"], 1000)
    # A sample without a perf block (probes disabled that second) must not zero anything.
    out = aggregate_samples([first, second, {"g": 1}])
    assert out["perf"]["loop"]["drift"]["count"] == 2
    assert out["perf"]["loop"]["drift"]["max"] == 1000
    assert out["perf"]["api"]["errors"] == {"kind": "sum", "x": 3}
    assert math.isclose(out["perf"]["g"], 3.0)
