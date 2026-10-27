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

A failed close that leaves the task BLOCKED does not dispose of its work,
because a BLOCKED task can be restarted on its branch, and deleting a remote
branch also closes its open pull request as the integration bot. While the task
exists and can still resume (neither COMPLETED nor FAILED, no abandoned work
outcome on its latest close or in its metadata, and no cancel's `stop_task`
terminal mark), its retirement is withdrawn (`task_resumes_on_branch` in
`src/database/queries/task_queries.py`). The branch stays until a later explicit
decision (cancel, abandon, archive, FAILED) or the daily backstop queues it
again. An explicit materialized-origin discard is the human decision, so it is
exempt. Every settled decision (complete, withdrawn or conflict) is logged as
`Branch retirement <state> of <branch> (task <id>, <request> '<reason>'):
<error>; remote <evidence>`, and the retirement row keeps the reason, state and
audit, so a bot-closed PR always has a recorded reason (2026-10-27, PR #2084).
Batch abort intent and terminal lifecycles override stale queue and cleanup
metadata when checking live refs. Aborted and promoted batches (including
superseded and settled batches, respectively) are no longer ref owners.
Deferred fixer batches and companions hold refs only while the batch remains
open. Independent task, session and unreleased writer guards still apply.
Local removal visits registered AQ checkout common directories for the same
repository and the retained integration and publisher stores. It never detaches
another writer. It removes audited `origin` tracking refs alongside local heads,
so deleted remote branches do not remain visible as stale local observations.

Repository binding uses the shared GitHub identity parser, including HTTPS
without `.git` and SSH forms, then the daemon's configured credential authority.
It does not rewrite operator remotes. An unavailable binding or missing/mismatched
client leaves GitHub retirement pending before any ref observation or retained-store
creation. Provider failures preserve their safe diagnostic instead of becoming
an unavailable-client error; no credential fallback is permitted.

Local filesystem remotes (paths or local `file://` URLs) use plain Git transport
against the registered repository location. Relative paths resolve from the
registered repository checkout, never the retained store or daemon working directory.
They require no GitHub binding and share the same durable audit, verified backup,
live-reference guards and exact-SHA push lease. An unreachable local repository is
a retryable transport failure, never proof that its branches are absent. Other
non-GitHub network transports remain pending when no authenticated binding exists.

Explicit materialized-origin deletes use the same retirement mechanism. Ordinary
delete with `branches=keep` remains an explicit preservation choice. Archive's
existing delivery and live-session admission guards remain required; admitted
archives now retire their branches instead of leaving orphan refs indefinitely.

Legacy source abandonment also supports explicit preservation:
`scripts/backfill-legacy-deliveries.py PROJECT --abandon-task TASK --keep-branch`
(or `--abandon-epic EPIC --keep-branch`). Preview reports `keep_branch`; apply
still requires a reason and records the usual `abandoned` delivery decision,
including for selected undelivered descendants, with `[branches: keep]` in its
audit reason. It queues no branch retirements. Without this option, abandonment
continues to queue retirement. The option requires an abandon selection and
cannot be combined with `--relanded`; it does not cancel existing retirements
or exempt branches from later explicit disposal or ordinary proven-merged cleanup.

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
worktrees. A checkout whose origin matches no project registration falls back to
the project's own repository (`project:<id>`, the project's default branch) when
the origin matches the project's `repo_url`, or when the project has neither a
`repo_url` nor a registration; an origin another project registers, or any other
unknown repository identity, is skipped and reported. Unique unmerged refs and
stashes remain intact.

Repository selection is scoped to the project's registrations. A linked repository
with no configured URL derives its origin from its registered source path, which
is also included in the sweep. If multiple registrations match a checkout's origin,
the project's integration repository binding selects among those matches. A binding
never overrides an origin mismatch; missing or still ambiguous identities are skipped.

Legacy provenance branches use the existing verified copy-before-delete migration
under those same live-reference guards, with a pre-delete audit entry. Invalid or
conflicting provenance remains blocked. The operator cleanup script shares the
Git proof and per-ref safety implementation; its default remains a dry run.

Each project's supervisor receives its own inbox report through `CommandHandler`,
with local/remote deletion and hold counts, retirement and migration outcomes,
unique refs, stash/registration counts and failures. Cleanup failures in one project
do not suppress another project's report.

The report separately counts pending/conflicted retirement refs associated with
terminal batches, grouped by batch, even when another guard still holds them.
Unresolved nonterminal legacy `integration-batch-*` holders and unsettled
`legacy-operation:*` publisher records remain conservative holds and appear in an
explicit operator-review list with their recorded states. Terminal legacy batches
and finished/cancelled publisher actions do not hold refs. Neither historical
metadata nor terminal membership authorizes ordinary sweep deletion without Git
containment or patch-equivalence proof; unique unmerged refs remain intact.

## Re-landed content

The ordinary legacy-delivery backfill also inventories completed tasks without
any recorded branch origin, in the integration repository (or with no explicit
repository). It excludes promotion tasks and tasks belonging to another project
or repository. A missing origin or deleted task branch does not prevent proof
from the completion's reported commits or other retained source locators. Git
must still prove delivery on the default target, and apply rechecks the same
completion generation before recording the audited row. Missing source evidence
stays unproven; a dry run writes no rows and a repeated apply is idempotent.
This recovery inventory does not relax the train's filing-origin requirements.

For an originless task with a passing completion, backfill also retains that
generation's exact final reported source when Git proves it is an ancestor of
the default target. This source binding lets ordinary dependency admission
re-evaluate containment through its existing Git provenance reader. Preview
publishes nothing. Repeating backfill repairs a previously recorded legacy row
that lacks this binding, using fresh Git proof rather than the row as authority.
An earlier reported commit, content equivalence, a failed completion or an
unavailable final source cannot supply this provenance. A changed generation
refuses retention, and later target rewinds still fail ordinary containment.
Admission tracks each snapshot's evaluated task IDs itself when checking
freshness, so this fence works with the shared Git truth snapshot API.

The same strict recovery includes completed epic children whose parent branch
has disappeared, even when they retain filing origins. Backfill inventories
these children regardless of whether their origins are live or retired. A fresh,
repository-authorized remote advertisement establishes parent absence; stale
tracking refs and unavailable remote reads cannot establish presence or absence.
An existing child completion marker is a source locator, not proof that its work
landed on the default target, and does not suppress this recovery preview. Apply
preserves an existing matching immutable source binding and records the audited
root delivery once; a conflicting binding stays unproven. Backfill proves against
the fetched default target and retains only the current
passing completion's final reported source under exact ancestry. An old legacy
delivery row cannot replace this binding. Deleted child refs are harmless when
the exact commit is reachable from the default target; missing or equivalent
sources remain unproven.

Epic cleanup refuses deletion while a completed code-bearing child lacks a
retained delivery witness for its current completion and task version. This
fence applies to retirement, sweep and train cleanup, including archived task
identities. Normal Git truth reconciliation retains the source, target and
completion identity before recording the witness used by this deletion fence.
A reclose restores the hold. Audited lifecycle dispositions and organizational
children carry no source obligation. The witness permits cleanup only; admission
continues to prove containment against the current target.

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

The archive transaction also writes `task.delivery_abandoned` with the explicit
`obsolete` or `retire` disposition before parent settlement. Git-first epic
readiness and PR collection keep the archived rows and their original statuses
as history, but exclude that explicitly abandoned subtree from required delivery.
A plain delivery archive remains required; an event on active work is not a
waiver. Existing retained abandonment events carry the same decision.

Ordinary container settlement does not retain an aggregate passing completion.
The train still requires current Git containment for the remaining sources,
configured checks, tree reviews, holds and gates, and a fresh fenced head before
it records an aggregate completion and opens its PR. Legacy checkpoints and
cancelled episodes stay historical; archive never synthesizes verification or
passing evidence. Read-only train status retains the latest bounded visit until
the next bounded visit refreshes it, so a pre-archive blocker can remain visible
in the cache temporarily.

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
