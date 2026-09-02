"""Engine constants (spatial-layout design §3.2, §4.2, §4.3)."""

from __future__ import annotations

CARD_W = 1.0
CARD_H = 1.0
SIBLING_GAP = 0.2
LINE_GAP = 0.3
PADDING = 0.15
HEADER_H = 0.35
TARGET_ROW_WIDTH = 4.0
TARGET_ROW_WIDTH_ROOT = 6.0
GROWTH_BANDS = (1.5, 3.0, 6.0, 12.0, 24.0, 48.0)
CELL_SIZE = 8.0

W_CROSS = 10.0
W_SPAN = 1.0
W_WRAP = 2.0
W_SLACK = 0.5

MAX_OPTIMIZED_SIBLINGS = 500
INCREMENTAL_EVALS = 200
# Wall-clock safety valve only (never used to decide layout — that would
# break determinism). 10x the nominal per-eval budget so it never trips
# under normal operation; it only guards against a pathological case where
# eval count doesn't bound wall time.
INCREMENTAL_SECONDS = 0.5
TIDY_EVALS = 5000
TIDY_SECONDS = 20.0
TIDY_JOB_SECONDS = 60.0

FINISHED_STATUSES = frozenset({"COMPLETED", "CANCELED", "CANCELLED", "SKIPPED"})
RUNNING_STATUSES = frozenset({"ASSIGNED", "IN_PROGRESS"})
RANKING_DEP_TYPES = frozenset({"blocks", "waits-for", "conditional-blocks"})
VARIANTS = ("all", "active")
ROOT = "__root__"


def band_up(size: float) -> float:
    """Round a content size up to the next growth band (§3.4)."""
    for b in GROWTH_BANDS:
        if size <= b:
            return b
    b = GROWTH_BANDS[-1]
    while b < size:
        b *= 2
    return b
