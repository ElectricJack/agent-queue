"""Fleet metrics: the sampler that feeds the dashboard's Metrics tab."""

from src.metrics.histogram import (
    BOUNDS_MS,
    HIST_KIND,
    SUM_KIND,
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
from src.metrics.perf import (
    OVERFLOW_LABEL,
    ROUTE_LIMIT,
    LoopLagProbe,
    PerfRegistry,
    install_registry,
    perf_registry,
)
from src.metrics.sampler import (
    METRIC_TICK_EVENT,
    RESOLUTIONS,
    MetricsSampler,
    aggregate_samples,
    floor_bucket,
    read_machine,
)

__all__ = [
    "BOUNDS_MS",
    "HIST_KIND",
    "METRIC_TICK_EVENT",
    "OVERFLOW_LABEL",
    "RESOLUTIONS",
    "ROUTE_LIMIT",
    "SUM_KIND",
    "LoopLagProbe",
    "MetricsSampler",
    "PerfRegistry",
    "aggregate_samples",
    "count_over",
    "floor_bucket",
    "install_registry",
    "is_hist",
    "is_sum",
    "merge_hists",
    "merge_sums",
    "new_hist",
    "new_sum",
    "observe",
    "percentile",
    "perf_registry",
    "read_machine",
]
