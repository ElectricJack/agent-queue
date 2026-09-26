# Completed-source development conflict repair

Task: `bright-flare`, 2026-09-26.

A development publisher merge conflict on a completed source must journal the
exact source revision and conflicting paths, then create or reuse one bounded
repair task for that manifest. Independent work may continue. Dispatch resumes
from parked rows after a daemon restart.

The repair names each source branch and the repository's configured target. It
rebases the source changes onto that target in its own task branch, resolves the
named conflicts, publishes that branch and closes with checks. The original
source revision remains the repair contract even when rebasing changes its SHA.

For a single live original source, place the repair under the completed source
when structural depth permits. At the depth cap, explicitly place it at root,
retain its discovered-from provenance, and make the source block on the repair,
as shared repairs already do. Decide placement under the project hierarchy lock;
never relax the depth bound or silently drop required repair work.

A passing repair close alone does not release source dependents. Only delivery
of the exact passing repair to the configured default branch adopts the parked
source receipt and releases those dependents. Repeated sweeps reuse the repair
identity and preserve existing retry and generation budgets.

Verify with real Git and private PostgreSQL: a completed child at depths two
and three conflicts with an advanced target, dispatches one repair across repeated
sweeps, remains blocked after the rebased repair closes, and releases its
successor only after the publisher delivers the repair and adopts the source.
