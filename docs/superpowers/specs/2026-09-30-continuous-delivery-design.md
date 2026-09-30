# Continuous delivery and bounded repair continuation

Status: authorized implementation, task `fair-current`, 2026-09-30.

The operator explicitly authorizes routine source CI repair, aggregate conflict
repair, fresh repair workers, and continuation to exact green main promotion.
This authorization supersedes the earlier policy that routine repair exhaustion
requires a human decision. Product scope and explicit approval gates still apply.
Deployment remains owned by the global supervisor; a worker never changes daemon
state, substitutes credentials, or writes directly to the operator database.

## Admission and continuation

Observe authorized feature and bugfix sources using immutable task, repository,
base, head and completion identities. The explicit `root.authorized_task_ids`
allowlist also admits individually authorized chores such as `steady-delta`,
preserving their type and genuine parent/child verification. Failed and cancelled source checks create
one actionable repair per source revision. Repair keeps source lineage and returns
the repaired head to admission. An approved aggregate may cover smaller branches
only with Git/content evidence; PR count is not evidence of distinct work.
Untracked branches require original authorization and an overlap audit.

Periodic sweeps must continue after empty, promoted and recovered batches. READY
sources must acquire their ordinary materialized origin and canonical reservation
through supported controls before entering the claim frontier. Collection and
publication share the existing durable schedule, outbox and repository owner.
New continuous candidates use observed main as their construction base while
preserving each source's original frozen base, avoiding stale-base CI work.

## One batch repair workspace

Freeze the batch source manifest and candidate base. A repair worker receives all
remaining members, including every reviewed base/head and the already applied
prefix. It resolves the complete aggregate in its reserved integration workspace,
preserving member ancestry and recording the resulting tree and repair commits.
It may edit any necessary file, including prior features and migrations. Generated
files are regenerated from resolved sources. Migration heads are checked and
colliding revisions re-chained. The daemon derives batch, revision, workspace,
claim and fence from the authenticated assignment and publishes the result through
the existing exact remote-head mutation journal.

The legacy member resolver remains available for frozen policies. Aggregate
authority cannot select another batch, alter the frozen source heads, or write
main. Every accepted repair creates a new candidate identity and invalidates
previous CI evidence.

The deployment-compatible merge also retains the operator's linear repair
permission to edit any necessary file, while keeping its exact commit list and
single-member ancestry shape. Detached delegate reservation recovery and pending
handoff proof remain available alongside the new bounded successor stages.

## Finite sessions, durable work

Each repair session retains its attempt and wall-clock budget. At exhaustion the
service first proves the old writer stopped and preserves checkpoint/dossier
evidence, then creates or reuses an operation-bound successor assignment with a
fresh session. History, counters and predecessor fences are retained. Ambiguous
writes remain fenced until their observed remote state is reconciled. Routine
exhaustion routes to supervisor recovery rather than a human permission gate.

A deliberate supervisor hold can be released with the exact incident identity,
reason and prior hold decision as a compare-and-swap guard. Release reruns all
stopped-writer, workspace, claim, ownership and remaining-budget checks. It records
both decisions and preserves the existing branch/workspace. It cannot release a
different incident or silently reset retry counters.

## Validation and publication

Candidate CI failure dispatches repair automatically. Only the exact current
candidate with authenticated required green checks may be promoted. Main movement
forces rebuild and new checks. Proven covered sources/PRs are cleaned through the
configured publisher; release queues the next batch independently of cleanup.

Implementation sequence:

1. Diagnose admission/scheduler state and held incident; add focused regressions
   and a guarded hold-release command.
2. Add aggregate repair authority and complete-batch lineage validation, preserving
   existing mutation and session fencing.
3. Add source failure repair and operation-bound fresh-worker continuation;
   provision only the required command capabilities through supported profiles.
4. Update reviewed train policy and regenerate reviewed/generated artifacts.
5. Run focused and related-area checks, plus a disposable real Git/PostgreSQL
   scenario with multiple conflicts, migration collision, CI failure, worker
   replacement, exact promotion, and the following batch. Run the swarm smoke
   fixture for claim/frontier changes.
6. Publish the implementation branch, coordinate supervisor deployment, and record
   exact live delivery evidence for the backlog and any genuine remaining gate.

The acceptance report must distinguish code/test evidence, deployment, source
coverage, candidate checks and observed main SHA. A proposal, ticket or CI status
alone does not complete this task.

## Implemented checkpoint

The connected real Git/PostgreSQL fixture repairs two simultaneous conflicts,
re-chains three colliding migration revisions, replaces a closed repair worker,
observes red then exact authenticated green candidate CI, promotes that exact
commit, cleans three covered source PRs, and seals/builds the following periodic
batch on promoted main without a new approval event. Retained pool successors
also preserve tracked, untracked, unmerged and unpushed workspace states while
releasing only the predecessor's exact claim epoch.

The related area run passed 2,105 tests and exposed one obsolete two-stage contract
assertion. After updating it to allow nonnegative stages and reject negative ones,
104 contract, acceptance, migration and supervisor capability tests passed. Lint,
generated-file drift, reviewed-artifact drift and the single Alembic head check
passed. The disposable daemon swarm run passed S1–S18; S19 refused a claim because
the fake provider remained exhausted after failover. S19 passed after resetting
only the disposable fixture. The original full-run failure remains reported.

Deployment requires operator migration `a00000000048` before loading new daemon
code. It adds exact source-CI history and permits retained repair stages beyond
ordinal one. Older policy snapshots retain member repair, human exhaustion and
reviewed admission defaults. Downgrade refuses retained successor stages. Shipped
supervisor `write_note` capabilities merge additively on profile reload/start;
operator profile edits and worker scope remain intact. The worker has not migrated
the operator database or restarted its daemon. Live deployment, hold recovery and
backlog coverage will be recorded on `fair-current` by supported commands.

Hosted CI found stale live contract pins in golden test artifacts, missing
source-CI table documentation/importer exclusions, the note grant in the wrong
capability namespace and onboarding policy drift. The follow-up regenerates the
goldens/graph/receipts, places trusted observations outside agent MCP, grants
`write_note` under `plugin_tools`, and makes explicit agent-queue onboarding
reproduce its continuous policy. It merges deployed `65e54a1` without discarding
LAN, supervisor, delegate reservation or superseded-repair recovery.

Legacy PR #660 has a real completed repair child but no collection episode or
parent verification. Deployment uses a fresh explicitly authorized repair root
carrying source and repair-child ancestry, followed by proven supersession of
covered legacy work; no parent receipt is fabricated.
