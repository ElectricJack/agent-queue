# Completed-source development conflict repair

Tasks: `bright-flare`, 2026-09-26; `wise-bridge`, `solid-horizon`, 2026-09-27.

A development publisher merge conflict on a completed source must journal the
exact source revision and conflicting paths, then create or reuse one bounded
repair task for that manifest. Independent work may continue. Dispatch resumes
from parked rows after a daemon restart.

The repair names each source branch and the repository's configured target. In
its own task branch, which starts from that target, it merges each listed source
revision by its exact SHA, resolves the named conflicts, publishes that branch
and closes with checks. It does not rebase, squash or cherry-pick, so every
source revision stays an ancestor of the repair and delivering the repair
delivers the source. A generated file that conflicts is regenerated from the
merged sources, not hand-merged. The original source revision remains the repair
contract. A repair that rewrote the commits anyway is still accepted through its
delivery proof (below).

For a single live original source, place the repair under the completed source
when structural depth permits. At the depth cap, explicitly place it at root,
retain its discovered-from provenance, and make the source block on the repair,
as shared repairs already do. Decide placement under the project hierarchy lock;
never relax the depth bound or silently drop required repair work.

A passing repair close alone does not release source dependents. Only delivery
of the exact passing repair to the configured default branch adopts the parked
source receipt and releases those dependents. Repeated sweeps reuse the repair
identity and preserve existing retry and generation budgets.

## The repair chain

The target can move again before a closed repair is published, so the repair's
own publication can park on a new conflict. That row gets a repair of its own
(generation 2, rooted, discovered-from the first repair), and so on up to three
generations. The source is carried by the whole chain, not by the first repair
alone.

No repair ever `blocks` on another repair. A repaired repair's parked row is what
waits for the next generation's delivery, and the next generation's contract
already names it, so a blocking edge back would be a dependency cycle. Chains
filed before this rule (`solid-horizon`, 2026-09-27) carry those edges. The
publisher supersedes such a cycle with its newest repair when the chain's
contracts, followed repair to repair, name every older member's exact revision.
It publishes that repair onto the current target and lists the older ones as
`superseded_by` it. Before this, only a two-repair cycle qualified, and a
three-repair chain was held every tick.

`repair_chain` in `src/integration/development.py` follows the chain from a
parked row to the repair still carrying it:

* `open`: a repair in the chain is DEFINED, READY, ASSIGNED, IN_PROGRESS,
  WAITING_INPUT or PAUSED;
* `awaiting_publication`: the last repair closed and has not been parked or
  delivered yet;
* `delivered`: the last repair reached the target, so the next sweep adopts the
  row;
* `missing`, `finished`, `loop`: nothing carries the source. Either no repair
  exists for the last parked row, the last repair ended FAILED or BLOCKED, or the
  journal repeats.

Exactly one repair exists per parked manifest:

* `--retry` and `--recover-child` merge parked content again. A repeat conflict
  refreshes the parked row that already names that exact source
  (`evidence.reconflicted_at`) instead of inserting a second row.
* An ended repair is never re-filed automatically.
* When a fourth generation would be needed, the publisher files nothing and
  records the `repair_generation_exhausted` diagnostic on the batch.

A recovery that leaves the child unpublished names the repair carrying its park
and the chain, or says that no repair carries it.

`aq doctor --check integration.development_conflicts_unrepaired` (ERROR, report
only) lists every completed source parked on a merge conflict whose chain has no
open repair. It skips rows younger than ten minutes, rows whose sources are no
longer live COMPLETED tasks, and duplicates of one manifest. A repair's own
conflict row is reported through its original source.

Verify with real Git and private PostgreSQL:

* A completed child at depths two and three conflicts with an advanced target,
  dispatches one repair across repeated sweeps, stays blocked after the rebased
  repair closes, and releases its successor only after the publisher delivers
  the repair and adopts the source.
* A source skipped as `source_parked` keeps one repair and one parked row
  through repeated sweeps and a recovery, and doctor lists it once its repair
  fails.
* A merged repair whose publication conflicts again is carried by its
  generation-2 repair to a delivery that contains the original revision.
* A generation-3 chain reaches the target, whether it was filed with no repair
  blocking on another or with the pre-fix edges, which the newest repair
  supersedes.
* The generation budget records its diagnostic, and doctor lists the batch.
