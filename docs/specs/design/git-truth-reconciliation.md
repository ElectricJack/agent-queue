# Git-derived integration projections

This implements revision 4 decisions 3 and 5 of the fast smooth integration
specification (2026-10-20). `IntegrationService` invokes one bounded
`integration_reconcile_git_truth` command each pass, even when no train writer
is selected. Only the daemon service principal may invoke this command.

Git is authoritative for source containment, materialized branch heads and
worktree attachment. Database rows still bind repository/task identities,
human decisions and live writers; branch-name conventions cannot reconstruct
those identities. Reconciliation never starts a writer or overrides a hold.

The reconciler walks completed and archived tasks, stopped owners, orphan
checkpoints, origin records and managed slots using separate keyset cursors. Cursors advance past
unknown facts and individual failures so an unavailable repository cannot
starve later rows. A restart safely revisits rows. The service bounds each
pass and prevents overlapping instances. Each source has a 60-second budget;
source order rotates so repeated timeouts cannot starve later sources.

Every completed, failed or cancelled pass emits one structured
`git truth reconciliation timing` log with `git_truth_timing`. Its phase entries
include elapsed seconds, outcome, repository/project fan-out, Git proof counters,
SQL statement and pool-checkout time, graph-lock wait/hold time, shared-observation
waits, snapshot-lock waits, and Git operations (including authenticated network
operations and their credential cleanup). `wait.git_read_budget_scope` measures
the inclusive bounded read, not a separate queue. These timings overlap and must
not be summed as disjoint wall time; shared producers can run in another caller's
context. Work that outlives a cancelled phase cannot change its recorded profile.
Diagnostics record no SQL text, parameters, command arguments or credentials,
and do not change admission, budgets, cursor order or repair decisions.

For a completed source, the shared Git observer proves containment at the
configured final or promotion-chain target. Containment at just the immediate
parent is insufficient to retire a child's collection inputs. Before changing
records, reconciliation publishes an immutable witness under
`refs/aq/provenance/delivery/`. Its metadata names the exact task generation,
source, source base, observed target and proof method. Its parent objects
retain the source, base and target across branch deletion, squashing and Git
garbage collection. Publication requires an absence lease or an identical
existing object, followed by remote readback.

An idempotent receipt records this historical observation and the first time
AQ observed it. This timestamp is explicitly **not** an invented original
landing time. The witness and receipt are historical evidence; current
delivery decisions still evaluate Git against the current target.

When that exact receipt already exists and no live origin, checkpoint or
unreleased owner remains for its task and repository, reconciliation has no
mutation to make. A single SQL existence probe ends that visit before remote
freshness and writer-lock checks. Newly created projections are revisited on
the next pass; every actual repair retains the checks below.

After rechecking task identity, completion, target freshness and writer
exclusion, the reconciler:

- retires the live origin reservation while preserving its immutable filing
  base and history;
- removes the terminal working checkpoint, recording its complete prior value
  in the reconciliation event;
- releases detached worker/repair reservations with a fresh legacy fence,
  only when their branch has no later remote work or competing lease/mutation.

Origins remain usable as historical source-base and promotion-target evidence
after retirement. An active parent episode, session, workspace holder or
integration operation keeps its checkpoint. Undelivered or unknown sources
keep their records. Existing provider-backed owner recovery handles attached
writers and preserves unpublished work before releasing them.

For a deleted task, an orphan checkpoint's exact source and verification
objects, plus origin bases, are retained under content-addressed
`refs/aq/provenance/retained/` refs before removing the checkpoint. This does
not create delivery evidence. Missing objects remain an explicit retryable
gap; a task name or missing branch never proves containment. Deleted tasks
without checkpoints have their origin base and any observed branch head
retained before the origin reservation is retired.

For a surviving task with origin drift, the actual published branch must
contain the immutable filing base before the reconciler repairs its branch
name/materialization flags. It rechecks task and origin identity under the
project lock and does not alter the filing base or task state. Materialized
identities remain immutable: retain the observed objects, retire the old row,
and insert the corrected live projection while preserving the original history.

Sentinels are derived from registered Git worktrees, scoped by their managed
workspace/base rows. An idle slot's stale identity and branch are repaired
under the workspace lock. Live sessions (including alternate spellings of
the same path), job pins and attached owners exclude repair. The reconciler
does not reset a worktree, delete an index, abort a merge or discard dirty work.

Regression coverage includes the retrospective populations (597 origins,
1,274 working checkpoints and 326 reserved owners), stale observation and
publication races, orphan history, source-base survival after retirement, and
real Git squash/deletion/GC histories.
