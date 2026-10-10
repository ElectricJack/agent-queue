# Explicit branch retirement

An archive, abandon, cancel, failed close, superseded task or aborted batch
retires its private branches. Protected branches (`main`, `dev`, `staging`,
the repository default, `gh-pages` and every configured promotion target)
remain. Aborting a batch retires only its candidate; its sources still own
their work. A retry, provider failure or temporary pause is not abandonment.

The lifecycle transaction records a `branch_retirements` intent for each named
branch, with no task foreign key so it survives archive/delete. Requests are
idempotent within a decision. The daemon drains these intents after restart.
Batch aborts and supersedes queue both private candidates in that same transaction.
Before deleting an observed local or remote tip it commits its name, SHA,
location and verified backup bundle to the audit row. Unmerged commits remain
restorable from bundles, rather than surviving as branches.

Deletion holds the existing per-ref exclusion, rechecks task generation,
live sessions, owners, active subjects/batches and attached worktrees, and
uses the observed SHA as an exact lease. Reopened or re-claimed work invalidates
an older decision. A moved head is a conflict; transport failures and temporarily
held branches retry with backoff. The audit is retained after successful removal.
Local removal visits registered AQ checkout common directories for the same
repository and the retained integration and publisher stores. It never detaches
another writer. It removes audited `origin` tracking refs alongside local heads,
so deleted remote branches do not remain visible as stale local observations.

Explicit materialized-origin deletes use the same retirement mechanism. Ordinary
delete with `branches=keep` remains an explicit preservation choice. Archive's
existing delivery and live-session admission guards remain required; admitted
archives now retire their branches instead of leaving orphan refs indefinitely.

The earlier `branch_deletion_audit` schema and revision remain as history.
Pending, held or failed rows are imported once into retirement intents; their recorded
SHA stays a deletion precondition, and the old audit is settled on confirmation.

## Daily backstop

While Git-first integration is active, the orchestrator runs the backstop once
per UTC day for every registered project, including paused and archived projects.
Persisted daily project markers survive daemon restarts. A failed supervisor
report leaves that project unmarked so reporting can retry without rerunning
projects whose reports were accepted.

The backstop replays landing cleanup and recovers missed terminal/abandoned
retirement decisions through `BranchRetirementService`, including accepted
archives whose branch retirement was missed. It uses that service's
durable SHA audit and verified backup bundles for unique abandoned work. Ordinary
completed work without a landing proof stays held. Live, paused or waiting tasks
never become retirement candidates merely because an old abandonment marker exists.

Registered workspace, repository base, integration and publisher checkout common
directories are visited once. The ordinary sweep deletes only tips Git proves
contained or cleanly patch-equivalent to fetched `dev`, `main` or the repository
default branch. Each deletion records its exact SHA and proof in the project's
daily TSV before mutation, and uses an expected-old-SHA lease. Before each delete,
the sweep holds the shared per-ref exclusion, locks associated task rows and
rechecks live references, unreleased owners, configured flow targets and attached
worktrees. Unknown repository identities are skipped. Unique unmerged refs and
stashes remain intact.

Legacy provenance branches use the existing verified copy-before-delete migration
under those same live-reference guards, with a pre-delete audit entry. Invalid or
conflicting provenance remains blocked. The operator cleanup script shares the
Git proof and per-ref safety implementation; its default remains a dry run.

Each project's supervisor receives its own inbox report through `CommandHandler`,
with local/remote deletion and hold counts, retirement and migration outcomes,
unique refs, stash/registration counts and failures. Cleanup failures in one project
do not suppress another project's report.

## Re-landed content

Salvage, repair and squash re-landings put a task's content on the default
branch under different SHAs, so neither ancestry nor patch-id proves the source
branch merged. `scripts/backfill-legacy-deliveries.py PROJECT --relanded
TASK_ID=SHA --reason REASON --apply` (`record_relanding` in
`src/integration/legacy_backfill.py`) records that landing commit against the
source task. The task must be COMPLETED and of the project, the SHA must be a
full, fetched object id the default branch tip reaches, and a task whose own
source is already there is refused (the ordinary backfill records that proof).
It writes a `superseded` row in `integration_legacy_deliveries`, which the
delivery truth honours as landed, and in the same transaction queues the task's
branches for retirement with that proof as the reason, so each unmerged tip is
bundled before it is deleted. The default is a dry run.

## Archive dispositions

Archiving or abandoning COMPLETED work the default branch has not received names
what happens to it: `aq task archive TASK --disposition deliver|obsolete|retire
--reason REASON`. A bare `--abandon-undelivered` is refused with
`hierarchy.disposition_required`, and the `integration_undelivered` refusal names
the three choices.

* `deliver` keeps the task, records the decision as a comment and a
  `task.delivery_disposition` event, and returns the undelivered holders plus,
  for a train root, the authorize-root dry run, so the operator sees exactly what
  its delivery waits on.
* `obsolete` archives with the reason as the note; `retire` archives. Both imply
  `--abandon-undelivered`.

The decision is written on the abandon comment and on every retirement the archive
queues (`task archived (<disposition>): <note>`), so the audited bundle-then-delete
path carries it. Remove records `obsolete`.

## Completed but undelivered

COMPLETED means the worker finished, not that the code reached the default branch.
`aq doctor --check integration.completed_undelivered` reconciles the two for every
project in a delivering mode (`hierarchy`, `train`, `development`) with a
repository: for each COMPLETED task, live or archived, whose origin branch (or
`-wip` sibling) the default branch cannot reach, it names the rule that accounts
for it (`completed_branch_dispositions` in `src/integration/delivery_branches.py`):

| Rule | Meaning | Finding |
|---|---|---|
| `retiring` | a branch retirement is pending | no |
| `retirement_conflict` | the retirement stopped on a moved head | yes |
| `carried` | a live batch has the task as a member | no |
| `recorded` | a legacy-delivery row records its delivery, re-landing or abandonment | no |
| `delivered` | the delivery truth proves the work landed | no |
| `integrated` | an ancestor task's origin branch contains the tip | no |
| `awaiting_delivery` | a root with a pull request, completed under 24 hours ago | no |
| `stranded_root` | the same root, older than that | yes |
| `awaiting_backstop` | archived under 48 hours ago; the daily sweep queues its retirement | no |
| `unreconciled` | nothing on record explains it | yes |

Findings make the check WARN and carry the remedy: the archive disposition, the
`--relanded` proof, or, for an archived task, the backstop failure in the daemon
log. A tip whose reachability cannot be read is reported as unknown, never as a
finding. The supervisor sees the same read in three places: the doctor check, the
`stall.sweep` patrol (a `completed_undelivered` finding per entry, under a 30-second
budget) and `aq task explain` / the dashboard task view (a `completed_undelivered`
blocker on a COMPLETED task no batch carries, from the cached snapshot).
