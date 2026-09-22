# Archiving finished tasks that carry integration audit history

> **Status (2026-09-22): approved by Jack.** The 2026-09-20 draft was rejected by an adversarial review (§12, kept verbatim); this revision answers every blocking and should-change finding. Decisions D1, D2, D3 and D5 were approved; D4 was completed separately by `aq integration release-owner`.

**Date:** 2026-09-20, revised 2026-09-21
**Status:** approved (Jack, 2026-09-22)
**Revised by:** task `swift-orbit.1` (epic `swift-orbit`), against `origin/main` `6bc84ba7d`
**Follows:** PR #612 (`keen-crest.3`: the live-owner guard, delegate release, revision `a00000000011`), which deliberately kept the four foreign keys; `2026-09-20-integration-delegate-release-design.md`
**Touches:** `2026-09-04-hierarchical-integration-trains-design.md` §11.4, `2026-09-08-task-deletion-with-materialized-branches-design.md` §2

## Changes against the review

| Finding | Answer | Where |
|---|---|---|
| **B1** terminal operation ≠ work landed | "Safe to archive" is now a **delivery** rule, stated once for hierarchy/train and once for development mode, with a separate explicit abandonment control. Operation state only answers "is something still running". | §3 |
| **B2** tests through the real collection path; outcome per earlier refusal | The test plan builds every root through `file_children` → `checkpoint_parent` → receipts → `complete_parent`, then through the real mode switch and a real development sweep. §3.6 states the before/after outcome of `sealed`, `delivery_target_fixed` and `branch_discard_required` in each mode. | §3.6, §9 |
| **B3** "no reader needs a change" is false | Redone as a complete table, every hit read one by one. Fifteen statements must change, nine more are hardened, and the rest — all listed — are either correctly exclusionary or unreachable *given a rule this spec adds*, where before it was an accident of the project's mode. Helper specified, with the callers that must tolerate an archived parent and the ones that must not. | §5 |
| **S1** CAS on the two `repair.py` escalation updates | Both gain a state CAS and a rowcount check. | §6.2 |
| **S2** match foreign keys by columns, fail if any remains | The revision finds the constraints through `pg_constraint` by `(table, column) → tasks`, drops whatever it finds, then re-queries and raises if one is left. | §7.2 |
| **S3** `delete_project` is a third removal path | Guarded with a typed refusal; so is `delete_archived_task`, the fourth. | §4.5 |
| **S4** archive releases a lock retirement keeps as a cleanup blocker | New refusal `integration_cleanup_blocked`: a retained owner row, a delegate's workspace lock or a delegate's attached session refuses archive **and** delete. | §4.4 |
| **S5** migration-marked tests assert the foreign keys exist | Named, with the command that actually runs them. | §7.5 |
| **S6** `idx_` naming; index for `integration_repair_stages.repair_task_id` | Done; three indexes. | §7.2 |
| Preflight: all six roots are in `development` mode | Every rule is stated per mode, and the read half of the guard becomes mode-independent. | §3, §4.2 |

What this revision found that the review did not (all read-only, §11.1):

1. **The seven rows in the epic are the entire population.** Across the whole production database the four foreign-key columns name exactly seven task ids — five episode parents, one verifier, one candidate-resolution repair task. There is no eighth row waiting to surprise the migration.
2. **No production project is in `hierarchy` or `train` mode** (10 `disabled`, 3 `development`). The hierarchy/train half of §3 cannot be checked against live data; it is specified from the code and covered by tests only.
3. **The daemon's own delivery predicate calls three adopted tasks undelivered.** `_development_delivery_pending` never recognises an operator adoption (filed as `swift-orbit.4`). The development-mode rule reuses that predicate, so this is **prerequisite P1**: without it `keen-harbor` and `noble-ridge` would stay refused for a reason that is false.
4. **`integration.stranded_delegates` is blind to one of the seven rows**: a stopped session that still carries `claim_phase` counts as a live writer (filed as `swift-orbit.5`, **prerequisite P2** for that row only).
5. **PR #612's refusal names a command that refuses.** It tells the operator to run `aq integration abort`, which only accepts a `human_required` operation (`recovery_controls.py:567`). All three live operations in production are `active`. §4.3 makes the message state-aware.
6. **In hierarchy/train mode the four foreign keys were never the only thing in the way.** `delivery_target_fixed` refuses the archive of any subtree containing a delivery-receipt source — which is every parent that collected a child. This spec does **not** relax it (decision D3, §13), and says so plainly rather than promising an archive it cannot deliver.
7. The draft's `repair.py` line numbers were stale by 96 lines (commit `18c760836`). Every citation below was re-read at `6bc84ba7d`.

---

## 1. Problem

Four foreign keys onto `tasks` are not disposed of by task removal. All four are in the integration subsystem, and all four block the final `DELETE FROM tasks` that `archive_task` and `delete_task` both end in (`src/database/queries/task_queries.py:1656`, reached through `_delete_one`):

| constraint | column | `ondelete` | nullable |
|---|---|---|---|
| `fk_integration_parent_episodes_parent_task` | `integration_parent_episodes.parent_task_id` | RESTRICT | NOT NULL |
| `fk_integration_parent_verifications_parent_task` | `integration_parent_verifications.parent_task_id` | RESTRICT | NOT NULL |
| `fk_integration_repair_operations_verifier_task` | `integration_repair_operations.verifier_task_id` | RESTRICT | nullable |
| `fk_integration_candidate_resolutions_task` | `integration_candidate_resolutions.repair_task_id` | NO ACTION | NOT NULL |

(Names and delete rules confirmed against `pg_constraint` on the production database, 2026-09-21.)

The audit rows themselves may never be deleted: `migrations/integration_guards.py:427–448` installs `trg_integration_parent_episodes_{update,delete}` and `trg_integration_parent_verifications_{update,delete}`, all running `integration_parent_audit_append_only()` (`:156`), whose whole body is `RAISE EXCEPTION 'integration parent evidence is append-only'`. So the only way a finished parent can leave the queue is for its evidence to stop pinning the `tasks` row.

**State of the guards today (after PR #612).** Two checks refuse a removal:

* `live_integration_owner` (`src/integration/delegate_release.py:382`) — refuses while a *running* operation owns a task in the subtree, naming the operation. It runs only inside `guard_integration_mutation` and only **after** that method's mode gate (`src/database/queries/hierarchy_queries.py:759–760`), so it never runs for a `development` or `disabled` project.
* `assert_no_integration_task_references` (`src/database/queries/task_references.py:141`) — the blanket guard: refuses whenever any of the four tables names the task, ended or not. Called from `hierarchy_queries.py:880`, `archive_queries.py:154` and `task_queries.py:1507`. Because PR #612 kept the foreign keys, this is still load-bearing: without it the removal reaches the raw `IntegrityError`.

Result on the live install: `aq doctor --check integration.stranded_delegates` reports ok, the hourly sweep records `integration_owned` for six roots, and `aq task archive` refuses the seventh row too. `keen-crest.3`'s acceptance ("unblocks archive for the five roots") is not met, and cannot be met by deleting the blanket guard alone — §11 shows that one of the seven rows is safe to release the moment the constraint goes, two more once prerequisite P1 lands, and four must stay refused until an operator acts.

---

## 2. Position

### 2.1 History names a task by id, never by foreign key

Audit history is *history about a task id*, exactly like `task_comments`, `task_completion_records`, `task_branch_origins`, `task_delivery_receipts`, `task_integration_checkpoints`, `integration_delegate_releases` and — inside this very subsystem — `integration_repair_stages.repair_task_id`, which records a repair delegate's task id with no foreign key at all. The delegate-release spec already states the rule (§2 there); this spec carries it out.

So:

1. Drop the foreign key onto `tasks` from all four tables. Columns, `NOT NULL`, every *other* foreign key (to `repos`, the episode, the operation, the stage, `sessions`, `workspaces`, the member result) and every trigger stay exactly as they are.
2. Replace the blanket refusal with **four questions asked in a fixed order, in every project mode** (§4.2): is the subtree sealed; does a running operation own it; does it still hold a preserved resource; has its work reached the default branch.
3. `delete` — of a live task, an archived task or a whole project — stays refused whenever integration history names the id, because deletion removes the id from `archived_tasks` too and the evidence would then resolve to nothing.

### 2.2 Drop `fk_integration_repair_operations_verifier_task`; do not NULL the column

The originally agreed direction was to keep this one constraint "because it names a live verifier task" and allow removal only when the operation is terminal. Those halves contradict each other: `ON DELETE RESTRICT` knows nothing about operation state, so keeping it and still archiving means `SET verifier_task_id = NULL` (by hand or by `ON DELETE SET NULL`). That is wrong:

* **It corrupts an authority input.** `RepairService._reuse_verifier_on` (`src/integration/repair.py:2233`) computes `expected_task_id = verifier_task_id or parent_task_id`; `_predecessor_matches` (`:2627`) accepts `owner_id in {verifier_task_id, parent_task_id}`. NULLing the column silently reassigns the expected writer identity to the parent. An hourly sweep must not change what a fence check accepts.
* **It re-arms verifier minting.** `ParentCompletion.mark_ready_on` (`src/integration/parent_completion.py:242`) mints a verifier when the column is NULL, at the deterministic id `verify-<operation_id>` (`:289`), while `archived_tasks` still holds that id. `_archive_one`'s `archive_identity_conflict` check (`archive_queries.py:404–407`) compares project ids only and would not catch it.
* **It destroys the one fact the chain exists to record** — which task verified this aggregate.
* **It buys nothing.** A state-aware refusal is strictly stronger than the constraint: it also covers `integration_repair_operations.parent_task_id`, `integration_repair_stages.repair_task_id`, `sessions.task_id` and `workspaces.locked_by_task_id`, none of which has a constraint.

The trigger layer permits either choice (§7.3); this is a judgement, recorded as decision D1. For `integration_candidate_resolutions` there is no choice: `trg_candidate_resolution_monotone` freezes `repair_task_id` against every UPDATE (`migrations/integration_guards.py:195`).

### 2.3 Archive is not read-only towards the integration subsystem

`_archive_one` reuses `_delete_one`, which **NULLs `sessions.task_id`** (`task_queries.py:1642–1644`) and **releases `workspaces.locked_by_task_id`** (`:1650–1654`), unconditionally. Live-repair predicates key through exactly those columns — `RecoveryControls._ambiguous_writes_on` (`recovery_controls.py:984–1167`) joins resolutions → `sessions` → `workspaces` and requires `sessions.task_id == resolution.repair_task_id`; the session reconciler's preservation (`execution.py:2297–2312`) is keyed on the workspace lock; `integration.stranded_fences` (`doctor/integration_checks.py:456–491`) diagnoses an owner row through both.

Two consequences, and they are different rules:

* **Liveness is read from `integration_repair_operations.state`, never from the audit row's own state.** A resolution may be `rejected` (terminal) while its operation is live. §4.3 therefore drops the `resolution.state IN ('reserved','pushed')` conjunct PR #612 put on the candidate-member seat.
* **A preserved resource is its own refusal, independent of operation state** (finding S4). Delegate retirement deliberately keeps a branch-owner row and a workspace lock "exactly as found" as named cleanup blockers (`delegate_release.py:247–257`). Archive would clear the lock and erase the join key in the same statement that removes the row. §4.4.

### 2.4 "The operation ended" does not mean "the work landed" (B1)

The draft allowed archive once the owning operation was `completed` or `cancelled`. That is the wrong question. An operation is a *repair or verification episode*; whether the task's commits reached the default branch is recorded somewhere else entirely, and differently per mode. Production proves the two are independent in both directions:

* `keen-harbor`'s operation is **cancelled**, yet its work **is** on `main` (an `adopted` row and a `delivered` row name it).
* `nimble-dune`, `smart-dune` and `sound-current` have operations that are still **active**, yet every task under them **is** delivered (zero pending by the daemon's own predicate). Those operations were reserved at the first parent checkpoint under hierarchy mode, have zero stages, `updated_at == created_at`, and nothing advances them in development mode.

And archive is destructive to deliverability: in hierarchy/train mode it retires the branch origin the train's candidate query inner-joins (`integration_train_queries.py:174–181`); in development mode it removes the row from `tasks`, which is what the publisher's candidate query is driven from (`development.py:486–497`). Either way an undelivered root silently never reaches the default branch. That has already happened: `keen-crest.1` (2026-09-20) found 25 parked `development_deliveries` rows whose sources had been archived out from under the publisher.

---

## 3. Safe to archive: the delivery rule

### 3.1 Vocabulary

* **Removal paths** — `archive_task` (manual, bulk `archive_completed_tasks`, hourly `archive_old_terminal_tasks`), `delete_task`, `delete_archived_task`, `delete_project`.
* **Subtree `S`** of the task being archived, `R` its root as passed to `archive_task`. `R` is a **top-level root** when `tasks.parent_task_id IS NULL`.
* **Mode** — `projects.hierarchical_integration_mode`: `hierarchy` / `train` (`HIERARCHY_MODES`), `development`, or `disabled` / NULL.
* **Default branch** — `repos.default_branch` of `projects.integration_repository_id`, compared with any leading `refs/heads/` stripped.
* **Delivered** means *recorded as having reached the default branch by the subsystem that owns delivery in that mode*. The archive path never inspects git.

The rule is evaluated only for `archive`. It is the **last** of the four integration checks (§4.2): a sealed subtree, a running operation and a preserved resource are each reported first, because each has a different remedy and fixing the later one first would be wasted work.

### 3.2 Hierarchy and train mode

Work flows child → parent branch (a receipt with `target_task_id = parent`) → … → top-level root → default branch (a **root receipt**: `target_task_id IS NULL`, written only by `main_promotion.py:1086–1127`, always `disposition = 'code'`).

**Rule H.** When `R` is a top-level root, archive is refused with `integration_undelivered` iff

```
NOT root_code_receipt(R)  AND  ( candidate_identity(R)  OR  collected_code(S) )
```

* `root_code_receipt(R)` — a `task_delivery_receipts` row with `source_task_id = R`, `target_task_id IS NULL`, `disposition = 'code'`, `repository_id` = the integration repository, `target_branch` = the default branch. This is `delivered_to_root` in `eligible_root_page_on` (`integration_train_queries.py:131–141`), reused, not re-derived.
* `candidate_identity(R)` — `R` would be returned by `eligible_root_page_on` **if its holds were lifted**: the same statement restricted to `tasks.id = R` with the three transient clauses removed (`~held`, `~unresolved_gate`, `~active_membership`, `:199–201`). What is left is identity: `COMPLETED`, top-level, `repo_id` matches, `pr_url` non-blank, a checkpoint row, an **un-retired origin**, and `leaf_identity` or `parent_identity`. Implementation: factor the statement into one builder taking `include_holds: bool`, so the scheduler and the archive rule cannot drift (a test asserts both call it).
* `collected_code(S)` — a receipt with `disposition = 'code'`, `source_task_id ∈ S` and `target_task_id ∈ S`: code collected onto a parent branch inside this subtree. With no root receipt it never left that branch. This covers a `FAILED` root too — its children's code is real and undelivered, whatever the root's status.

The refusal names **what holds the work**, first match per task: a `hold:%` label on `R` (`task_labels`), a gate on `R` whose status is not `resolved`, a checkpoint that is not verified (`task_integration_checkpoints.state`), otherwise "eligible for the next train". An active batch membership never reaches this rule — it is `sealed`, reported first.

Deliberately **not** undelivered: a `COMPLETED` root that was never a candidate and collected nothing — no `pr_url`, no checkpoint, e.g. the `Review: …` tasks the 2026-09-08 spec exists to let leave the queue. A `FAILED` leaf root with commits on its own branch is likewise not held: the train only ever takes `COMPLETED` roots, archive keeps the branch, and that is the meaning `FAILED` + archive already has everywhere.

When `R` is **not** a top-level root the rule does not apply and nothing changes: a subtree containing any receipt source is still refused `delivery_target_fixed`, because archiving it would advance the surviving parent's generation and void its verification (`hierarchy_queries.py:942–958`).

This rule also closes a hole that exists **today** and has nothing to do with the foreign keys: a `COMPLETED` *leaf* root held by a `hold:%` label has no episode and no receipts, so every current guard passes, archive retires its origin, and it silently leaves the train.

Rule H is evaluated in every mode except `development`. In a project that never used hierarchy it is vacuous (no task has an origin or a receipt); in a `disabled` project that once did, it still protects a root that would become a candidate again if the mode were re-enabled.

### 3.3 Development mode

`guard_integration_mutation` stands down entirely in this mode today. There are no receipts; delivery is a `development_deliveries` row in state `delivered` or `adopted`, targeting the default ref, whose manifest names the task at its current completion revision. The readiness projection already defines this precisely: `_development_delivery_pending` (`src/database/queries/blocked_state.py:86–152`). The archive rule is that predicate, not a copy of it:

> **Rule D.** Archive is refused with `integration_undelivered` iff some task `t ∈ S` has `status = 'COMPLETED'` and `_development_delivery_pending(t)`.

Rule D applies to **every** archive in a development project, not only to tasks with integration history — the hazard in §2.4 does not depend on history, and the production evidence for it (`keen-crest.1`) had none.

What holds the work, first match per task: an unresolved gate (`_gate_open`), an unmet blocking dependency (`unmet_dependency_predicate`), otherwise "not yet published to `<default branch>`" — which covers "awaiting the next sweep" and "source branch is gone and its completion revision is not an ancestor of the default branch" (`development.py:554–571`). A **parked** or **publishing** batch that lists the task, and an open development repair that lists it as a source, are reported before this by the existing `_development_integration_hold` (`archive_queries.py:243–315`), unchanged, because it can name the batch.

`FAILED` and `BLOCKED` tasks are never pending: the publisher only ever takes `COMPLETED` tasks (`development.py:489`).

Leftovers from a project's earlier hierarchy mode — un-retired origins, `reserved` worker owner rows, a checkpoint stuck at `awaiting_children` — are **not** delivery facts in development mode and do not refuse. All six production roots carry them (7–17 live origins each). The publisher superseded them: it delivers any `COMPLETED` branch, and records branches already contained in the default branch (`development.py:572–589`).

**Prerequisite P1 (`swift-orbit.4`).** `DevelopmentIntegration.adopt` writes the completion record with `commits = [target head]` but the manifest member with `source_sha = branch head` (`development.py:340–346`, `:417–426`), so the predicate's `{task_id, source_sha}` containment never matches and `reconcile_completion_sources` skips the row (`:823–826`). On production this misreports `keen-harbor`, `noble-ridge` and `noble-ridge.12` as pending although each is named by an `adopted` row. The rule above must not ship before P1, and must not work around it with a second definition of "delivered".

### 3.4 `disabled` / no integration mode

No delivery machinery exists, so there is no delivery rule beyond rule H's protection of hierarchy-era state (§3.2). The other three checks (§4.2) still run: history rows outlive a mode change, which is exactly how production got here.

### 3.5 Abandonment is explicit

Some work should never land. Without a control for that, `integration_undelivered` would recreate the permanent refusal this spec exists to remove — and `delete` is no escape, because history refuses it (§4.5).

`archive_task` gains `abandon_undelivered: bool` with a required `reason` (`aq task archive --task-id ID --abandon-undelivered --reason "..."`):

* It skips **only** the delivery rule. `sealed`, `integration_owned` and `integration_cleanup_blocked` still refuse.
* Refused for a non-elevated session scope (`hierarchy.abandon_not_for_sessions`) — the same test `pin_not_permitted_refusal` applies (`task_commands.py:144–154`): a human, or a supervisor with elevated scope, may decide; a worker may not. Bulk mode and the hourly sweep never pass it.
* Before the rows move it writes a `note` comment on `R` (comments survive archive) listing each abandoned task, its holder, the principal and the reason, and logs `task.delivery_abandoned`. No new table.
* Branches are kept, as archive always keeps them.

### 3.6 Earlier guard refusals: expected outcome in each mode

The order below is the order of evaluation after this change; "today" is `6bc84ba7d`.

| Refusal | Raised when | Hierarchy / train | Development / disabled |
|---|---|---|---|
| `sealed` | a task in `S`, or an ancestor, is a member of a batch in an active lifecycle | today: refuses, first. After: **unchanged**. | today: **never evaluated** (guard returns at `:759–760`). After: **refuses** — the read half is mode-independent (§4.2). Production: no such membership in any of the seven subtrees. |
| `integration_owned` (running operation) | `live_integration_owner` finds a seat | today: refuses. After: refuses, state-aware message, candidate-member seat widened (§4.3). | today: only the repair-stage seat, by the inline check at `archive_queries.py:161–171`. After: **all four seats refuse**. Production: `nimble-dune`, `smart-dune`, `sound-current`. |
| `integration_cleanup_blocked` | §4.4 | new | new. Production: `sound-current` (once its operation ends) and the repair task. |
| `integration_owned` (development hold) | unsettled batch or open repair lists the task | n/a | **unchanged**. |
| `integration_undelivered` | §3.2 / §3.3 | new; evaluated **before** `delivery_target_fixed` so an undelivered root gets the message that names a remedy. | new. |
| `delivery_target_fixed` | any receipt whose source is in `S` (`hierarchy_queries.py:847–871`; no disposition filter, and `record_disposition` writes receipts for `noop` / `ineligible` / `skipped` too) | today: refuses archive **and** delete of every parent that collected a child. After: **unchanged — deliberately (D3)**. A delivered root built through the real collection path is still refused here, after this change as before it. | today and after: **never evaluated** — it lives in the write half, which stays mode-gated. This is why production roots with child receipts (`keen-harbor` 16, `noble-ridge` 3, `nimble-dune` 1) are not held by it. |
| `integration_owned` (history reference — the blanket guard) | any of the four tables names `S` | today: refuses. After: **gone for archive**; becomes `integration_history_retained` for delete. | same. |
| `branch_discard_required` | `delete` with no branch policy and a materialized origin in `S` | archive never raises it (it passes `branch_policy="keep"`). Unchanged for delete. | never evaluated. Unchanged. |
| `live_descendants`, `non_terminal_root`, `open_descendants` | as today | unchanged | unchanged |

What this means for hierarchy/train mode, stated without hedging: the change frees verifier tasks, candidate-resolution delegates and parents that collected nothing; it **adds** protection for undelivered roots; and it leaves a delivered parent exactly as unarchivable as it is today, behind `delivery_target_fixed`. Relaxing that is D3 (§13), not this change.

---

## 4. The guards after PR #612: end state

### 4.1 What replaces `assert_no_integration_task_references`

It is **deleted**, not renamed. Its one blanket question becomes four specific ones in a new module, `src/integration/removal_guard.py`, which does reads only, on the caller's connection:

```python
async def assert_integration_permits_removal(
    db, conn, *, root_id: str, ids: Sequence[str], mutation: Literal["archive", "delete"],
    project_id: str, mode: str | None, abandon: AbandonUndelivered | None = None,
) -> None:
    """Raise HierarchyError for the first of, in this order:
       sealed · integration_owned · integration_cleanup_blocked ·
       integration_undelivered (archive)  |  integration_history_retained (delete)."""
```

`src/database/queries/task_references.py` keeps the registry and `find_integration_task_references` (read-only; now used by the delete refusal, `aq task explain` and the doctor check), and `TASK_REFERENCE_DISPOSITIONS` gains the value `"history"` (§4.7).

### 4.2 Where it runs, and how it composes

`guard_integration_mutation` gains a removal-only preamble **above** its mode gate. `reopen` and `disposition` are untouched — they keep the row and are fenced exactly as today.

```
guard_integration_mutation(task_id, mutation, …)
  1. load project + mode                                   (as today, :745–758)
  2. if mutation in {"archive", "delete"}:                 NEW — every mode
         lock_hierarchy_project(conn, project_id)
         ids = subtree_ids(task_id)
         assert_integration_permits_removal(…)
  3. if mode not in HIERARCHY_MODES: return False          the gate, unchanged
  4. lock · ids · sealed                                   unchanged (the advisory lock is re-entrant;
                                                           sealed still fences reopen / disposition)
  5. delivered check → delivery_target_fixed               unchanged
  6. origins: branch_discard_required / retire / bumps     unchanged
```

`assert_integration_permits_removal` and step 4 call **one** sealed-membership helper (today's `:765–805`, extracted), so there is a single definition of an active batch. Deleted as now redundant: the removal-only live-owner block at `:806–827` (moved into step 2), the blanket call at `:880`, both `if not hierarchical:` fallbacks (`archive_queries.py:148–154`, `task_queries.py:1504–1507`) and the inline repair-stage check at `archive_queries.py:161–171`. `_development_integration_hold` stays where it is.

The advisory lock is what makes the reads mean something: `adopt`, `configure` and every hierarchy writer already take `lock_hierarchy_project`, and lock order (project advisory lock → sessions → tasks) is unchanged. Deliveries only ever move *towards* delivered, so the one unlocked writer (`DevelopmentIntegration.save`) can only turn a refusal into a permit on the next attempt, never the reverse.

**Composition with delegate release.** The three pieces answer three different questions and hand over in one direction:

```
operation running ──(complete_parent | abort | cancel-preserving)──▶ operation ended
   integration_owned refuses                                          │
                                                                       ▼
                        release_delegates (tick · doctor --fix · CLI): ticket → FAILED,
                        audit row, preserved resources recorded, never released
                                                                       │
                                                                       ▼
                        integration_cleanup_blocked refuses until each preserved
                        resource is released by its own guarded control
                                                                       │
                                                                       ▼
                        delivery rule  ──▶  archive  (history stays, by id)
```

`live_integration_owner` never releases anything; `release_delegates` never archives anything; the removal guard never settles a ticket. `stranded_delegates` stays deliberately narrow (it lists unsettled tickets, not history).

### 4.3 The running-operation refusal (`integration_owned`)

`live_integration_owner` keeps its four seats (verifier, parent, repair stage, candidate member) with one change: the candidate-member seat refuses whenever the resolution's **operation** is live, whatever the resolution's own state (§2.3). An unfinished reservation of an *ended* operation stays moot, as PR #612 decided.

The message names the control that works for the operation's state — today's names `abort` for every state, and `abort` refuses anything but `human_required`:

| Operation state | Control named |
|---|---|
| `human_required` | `aq integration resume OPERATION_ID` to let it finish, or `aq integration abort OPERATION_ID --reason "..."` |
| `active`, `escalated` | wait for it, or — when it is obsolete — `aq integration cancel-preserving OPERATION_ID --reason "..."` |

Either way followed by `aq integration release-delegates OPERATION_ID`.

**Prevention.** The production failure mode is an operation no engine will ever advance: reserved under hierarchy mode, stranded when the project moved to development. Two additions, neither of which cancels anything on its own:

* `aq integration develop` (`DevelopmentIntegration.configure`) returns `stranded_operations: [...]` — every live parent operation in the project — so the operator learns at the moment of the switch.
* Doctor check `integration.unadvanceable_operations` (WARN, report-only): a live operation whose project mode is not in `HIERARCHY_MODES`, naming the parent, the operation's age and `cancel-preserving`. No `--fix`: ending an operation is an operator's decision.

### 4.4 Preserved resources (`integration_cleanup_blocked`, S4)

Refuses `archive` and `delete` when, for any task in `S`:

| Blocker | Predicate | Why removal must not proceed |
|---|---|---|
| `branch_owner_retained` | an `integration_branch_owners` row with `owner_id = task`, `handoff_state <> 'released'`, and (`session_id IS NOT NULL` or `workspace_id IS NOT NULL` or `handoff_state <> 'reserved'`) | the row is a fence over a ref and points at a checkout that may hold unsent work; removal NULLs the session join key and leaves the row naming nothing, which `integration.stranded_fences` then reports as "gone" and `aconfirm_integration_owner_handoff` can never confirm (`workspace.py:1215–1256`) |
| `workspace_locked` | `workspaces.locked_by_task_id = task` **and** the task is a delegate of any operation (`delegate_release._owned_by`) | `_delete_one` clears the lock unconditionally; retirement keeps it on purpose so allocation cannot recycle a dirty checkout |
| `session_attached` | the task is a delegate and has a session with `state <> 'stopped'` or `desired_state <> 'stopped'` or `claim_phase IS NOT NULL` | same predicate `get_integration_delegate_cleanup` reports (`integration_state_queries.py:73–82`) |

A **detached reservation** — `handoff_state = 'reserved'`, no session, no workspace — "cannot issue Git writes" (`development.py:1344–1345`) and is not a blocker. The removal releases it in the same transaction, in every mode and for every owner role, extending the step that today does so only for `worker` rows in hierarchy/train mode (`hierarchy_queries.py:934–941`). Production: 40 such rows across the five parent subtrees.

For a non-delegate task a stale workspace lock is a leak, not a preserved resource, and archive keeps clearing it as today. While here, `_delete_one` also clears `workspaces.lock_mode`, which every other release path clears and it does not.

**The retirement record must outlive the task.** `_delete_one` deletes `task_metadata` (`task_queries.py:1597`), and with it `integration_retirement` — for a delegate retired before PR #612 (production's verifier, and `integration_delegate_releases` is empty there) the only record of why it ended. `_archive_one` therefore writes an `integration_delegate_releases` row from that metadata (`released_by = "archive:backfill"`) when none exists for `(task_id, operation_id)`. The table carries no constraint onto `tasks` for exactly this reason.

Controls, per blocker: `session_attached` — it clears when the session stops (`aq session kill SESSION_ID` if it never will); `branch_owner_retained` / `workspace_locked` — inspect the checkout for unsent work, then let the development preservation pass release it (`preserve_stopped_owners`, every development tick) or, for a batch, `aq integration retry-cleanup BATCH_ID`. **Gap, stated rather than papered over:** there is no supervisor-runnable command that releases one attached owner row whose session is provably stopped; today that is a LOCAL-operator action (`task_commands.py:189–199`). Decision D4 (§13) asks whether to add `aq integration release-owner`.

### 4.5 Delete, `delete_archived_task`, `delete_project` (S3)

**`delete_task`** — refused, in any operation state, when one of the **four** history columns names a task in the subtree — exactly the set today's blanket guard checks — with its own code, so the remedy is not confused with a running operation's:

> `integration_history_retained`

Deliberately **not** widened to `integration_repair_stages.repair_task_id`: PR #612 decided that a *released* repair-stage delegate may be deleted, because `integration_delegate_releases` exists to outlive it, and pins that in `tests/test_integration_delegate_release.py` (`test_the_audit_row_outlives_the_delegate_it_released`, `test_aborting_an_operation_leaves_no_owned_delegate_behind`). A parent named by `integration_repair_operations.parent_task_id` needs no entry of its own: the composite key `fk_integration_repair_operations_parent_episode` guarantees an episode names it too.

**`delete_archived_task`** (`archive_queries.py:744–786`) has no integration check at all and no command in front of it today (`src/database/base.py`; `restore_task` is a response model only). It gains the same refusal **before** this migration ships: it is the one path that can leave audit rows resolving in neither table.

**`delete_project`** (`project_queries.py:161–221`) bulk-deletes `tasks` at `:205`, bypassing every guard. Today a project with an episode is undeletable *by accident*: `:205` hits the RESTRICT constraint, the transaction rolls back, and `_cmd_delete_project` (`project_commands.py:514–539`, no try/except) surfaces a raw `IntegrityError`. After the drop that statement succeeds, and the failure merely moves — `DELETE FROM repos` hits `fk_integration_parent_episodes_repository`, `DELETE FROM workspaces` hits `fk_integration_candidate_resolutions_workspace`, `DELETE FROM projects` hits four more — **except** for a project whose only history is a verifier operation or a resolution pinning no workspace of its own, which now deletes cleanly and orphans its audit rows. `archived_tasks` rows survive the delete in every case.

So `_cmd_delete_project` gains a typed refusal, checked before `db.delete_project`, when any integration history row names a task of the project in `tasks` **or** `archived_tasks`, or names one of its repositories. `db.delete_project` repeats the check inside its transaction, under the project row lock it already takes. There is deliberately no override: the rows are append-only, so a project with integration history is retained. (That `delete_project` can also raise on `task_labels`, `task_gates`, `task_workspace_requirements`, `sessions.task_id` and workspace locks is a separate, pre-existing defect and out of scope.)

### 4.6 Refusal catalogue

Every refusal is a `HierarchyError`, surfaced as `hierarchy.<code>`. `<…>` is interpolated; the first sentence is written to fit the 200-character `archive_refusal.detail` the sweep records.

| Code | Exact operator message | `context` | Supervisor control | Prevention |
|---|---|---|---|---|
| `sealed` | `<mutation> would change a sealed subtree: <task_id> is a member of integration batch <batch_id> (<lifecycle>). Wait for the batch to finish; see \`aq integration status <project_id>\`.` | `{"batch_id", "lifecycle", "task_id"}` | wait; if the batch's operation is `human_required`, `aq integration resume` / `abort` | unchanged — sealing is the design |
| `integration_owned` | `integration operation <op> is <state> and owns <task_id> as its <role>; <mutation> is refused while it runs. <state-specific control, §4.3>, then \`aq integration release-delegates <op>\`.` | `{"integration_operation": {"operation_id","state","role","task_id"}}` | §4.3 | `stranded_operations` on `develop`; doctor `integration.unadvanceable_operations` |
| `integration_cleanup_blocked` | `<task_id> still holds <n> preserved resource(s): <first blocker, as rendered by _integration_cleanup_reason>. <mutation> would release it silently; clear it first — \`aq task explain <task_id>\` lists each one.` | `{"blockers": [...]}` (the `get_integration_delegate_cleanup` shape) | §4.4 | P2; D4 |
| `integration_undelivered` | `<n> task(s) under <root> have work that has not reached <default_branch>: <task_id> (<holder>)[, …]. Deliver it (<mode control>), or abandon it with \`aq task archive --task-id <root> --abandon-undelivered --reason "..."\`.` | `{"mode", "default_branch", "undelivered": [{"task_id","holder","detail"}]}` | hierarchy/train: `aq task set <id> --label -hold:<who>`, `aq task gate-resolve`, `aq integration flush <project>`. development: `aq task gate-resolve`, `aq integration sweep <project> [--retry]`, or `aq integration adopt <project> --task <id> --head-sha <sha> --reason "..."` when it is already in the default branch. Either: `--abandon-undelivered`. | the rule itself is the prevention fix for §2.4 |
| `integration_history_retained` | `<mutation> is refused: <n> integration audit record(s) name <task_id> (<table>[, …]) and audit history is append-only. Archive the task instead; its history stays readable by id.` | `{"references": [{"task_id","table","column"}]}` | `aq task archive --task-id <id>` | none needed — this is the intended end state |
| `delivery_target_fixed` | unchanged | unchanged | none today — D3 | D3 |
| `abandon_not_for_sessions` | `--abandon-undelivered is an operator decision; a worker session cannot make it.` | `{}` | run it as the operator or an elevated supervisor | — |

**Who can run the controls — stated as it is, not as the standing rule would like it.** Every integration *recovery* control named above — `resume`, `abort`, `cancel-preserving`, `release-delegates`, `retry-cleanup`, `adopt`, `sweep` — requires the `LOCAL` principal today (`_integration_local_operator`, `integration_commands.py:291–293`): the loopback CLI with no session token. A `SESSION` principal is answered `unauthorized`, and `src/commands/principal.py:19–25` is explicit that elevation must never widen that. `aq task set --label`, `aq task gate-resolve`, `aq integration flush`, `aq task archive` (with or without `--abandon-undelivered`) and `aq task explain` are capability-gated and supervisor-runnable. So the rule "every integration failure mode gets a supervisor-runnable control" is met by this spec for `integration_undelivered` and `integration_history_retained`, and is met for `integration_owned` and `integration_cleanup_blocked` **only on an install where the supervisor's shell is the LOCAL principal**. This spec names the controls; it does not change who may run them. D4 asks.

Holders in `integration_undelivered`: `hold_label`, `open_gate`, `unverified_checkpoint`, `awaiting_train`, `collected_not_promoted`, `blocked_dependency`, `awaiting_publication`.

`_record_archive_refusal` currently rewrites a root's record only when the **code** changes (`archive_queries.py:607–612`). After this change `integration_owned` means something narrower than the six records production holds. It compares `(code, detail)` instead; details are deterministic for a stable condition, so a permanently refused root still costs no hourly write, and the first post-upgrade sweep replaces every stale record without a data migration.

Documentation that names the old behaviour and must change with the code: `docs/reference/database/data-lifecycle.md:194`, `docs/guides/integration-troubleshooting.md:267`, the `CLAUDE.md` "Integration delegate release" entry, the module docstrings of `task_references.py` and `delegate_release.py`, `retired_delegate_message` (whose last sentence becomes false), and `src/api/models/task.py:361–372`.

### 4.7 `TASK_REFERENCE_DISPOSITIONS` and the ratchet

```
#: * "history" — the id is kept as history; there is no foreign key, and removal is
#:   decided by src.integration.removal_guard instead (see INTEGRATION_TASK_REFERENCES).
```

The four entries change from `"refused"` to `"history"`. `INTEGRATION_TASK_REFERENCES` keeps exactly those four (§4.5); the two non-constraint columns are seats of `live_integration_owner`, not entries here. In `tests/test_hierarchy_archive_delete.py::TestTaskReferenceDispositions`:

* `test_every_foreign_key_onto_tasks_is_classified` (`:201`) — exempt `"history"` keys from "names a dead foreign key".
* **new** `test_history_dispositions_have_no_foreign_key` — every `"history"` key is absent from the metadata walk. This is the real ratchet: re-adding such a constraint fails a test instead of the sweep.
* `test_refused_dispositions_are_the_ones_the_guard_checks` (`:254`) — becomes `history == checked`, plus "every checked entry names a real `(table, column)`".
* `test_a_blocking_key_is_either_cleaned_up_in_delete_one_or_refused` (`:223`) — add `"history"` to the skip.
* `test_archive_and_delete_refuse_each_referencing_table` (`:273`) — hand-seeds rows; replaced by §9.
* `tests/test_archive.py:1646` provokes a real `IntegrityError` through the episode constraint to test `_failure_signature`; it moves to a constraint that still exists (`task_gates`).

---
## 5. Reader audit (B3)

### 5.1 Method, and the one argument everything rests on

Three independent passes over `src/` at `6bc84ba7d` — `repair.py` + `recovery_controls.py`; the rest of `src/integration/`; everything outside it — each reading **every** hit of `tasks.c.`, `select(tasks`, `get_task(`, `_get_task_conn`, `join(tasks`, the six integration table names, and `parent_task_id` / `verifier_task_id` / `repair_task_id` / `owner_id` near integration code. A statement is listed when an id that came **from an integration row** is looked up in `tasks`. `dashboard/src/` has no reference to any of these tables or columns.

Ids covered: the four constrained columns, plus the columns that were only ever pinned *transitively* or not at all — `integration_repair_operations.parent_task_id`, `integration_repair_stages.repair_task_id`, `integration_parent_operation_completions.parent_task_id`, `integration_child_dispositions.*`, `task_delivery_receipts.source_task_id` / `target_task_id`, `task_integration_checkpoints.task_id`, `task_branch_origins.task_id`, `integration_branch_owners.owner_id`, `integration_batch_members.task_id`.

**The draft's error was the phrase "unreachable: the operation is live".** That is only an argument if archive is refused for a live operation — and today it is refused by `live_integration_owner` in hierarchy/train mode **only**. In a development or disabled project the blanket guard was the sole protection for the parent and verifier seats, and the delete path had no fallback at all. Production has three live operations in exactly that position. So every "live-only" row in §5.4 is safe **because §4.2 moves the running-operation refusal above the mode gate**, and §9 tests that dependency directly. Remove that and §5.4 becomes §5.2.

"Missing" below means the id resolves in `archived_tasks` (or nowhere) and not in `tasks`. `CMD-ERR`: the exception escapes the service and `CommandHandler.execute` returns an untyped `{"error": …}` after a rollback. `TICK-RETRY`: it reaches `IntegrationService._isolated` (`src/integration/service.py:216–223`), rolls back, and is retried every tick forever.

### 5.2 Must change — reachable after a legitimate archive

These run for an **ended** operation, or on a replay, which is precisely when the parent has become archivable.

| # | `file:line` — function | id resolved | Today, row missing | Required change |
|---|---|---|---|---|
| 1 | `recovery_controls.py:757–769` `_project_id_on` | `operation.parent_task_id` | `ValueError("operation target has no owning project")`; no caller catches | resolve through the helper (§5.7); needs `project_id` only |
| 2 | `recovery_controls.py:72`, `:78` `resume` → #1 | same | CMD-ERR, **before any state guard**, so for `completed` / `cancelled` too; `_recover_stopped_delegate_claim` (`:59`) has already committed | via #1; an ended operation then reaches the typed `invalid_state` (`:193–194`) |
| 3 | `recovery_controls.py:566` `abort` → #1 | same | CMD-ERR before the state guard at `:567` | via #1; refusal stays typed |
| 4 | `recovery_controls.py:643` `release_delegates` → #1 | same | CMD-ERR before the ended-state guard at `:644`. **Worst of the three**: this control exists *only* for ended operations; "parent archived, delegate still unsettled" is its realistic input | via #1. `project_id` is used for the result payload only. (The fleet-wide paths take it from the delegate's own row, `delegate_release.py:334`, and are unaffected.) |
| 5 | `repair.py:3044–3063` `_operation_project_id_on` | same | `ValueError("repair operation project identity is missing")` | same helper — one implementation serves #1, #5 and #7 |
| 6 | `repair.py:217–221` `start` → #5 | same | caught → `invariant_error`; runs before any state guard | via #5; a replay then answers `already_started` / `stale` (`:273–278`, `:294–295`) |
| 7 | `integration_commands.py:967–974` `_integration_operation_project_id` → `_repair_command_authorized` (`:976–987`) → `integration_repair_start` / `record_repair` / `repair_timeout` / `repair_dispatch` | same | returns `None` → a false **`unauthorized`**, masking the service's own `stale`; no state filter, reachable from playbook replays and timer deadlines | same helper |
| 8 | `development.py:1379–1387` `cancel_preserving` | same | **`.one()` → `NoResultFound`**, after the delegate pauses, owner releases, `state="cancelled"` and stage cancels already issued in the same transaction — all rolled back. Not in the command's `except (ValueError, RuntimeError, KeyError)` (`integration_commands.py:1792–1799`). Reachable for an operation **already `cancelled`** (the replay branch, `:1239`) whose verifier — a separate root — is still unsettled | helper; needs `project_id`, `repo_id`, `branch_name` for the journal row. Absent from both tables → typed `ValueError` |
| 9 | `execution.py:1523` (verifier close) | `verifier_operation.parent_task_id`; lookup at `:1455` is state-independent | `parent is None` → "Complete the verified parent integration before closing this verifier task" → **the verifier can never close** | helper; an archived parent whose archived `status` is `COMPLETED` counts as completed |
| 10 | `git_ops.py:155` `_phase_verify_aggregate_verifier` | same; `state == "completed"` is accepted at `:149` for exactly this crash-replay window | `_aggregate_verifier_retry("…not bound to the current parent checkpoint")`, forever | helper; needs `repo_id`, `branch_name`. The checkpoint row survives archive (no constraint; `_delete_one` never touches it) |
| 11 | `parent_completion.py:1177–1182` `_locked_context_on`, via `complete_parent`'s replay branch (`:1020–1065`) | the parent id | `HierarchyError("invariant_error", "parent task does not exist")` — typed, but wrong | for `operation.state == "completed"`, answer `already_completed` from the completion and verification rows when the helper says the parent is archived |
| 12 | `integration_state_queries.py:143–150` `get_integration_operation_artifact_route` | `operation.parent_task_id` (OUTER JOIN for `coalesce(batch.project_id, tasks.project_id)`) | row returned with `project_id = NULL`; `resolve_integration_route` (`playbooks/services.py:148–149`) then drops a **system-scoped** route and the operation-bound event is silently unrouted | derive the project through `integration_parent_episodes.repository_id → repos.project_id` — it also survives `delete_archived_task` |
| 13 | `integration_state_queries.py:245–256` `get_repair_filing_scope` (parent target) | `operation.parent_task_id` | returns `None`, which callers cannot tell from "not a repair delegate" → **silent path change**: `execution.py:1349–1353` skips the retired-delegate refusal and takes the generic close/PR path over a retained repair branch; `task_commands.py:1559–1563`, `:2292–2297` treat the filing as an ordinary worker's | helper (`id`, `project_id`, `repo_id`, `branch_name`); when the target is gone entirely return the scope with `active=False`, never `None` |
| 14 | `integration_commands.py:1717` `_cmd_delivery_receipts` | the caller-supplied `source_task_id` | false **`unauthorized: receipt query is outside the source project`** although the read at `:1730` would succeed — a pure history read, the one an operator wants *after* archive | helper; `project_id`, `repo_id` |
| 15 | `promotion.py:845–848` `_validated_route`, called by `prepare` at `:130` **before** the existing-intent check at `:134–141` | `request.source_task_id`, then its parent | `PromotionSourceMoved` on an idempotent replay of an already-`committed` intent | when an intent with this domain key is already `committed`, return its stored value **before** validating the route against live rows; an uncommitted intent still validates first |

### 5.3 Hardening — live-only, but the failure mode is unacceptable

| # | `file:line` — function | Today, row missing | Change |
|---|---|---|---|
| 16 | `repair.py:2970` `_activate_debug_on` → #5 | uncaught; the stage and operation UPDATEs at `:2910–2969` roll back; **TICK-RETRY forever** because the stage never expires | via #5 |
| 17 | `repair.py:3031` `_human_block_on` → #5 | same | via #5 |
| 18 | `repair.py:3017–3024` `_human_block_on` → `_apply_transition(parent → BLOCKED)` | a logged no-op (`task_queries.py:928–939`); only #17's raise undoes it | **required once #17 is tolerant**: an explicit missing-parent refusal here, or the operation becomes `human_required` with no parent BLOCKED |
| 19 | `recovery_controls.py:483–485` `_restore_completed_delegate_on` (the reviewer's `:484`) | `.one()` → `NoResultFound` → CMD-ERR; shielded today only by call order (`repair.py:421–422` returns `stale` first) | `.one_or_none()` → `(None, "stale")`. The full live row is needed here, so no archive fallback |
| 20 | `controls.py:1193–1209` `_has_active_work_on` | OUTER JOIN `tasks` for `project_id`; a live parent operation whose parent row is gone counts for **no** project, so a drain completes over it — fails open | derive the project as in #12 |
| 21 | `doctor/integration_checks.py:456–491` `_find_stranded_fences` | already degrades, reporting "gone" | use the helper to say "archived (status X)" |
| 22 | `completion_recovery.py:375` `recover_completed_pool_claims` | `get_task` not None-checked → `AttributeError` (a race with `:357–372`) | `continue` |
| 23 | `promotion.py:1116` `_provenance` | `(await get_task(…)).branch_name` → `AttributeError` (a race with #15) | use the row #15 already resolved |
| 24 | `repair.py:1892–1908` `record_batch_rebuild_conflict_on` | returns `busy` — retryable-looking — and, unlike `dispatch`, never tries `_restore_archived_delegate_on` | optional: a distinct outcome |

### 5.4 Live-only — unreachable once archived, *given §4.2*

Every row is guarded by an operation-state or live-session check, so it cannot meet an archived id while a running operation refuses archive in every mode. Each already degrades to a typed `stale` / `None` / `False` unless noted.

| `file:line` — function | id | If it ever did run |
|---|---|---|
| `orchestrator/workspace.py:610–615` (the reviewer's `:611`; `get_task` at `:613`) | `operation.parent_task_id` via `get_active_integration_verifier_for_task` (live states only) | **raises** `ValueError("integration owner subject does not exist")` — accurate as reported. Unreachable *only because* the parent seat refuses (`delegate_release.py:396–399`). This row is the reason §9 test 14 exists |
| `orchestrator/workspace.py:548–563` (`get_task` at `:554`) | `repair.parent_task_id`, operation `active` / `escalated` | `ValueError("repair parent target does not exist")` → prepare failure |
| `repair.py:3120–3126` `_start_context_on` (the reviewer's `:3218`, before `18c760836`) | `operation.parent_task_id` | `_RepairInvariant`, caught by both callers → `invariant_error` / `stale` |
| `recovery_controls.py:377–381`, `:434–457` `_restore_parent_collection_on` | same | `(None, "stale")` |
| `recovery_controls.py:475–477`, `:545–556` | `stage.repair_task_id` | `stale` |
| `recovery_controls.py:791–793`, `:795–796` `_safe_live_resolution_resume_on` | parent; `stage.repair_task_id` | `None` → generic resume |
| `repair.py:414–422`, `:506–510` | `operation.parent_task_id` | `stale` |
| `repair.py:556–562`, `:692–701`, `:898–906`, `:1062–1066`, `:1127–1133`, `:1503–1509`, `:1525–1532`, `:1934–1945`, `:1974–1982` | `stage.repair_task_id` | `stale` / `human_required` / `False`. `:898–906` already falls to `_restore_archived_delegate_on` (`:2579–2606`), the existing un-archive of a live stage's delegate |
| `repair.py:933–941` `dispatch` | computed `repair-<op>-<stage>` — a collision probe that looks in `tasks` only | creation and stage linking share one transaction; see §8.2 |
| `repair.py:1163–1167`, `:1171–1173`, `:1245–1251` `complete_delegate` | the closing session's own task | `stale` |
| `repair.py:2160–2164` `_dispatch_context_on` | `operation.parent_task_id` | `None` → `stale` |
| `repair.py:2261–2263` `_reuse_verifier_on` | `verifier_task_id or parent_task_id` | `None` → files a new delegate. Breaks only if the column is NULLed (§2.2) |
| `repair.py:2426–2430` `_retained_debug_handoff` | owner's `owner_id` | `None` → `busy` |
| `repair.py:543–545` `_continue_parent_stage_on` | `integration_promotion_intents.source_task_id` — the completed **child** whose delivery conflicted | `stale`. No seat protects this child today and none did before; a pre-existing exposure, recorded, out of scope |
| `parent_completion.py:290–292` `mark_ready_on` | derived `verify-<op>`, only while `verifier_task_id IS NULL` | creates the verifier |
| `parent_completion.py:389–404` `readiness_on` | children, driven FROM `tasks` | an archived child drops out and a mid-chain `code` receipt yields a `receipt_chain` blocker (`:555–558`) — fail-silent. Refused today by `delivery_target_fixed`; a D3 hazard |
| `parent_completion.py:656–659`, `:664–666` `record_disposition` | child, then parent | typed `invalid` |
| `parent_completion.py:974–988` `wake_verifier` | `operation.verifier_task_id` | `_apply_transition` no-ops and the call still answers `woken` — fail-silent |
| `hierarchy.py:878`, `:906` → `_task_row` (`:1098–1102`) | `task_branch_origins.task_id` | `HierarchyError("invalid")`; the drain's page inner-joins `tasks` (`branch_materialization.py:80–107`) |
| `candidates.py:1639–1641` `_return_completed_repair_on` | `stage.repair_task_id` | returns; the batch then waits in `authority_wait` |
| `candidates.py:722`, `:1424`; `promotion.py:386`, `:660` → `get_repair_filing_scope` | live writer session | typed stale / `PromotionTargetMoved` |
| `promotion.py:776`, `:1268` → `finalize_integration_promotion` (`integration_delivery_queries.py:608–617`, `:716–726`) | `intent.target_task_id` | `ValueError("parent readiness projection failed")`; first finalize requires a live operation (`:625–645`) |
| `collection.py:73`, `:138–157`, `:214–216` | parent; owner's `owner_id` | `"waiting"`; returns |
| `ci.py:595–597`, `:615–621` | `subject.parent_task_id` | `stale_subject` |
| `review_evidence.py:212–216`, `:247–251`, `:293–299` | reviewer; `evidence.source_task_id` | typed `stale_head` |
| `development.py:1311–1319` `cancel_preserving` (delegate loop) | delegate ids | `if task and …` skips |
| `workspace.py:1398`, `:1558`, `:1766`; `workspace_attachments.py:171–181`, `:462–468` | owner / session task | `False` / `RuntimeError`; each requires `READY` or `IN_PROGRESS`, never archivable |
| `integration_commands.py:37` (called at `:80`, `:122`), `:89`, `:110`, `:202–212`, `:225–232`, `:1286`, `:1494` | parent / next owner / source / session task | typed `human_required`, `target_moved`, `source_moved`, `stale`. `:210` calls `readiness(parent_id)` outside its `try` — a `HierarchyError` would escape; live-only |
| `integration_delivery_queries.py:97–105`, `:107–115` `append_integration_review_evidence` | the `source_task_id` being written | `ValueError("review evidence source task project does not exist")` |
| `hierarchy_queries.py:477–538` `hierarchy_prerequisite_delivery_head` | `receipt.source_task_id` via `task_dependencies` | returns `None` and the child is prepared from its filing base **without** the delivered prerequisite head. Excluded today by `delivery_target_fixed`; the second D3 hazard |

### 5.5 Correct as-is — driven FROM `tasks`; an archived row *should* drop out

| `file:line` | Why exclusion is right |
|---|---|
| `delegate_release.py:169–187` `_stranded_statement` (and its users: `:259–265`, `recovery_controls.py:604–610`, `:646–649`, `repair.py:88–103`, `development.py:1427–1433`, doctor `integration.stranded_delegates`) | an archived delegate is already settled |
| `development.py:1242–1247` `cancel_preserving` replay probe | an archived delegate is terminal, so not "pending" |
| `status.py:288–321`, `:407–421`, `:522–525` (`aq integration status`, `aq task explain`) | an archived task must not be a rollout blocker. `:522–525` words an archived child's blocker as `open_child` — cosmetic. **The status read does not raise on an archived parent or verifier** |
| `collection.py:40–60`, `parent_ci.py:63–84`, `branch_materialization.py:80–107`, `:163–182` | live loops |
| `integration_train_queries.py:45–216` `eligible_root_page_on` | exclusion is right **only because §3.2 now refuses** to archive a root that is still a candidate. This was B1's hazard |
| `hierarchy_queries.py:255–287`, `:305–380`, `:792–803`, `:847–871`; `claim_queries.py:75–81`, `:401` | frontier predicates and the guard's own reads, all over live ids |
| `task_recovery_queries.py:241–255`; `doctor/integration_checks.py:233–270`, `:299–328` | live tasks only; `:571–587` already reads `tasks` then `archived_tasks` |
| `workspace_attachments.py:241–249` `recover_stopped_integration_pool_claim` (from `recovery_controls.py:336–349`, before any state guard) | returns `False`, which is the right answer |
| `completion_recovery.py:98–116`, `:213–221`, `:357–372`; `development.py:1531–1533`, `:1574–1584` — the two owner reconcilers, inner-joining `tasks` on `owner_id` | an owner row whose task was archived becomes invisible to both while status keeps reporting `active_owner`. `owner_id` has no constraint, so this exists today; **§4.4 is what closes it** — an attached owner row now refuses the archive |
| `workspace.py:1188–1256` `aconfirm_integration_owner_handoff` + `workspace_attachments.py:1046–1050` | after archive NULLs `sessions.task_id` the handoff can never be confirmed. Same closure: §4.4 |

### 5.6 Never resolved against `tasks` — where the draft was right

Filter columns only: every use of `integration_parent_episodes.parent_task_id`, `integration_parent_verifications.parent_task_id` and `integration_parent_operation_completions.parent_task_id` in `parent_completion.py` (`:146`, `:173`, `:184`, `:386`, `:475`, `:480`, `:1037`, `:1052`) and `hierarchy.py:727–764`; `integration_candidate_resolutions.repair_task_id` in `candidates.py` (`:786`, `:821`, `:1097–1112`, `:1443`); `_ambiguous_writes_on` (`recovery_controls.py:984–1167`, through `sessions` and `workspaces` — §2.3); `live_integration_owner`; `get_terminal_integration_delegate_operation`, `get_active_integration_verifier_for_task`, `get_integration_verifier_operation`, `get_active_parent_integration_operation`, `_recovery_owner` (`task_recovery_queries.py:160–230`), `list_integration_delivery_receipts`; and all of `cleanup.py`, `main_promotion.py`, `scheduler.py`, `release.py`, `ownership.py`, `attestation.py`, `branch_discard.py`, `outbox.py`, `preflight.py`, `service.py`.

### 5.7 The resolve-task-or-archived-task helper

Five per-caller versions of this exist already (`DevelopmentIntegration.resolve_task` at `development.py:950–982`, `doctor/integration_checks.py:571–587`, `playbook_run_queries.py:1422–1440`, `activity_queries.py:114–128`, `task_comment_queries.py:198–209`), and `task_names.py:101` answers only "is the id taken". One shared, connection-taking form, in `src/database/queries/task_identity.py`:

```python
@dataclass(frozen=True)
class TaskIdentity:
    task_id: str
    project_id: str
    repo_id: str | None
    branch_name: str | None
    parent_task_id: str | None
    status: str
    archived: bool          # True: the row came from archived_tasks

async def resolve_task_identity_on(conn, task_id: str) -> TaskIdentity | None:
    """The live row if there is one, else the archived row, else None.  Read-only, no lock."""
```

Rules:

1. **Identity only.** It never locks and is never the basis of a transition. A caller that must act on the live row keeps its own `SELECT … FOR UPDATE` and treats a miss as its existing typed `stale`.
2. **The live row wins.** `_restore_archived_delegate_on` recreates the active identity *before* deleting the archive snapshot, so both can exist inside one transaction.
3. **`None` is a legal answer** — after `delete_archived_task` of an unreferenced id, and for pre-existing orphans. Every caller maps it to a typed outcome; none may raise an untyped error. Where only the project is needed and an episode exists, prefer `episode.repository_id → repos.project_id` (#12, #20), which outlives both tables.
4. `archived_tasks` carries `project_id`, `repo_id`, `branch_name`, `status`, `pr_url`, `parent_task_id`, `created_by_kind/id`; it does **not** carry `claim_epoch`, `dedup_key` or `intelligence_class`. No listed caller needs those.
5. `DevelopmentIntegration.resolve_task` delegates to it (adding `description`), so there is one rule for "where does this task live".

**Callers that must tolerate an archived parent:** #1–#15, #16–#17, #21. **Callers that must not** (they need the live row and answer `stale`): #19 and everything in §5.4.

---

## 6. Writer audit

### 6.1 Can a write arrive naming a task that is already archived?

| writer | `file:line` | guarded by | post-archive? |
|---|---|---|---|
| episode INSERT | `parent_completion.py:104` | collection path; the parent is loaded from `tasks` `FOR UPDATE` and must have a checkpoint | no — it needs the parent back in `tasks`, and there is no unarchive for a parent (§8) |
| verification INSERT | `parent_completion.py:866` | requires a live operation and a live verifier closing | no — §4.3 refuses archive against a live operation |
| `verifier_task_id` UPDATE | `parent_completion.py:313–316` | CAS on `IS NULL`, parent `PAUSED` and live | no |
| resolution INSERT / UPDATEs | `candidates.py:776`; `:1117`, `:1173`, `:1283`, `:1367` | authenticated live session; CAS on `(id, state)` | no — its operation is live |

The argument rests on one fact: **an ended operation is never revived.** Every write that sets a live state CASes on `state == "human_required"` (`recovery_controls.py:281–288`, `candidates.py:1023–1026`); every other write moves *into* a terminal state and CASes too (`parent_completion.py:1148–1153`, `main_promotion.py:1188–1195`, `recovery_controls.py:573–580`). Two writes do not — §6.2. If a future "reopen a completed operation" feature weakens this, the refusal belongs in that reopen path, which needs the parent back in `tasks` anyway.

No database-level guarantee replaces the dropped constraints, deliberately — the same trade `task_comments`, `task_branch_origins`, `integration_delegate_releases` and `integration_repair_stages.repair_task_id` already make.

### 6.2 The two escalation updates gain a CAS (S1)

| `file:line` | sets | WHERE today | becomes |
|---|---|---|---|
| `repair.py:2962–2969` `_activate_debug_on` | `active_stage=1, state="escalated"` | `id = :id AND active_stage = 0` | `… AND state = 'active'` |
| `repair.py:3007–3014` `_human_block_on` | `state="human_required"` | `id = :id AND active_stage = :ordinal` | `… AND state IN ('active','escalated')` |

Neither filters on `state` nor checks that a row was updated. `expire` re-checks the state under the row lock (`:1316–1321`); `record_result` never does — it relies on the *stage* state (`:766`), which holds only because every transition to a terminal state settles the active stage in the same transaction. If that ever slips, a `completed` or `cancelled` operation is flipped back to a live state: §6.1's one-way claim becomes false and the archive rule with it. Both raise on `rowcount != 1`, as `:690–691` and `:2571–2576` already do.

---

## 7. The migration

### 7.1 Revision id

The tree's head at `6bc84ba7d` is **`a00000000013`**; production is stamped **`a00000000011`**, so `a00000000012` and `a00000000013` reach it in the same upgrade. The new revision is the next free id **at implementation time** — `a00000000014` today. Sibling branches have collided on these ids twice: re-read `migrations/versions/` and check `ScriptDirectory.get_heads()` immediately before writing the file. Hand-written, never autogenerated from a worktree; `tables.py` and the revision land in one commit.

### 7.2 DDL — match by columns, fail if any remains (S2, S6)

Idempotent: the squashed baseline builds from live metadata, so a database created after the `tables.py` edit never had these constraints and the revision must be a no-op there. It reads `pg_constraint` directly — PostgreSQL is the only backend, the catalog never caches, and it finds a constraint whatever it is named:

```python
_HISTORY_COLUMNS = (
    ("integration_parent_episodes", "parent_task_id"),
    ("integration_parent_verifications", "parent_task_id"),
    ("integration_repair_operations", "verifier_task_id"),
    ("integration_candidate_resolutions", "repair_task_id"),
)
_INDEXES = (  # name, table, column, partial predicate
    ("idx_integration_repair_operations_verifier_task", "integration_repair_operations",
     "verifier_task_id", "verifier_task_id IS NOT NULL"),
    ("idx_integration_candidate_resolutions_repair_task", "integration_candidate_resolutions",
     "repair_task_id", None),
    ("idx_integration_repair_stages_repair_task", "integration_repair_stages",
     "repair_task_id", "repair_task_id IS NOT NULL"),
)
_TASK_FKS = sa.text("""
    SELECT c.conname, array_length(c.conkey, 1) AS width
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.contype = 'f'
       AND c.conrelid = to_regclass(:table) AND c.confrelid = to_regclass('tasks')
       AND a.attname = :column
""")

def upgrade() -> None:
    bind = op.get_bind()
    for table, column in _HISTORY_COLUMNS:
        for name, width in bind.execute(_TASK_FKS, {"table": table, "column": column}).all():
            if width != 1:   # a composite key through this column is not ours to drop
                raise RuntimeError(f"{table}.{column}: composite foreign key {name} onto tasks")
            op.drop_constraint(name, table, type_="foreignkey")
    left = [(t, c, n) for t, c in _HISTORY_COLUMNS
            for n, _ in bind.execute(_TASK_FKS, {"table": t, "column": c}).all()]
    if left:
        raise RuntimeError(f"foreign keys onto tasks remain: {left}")
    # …then each index in _INDEXES, guarded by pg_indexes.
```

A table that does not exist yields no rows (`to_regclass` is NULL), which is the fresh-database no-op. The three indexes are not bookkeeping: none of the three columns is indexed today, and §4's probes are `column IN (:ids)` on every archive attempt. `integration_repair_operations.parent_task_id` and the episode / verification tables are already covered by unique indexes leading with that column.

**Lock note.** Dropping a foreign key takes `ACCESS EXCLUSIVE` briefly on the referencing table *and* on `tasks`. It is catalog-only, but it queues behind any open read and then blocks everything. Run with the daemon stopped and `SET lock_timeout = '5s'`; retry rather than wait.

### 7.3 What the triggers do and do not cover

* The append-only triggers (`integration_guards.py:427–448`) are row-level DML triggers; `DROP CONSTRAINT` is DDL. **No user trigger fires and none is touched.** Dropping the constraint does drop PostgreSQL's *internal* RI triggers — that is the mechanism being removed.
* `integration_repair_operations` has no append-only trigger: `trg_integration_repair_operation_stage_monotone` (`:379–381`) and the empty-batch guard only. NULLing `verifier_task_id` would be mechanically permitted; §2.2 decides, not the trigger layer.
* `integration_candidate_resolutions` freezes `repair_task_id` (`:195`).
* No `CheckConstraint` on the four tables mentions a task column.

### 7.4 `tables.py`, `legacy_sqlite_import.py`

Remove exactly the four `ForeignKeyConstraint(… ["tasks.id"] …)` entries; add the three `Index(...)` declarations. After the edit the metadata walk finds four fewer foreign keys onto `tasks`. `legacy_sqlite_import.py`: comment-only — the hand-written insert order stays valid, merely stricter than required.

### 7.5 Tests that assert the constraints exist (S5)

`tests/test_migration_parent_collection.py:58–62` and `tests/test_migration_candidate_authority.py:28` list the four names in a `<=` assertion, and **run only under the `migration` marker**, which `aq test` deselects by default — a green default run proves nothing about them. They change to assert absence, and the implementer runs, with `POSTGRES_TEST_DSN` exported:

```
aq test -m migration tests/test_migration_parent_collection.py tests/test_migration_candidate_authority.py
```

---

## 8. Restore / unarchive interplay

### 8.1 One narrow unarchive path, unchanged

`RepairService._restore_archived_delegate_on` (`repair.py:2579–2606`, from `dispatch` at `:898–906`) recreates a repair delegate from its `archived_tasks` snapshot and deletes the snapshot. It restores only a task matching `_delegate_task_matches` (`:2609`) — same project, no parent, same repo and branch, `created_by_kind == "integration_repair"`, `created_by_id == operation.id` — for a **still-active stage**, under the operation and stage locks. After this change it is reachable only for a delegate archived *before* the upgrade, because §4.3 refuses to archive a live operation's delegate. There is no general unarchive: `restore_task` is a response model and a formatter registration with no command.

### 8.2 Id reuse

`archive_identity_conflict` (`archive_queries.py:404–407`) fires when an id is archived under two projects. It cannot reach these tables: every referenced id is deterministic and uuid-derived (`verify-<operation_id>`, `repair-<operation_id>-<stage>`) or a generated root id, and `fresh_root_id` (`task_names.py:116`) reserves against `archived_tasks` as well as `tasks`. The collision probe at `repair.py:933–941` looks in `tasks` only; it is safe because delegate creation and stage linking are one transaction, and a delegate of a live operation cannot be archived.

### 8.3 `delete_archived_task`

§4.5. It already deletes `task_completion_records`, `task_comments` and `task_subtasks` only when no live row shares the id (`:753–785`), so a restore is not robbed of its history. Integration history is stronger still: it cannot be deleted, so the task deletion is what is refused.

---

## 9. Test plan (B2)

PostgreSQL only (`POSTGRES_TEST_DSN`). **No test may `INSERT` an episode, operation, verification, receipt or checkpoint by hand** — that is how the draft's headline test would have passed while the same scenario built honestly is refused by `delivery_target_fixed`. The builders in `tests/test_integration_parent_completion.py` (`_enable_project`, `_parent_tree`, `_code_receipt`, `_artifact`, `_boundary`) move to `tests/parent_collection_helpers.py` and gain:

* `collected_parent(db, *, children, outcome)` — `HierarchyIntegration.file_children` → `checkpoint_parent` (which creates the checkpoint, the episode and the operation through `_ensure_episode_on`) → one receipt per child bound to the real `parent_operation_id` / `parent_episode_id` → `mark_ready_on` → `record_verification` → `complete_parent`, stopping at the requested `outcome` (`collecting`, `verified`, `completed`, `human_required`).
* `switched_to_development(db, service)` — the real `DevelopmentIntegration.configure`, on the real-Git fixture `tests/test_development_integration.py` already has. **This reproduces production's shape exactly**: hierarchy-era episode, `active` operation with zero stages, live origins, `reserved` owner rows — in a development project.
* `delivered_by_sweep(service)` / `adopted(service, task_ids)` — the real publisher.

New file `tests/test_archive_integration_history.py`, plus edits to `tests/test_hierarchy_archive_delete.py` (§4.7), `tests/test_archive.py`, `tests/test_integration_delegate_release.py`.

**Schema**

1. `test_no_integration_history_column_has_a_foreign_key_onto_tasks` — metadata walk.
2. `test_history_dispositions_have_no_foreign_key` — §4.7's ratchet.
3. `test_upgrade_is_idempotent_and_drops_a_renamed_constraint` (`migration`) — rename one constraint first; `upgrade()` twice; both succeed, nothing remains. *Proves S2.*
4. `test_upgrade_fails_loudly_on_a_composite_key` (`migration`).
5. `test_append_only_triggers_survive_the_migration` — `UPDATE` and `DELETE` on an episode still raise.

**Earlier guard refusals, each through `collected_parent` (§3.6)**

6. `test_sealed_root_is_refused_in_every_mode` — hierarchy, and after `switched_to_development`: `sealed` both times. *Fails today in development mode.*
7. `test_delivered_hierarchy_root_is_still_refused_delivery_target_fixed` — `completed`, root receipt present: `delivery_target_fixed`, **unchanged**. *Pins D3 so nobody relaxes it by accident.*
8. `test_undelivered_candidate_reports_undelivered_not_delivery_target_fixed` — `completed`, no root receipt: `integration_undelivered`, holder `awaiting_train`; parametrized with a `hold:` label (`hold_label`) and an open gate (`open_gate`).
9. `test_held_leaf_root_is_refused` — a `COMPLETED` leaf root with `pr_url`, a checkpoint and a `hold:` label, no episode, no receipts. *Fails today: it archives and leaves the train.*
10. `test_failed_root_with_collected_code_is_refused` — `collected_not_promoted`.
11. `test_a_review_root_that_was_never_a_candidate_still_archives` — no `pr_url`, no checkpoint. *Guards the 2026-09-08 decision.*
12. `test_delete_still_asks_branch_discard_required` and `test_archive_never_does` — unchanged behaviour, pinned.

**The running-operation refusal**

13. `test_live_operation_refuses_every_seat_in_every_mode` — parent / verifier / repair stage / candidate member × `active` / `escalated` / `human_required` × hierarchy / development / disabled, for **archive and delete**.
14. `test_the_live_only_readers_are_unreachable` — for a live operation, archive of the parent is refused in development mode, *then* `workspace.py:610–615` and `controls._has_active_work_on` are exercised and succeed. *This is §5.1's argument as a test.*
15. `test_rejected_resolution_of_a_live_operation_refuses` — *fails against PR #612's seat definition.*
16. `test_message_names_a_control_that_accepts_the_state` — for each live state, the named command is run and does not answer `invalid_state`.

**Preserved resources (S4)**

17. `test_attached_owner_row_refuses_archive_and_delete`; 18. `test_a_delegates_workspace_lock_refuses_and_survives`; 19. `test_detached_reservations_are_released_by_the_removal` (every mode, every role); 20. `test_a_non_delegates_stale_lock_is_still_cleared`.

**Development-mode delivery**

21. `test_mode_switched_root_archives_once_delivered` — `collected_parent` → `switched_to_development` → `cancel_preserving` → sweep: archives; **episode, operation and receipts unchanged afterwards**. *The headline case.*
22. `test_completed_but_unpublished_task_is_refused` — holders `awaiting_publication`, `open_gate`, `blocked_dependency`; no integration history at all.
23. `test_adopted_task_archives` — depends on P1; **fails until `swift-orbit.4` lands**.
24. `test_parked_batch_still_reports_through_the_development_hold` — unchanged message.
25. `test_abandon_undelivered` — skips only the delivery rule; refused for a session principal; writes the comment and the event; still refused by `integration_owned`.

**Delete paths (S3)** — 26. `test_delete_refuses_history_in_every_state` (the four columns × live / `completed` / `cancelled`) and `test_a_released_stage_delegate_can_still_be_deleted` (PR #612's behaviour, pinned); 27. `test_delete_archived_task_refuses_history`; 28. `test_delete_project_refuses_history_typed` (history on a live task, on an archived task, on a repository); 29. `test_delete_project_without_history_still_works`.

**Readers (§5.2), each with the parent archived through the real path** — 30. `resume` / `abort` / `release_delegates` answer typed outcomes; 31. `cancel_preserving` replay of a `cancelled` operation completes and journals; 32. a verifier replays its close after the parent is archived and **closes**; 33. `complete_parent` replay answers `already_completed`; 34. a system-scoped route still resolves; 35. `get_repair_filing_scope` returns `active=False`, never `None`; 36. `delivery_receipts` reads an archived source; 37. `prepare` replay returns the committed intent; 38. every helper caller maps `None` to a typed outcome.

**Writers (S1)** — 39. `test_escalation_updates_refuse_an_ended_operation`.

**The sweep** — 40. `test_the_hourly_sweep_archives_a_released_root_and_clears_its_record`; 41. `test_a_changed_detail_rewrites_the_refusal_record` and `…an_unchanged_one_does_not`.

Area suites at the end: `tests/test_archive*.py`, `tests/test_hierarchy_archive_delete.py`, `tests/test_integration_{hierarchy,state,repair,candidates,controls,parent_completion,delegate_release,completion_recovery,promotion}.py`, `tests/test_development_integration.py`, `tests/test_doctor_integration_checks.py`, `tests/test_supervisor_recovery.py`, `tests/test_database_postgresql.py` (where `delete_project` is covered), the two `migration` files (§7.5), and `scripts/e2e-smoke.sh` (the hierarchy scenarios).

---

## 10. Deployment plan for the production database

Operator only, outside any worktree slot (`CLAUDE.md`, *Never migrate the operator's database*). PostgreSQL 18.3, container `aq-postgres`, port 5533.

**0. Prerequisites, deployed and observed first.** P1 (`swift-orbit.4`); P2 (`swift-orbit.5`) if row 7 is to move. Confirm `a00000000012` / `a00000000013` are acceptable to take in the same upgrade — they are already on `main`.

**1. Freeze.** Set `archive.enabled: false` in `~/.agent-queue/config.yaml`. This keeps the schema rollback open: until something referenced is archived, `downgrade()` still works. `aq stop`.

**2. Backup**, per [Migrations reference → Backup and restore](../../reference/database/migrations.md#backup-and-restore). `pg_dump` is not installed on the host; use the container's:

```bash
docker exec aq-postgres pg_dump --format=custom --no-owner -U agent_queue agent_queue \
  > ~/agent-queue-pre-history-fk-$(date +%F).dump
docker exec -i aq-postgres pg_restore --list < ~/agent-queue-pre-history-fk-*.dump | head   # readable?
```

**3. Preflight query** — expected on 2026-09-21: four constraints; seven referenced ids, all still in `tasks`; three live operations.

```sql
SELECT conrelid::regclass, conname, confdeltype FROM pg_constraint
 WHERE contype = 'f' AND confrelid = 'tasks'::regclass
   AND conrelid::regclass::text LIKE 'integration_%' ORDER BY 1;                       -- 4 rows

WITH named(src, tid) AS (
        SELECT 'episode', parent_task_id FROM integration_parent_episodes
  UNION SELECT 'verification', parent_task_id FROM integration_parent_verifications
  UNION SELECT 'verifier', verifier_task_id FROM integration_repair_operations
         WHERE verifier_task_id IS NOT NULL
  UNION SELECT 'resolution', repair_task_id FROM integration_candidate_resolutions)
SELECT n.src, n.tid, t.status, (a.id IS NOT NULL) AS archived
  FROM named n LEFT JOIN tasks t ON t.id = n.tid LEFT JOIN archived_tasks a ON a.id = n.tid
 ORDER BY 1, 2;                                          -- 7 rows, none archived, none orphaned

SELECT o.id, o.state, o.parent_task_id, p.hierarchical_integration_mode
  FROM integration_repair_operations o JOIN tasks t ON t.id = o.parent_task_id
  JOIN projects p ON p.id = t.project_id
 WHERE o.state IN ('active','escalated','human_required');                             -- 3 rows
```

**Stop if** a referenced id is missing from both tables, or an eighth id appears: §11 no longer describes the database.

**4. Migrate.** `aq db current` (expect `a00000000011`), then `aq db upgrade`. On a lock timeout, find the blocker and retry; never wait.

**5. Verification query** — before starting the daemon.

```sql
SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND confrelid = 'tasks'::regclass
   AND conrelid::regclass::text LIKE 'integration_%';                                   -- 0
SELECT indexname FROM pg_indexes WHERE indexname IN (
  'idx_integration_repair_operations_verifier_task',
  'idx_integration_candidate_resolutions_repair_task',
  'idx_integration_repair_stages_repair_task');                                          -- 3
SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE 'trg_integration_parent_%';
                                                         -- unchanged from before step 4
SELECT (SELECT count(*) FROM integration_parent_episodes)       AS episodes,             -- 5
       (SELECT count(*) FROM integration_repair_operations)     AS operations,           -- 11
       (SELECT count(*) FROM integration_candidate_resolutions) AS resolutions;          -- 1
```

Row counts equal to step 3 prove the DDL touched no row.

**6. Start and canary.** `aq start` (sweep still off). `aq doctor`; `aq integration status agent-queue`. Then archive **one** row by hand — the verifier, the only row with no other condition: `aq task archive --task-id verify-81d0aaee-0c3a-482c-b04c-d3afe6631cbe`. Verify `aq task list-archived` shows it, `integration_repair_operations.verifier_task_id` still names it, and `aq integration status` / `aq doctor --check integration.stranded_delegates` / `--check integration.stranded_fences` still answer. **This is the point of no return for the schema** (step 8).

**7. Re-enable the sweep** (`archive.enabled: true`, then `aq restart`) and watch the first hourly pass: `aq task archive-settings` and `aq doctor --check tasks.archive_blocked` should show §11's dispositions and nothing else.

**8. Rollback.**

* *Before the canary:* `alembic downgrade -1`, with `AGENT_QUEUE_DB_URL` exported (there is no `aq db downgrade`), re-creates the four constraints (the check below passes because nothing referenced has left `tasks`) and drops the indexes. Or restore the dump into a **new** database and repoint `database.url`.
* *After it:* `downgrade()` counts, per column, the ids no longer in `tasks`, and **refuses** with the counts when any exist. There is no remedy that keeps the data: the triggers forbid deleting the dangling audit rows, and there is no reverse of `_archive_one`. A loud refusal is deliberate — a schema that claims a constraint the database cannot satisfy is worse than a failed downgrade.
* *The real rollback is the code.* Revert the guard change and the blanket refusal is back exactly as today; the missing constraints are harmless, because a missing foreign key blocks nothing. `archive.enabled: false` and a restart stop further archives.
* *Last resort:* restore the dump. Everything written since step 1 is lost, so this is for corruption, not for regret.

---

## 11. Disposition of the seven rows

### 11.1 Facts

Read-only, 2026-09-21: three scripts of `SELECT`s inside `SET TRANSACTION READ ONLY`; nothing was written, and no integration operation was aborted, cancelled or waived.

* The seven are the entire population (§Changes, 1). All are in development-mode projects: `agent-queue` ×5, `matter-engine-cpp` ×2. Zero `integration_parent_verifications` rows exist.
* No `hold:` label, no unresolved gate, no workspace lock, no live session and no batch membership in any of the seven subtrees.
* Receipts: 37, of which 17 are root receipts whose sources are **already** in `archived_tasks` — history outliving its task is not new here.
* Six `archive_refusal` records exist, all `integration_owned`, all among these seven.

| # | Row | Status | Operation | Delivery (daemon's predicate) | Holds |
|---|---|---|---|---|---|
| 1 | `keen-harbor` (17 tasks, all COMPLETED) | COMPLETED | `81d0aaee…` **cancelled** | 1 pending — the root; **false**: `adopted` row `77108b62…` and `delivered` row `ef24664d…` both target `refs/heads/main` (P1) | 16 detached `reserved` rows |
| 2 | `noble-ridge` (13) | COMPLETED | `640ced19…` **cancelled** | 2 pending — root and `.12`; **false**, both in `adopted` row `7f461dd5…` (P1) | 12 detached `reserved` |
| 3 | `nimble-dune` (10) | COMPLETED | `4caf5ff3…` **active**, 0 stages, never updated | 0 pending | 5 detached `reserved` |
| 4 | `smart-dune` (12) | COMPLETED | `096374ac…` **active**, 0 stages, never updated | 0 pending | 6 detached `reserved` |
| 5 | `sound-current` (9) | COMPLETED | `b02de174…` **active**, 0 stages, never updated | 0 pending | root owner row **`attached`** (session `be431efc…` stopped, `claim_phase = 'active'`, workspace `ws-hidden-hall`); 1 detached `reserved` |
| 6 | `verify-81d0aaee-…` | FAILED, retired 2026-09 by delegate release, cleanup `clear` | `81d0aaee…` cancelled | n/a (`FAILED`) | none |
| 7 | `repair-repair-batch-…2c48…-1` | **BLOCKED**, unassigned, never released | batch operation **cancelled**; batch `aborted`; its resolution `rejected` | n/a | owner row **`attached`**, role `repair`, on `refs/heads/aq/integration/p-…/r-…` (session `4baaded6…` stopped, `claim_phase = 'active'`; workspace `ws-upper-dock`, since re-locked by another task) |

### 11.2 Expected disposition under this spec

| # | Row | Outcome | Why |
|---|---|---|---|
| 6 | the verifier | **Archived** — the step-6 canary | No running operation (cancelled); no preserved resource (its release recorded cleanup `clear`); `FAILED`, so nothing to deliver. The constraint was the only thing holding it. Its history stays: the operation row still names it, and its retirement record is carried into `integration_delegate_releases` at archive (§4.4), because it was retired before that table existed and `_delete_one` deletes the metadata. |
| 1 | `keen-harbor` | **Archived by the first sweep, given P1.** Without P1: refused `integration_undelivered` — *wrongly* | Operation ended; its 16 owner rows are detached reservations, released by the removal (§4.4); its work is on `main` by two delivery rows. It must not be released by abandonment — that would record delivered work as abandoned. |
| 2 | `noble-ridge` | same as #1 | same |
| 3 | `nimble-dune` | **Must stay refused**: `integration_owned` — operation `4caf5ff3…` is `active` and owns it as parent | The operation is resumable *by state*. If the project returned to hierarchy mode it would resume collection against this parent, and §5.4's readers would meet an archived id: `workspace.py:613` raises, `controls.py:1193` fails open, `development.py:1386` rolls a cancellation back. That it is obsolete is a judgement, and a sweep does not make judgements. Once ended: nothing else holds it (0 pending, reservations only) → archived. |
| 4 | `smart-dune` | same as #3 (`096374ac…`) | same |
| 5 | `sound-current` | **Must stay refused**, twice: `integration_owned` (`b02de174…`), then `integration_cleanup_blocked` (the attached root owner row) | As #3; and then the owner row fences `aq/sound-current` and points at a preserved checkout. Archive would NULL `sessions.task_id`, the key that row is diagnosed and confirmed through, leaving a fence that reads as "gone" (§2.3). |
| 7 | the repair task | **Must stay refused**: `integration_cleanup_blocked` | It is an *unsettled* delegate — `BLOCKED`, never released, because the stranded predicate reads its stopped session's leftover `claim_phase` as a live writer (P2) — and it holds the attached `repair` owner row over the batch's repair ref. Its workspace has already been handed to another task, which is exactly the silent loss S4 describes; the owner row is the last record of what that checkout was. P2 → `release-delegates` settles it `FAILED` → the owner row is released by its own control → archived. |

**Net:** one row at once, two more with P1, four refused for reasons an operator can read and act on — instead of seven refused for a reason nobody can.

### 11.3 What I would do about the three live operations, and why — not done

The epic forbids aborting, cancelling or waiving anything from this task; none was.

I would run `aq integration cancel-preserving <operation> --reason "parent COMPLETED and fully delivered by development batches; operation reserved under hierarchy mode and unadvanceable since the switch"` for `4caf5ff3…`, `096374ac…` and `b02de174…`, in that order, checking `aq integration status` between each. Reasons: every parent is `COMPLETED`; every task beneath them is delivered by the daemon's own predicate; each operation has **zero stages** and `updated_at == created_at`, so there is no repair work, no delegate and no writer to preserve; no engine advances a parent operation in development mode; and `cancel-preserving` is the control built for this ("cancel obsolete repair scheduling while retaining refs and attached workspaces") — it moves nothing in git, journals a `cancelled` delivery row and releases delegates in the same transaction. `abort` is the wrong tool: it refuses any state but `human_required`. The parents are still in `tasks`, so `cancel_preserving`'s `.one()` (§5.2 #8) is safe **today** — another reason to end them before anything is archived, not after.

`sound-current` then needs its attached owner row released, which is D4.

---
## 12. Review outcome and operator preflight (2026-09-20)

*Kept verbatim from the rejected draft. Section numbers inside it refer to that draft; "Changes against the review" at the top maps each finding to this revision.*

**Verdict of the adversarial review: REJECT in this form.** The migration DDL and the id-reuse analysis hold; the refusal rule and the reader audit do not.

Blocking findings to resolve in a revision:
- **B1 — a terminal operation does not mean the work landed.** A verified, `completed` root that is still a parent-train candidate (held by a `hold:%` label or an open gate, not yet delivered to the default branch) would become archivable; archive retires its branch origin, so it silently leaves the train and never reaches main. Today the episode FK is the only thing preventing that. The rule must also require that the root's work has landed (default-branch `code` receipt / terminal batch membership) or is provably abandoned.
- **B2 — tests must be built through the real collection path**, with receipts and checkpoints, and the spec must state the expected outcome for each earlier guard refusal (`sealed`, `delivery_target_fixed`, `branch_discard_required`).
- **B3 — "no reader needs a change" is false.** 20+ reads resolve `operation.parent_task_id` against `tasks`; `RecoveryControls.resume/abort` (`_project_id_on`), `RepairService._operation_project_id_on`, `recovery_controls.py:484`, `repair.py:3218`, `orchestrator/workspace.py:611` raise on an archived parent. Redo §3.5 as a full table and make the project-id resolvers tolerate an archived parent.
- Should-change: state CAS on the two `repair.py` escalation updates (S1); match FKs by columns not names and fail if any remains (S2); `delete_project` is a third unguarded removal path (S3); archive releases a workspace lock that retirement deliberately keeps as a cleanup blocker (S4); `tests/test_migration_parent_collection.py` / `test_migration_candidate_authority.py` assert the FKs exist and run only under `-m migration` (S5); `idx_` naming and an index for `integration_repair_stages.repair_task_id` (S6).

**Read-only preflight on the operator's database (facts, not inference):**
- The four FK names match the spec: `fk_integration_parent_episodes_parent_task`, `fk_integration_parent_verifications_parent_task`, `fk_integration_repair_operations_verifier_task` (all RESTRICT), `fk_integration_candidate_resolutions_task` (NO ACTION).
- **All six stuck roots are in projects whose mode is `development`, not `hierarchy`/`train`.** `guard_integration_mutation` stands down for that mode, which is why production reached the raw `DELETE`. Every rule in this spec must be stated for `development` mode as well.
- Operation states for the five episode-bearing roots: `keen-harbor` **cancelled**, `noble-ridge` **cancelled**, `nimble-dune` **active**, `smart-dune` **active**, `sound-current` **active** (generation 8) — although all five root tasks are `COMPLETED`. `verify-81d0aaee-…` is the FAILED verifier of `keen-harbor`'s cancelled operation. No episode lacks an operation row. Whole database: 5 completed, 3 active, 3 cancelled operations.
- Live (un-retired) branch origins remain in every one of those subtrees (`keen-harbor` 17, `noble-ridge` 13, `sound-current` 9, `nimble-dune` 7 of 10, `smart-dune` 7 of 12).

**Consequence.** Even a correct version of this change would release at most three of the six roots (`keen-harbor`, `noble-ridge`, the verifier); the other three are held by operations that are still `active` on `COMPLETED` parents, which is an integration-health question (are those operations stalled?), not an archival one. And B1 applies squarely to the two cancelled roots, which still carry many live origins. The visible goal — finished work out of the graph — is better served first by hiding finished subtrees from the `active` layout variant (Task 16b); this schema change should wait for a revision that answers B1–B3 and for a decision on the three active operations.

---

## 13. Decisions for the approver

Approving this spec means saying yes to D1 and D2 and choosing on D3–D5. Nothing is implemented until then.

**D1 — Drop `fk_integration_repair_operations_verifier_task` rather than keep it and NULL the column.** §2.2. Recommended: drop. This reverses the originally agreed direction and is the one judgement in the schema change itself.

**D2 — The delivery rule applies to every archive in a development project, not only to tasks with integration history.** §3.3. Recommended: yes — the hazard does not depend on history and has already cost 25 parked deliveries. The cost is that a `COMPLETED` task the publisher can never deliver (its branch is gone and it was never merged) stays refused until an operator adopts or abandons it; `aq doctor --check tasks.archive_blocked` will show how many on the first sweep. The narrower alternative — apply it only when history names the subtree — is safe to choose and leaves the existing hole open.

**D3 — Leave `delivery_target_fixed` alone.** §3.6. Recommended: yes, for now. In hierarchy/train mode it keeps every delivered parent unarchivable, which is the same complaint in another project mode, and relaxing it for a whole delivered top-level root looks sound (receipts and checkpoints carry no constraint; archive keeps the branch; a root has no surviving parent whose generation would move). But no production project is in that mode, so it cannot be checked against real data, and §5.4 names two readers that turn silently wrong when a receipt source disappears (`readiness_on`, `hierarchy_prerequisite_delivery_head`). It deserves its own spec when a hierarchy/train project exists again. Test 7 pins today's behaviour so it cannot change by accident.

**D4 — Who may run the controls this spec points at, and the one control that does not exist.** §4.4, §4.6. Two parts.

*(a) Authority.* Every integration recovery control requires the `LOCAL` principal; none is runnable from a supervisor **session** token. The standing rule says each failure mode gets a supervisor-runnable control. Either the supervisor on this install already runs as LOCAL (then nothing is owed), or the rule needs a capability-gated path for `cancel-preserving`, `release-delegates`, `abort` and `resume`. That is a security decision about the supervisor's token (`principal.py:19–25` warns against exactly the shortcut), so it is not decided here. Recommended: confirm which it is on this install before approving; if the supervisor is a `SESSION` principal, file the authority change as its own reviewed task rather than folding it into a schema change.

*(b) `aq integration release-owner OWNER_ROW_ID --reason`.* `integration_cleanup_blocked` has no command at all for one case: an attached owner row whose session is provably stopped. Add it, fenced the way `cancel_preserving` is — provider termination proof via `confirm_stopped`, compare-and-swap on the owner row, workspace disabled before it is unlocked (as `preserve_stopped_owners` does, `development.py:1560–1572`). Recommended: yes, as its own task before this change ships; rows 5 and 7 cannot leave without it.

**D5 — `abandon_undelivered` as specified, or no abandonment control at all?** §3.5. Without it, work that should never land is refused forever and cannot be deleted either. Recommended: as specified.

**Still open from the draft.** *Q4* — `integration_owned` and the new codes have no dashboard surface; `BranchDiscardPrompt` never appears for a subtree that is also integration-held, because these refusals are raised first. Unchanged by this spec; the refusals are rarer and now each names its remedy in text. *Q3* (does anything need to **render** an archived parent or verifier?) is answered: yes — §5.2 #14 and §5.3 #21 — and the helper is in scope.

**Sequence, once approved:** P1 (`swift-orbit.4`) → P2 (`swift-orbit.5`) → D4 if chosen → the operator ends the three obsolete operations (§11.3) → this change (code, revision, tests) → §10.
