# Archiving finished tasks that carry integration audit history

**Date:** 2026-09-20
**Status:** proposed — gate for a schema change on a production database
**Branch:** `feat/graph-visibility` (HEAD `df86e2bcf`)
**Follows:** `.superpowers/sdd/2026-09-19-graph-visibility-markers-subtasks-implementation/task-15-report.md`
(commits `5a353fa27` sweep robustness, `6103b2f9d` clean refusal, `7a4898362`,
`df86e2bcf`)
**Touches:** `2026-09-04-hierarchical-integration-trains-design.md` §11.4 (which is
silent on what happens to an episode when its parent task leaves the queue),
`2026-09-08-task-deletion-with-materialized-branches-design.md` §2

---

## 1. Problem

Four foreign keys onto `tasks` are not disposed of by task removal. All four are
in the integration subsystem, and all four block the final `DELETE FROM tasks`
that both `archive_task` and `delete_task` end in
(`src/database/queries/task_queries.py`, `_delete_one`, the `delete(tasks)` at the
end of the method):

| constraint | column | `ondelete` | nullable |
|---|---|---|---|
| `fk_integration_parent_episodes_parent_task` | `integration_parent_episodes.parent_task_id` | RESTRICT | NOT NULL |
| `fk_integration_parent_verifications_parent_task` | `integration_parent_verifications.parent_task_id` | RESTRICT | NOT NULL |
| `fk_integration_repair_operations_verifier_task` | `integration_repair_operations.verifier_task_id` | RESTRICT | nullable |
| `fk_integration_candidate_resolutions_task` | `integration_candidate_resolutions.repair_task_id` | *(NO ACTION)* | NOT NULL |

(Enumerated from `src.database.tables.metadata`, not by eye; the same walk is the
ratchet in `tests/test_hierarchy_archive_delete.py::TestTaskReferenceDispositions`.
Twenty foreign keys reference `tasks`; sixteen are handled.)

Since `6103b2f9d` both paths refuse cleanly with
`HierarchyError("integration_owned")` instead of surfacing a raw `IntegrityError`
(`src/database/queries/task_references.py:140`, wired at
`src/database/queries/hierarchy_queries.py:817`,
`src/database/queries/archive_queries.py:135`,
`src/database/queries/task_queries.py:1426`). That stopped the hourly sweep dying;
it did not let the work leave the graph. On the operator's install six finished
roots (62 tasks) are permanently stuck — five held by
`integration_parent_episodes`, one (`verify-81d0aaee-…`) by
`integration_repair_operations.verifier_task_id`.

The rows themselves may never be deleted. `migrations/integration_guards.py`
installs `trg_integration_parent_episodes_{update,delete}` and
`trg_integration_parent_verifications_{update,delete}`, both running
`integration_parent_audit_append_only()`, whose whole body is
`RAISE EXCEPTION 'integration parent evidence is append-only'`. So the only way a
finished parent can leave the queue is for its evidence to stop pinning the
`tasks` row.

---

## 2. Position

Audit history is *history about a task id*, exactly like `task_comments`,
`task_completion_records`, `task_branch_origins`, `task_delivery_receipts`,
`task_integration_checkpoints` and — inside this very subsystem —
`integration_repair_stages.repair_task_id`, which records the repair delegate's
task id **with no foreign key at all**. That last one is the decisive precedent:
the integration subsystem already stores a worker task id as history without
pinning the `tasks` row, and nothing about a verifier or an episode is different
in kind.

So:

1. Drop the foreign key onto `tasks` from all four tables. Columns, `NOT NULL`,
   every *other* foreign key (to `repos`, to the episode, to the operation, to the
   stage, to `sessions`, to `workspaces`, to the member result) and every trigger
   stay exactly as they are.
2. Replace the blanket `integration_owned` refusal with a **liveness** rule:
   archive is refused while the *operation* that owns the reference can still be
   resumed, and allowed once it cannot.
3. `delete` — of a live task or of an archived one — stays refused whenever any of
   the four tables names the id, because deletion removes the id from
   `archived_tasks` too and would leave the evidence unresolvable.

### 2.1 Where the agreed direction is wrong

The agreed direction was: *keep* `fk_integration_repair_operations_verifier_task`
because it "names a live verifier task", and allow removal only when the owning
operation is terminal. **Those two halves contradict each other**, and the code
says which one to drop.

`ON DELETE RESTRICT` is unconditional. It knows nothing about operation state; it
blocks `DELETE FROM tasks` whether the operation is `active` or `completed`.
There are exactly two ways to keep the constraint and still archive:

* `UPDATE … SET verifier_task_id = NULL` before the delete, or
* change the constraint to `ON DELETE SET NULL`, which is the database doing the
  same thing.

Both are the same act, and both are wrong:

* **It corrupts an authority input.** `RepairService._reuse_verifier_on`
  (`src/integration/repair.py:2329`) computes
  `expected_task_id = operation.get("verifier_task_id") or operation.get("parent_task_id")`.
  NULLing the column silently *reassigns the expected writer identity to the
  parent task*. `_predecessor_matches` (`src/integration/repair.py:2729`) does the
  same: `owner["owner_id"] in {verifier_task_id, parent_task_id}`. An hourly
  archive sweep must not change what a fence check accepts.
* **It re-arms verifier minting.** `ParentCompletion.mark_ready_on`
  (`src/integration/parent_completion.py:286`) mints a verifier when
  `verifier_task_id is None`, at the deterministic id `verify-<operation_id>`
  (`:289`). A NULLed operation whose verifier task has been archived would
  recreate that id in `tasks` while `archived_tasks` still holds it — two rows,
  one identity. `_archive_one`'s `archive_identity_conflict` check
  (`src/database/queries/archive_queries.py:286`) only compares project ids, so it
  would not catch this.
* **It destroys the one fact the evidence chain exists to record** — which task
  verified this aggregate.
* **It buys nothing.** The protection the FK is imagined to provide (don't remove
  a task a live operation still needs) is not what it does. A state-aware refusal
  in the removal path is *strictly stronger*: it also covers
  `integration_repair_operations.parent_task_id`,
  `integration_repair_stages.repair_task_id`, `sessions.task_id` and
  `workspaces.locked_by_task_id` — none of which is FK-protected — and it covers
  them for a live operation, which is the only time it matters.

**Recommendation: drop this FK with the other three** and gate on operation state.
The triggers permit the NULL (see §5.3), so this is a judgement call, not a
mechanical one — it needs the operator's sign-off and is recorded as open
question Q1.

### 2.2 The second thing archive does that the direction did not account for

Archive is not read-only with respect to the integration subsystem's identity
chains. `_archive_one` reuses `_delete_one`, which **NULLs `sessions.task_id`**
and **releases `workspaces.locked_by_task_id`**. Several live-repair predicates
key through exactly those columns — most sharply
`RecoveryControls`'s `exact_rejected_writer`
(`src/integration/recovery_controls.py:978–1011`), which joins
`integration_candidate_resolutions` → `sessions` → `workspaces` →
`integration_repair_stages` and requires `sessions.c.task_id == resolution.repair_task_id`.

Archiving a repair delegate whose resolution is `rejected` but whose *operation is
still live* would therefore stop that excuse applying, and recovery would start
treating a dead writer as a live ambiguous one. That case is invisible if the rule
is written against the audit row's own state, because a `rejected` resolution is
terminal while its operation is not.

**Consequence for the rule:** liveness must be read from
`integration_repair_operations.state`, not from the audit row. §4 is written that
way.

---

## 3. Reader audit (the gate)

Method: `grep -rn` for each of the four table objects **and** for the distinctive
column names `parent_task_id`, `verifier_task_id`, `repair_task_id`, `episode_id`
across `src/` and `dashboard/src/`, then read every hit. `dashboard/src/` contains
**zero** references to any of the four tables or their columns — these rows reach
no API response model and no dashboard component; they are reachable only through
`/api/execute` on integration commands. Files listed below are every file in
`src/` with a non-definition hit.

Column meanings: **joins tasks?** — does the statement resolve the task id against
`tasks`. **today, task row missing** — behaviour once the id lives only in
`archived_tasks`.

### 3.1 `integration_parent_episodes`

| file:line | what it does | joins `tasks`? | task row missing | change? |
|---|---|---|---|---|
| `src/integration/parent_completion.py:104` | INSERT — the writer (`_ensure_episode_on`) | no | n/a (writer) | no — see §6 |
| `src/integration/parent_completion.py:182–188` | carry-forward validation: episode by `(id, parent_task_id, repository_id)` | no | unreachable: caller holds the live parent under lock | no |
| `src/integration/parent_completion.py:381–387` | `readiness_on`: episode by `checkpoint.episode_id`, asserts `episode["parent_task_id"] == parent["id"]` | no | unreachable (live parent) | no |
| `src/integration/recovery_controls.py:392–402` | `_locked_parent`: episode `FOR UPDATE`, cross-checks against the parent row | reads `tasks` separately at `:379`; returns `(None, "stale")` when the parent is absent | already degrades correctly | no |
| `src/database/legacy_sqlite_import.py:242,252` | insert-ordering list; comment says `FK → repos, tasks` | no | n/a | **comment only** — the ordering stays valid (a stricter order than required is harmless) |

**Three reads. None needs a code change.**

### 3.2 `integration_parent_verifications`

| file:line | what it does | joins `tasks`? | task row missing | change? |
|---|---|---|---|---|
| `src/integration/parent_completion.py:866` | INSERT — `record_verification` (the writer) | no | n/a | no |
| `src/integration/parent_completion.py:141–151` | previous verification for rollover, by `(id, operation_id, parent_task_id, episode_id, head_sha)` | no | unreachable (live parent) | no |
| `src/integration/parent_completion.py:451–476` | `carried_receipt_ids`: acceptances ⋈ verifications ⋈ operations ⋈ completions, all by id | no | unreachable | no |
| `src/integration/parent_completion.py:856–859` | idempotence probe by `(operation_id, generation, head_sha)` | no | unreachable | no |
| `src/integration/parent_completion.py:1033–1041` | `complete_parent` re-check under lock | no | unreachable | no |
| `src/integration/parent_completion.py:1088–1093` | same, second boundary | no | unreachable | no |
| `src/integration/hierarchy.py:733–760` | `prior` verified aggregate: operations ⋈ completions ⋈ verifications, filtered `operation.parent_task_id == task_id` | no | unreachable (task being collected is live) | no |
| `src/database/queries/integration_train_queries.py:64,89` | `parent_identity` in the train-candidate query | **driven FROM `tasks`** (`select … .where(tasks.c.project_id == …)`) | archived parent is simply not a candidate | no — correct by construction |
| `src/database/legacy_sqlite_import.py:271,290` | ordering list + comment | no | n/a | comment only |

**Seven reads. None needs a code change.** Note the structural point found while
writing the ratchet: `integration_parent_verifications` reaches its episode through
the composite FK `(parent_task_id, episode_id) → integration_parent_episodes`, so
it can never be the *sole* blocker for a task.

### 3.3 `integration_repair_operations.verifier_task_id`

| file:line | what it does | joins `tasks`? | task row missing | change? |
|---|---|---|---|---|
| `src/integration/parent_completion.py:123` | INSERT default `None` | — | — | no |
| `src/integration/parent_completion.py:312–318` | UPDATE, CAS on `verifier_task_id IS NULL` — the only writer | — | — | no |
| `src/integration/parent_completion.py:286` | mint-verifier branch | no | unreachable (parent must be `PAUSED` and live) | no |
| `src/integration/parent_completion.py:332,357` | `next_owner_id`, `task.integration_ready` payload | no | unreachable | no |
| `src/integration/parent_completion.py:953,974,1099` | `expected_owner = verifier or task_id` | no | unreachable | no |
| `src/integration/repair.py:118` | `retire_terminal_delegates`: `tasks JOIN operation ON verifier_task_id == tasks.id` | **yes, driven FROM `tasks`** | archived row invisible — and it is already terminal, which is what retirement wants | no |
| `src/integration/repair.py:272` | batch-operation INSERT default | — | — | no |
| `src/integration/repair.py:2329` | `_reuse_verifier_on`: `verifier_task_id or parent_task_id` | no | **unchanged if we drop the FK; silently wrong if we NULL** (§2.1) | no — provided we do not NULL |
| `src/integration/repair.py:2729` | `_predecessor_matches` fence check | no | same as above | no |
| `src/integration/status.py:306` | `relevant_task` existence probe in `aq integration status` | **driven FROM `tasks`** | archived task drops out of the rollout scan — intended: rollout readiness is about live work | no |
| `src/database/queries/integration_state_queries.py:29` | `get_terminal_integration_delegate_operation(task_id)` | no | returns the operation by id regardless; callers pass a live id | no |
| `src/database/queries/task_queries.py:666` | resume guard: refuse resume while a live operation names the task | no | a task in `archived_tasks` cannot be resumed | no |
| `src/database/queries/integration_delivery_queries.py:120–133` | `get_active_integration_verifier_for_task` (live states only) | no | caller holds a live task | no |
| `src/database/queries/integration_delivery_queries.py:136–152` | `get_integration_verifier_operation` — deliberately state-independent, used on close | no | caller is closing a live task | no |
| `src/database/queries/task_recovery_queries.py:170` | `_recovery_owner` | no | called with a live task | no |
| `src/commands/integration_commands.py:116` | handoff destination match for `next_role == "verifier"` | via `get_task(owner_id)` at `:110` → `None` → `False` | live handoff only | no |
| `src/integration/development.py:1042–1055` | abort/cancel: appends the verifier to `delegates`, then `select(tasks.c.id).where(tasks.c.id.in_(delegates), status NOT IN (COMPLETED, FAILED, PAUSED))` | **yes** | **silently returns nothing** → `pending is None` → `{"outcome": "already_terminal"}` | **no change, but say so**: an archived delegate *is* terminal, so the early return is the right answer. Recorded here so it is not rediscovered as a bug. |
| `src/orchestrator/git_ops.py:144` | `operation.get("verifier_task_id") != task.id` in the aggregate-verifier phase | no | live session | no |
| `src/event_schemas.py:1433` | payload schema for `task.integration_ready` | — | — | no |

**Sixteen reads. None needs a code change**, *conditional on dropping the FK
rather than NULLing the column* — `repair.py:2329` and `:2729` are the two sites
that a NULL would silently corrupt.

### 3.4 `integration_candidate_resolutions.repair_task_id`

| file:line | what it does | joins `tasks`? | task row missing | change? |
|---|---|---|---|---|
| `src/integration/candidates.py:776` | INSERT — the writer | — | — | no |
| `src/integration/candidates.py:766,3178` | SELECT by `id == reservation_id` | no | unaffected | no |
| `src/integration/candidates.py:1117,1173,1283,1367` | UPDATEs by `(id, state, …)` | no | unaffected | no |
| `src/integration/recovery_controls.py:978–1011` | `exact_rejected_writer`: resolutions ⋈ `sessions` ⋈ `workspaces` ⋈ stages ⋈ owners, requiring `sessions.task_id == resolution.repair_task_id` and `workspaces.locked_by_task_id == resolution.repair_task_id` | **no `tasks` join — but keys through `sessions.task_id` and `workspaces.locked_by_task_id`, both of which `_delete_one` clears on archive** | **silently stops matching**, so a stopped writer stops being excused and recovery treats it as live and ambiguous | **no code change; this is what forces the §4 rule to be operation-state based.** A resolution may be terminal (`rejected`) while its operation is live. |
| `src/integration/recovery_controls.py:1050–1052` | unresolved-reservation probe, `state IN ('reserved','pushed')` | no | unaffected | no |
| `src/integration/release.py:378–381` | unresolved members for a batch revision | no | unaffected | no |
| `src/commands/integration_commands.py:456` | reservation by id, live session | no | unaffected | no |
| `src/commands/integration_commands.py:1495–1510` | exact-row match built from the live session's own identity | no | unaffected | no |
| `src/database/legacy_sqlite_import.py:284` | ordering list + comment | — | — | comment only |

**Nine reads. None needs a code change**, given the §4 rule.

### 3.5 The transitive readers the four FKs were also pinning

Dropping `fk_integration_parent_episodes_parent_task` un-pins more than the episode
table, because several other integration tables reach `tasks` only *through* the
episode:

* `integration_repair_operations.parent_task_id` — FK is onto
  `integration_parent_episodes(parent_task_id, id)`, **not** onto `tasks`.
* `integration_parent_operation_completions.parent_task_id` — reaches `tasks` only
  through the verification's composite FK.
* `integration_child_dispositions`, `integration_episode_receipt_acceptances`,
  `task_delivery_receipts`, `task_integration_checkpoints` — all FK to the episode
  or the verification, never to `tasks`.

So today `integration_repair_operations.parent_task_id` cannot dangle; after the
change it can. One reader breaks outright:

| file:line | what it does | today | after |
|---|---|---|---|
| `src/integration/development.py:1186–1193` | `select(tasks).where(tasks.c.id == operation["parent_task_id"]).mappings().one()` | always resolves | **raises `NoResultFound`** if the parent has been archived |

It is reached only from the abort/cancel path for an operation that is still live,
so the §4 rule (archive refused while the operation is live) keeps it safe. It is
listed because it is the one `.one()` in the whole audit and because the safety
argument now depends on the refusal rule rather than on the database.

Two further reads on the same column are already tolerant:
`src/database/queries/integration_state_queries.py:149` is an `outerjoin` whose
caller guards with `if row is None or not any(row.values())` (`:156`), and
`src/commands/integration_commands.py:957` returns `None` when
`get_task(parent_task_id)` misses.

Checkpoint rows also survive archive (`task_integration_checkpoints.task_id` has no
FK and `_delete_one` never touches it). Every read of that table in `src/` is either
keyed by an explicit `task_id` or driven from a `tasks` join
(`src/integration/completion_recovery.py:412`,
`src/database/queries/hierarchy_queries.py:1302`), so an archived parent's
checkpoint is inert. No change.

### 3.6 Audit summary

**35 reads across 15 files. None requires a change to the read itself.** Three
require an entry in the record rather than a patch:

1. `src/integration/recovery_controls.py:978` — dictates that the refusal rule be
   operation-state based (§4).
2. `src/integration/development.py:1186` — a `.one()` whose safety moves from the
   database to the refusal rule.
3. `src/integration/development.py:1042` — silently returns nothing for an
   archived delegate, which is the correct answer.

And two sites are the reason the `verifier_task_id` FK must be *dropped* rather
than nulled: `src/integration/repair.py:2329` and `:2729`.

---

## 4. The narrowed refusal rule

Today's rule (`src/database/queries/task_references.py:140`) is: *any* reference
in any of the four tables refuses both `archive` and `delete`. The new rule splits
by mutation.

### 4.1 Archive — refused only while the work can still move

Archive is refused with `HierarchyError("integration_owned", …)` when any task in
the subtree is named by a reference whose **owning operation is still resumable**:

```
LIVE_OPERATION_STATES = ("active", "escalated", "human_required")
```

| reference | refuses archive when |
|---|---|
| `integration_parent_episodes.parent_task_id` | an `integration_repair_operations` row with this `(parent_task_id, episode_id)` is in a live state |
| `integration_parent_verifications.parent_task_id` | its `operation_id` is in a live state |
| `integration_repair_operations.verifier_task_id` | that operation is in a live state |
| `integration_candidate_resolutions.repair_task_id` | its `operation_id` is in a live state **or** the resolution's own `state IN ('reserved','pushed')` |
| `integration_repair_operations.parent_task_id` *(new — no FK, §3.5)* | that operation is in a live state |
| `integration_repair_stages.repair_task_id` *(existing check, `archive_queries.py:136–146`)* | its operation is in a live state |

The last two are new to the *registry* but not to behaviour: the stage check
already exists inline in `archive_task`, and the operation's `parent_task_id` was
previously pinned transitively. Folding all six into one predicate removes the
second copy of the rule that `7a4898362` was written to eliminate.

**Why `LIVE_OPERATION_STATES` is exactly the revivable set.** `completed` and
`cancelled` are never re-entered: every `update(integration_repair_operations)` in
`src/` that sets a live state CASes on `state == "human_required"`
(`src/integration/recovery_controls.py:283–288`,
`src/integration/candidates.py:1023–1026`); the only other writes move *into*
terminal states (`src/integration/main_promotion.py:1188–1194`,
`src/integration/recovery_controls.py:576–580`,
`src/integration/repair.py:3060`, `:3105`). There is no trigger permitting a
terminal→live transition and no code path that performs one. So "the operation is
terminal" is a one-way predicate, which is what makes it safe to archive against
(§6).

The verifier case the operator is stuck on: `verify-81d0aaee-…` is a verifier task
that completed its parent operation. `ParentCompletion` completes the operation
before the verifier task closes (`src/orchestrator/execution.py:1345–1360` and
`src/orchestrator/git_ops.py:130–144` require `state in {"active","escalated","completed"}`
and re-check the parent reached `COMPLETED`), so its operation is `completed` and
the root archives under the new rule.

### 4.2 Delete — unchanged, and extended to the archive

`delete_task` stays refused for **any** reference in any state, with the same
`integration_owned` code. Deleting removes the id from `tasks` *and*, for
`delete_archived_task`, from `archived_tasks`, after which the evidence names
nothing at all.

`delete_archived_task` (`src/database/queries/archive_queries.py:626`) currently
performs no integration check. It has no caller in `src/commands/` today — it is a
database-API method only (`src/database/base.py:833`); `restore_task` is a
vestigial response model (`src/api/models/task.py:349,943`) with no command behind
it. It must gain the same any-reference refusal before this migration ships, or
the one path that can orphan evidence is the one path left unguarded.

### 4.3 Where the checks live

Unchanged in shape from `6103b2f9d`: the predicate runs inside
`guard_integration_mutation` (`src/database/queries/hierarchy_queries.py:817`),
under the per-project hierarchy advisory lock and **before** any branch origin is
retired, and is repeated in `archive_task` / `_delete_task_body` for the
non-hierarchical fallback where the guard stands down. The inline active-repair
check at `archive_queries.py:136–146` folds into the registry and the duplicated
statement goes away.

### 4.4 `TASK_REFERENCE_DISPOSITIONS` and the ratchet

`TASK_REFERENCE_DISPOSITIONS` (`src/database/queries/task_references.py:47`) gains
one value:

```
#: * "history" — the id is kept as history; there is no foreign key, and the
#:   archive path is gated on liveness instead (see INTEGRATION_TASK_REFERENCES).
```

The four entries change from `"refused"` to `"history"`. Three ratchet tests in
`tests/test_hierarchy_archive_delete.py::TestTaskReferenceDispositions` need
matching edits:

* `test_every_foreign_key_onto_tasks_is_classified` (`:201`) asserts
  `declared - found == set()` — "names a dead foreign key". A `"history"` entry is
  *by definition* not in `found`, so the assertion must exempt those keys.
* A **new** assertion replaces what that one used to guarantee:
  `test_history_dispositions_have_no_foreign_key` — every `"history"` key must be
  absent from the metadata walk. This is the real ratchet: if someone re-adds an
  FK onto `tasks` for one of these tables, the test fails instead of the sweep.
* `test_refused_dispositions_are_the_ones_the_guard_checks` (`:254`) compares the
  `"refused"` set against `INTEGRATION_TASK_REFERENCES`. It becomes
  `"refused" | "history"` on one side, and `INTEGRATION_TASK_REFERENCES` grows the
  two non-FK entries from §4.1 — so the comparison becomes
  `history ∪ refused ⊆ checked`, with a separate assertion that every checked
  entry names a real `(table, column)` in the metadata.
* `test_a_blocking_key_is_either_cleaned_up_in_delete_one_or_refused` (`:223`)
  already skips `"refused"`; add `"history"` to that skip (it iterates the
  metadata walk, so the keys are gone anyway — the change is defensive).
* `test_archive_and_delete_refuse_each_referencing_table` (`:273`) splits in two:
  delete still refuses all four; archive refuses only with a live operation, and
  gains a sibling proving archive *succeeds* with a terminal one.

`INTEGRATION_TASK_REFERENCES` (`:81`) grows a `liveness` field per entry — the
statement that decides whether the reference is live — so
`find_integration_task_references` can take a `mutation` argument and apply the
right predicate. `assert_no_integration_task_references` is renamed
`assert_task_references_permit(conn, ids, mutation)`; the refusal code and the
`context["references"]` shape stay identical, so no surface changes.

---

## 5. The migration

### 5.1 Revision id

Current head is **`a00000000010`** (`migrations/versions/a00000000010_task_subtasks.py`;
verified by reading every `revision` / `down_revision` in `migrations/versions/`,
which form a single chain from `a00000000001`). The new revision is
**`a00000000011`**, file
`migrations/versions/a00000000011_integration_audit_outlives_task.py`,
`down_revision = "a00000000010"`.

Sibling branches have collided on these ids before. **Re-run `alembic heads`
against a throwaway database, and re-read `migrations/versions/`, immediately
before writing the file** — another agent is committing to this branch
concurrently.

### 5.2 DDL

Idempotent and inspector-guarded, per the branch rule: the squashed baseline
builds its tables from live `src.database.tables` metadata, so a database created
*after* the `tables.py` edit already lacks these constraints and the revision must
be a no-op there.

```python
_DROPPED_FKS = (
    ("integration_parent_episodes", "fk_integration_parent_episodes_parent_task",
     ["parent_task_id"]),
    ("integration_parent_verifications", "fk_integration_parent_verifications_parent_task",
     ["parent_task_id"]),
    ("integration_repair_operations", "fk_integration_repair_operations_verifier_task",
     ["verifier_task_id"]),
    ("integration_candidate_resolutions", "fk_integration_candidate_resolutions_task",
     ["repair_task_id"]),
)

_ADDED_INDEXES = (
    ("ix_integration_repair_operations_verifier_task",
     "integration_repair_operations", ["verifier_task_id"],
     "verifier_task_id IS NOT NULL"),
    ("ix_integration_candidate_resolutions_repair_task",
     "integration_candidate_resolutions", ["repair_task_id"], None),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table, constraint, _cols in _DROPPED_FKS:
        if table not in tables:
            continue
        if constraint in {fk["name"] for fk in inspector.get_foreign_keys(table)}:
            op.drop_constraint(constraint, table, type_="foreignkey")
    for name, table, cols, where in _ADDED_INDEXES:
        if table not in tables:
            continue
        if name not in {i["name"] for i in inspector.get_indexes(table)}:
            op.create_index(
                name, table, cols,
                postgresql_where=sa.text(where) if where else None,
            )
```

The two indexes are not optional bookkeeping. Neither
`integration_repair_operations.verifier_task_id` nor
`integration_candidate_resolutions.repair_task_id` has an index today (checked
against `metadata`: the only indexes on those tables are
`uq_integration_repair_operations_{active_parent,parent_episode,active_batch}` and
`uq_integration_candidate_resolutions_current_member`), so
`find_integration_task_references`'s `column.in_(ids)` probe sequentially scans
both on every archive attempt — once an hour per blocked root today, and once per
archived root after. The episode and verification tables already have
`uq_*_parent_id (parent_task_id, id)` with `parent_task_id` leading, so they are
covered.

Lock note for the production run: `ALTER TABLE … DROP CONSTRAINT` on a foreign key
takes `ACCESS EXCLUSIVE` briefly on both the referencing table and on `tasks`. It
is a catalog-only operation (no rewrite, no scan), but on a busy daemon it can
queue behind a long read and then block everything. Run it with
`SET lock_timeout = '5s'` and the daemon stopped, and retry rather than wait.

### 5.3 What the triggers do and do not cover — confirmed, not assumed

* `trg_integration_parent_episodes_{update,delete}` and
  `trg_integration_parent_verifications_{update,delete}`
  (`migrations/integration_guards.py:427–448`) are row-level DML triggers running
  `integration_parent_audit_append_only()`. `ALTER TABLE … DROP CONSTRAINT` is
  DDL: **no user trigger fires, and none of these definitions is touched.** The
  append-only guarantee is exactly as strong after the migration as before.
* Dropping a foreign key does implicitly drop Postgres's own *internal* RI
  constraint triggers on both tables. That is the mechanism being removed and is
  the whole point; it is not a user trigger.
* `integration_repair_operations` has **no** append-only trigger. It carries
  `trg_integration_repair_operation_stage_monotone` (BEFORE UPDATE: `active_stage`
  may not decrease) and `trg_integration_repair_operations_reject_empty_batch`
  (BEFORE INSERT OR UPDATE: `batch_id` must not name an `empty` batch). DELETE is
  unguarded. So NULLing `verifier_task_id` *would* be mechanically permitted — a
  parent-target operation has `batch_id IS NULL` by
  `ck_integration_repair_operations_target`, so the empty-batch guard does not
  fire, and `active_stage` is unchanged. **The trigger layer does not decide this
  question; §2.1 does.**
* `integration_candidate_resolutions` carries `trg_candidate_resolution_monotone`,
  whose identity `ROW(...)` comparison includes `NEW.repair_task_id`
  (`migrations/integration_guards.py:195`). **`repair_task_id` is frozen against
  every UPDATE**, so for this table nulling is not even available — dropping the
  FK is the only route.
* No `CheckConstraint` on any of the four tables mentions the task columns
  (checked against `metadata`), so nothing else needs naming or renaming.

### 5.4 `src/database/tables.py`

Remove exactly four `ForeignKeyConstraint(... ["tasks.id"] ...)` entries — the ones
named in §5.2. Add the two `Index(...)` declarations. Leave the columns, their
`nullable=False`, and every other constraint on those tables untouched. After the
edit the metadata walk finds 16 foreign keys onto `tasks`, not 20.

Per CLAUDE.md, the change to `tables.py` and the revision land in the same commit,
and the revision is **hand-written, not autogenerated from a worktree**.

### 5.5 `src/database/legacy_sqlite_import.py`

Comment-only: `:252`, `:271`, `:284`, `:290` name `tasks` among the FKs of these
tables. The insert ordering itself is a hand-written list and remains valid (it is
now stricter than required). No `_DEFERRED_COLS` entry covers any of the four
columns, so nothing functional changes.

---

## 6. Writer audit

Can a write arrive naming a task that is already archived?

| writer | file:line | guarded by | can it arrive post-archive? |
|---|---|---|---|
| episode INSERT | `parent_completion.py:104` (`_ensure_episode_on`) | called only from the collection path, which loads the parent from `tasks` under `FOR UPDATE` and requires a checkpoint | **No.** A new episode for an archived parent needs the parent back in `tasks`; there is no unarchive path for a parent (§7). |
| verification INSERT | `parent_completion.py:866` | requires the operation and a live verifier task closing | **No** — the operation must be live, which §4.1 refuses to archive against. |
| `verifier_task_id` UPDATE | `parent_completion.py:312–318` | CAS on `IS NULL`, inside `mark_ready_on`, requires the parent `PAUSED` and live | **No.** |
| resolution INSERT | `candidates.py:776` | authenticated live session holding the claim | **No** — its operation is live. |
| resolution UPDATEs | `candidates.py:1117,1173,1283,1367` | CAS on `(id, state)` of a live reservation | **No.** |

The argument rests on one durable fact, established in §4.1: a `completed` or
`cancelled` operation is never revived. Because every revival CASes on
`human_required` — a state §4.1 refuses to archive against — "archived" and "a
writer arrives" cannot both happen.

**If that invariant is ever weakened** (a future "reopen a completed operation"
feature), the correct response is a refusal in the reopen path, not a foreign key:
the reopen would need the parent task back in `tasks` anyway, and *that* is where
the check belongs.

No database-level guarantee replaces the dropped FK, and that is deliberate — the
same trade `task_comments`, `task_completion_records`, `task_branch_origins` and
`integration_repair_stages.repair_task_id` already make.

---

## 7. A `resolve task or archived task` helper

**One already exists, and it is not the right shape.** `src/task_names.py:101`
(`_task_identity_exists`) is a `union_all` over `tasks.c.id` and
`archived_tasks.c.id` — but it answers only "is this id taken", is private to the
id-minting path, and returns a bool.

The other union patterns are per-caller and return different things:

* `src/database/queries/activity_queries.py:114,128` — iterates
  `(tasks, archived_tasks)` and tags rows with `archived: bool`.
* `src/database/queries/digest_queries.py:117` — same loop, for the digest.
* `src/database/queries/token_queries.py:299–309` — backfills titles/statuses from
  `archived_tasks` for ids missing from `tasks`.
* `src/database/queries/task_comment_queries.py:198–209` — scope check that accepts
  either table.

**Which readers from §3 should use one? None.** That is the audit's conclusion,
not an omission. Every reader either (a) is driven *from* `tasks` and correctly
wants archived rows excluded (`repair.py:118`, `status.py:306`,
`integration_train_queries.py:64`, `development.py:1042`), or (b) is keyed by an id
the caller already holds live. No reader in §3 renders a task *name* or *title*
from one of these rows, which is the one case a resolver would serve.

**Recommendation: do not add a helper in this change.** Add
`resolve_task_or_archived(conn, task_id) -> dict | None` only when a surface first
needs to *display* an archived verifier or parent — most likely a future
`aq integration explain` or a dashboard rendering of `integration_owned`. Adding
it now would be an unused abstraction, and the `activity_queries` /
`token_queries` loops are the pattern to follow when it is needed. Recorded as
open question Q3.

---

## 8. Restore / unarchive interplay

### 8.1 There is a real unarchive path, and it is narrow

`RepairService._restore_archived_delegate_on`
(`src/integration/repair.py:2675–2702`) recreates a task in `tasks` from its
`archived_tasks` snapshot and then `DELETE`s the archive row. Called from
`repair.py:1000`. Its docstring — "Recover a legacy archive of this still-active
stage's exact delegate" — is precisely the case this change makes ordinary rather
than legacy.

It is safely narrow: it restores only a task matching `_delegate_task_matches`
(`:2704`) — same project, no parent, same repo and branch,
`created_by_kind == "integration_repair"`, `created_by_id == operation["id"]` — for
a **still-active stage**, under the operation and stage locks. That is a repair
delegate (`integration_repair_stages.repair_task_id`, no FK), never a verifier and
never a parent. Evidence re-attaches correctly because nothing was detached: the
stage row still names the id, and the restore puts that id back in `tasks`.

There is **no general unarchive**: `restore_task` exists only as a response model
(`src/api/models/task.py:349,943`) and a formatter registration
(`src/cli/formatter_registry.py:390`), with no command behind it.

### 8.2 Id reuse

`archive_identity_conflict` (`src/database/queries/archive_queries.py:286`) fires
when an id is archived twice under *different* project ids — legacy id allocation
could reuse an archived id across projects. That risk does not reach these four
tables: every referenced id is either deterministic and uuid-derived
(`verify-<operation_id>` at `parent_completion.py:289`,
`repair-<operation_id>-<stage>` at `repair.py:1028`) or a generated root id, and
`fresh_root_id` reserves against `archived_tasks` as well as `tasks`
(`src/task_names.py:101–124`). So a *different* task can never inherit an id that
audit rows already name.

### 8.3 `delete_archived_task`

Covered in §4.2: it must gain the any-reference refusal. Note it already has the
right instinct for the adjacent tables — it deletes `task_completion_records`,
`task_comments` and `task_subtasks` only when no live `tasks` row shares the id
(`archive_queries.py:636–667`), precisely so a restore that recreated the identity
is not robbed of its history. The integration audit rows must be stronger still:
they may not be deleted at all, so the *task* deletion is what gets refused.

---

## 9. Test plan

Postgres only (`POSTGRES_TEST_DSN`), in `tests/test_archive.py`,
`tests/test_hierarchy_archive_delete.py`, `tests/test_database.py`.

**Schema**

1. `test_no_integration_table_has_a_foreign_key_onto_tasks` — walks
   `metadata` and asserts the four constraints are gone and 16 remain. *Proves the
   `tables.py` edit is complete and nothing re-adds one.*
2. `test_history_dispositions_have_no_foreign_key` — §4.4's replacement ratchet.
3. `test_migration_is_idempotent_on_a_fresh_metadata_database` (marker
   `migration`) — `upgrade()` then `upgrade()` against a database built from live
   metadata; both no-ops, no error. *Proves the inspector guards.*
4. `test_append_only_triggers_survive_the_migration` — after upgrade, an `UPDATE`
   and a `DELETE` on `integration_parent_episodes` still raise
   `integration parent evidence is append-only`. *Proves §5.3's central claim.*

**The refusal rule**

5. `test_archive_succeeds_for_a_terminal_parent_with_episode_history` — seed a
   COMPLETED root, an episode, a verification and a `completed` operation; archive
   succeeds; the root is in `archived_tasks`; **the episode and verification rows
   are still present and unchanged**. *The headline case — five of the operator's
   six stuck roots.*
6. `test_archive_succeeds_for_a_terminal_verifier_task` — the `verify-<op>` shape,
   operation `completed`. *The operator's sixth root.*
7. `test_archive_is_refused_while_the_operation_is_live` — parametrized over
   `active`, `escalated`, `human_required` × the four tables; asserts
   `integration_owned` and that the task is untouched. *Proves the narrowing did
   not become a hole.*
8. `test_archive_is_refused_for_a_rejected_resolution_of_a_live_operation` — the
   §3.4 / `recovery_controls.py:978` case: resolution `rejected`, operation
   `active`. *Proves the rule reads the operation, not the audit row — this test
   fails against a rule written the obvious way.*
9. `test_archive_is_refused_while_a_repair_stage_of_a_live_operation_names_the_task`
   — the check folded in from `archive_queries.py:136`; proves the fold lost
   nothing.
10. `test_delete_is_still_refused_for_every_reference_in_every_state` —
    parametrized over the four tables × `{live, completed, cancelled}`. *Proves
    §4.2.*
11. `test_delete_archived_task_is_refused_while_evidence_names_it` — archive
    succeeds (terminal operation), then `delete_archived_task` refuses. *Proves the
    one path that could orphan evidence is closed.*

**The sweep**

12. `test_the_hourly_sweep_archives_an_integration_tracked_root` — end to end
    through `archive_old_terminal_tasks`; the root is archived and its
    `archive_refusal` metadata record is cleared. *Proves the operator's backlog
    actually drains.*
13. `test_a_live_operation_root_is_reported_not_archived` — still appears in
    `list_archive_blocked_roots` with `reason == "integration_owned"`.

**Regression around the readers §3 flagged**

14. `test_recovery_still_excuses_a_stopped_writer_after_the_migration` — exercises
    `exact_rejected_writer` with the operation live and the delegate *not*
    archived (because §4.1 refused it), proving the predicate still matches.
15. `test_abort_of_a_live_operation_resolves_its_parent` — guards
    `development.py:1186`'s `.one()`.
16. `test_verifier_task_id_is_never_nulled` — asserts no
    `update(integration_repair_operations)` in `src/` sets `verifier_task_id=None`
    (source inspection, same technique as
    `test_a_blocking_key_is_either_cleaned_up_in_delete_one_or_refused`). *Locks in
    §2.1 so a future author does not reintroduce the NULL.*

Area suites to run at the end: `tests/test_archive.py`,
`tests/test_hierarchy_archive_delete.py`, `tests/test_task_doctor.py`,
`tests/test_database.py`, `tests/test_integration_hierarchy.py`,
`tests/test_integration_state.py`, `tests/test_integration_repair.py`,
`tests/test_integration_candidates.py`, `tests/test_development_integration.py`,
`tests/test_supervisor_recovery.py`, `tests/test_orchestrator.py`.

---

## 10. Rollback

`downgrade()` re-adds the four constraints, but only when it can:

```python
def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    dangling = {}
    for table, constraint, cols in _DROPPED_FKS:
        if table not in tables:
            continue
        col = cols[0]
        n = bind.execute(sa.text(
            f"SELECT count(*) FROM {table} t "
            f"WHERE t.{col} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM tasks k WHERE k.id = t.{col})"
        )).scalar_one()
        if n:
            dangling[f"{table}.{col}"] = n
    if dangling:
        raise RuntimeError(
            "cannot restore the task foreign keys: "
            + ", ".join(f"{k} has {v} id(s) no longer in tasks" for k, v in sorted(dangling.items()))
            + " — those tasks have been archived; see "
            "docs/superpowers/specs/2026-09-20-archive-tasks-with-integration-history-design.md §10"
        )
    # …then re-create each constraint (guarded by the inspector) and drop the
    # two indexes.
```

**Be honest about what this means: the migration is one-way in practice.** The
moment the sweep archives one referenced root, `downgrade()` will refuse, and
there is no remedy that preserves the data — the append-only triggers forbid
deleting the dangling audit rows, and the `archived_tasks` snapshot cannot be moved
back into `tasks` wholesale (it has no reverse of `_archive_one`). The refusal is
deliberately loud rather than a silent skip: a schema that claims a constraint the
database does not carry is worse than a failed downgrade.

The safe rollback for a bad *deploy* is therefore the code, not the schema. Revert
the `task_references.py` / `hierarchy_queries.py` / `archive_queries.py` change and
the archive path is refusing again exactly as it does today, with the dropped
constraints harmless — a missing FK blocks nothing. Say so in the release note.

Operator sequence for the production run:

1. `aq db current` to confirm the head is `a00000000010`.
2. Stop the daemon.
3. `aq db upgrade` (as operator, outside any worktree slot — CLAUDE.md, *Never
   migrate the operator's database*).
4. Start the daemon; watch the first hourly sweep. `aq task archive-settings`
   should show `blocked_count` fall by the six roots, and `aq doctor --check
   tasks.archive_blocked` should stop naming them.

---

## 11. Open questions

**Q1 — Drop `fk_integration_repair_operations_verifier_task`, or keep it and NULL?**
§2.1 argues it must be dropped: nulling silently reassigns an authority input at
`repair.py:2329` and `:2729`, re-arms verifier minting at
`parent_completion.py:286`, and destroys the audit fact. The triggers permit
either. This reverses the agreed direction and is the one decision in this spec
that the controller and senior reviewer must re-confirm before any code is
written.

**Q2 — Is `human_required` live for archive purposes?** The spec says yes: it is
the only state a resumption CASes on, so a `human_required` operation is
resumable by definition. But it can also sit there indefinitely awaiting a human
who never comes, which reproduces the current complaint in a smaller form. If the
operator would rather archive those too, the answer is not to widen this rule but
to add an explicit disposition (`aq integration cancel-operation`) that moves the
operation to `cancelled` first. Recommend: leave `human_required` refusing, and
check whether such a command already exists on the operator's install.

**Q3 — Does anything need to *render* an archived verifier or parent?** §7
recommends adding no resolver until a surface needs one. If the intent is that
`aq integration status` or the dashboard should show "verified by `verify-…`
(archived)", that changes the answer and the helper belongs in this change.

**Q4 — The `integration_owned` refusal still has no dashboard surface.** Recorded
in the task-15 round-1 concerns and unaddressed: `integration_owned` is raised
*before* the branch-policy check, so a subtree that is both integration-owned and
holds a materialized branch reports `integration_owned` and
`dashboard/src/components/BranchDiscardPrompt.tsx` never appears. This change makes
`integration_owned` rarer but not gone (§4.1 still raises it for live operations),
so the follow-up stands.

**Q5 — Should the sweep's `archive_refusal` record distinguish "live operation"
from the old blanket reason?** The record stores `{"code", "detail", "at"}` and
`_record_archive_refusal` skips the write when the *code* is unchanged
(`archive_queries.py`, per `7a4898362`). After this change `integration_owned`
means something narrower; an operator reading a stale record written before the
upgrade would be misled about whether the root is permanently or temporarily
blocked. Cheapest fix: have the upgrade path clear every stored
`archive_refusal` record whose code is `integration_owned`, so the first
post-upgrade sweep writes a fresh one. Not specified above because it is a
convenience, not a correctness issue — but it is one `DELETE` in the migration and
probably worth it.
