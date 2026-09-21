"""Engine constants (spatial-layout design §3.2, §4.2, §4.3)."""

from __future__ import annotations

CARD_W = 1.0
CARD_H = 1.0
# Comfortable is deliberately compact enough for ordinary 10–30 card epics
# without crowding arrowheads or container chrome.  The dashboard can scale
# this presentation locally, but these values remain the stable, persisted
# world-coordinate default.
SIBLING_GAP = 0.15
LINE_GAP = 0.22
PADDING = 0.1
HEADER_H = 0.35
# The row-wrap targets are FLOORS, not constants: ``flow.row_target`` widens
# them for a large scope so its aspect ratio stays landscape-ish instead of
# degrading linearly with the child count (reorganisation design §3.1). A
# scope whose ideal target is below its floor keeps today's geometry exactly.
TARGET_ROW_WIDTH = 4.5
TARGET_ROW_WIDTH_ROOT = 7.0
ROW_ASPECT = 1.3
GROWTH_BANDS = (1.5, 3.0, 6.0, 12.0, 24.0, 48.0)
CELL_SIZE = 8.0

ENGINE_RULES_VERSION = 1
"""The generation of the engine's *geometry and ordering* rules.

Bump this by hand in any change to ``flow.py``, to ordinal assignment in
``engine.py``, or to the geometry constants above. Bumping it is the **only**
action a developer takes to roll the change out: geometry is persisted per
project and ``LayoutDriver.reconcile`` deliberately chases presence rather than
geometry, so an install already laid out would otherwise keep the old rules
forever. The orchestrator's sweep re-tidies every ``(project, variant)`` whose
convergence ledger — a ``layout_jobs`` row of kind ``f"rules:{...}"`` — does not
name this version (reorganisation design §3.3).
"""


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
#: Dependency types the canvas actually draws an arrow for. Re-exported by
#: ``view.py`` (its historical home); the driver needs it too, to tell whether
#: a finished container is still somebody's edge endpoint.
DRAWN_TYPES = frozenset({"blocks", "waits-for", "conditional-blocks", "discovered-from"})
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
