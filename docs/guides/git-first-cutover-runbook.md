# Git-first integration cutover: transfer, upgrade, restart, canary

Operator procedure for selecting `integration.git_first: active`, the
five-table protocol that replaces the old subject runtime as the delivery path
for a project. The design is `projects/agent-queue/specs/2026-10-04-git-first-integration-train.md`
and `projects/agent-queue/plans/2026-10-04-git-first-integration-plan-2.md`
§4 ("Stages, owners and cutover") and §3. The invariants this runbook depends on
are asserted in `tests/test_integration_cutover.py` and `tests/test_integration_replay.py`.

A worker session must never run any step below. Every one of them is an operator
or supervisor action: a database upgrade, a daemon configuration change, a
restart, or a live observation of the real queue.

## What selecting `active` actually changes

`integration.git_first` is a **protocol selector for reconciler-owned subjects
only**. It is not a second activation of root ownership and it is not a new
mode beside `reconciler_shadow` / `reconciler_active`.

| `git_first` | What runs |
|---|---|
| `shadow` (default) | the old subject protocol stays authoritative; one read-only comparison of git-first facts against fetched git is logged per visit. It has no writer, no action port and no policy effect. |
| `active` | `IntegrationTrain` takes the whole per-project loop. The root, parent and development subject runtimes are not built at all, the integration outbox is not dispatched, and no green continuation is emitted. |

The selector refuses `IntegrationService` a train beside any subject runtime, so
the two engines can never run concurrently.

Prerequisites, all of which must be true **before** step 4:

- the whole reduced loop and both check providers exist
  (`src/integration/{git_truth,lock,checks,reviews,batches,epics,train,train_sources}.py`);
- the compatible schema is installed (steps 1–2 below);
- every root of the target repositories is reconciler-owned (step 3);
- `aq test tests/test_integration*.py tests/test_config.py` is green on the
  checkout being deployed;
- a disposable smoke run of the swarm kit has completed **with
  `git_first: active`**: `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh`.

## 1. Dump before the upgrade

```bash
pg_dump --format=custom --file "$HOME/aq-cutover-$(date +%Y%m%dT%H%M%S).dump" "$AGENT_QUEUE_DB_URL"
sha256sum "$HOME"/aq-cutover-*.dump
```

Record the dump's path and hash in the canary evidence below. Verify it restores
onto a disposable database before you rely on it; an unverified dump is not a
dump.

## 2. Upgrade the schema

```bash
aq db current          # read-only: which revision the database is stamped at
aq db upgrade          # to a00000000075, the merged single head
```

`aq db upgrade` is refused with `AQ_DB_SCOPE=worker`; that refusal is the guard
working. The three stage-2 revisions are additive and compatible with the old
readers: they add columns, indexes and check constraints, they reshape
`integration_check_evidence` in place, and nothing is dropped. If the database
reports a revision this checkout lacks, that is
`aq doctor --check db.alembic_orphan [--fix]`, not a manual stamp.

## 3. Transfer every root that is not reconciler-owned

Root ownership is durable and already enforced in code
(`RootEngineOwnership._operation`, `src/integration/engine.py:264`). There is
nothing to activate: this step exists only for a repository that never received
a transfer.

Preview first, then apply the exact previewed versions. The preview prints the
`SUBJECT_ID:VERSION` pairs; the apply refuses anything but those exact pairs, a
non-empty reason, and at least one evidence reference.

```bash
aq integration engine-transfer "$REPOSITORY_ID" --engine reconciler
aq integration engine-transfer "$REPOSITORY_ID" --engine reconciler \
    --expected-subject SUBJECT_ID:VERSION [--expected-subject ...] \
    --reason "git-first cutover canary" \
    --evidence "aq test tests/test_integration*.py tests/test_config.py: green" \
    --evidence "scripts/e2e-smoke.sh with git_first=active: green" \
    --apply
```

Development-mode projects transfer per project:

```bash
aq integration development-engine-transfer "$PROJECT_ID" --engine reconciler
```

Record, for the canary: the repository or project id, and **every transfer
performed** with its exact subject ids and versions, or the explicit statement
that none was needed because the roots were already owned.

## 3a. Backfill legacy deliveries the train cannot see

Completions closed before retained provenance existed (merged pull requests, the
old engine's batches) are `missing_git_provenance` to the train: they are listed
as unknown blockers on the root target, and anything that depends on them is held
out of every batch. Record the ones git proves are already on the default branch:

```bash
.venv/bin/python scripts/backfill-legacy-deliveries.py "$PROJECT_ID" --output preview.json
.venv/bin/python scripts/backfill-legacy-deliveries.py "$PROJECT_ID" --apply \
    --reason "git-first cutover: legacy work already on main"
```

For each such task it locates exact candidate sources (reported completion commits,
origin branch tip, old batch members and delivery receipts, the pull request
head), and writes an `integration_legacy_deliveries` row only when a candidate is
an ancestor of the tip (`development_delivery`) or merging it changes nothing
(`content_equivalent`). It never writes provenance, moves a ref or edits a task;
anything unproven is listed and left alone for an explicit decision. A reopened
task's new completion is not covered by the old row.

A child of a still-open epic whose exact source is already on the epic branch
gets retained provenance instead (`epic_branch_provenance`), because the epic's
readiness reads provenance, not rows. For the rest — a completion git can no
longer prove and that must not now be delivered — an explicit, reasoned decision
is the only answer, and it is per task:

```bash
.venv/bin/python scripts/backfill-legacy-deliveries.py "$PROJECT_ID" \
    --abandon-task TASK_ID --abandon-task ANOTHER_TASK_ID
.venv/bin/python scripts/backfill-legacy-deliveries.py "$PROJECT_ID" \
    --abandon-task TASK_ID --abandon-task ANOTHER_TASK_ID \
    --apply --reason "superseded by later work on main"
```

`--abandon-task` names any one completed task, leaf or container, and records
`abandoned` rows for it and, when it is a container, for its undelivered
descendants, so the train neither blocks on them nor merges a stale branch.
Both flags are repeatable and both are refused when git already proves the work
(the ordinary backfill then records that proof instead), when the task is not
`COMPLETED`, or when `--apply` has no nonblank `--reason`. A container with no
branch of its own is decided the same way; `--abandon-epic` is the same decision
by name for a container, and refuses a task with no children. Nothing is deleted;
removing the rows restores the previous state.

Re-run the ordinary backfill afterwards: it lists whatever still cannot be
accounted for, and every such task needs its own decision here.

## 4. Select `active` and restart

```yaml
# ~/.agent-queue/config.yaml
integration:
  git_first: active
```

```bash
aq restart --no-dashboard
```

If this deploy changed dashboard sources, also rebuild and restart the dashboard:
`python3 ~/.agent-queue/operator-checks/build-dashboard.py`, then `aq dashboard restart`.
Restarting the daemon does not rebuild the dashboard.

`aq restart --no-dashboard` preserves agent tmux sessions. Never `aq stop` then
`aq start` for an update: `aq stop` kills every agent session.

Then verify, in this order, before believing anything else:

```bash
curl -fsS localhost:PORT/api/health
curl -fsS localhost:PORT/ready
aq integration status "$PROJECT_ID"          # projection_kind: train
```

`projection_kind: train` is the proof the configured mode actually ran. If it
says otherwise, the selector did not take effect and nothing below is evidence
of anything.

## 5. Observe one real delivery

Let one ordinary completed task route to its configured parent or default ref
and watch it land. Collect, at minimum:

| Field | Where it comes from |
|---|---|
| configured mode | `aq integration status` → `projection_kind`, plus the `integration.git_first` value the daemon loaded |
| task id | the delivered task |
| source OID | the task's exact pushed source (`AQ-Source: <task-id>@<sha>` on the merge commit) |
| candidate OID | the batch's candidate ref tip that the checks ran on |
| target ref and target OID | before and after the fast-forward |
| checks | the exact-commit verdicts read for the candidate OID, from `integration_check_evidence` |
| transfers | every `engine-transfer` apply from step 3 |
| dump path and hash | step 1 |

Record the mode that actually ran. A development-mode delivery does **not** prove
hosted-train operation and a hosted-train delivery does not prove the local
resource-job provider; both are proven separately in the disposable smoke run,
and the project's actual configured mode is proven only by the live evidence
above.

## 6. Confirm old-event isolation

Under `active` the scheduler, the outbox and the green continuations are stopped
for owned projects, and pending legacy events stay pending. After the restart:

```sql
SELECT event_type, count(*), sum(attempts) FROM integration_outbox GROUP BY 1;
SELECT count(*) FROM integration_subject_journal;
```

Both must be unchanged from immediately before the restart. A pending legacy
event whose `attempts` moved, or a new subject-journal row, means something is
still dispatching into an old writer: stop, keep `git_first: shadow`, and report
the event id. `tests/test_integration_cutover.py` asserts this against real rows
across a restart; it is not a substitute for the live check, and the live check
is not a substitute for the assertion.

## Rollback

`git_first` is a selector, not an activation: setting it back to `shadow`
restores the old protocol as authoritative for the next visit. That is the whole
of the rollback, and it is why the step-3 transfer is safe to keep. Rolling back
does not un-transfer ownership — `engine-transfer` has no reverse direction, by
design.

A batch the train froze before the rollback (`target_ref` set on its
`integration_batches` row) stays where it is and is **inert**: nothing visits it
again, so the old engine neither reads it as busy nor holds its members out of
the frontier — those members are re-sealed and delivered by the old protocol as
usual. Leave the row in place rather than aborting it: `intent = 'aborted'` is
irreversible, so a hand edit would throw away the frozen inputs a later
roll-forward could still settle from (a visit treats an already-contained
candidate as delivered). Its `refs/heads/aq/batches/<digest>` candidate ref is
left on the remote too; after a rollback nothing publishes or deletes it.

If a cutover produced a wrong publication, do not revert the branch from the
daemon. Delivery means historical inclusion, so a revert is new work: file a
task, let the train publish it through the normal path.

## What this procedure does not do

- It does not declare any observation window elapsed. There is no fixed soak and
  no seven-day legacy-exclusive shadow report; the retired
  `aq integration shadow-report` cannot certify this protocol and is gone with
  its module.
- It does not settle the recorded backlog stalls. `s3_settle` is operator
  evidence gathered through the supervisor.
- It does not remove the selector, the old protocol or any legacy table. Stage 4
  does that, after this cutover has run.
