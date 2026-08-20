---
tags: [implementation, workspaces, worktrees, git, merge-slot, alembic]
---

# Worktree Execution — Implementation Spec

**Status:** Draft — approved direction (2026-08-19)
**Design:** [[design/worktree-execution]] (model, lifecycle, merge slot, reaping)
**Related:** [[design/workspaces-v2]], `docs/analysis/framework-overhaul-todo.md` (Workstream W), session-runtime spec, work-graph spec, aq-surface spec

---

## 1. Scope

Implements per-slot worktrees, the merge slot, slot reaping, adoption on restart, and `aq workspace list|doctor|reap` command behaviors. Out of scope here: session env / `work_dir` delivery (session-runtime spec), work-state metadata schema on tasks (work-graph spec), CLI flag plumbing (aq-surface spec). Every command returns `{"success": bool, ...}`; all state changes go through `CommandHandler`; all git calls use the `GitManager` async (`a`-prefixed) API; subsystems communicate via `EventBus`.

---

## 2. Module Layout

New modules:

| Path | Contents |
|---|---|
| `src/orchestrator/worktree_manager.py` | `WorktreeSlotManager` — slot create/reset/salvage/adopt/reap, sentinel I/O, `.git/info/exclude` management |
| `src/orchestrator/merge_slot.py` | `acquire_merge_slot` / `renew_merge_slot` / `release_merge_slot` / `break_expired_merge_slots` |
| `src/database/queries/merge_slot_queries.py` | `MergeSlotQueriesMixin` — dialect-aware conditional UPDATEs |
| `tests/test_worktree_manager.py`, `tests/test_merge_slot.py`, `tests/test_worktree_reaper.py`, `tests/test_worktree_migration.py` | See §10 |

Changed modules (integration points verified in §6): `src/database/tables.py`, `src/models.py`, `src/orchestrator/workspace.py`, `src/orchestrator/workspace_attachments.py` (no semantic change; see §6.3), `src/orchestrator/execution.py`, `src/orchestrator/git_ops.py`, `src/orchestrator/core.py`, `src/orchestrator/agent_reconciler.py`, `src/git/manager.py`, `src/database/queries/workspace_queries.py`, `src/profiles/workspace_kind_parser.py`, `src/commands/agent_commands.py`, `src/config.py`, `vault/workspace-kinds/project-repo.md`.

---

## 3. Schema Deltas + Alembic

All changes go into `src/database/tables.py` **with** one Alembic revision (`alembic revision --autogenerate -m "worktree execution: kind mode, slot columns, merge_slots"`), reviewed by hand — autogenerate will produce the adds but the data steps are manual.

### 3.1 `workspace_kinds`

| Column | Type | Notes |
|---|---|---|
| `mode` | `Text NOT NULL server_default 'worktree'` | `'worktree' \| 'exclusive-clone' \| 'directory-isolated'`. Meaningful when `is_git_repo=true`. |
| `worktree_setup` | `Text NOT NULL server_default '[]'` | JSON-encoded `list[str]` of shell commands (design §3.6). Stored as Text for SQLite/PG parity (matches existing JSON-in-Text usage). |

**Data step (same revision):** `UPDATE workspace_kinds SET mode='exclusive-clone'` for **all rows existing at migration time**. Existing installs therefore keep clone behavior verbatim; `mode='worktree'` arrives only via markdown edits or new kind rows. Idempotent: the UPDATE is guarded by a marker (`WHERE mode='worktree' AND created_at < <revision timestamp>` is fragile — instead run the UPDATE unconditionally *before* any new rows can exist, which is true within the migration transaction; a re-run after new rows exist is prevented by Alembic's linear history).

### 3.2 `workspaces`

| Column | Type | Notes |
|---|---|---|
| `slot_index` | `Integer` nullable | NULL for clones/links/bases; `0..N-1` for slot worktrees. |
| `base_workspace_id` | `Text` nullable | Soft self-reference to the base clone's `workspaces.id` (no FK — matches `kind_id` soft-ref precedent). Set only on slot rows. |

Partial unique constraint `(base_workspace_id, slot_index)` where both are NOT NULL: on PostgreSQL a partial unique index (`postgresql_where`); on SQLite a partial index is also supported (`sqlite_where`) — Alembic needs the index written manually since autogenerate handles partial indexes poorly. No data step: existing rows are clones (`slot_index` NULL) and behave as before.

### 3.3 New table `merge_slots`

| Column | Type | Notes |
|---|---|---|
| `project_id` | `Text PK, FK → projects.id` | One row per project, created lazily on first acquire. |
| `holder_task_id` | `Text` nullable | NULL = free. Soft ref to `tasks.id` (survives task archival). |
| `acquired_at` | `Float` nullable | |
| `expires_at` | `Float` nullable | Lease expiry; renewed by the pipeline. |
| `updated_at` | `Float NOT NULL` | |

### 3.4 Dialect notes

- **SQLite:** all three changes are plain `ALTER TABLE ... ADD COLUMN` (nullable or with `server_default`) plus `CREATE TABLE` — no batch mode needed, no table rebuild. `server_default` is mandatory for the NOT NULL adds (`mode`, `worktree_setup`).
- **PostgreSQL:** identical DDL; `server_default` on NOT NULL adds avoids a table rewrite lock on PG ≥ 11. The partial unique index uses `postgresql_where=text("base_workspace_id IS NOT NULL")`.
- Downgrade: drop table, drop columns (SQLite ≥ 3.35 supports `DROP COLUMN`; acceptable since dev targets Python 3.12-era toolchains). Test with `pytest tests/test_database.py -v` and §10's migration test on both dialects.

### 3.5 Models (`src/models.py`)

`WorkspaceKind` gains `mode: str = "worktree"` and `worktree_setup: list[str] = field(default_factory=list)`. `Workspace` gains `slot_index: int | None = None`, `base_workspace_id: str | None = None`. New `MergeSlot` dataclass mirroring §3.3. New `WorktreeSentinel` dataclass for `.aq-worktree.json` (design §2.5).

### 3.6 Parser (`src/profiles/workspace_kind_parser.py`)

`parse_workspace_kind_file` accepts frontmatter `mode` (validated against `{"worktree", "exclusive-clone", "directory-isolated"}`, default `"worktree"`) and `worktree_setup` (list of strings; reject non-list). `vault/workspace-kinds/project-repo.md` ships with `mode: worktree` and an empty `worktree_setup`.

---

## 4. New Component Signatures

```python
# src/orchestrator/worktree_manager.py
class WorktreeSlotManager:
    def __init__(self, db: Database, git: GitManager, bus: EventBus,
                 config: WorktreesConfig, git_mutex: Callable[[str], asyncio.Lock]): ...

    async def ensure_slots(self, project, base_ws: Workspace,
                           kind: WorkspaceKind, count: int) -> list[Workspace]:
        """Create missing slot rows/dirs up to `count` (lazily; design §3.1)."""

    async def create_slot(self, base_ws, kind, slot_index: int) -> Workspace:
        """exclude-block → worktree prune → fetch → worktree add --detach →
        worktree_setup → sentinel → DB row → emit worktree.created."""

    async def reset_slot_for_task(self, slot_ws, task, *,
                                  base_branch: str | None = None,
                                  resume_branch: str | None = None) -> str:
        """Salvage-if-dirty → fetch → reset --hard + clean -fd -e sentinel →
        switch -c aq/<task_id> (or resume) → sentinel update →
        emit worktree.reset. Returns the branch name."""

    async def salvage_dirty(self, slot_ws, prev_task_id: str | None) -> bool:
        """add -A; diff --cached HEAD → task_context(type=worktree_salvage)."""

    async def adopt_existing(self, project) -> AdoptReport:
        """Boot-time adoption: cross-check `git worktree list --porcelain`
        vs slot rows vs sentinels; repair exclude; prune stale registrations."""

    async def reap_slot(self, slot_ws, *, reason: str) -> bool:
        """Liveness check (AQ_TASK_ID env / cwd via /proc) → worktree remove
        (--force fallback) → worktree prune → delete row → emit worktree.reaped.
        Returns False (no-op) when liveness is confirmed or unknowable."""

    async def prune_branches(self, base_ws, *, default_branch: str) -> list[str]:
        """Delete merged local aq/* branches; failed ones past retention;
        remote per config.prune_remote_branches."""

    @staticmethod
    def ensure_git_exclude(base_path: str) -> bool:  # sync, tiny file I/O
        """Idempotent marker block in <base>/.git/info/exclude (design §2.4)."""

# src/orchestrator/merge_slot.py  (thin wrappers over merge_slot_queries)
async def acquire_merge_slot(db, project_id: str, task_id: str, ttl: float) -> bool
async def renew_merge_slot(db, project_id: str, task_id: str, ttl: float) -> bool
async def release_merge_slot(db, project_id: str, task_id: str) -> None
async def break_expired_merge_slots(db, bus) -> int   # cascade step
```

`acquire_merge_slot` is one atomic conditional UPDATE — SQLite: inside `BEGIN IMMEDIATE`, `UPDATE merge_slots SET holder_task_id=?, ... WHERE project_id=? AND (holder_task_id IS NULL OR expires_at < ?)`, rowcount-checked (insert-if-missing first via `INSERT OR IGNORE`); PostgreSQL: same UPDATE (with `INSERT ... ON CONFLICT DO NOTHING` seed), no `FOR UPDATE` needed since it's a single-row conditional write.

`GitManager` (`src/git/manager.py`) additions, alongside the existing async API:

```python
async def aworktree_add(self, base_path, worktree_path, *, ref, detach=True) -> None
async def aworktree_prune(self, base_path) -> None
async def aworktree_list(self, base_path) -> list[dict]   # --porcelain parsed
async def alist_merged_branches(self, base_path, *, into: str, prefix: str = "aq/") -> list[str]
async def adelete_local_branch(self, base_path, branch: str, *, force: bool = False) -> None
```

---

## 5. Config Keys

New `WorktreesConfig` dataclass in `src/config.py`, mounted on `AppConfig` as `worktrees:` (alongside `scheduling`, `archive`, etc.), editable via `config_editor`:

```yaml
worktrees:
  enabled: false              # v1 default; flips to true after burn-in (§9)
  retain_failed_days: 7       # branch retention for failed/blocked/abandoned
  merge_slot_ttl_seconds: 600 # lease TTL; renewed during integration
  prune_remote_branches: false
  setup_timeout_seconds: 900  # per worktree_setup command list
  salvage_dirty: true         # false = plain hard-reset, no patch archive
  salvage_max_bytes: 5242880  # cap on one archived patch; past it the
                              # --stat summary is stored instead. 0 = no cap
  spawn_conflict_continuation: false  # auto-create resolve-conflict task
```

When `enabled: false`, acquisition treats every git kind as `exclusive-clone` regardless of the kind's `mode` — the flag is the rollout gate; the markdown is the steady-state truth (principle #1 is honored once the flag retires).

---

## 6. Exact Integration Points (verified against source)

### 6.1 `src/orchestrator/workspace.py`

- **`WorkspaceMixin._prepare_workspace`** (line 35): after `acquire_for_task` returns, branch on the resolved kind's mode. `worktree` mode replaces the entire CLONE/LINK git-provisioning block (lines 209–294) and the branch-name computation (lines 188–202) with `WorktreeSlotManager.reset_slot_for_task`; branch becomes `aq/<task_id>` (plan-subtask branch reuse maps to the `resume_branch` parameter). The `.agent-queue-lock` sentinel logic (lines 143–183) stays for exclusive-clone; slot rows rely on `.aq-worktree.json` + DB locks instead. The `AcquisitionFailed` fallback block (lines 79–108) is **deleted**.
- **`_create_branch_isolated_worktree`** (line 307): **retired** — delete, along with `_get_worktree_base_path` (line 385; callers switch to a `base_workspace_id` DB lookup) and `find_branch_isolated_base` in `workspace_queries.py` (line 401).
- **`_release_workspace_and_cleanup` / `_cleanup_worktree_workspace` / `_release_workspaces_for_task`** (lines 404–465): stop deleting worktrees on release. Slot rows are unlocked (`db.release_workspace`), never removed; only `WorktreeSlotManager.reap_slot` deletes. `_cleanup_worktree_workspace` survives solely for the exclusive-clone legacy until Phase 4 deletes it.

### 6.2 `src/orchestrator/core.py`

- **`_recover_stale_state`** (line 1336): the WORKTREE cleanup branch (lines 1419–1425) is replaced by adoption — release the lock if held, keep row and directory, then `WorktreeSlotManager.adopt_existing` per project runs after recovery. `--reset` keeps the old wipe as an escape hatch.
- **`_git_mutex` / `_resolve_git_lock`** (lines 333–349): `_resolve_git_lock` resolves worktree paths to the base via a cached `{slot_path → base_path}` map built from slot rows, replacing the `.worktrees-<base>` path parsing.
- **`run_one_cycle`** (line 1658): Phase 3 housekeeping gains step 7d `break_expired_merge_slots` and step 7e `worktree-reaper` (retired-slot sweep + `prune_branches`), rate-limited to one pass per few minutes like log cleanup.

### 6.3 `src/orchestrator/workspace_attachments.py` + `src/database/queries/workspace_queries.py`

`effective_requirements` / `acquire_for_task` are **unchanged** (canonical order, all-or-nothing preserved). `acquire_one_unlocked` (line 267) gains mode awareness: when the resolved kind has `mode='worktree'` (and `worktrees.enabled`), the candidate filter adds `slot_index IS NOT NULL` (bases excluded) and, when fewer free slots than the cap exist, the caller (`_prepare_workspace`) invokes `ensure_slots` and retries once. Both dialect strategies (SQLite `BEGIN IMMEDIATE` + rowcount, PG `FOR UPDATE SKIP LOCKED`) apply to slot rows unmodified.

### 6.4 `src/orchestrator/execution.py`

**`_execute_task`** (line 251): no structural change — `workspace = await self._prepare_workspace(task, agent)` (line 346) now returns the slot path; the no-workspace PAUSED backoff (lines 354–383) covers slot exhaustion and fetch-failure-at-creation identically. The slot path is recorded as the task's `work_dir` work-state key here (value only; schema per work-graph spec). Session delivery of `work_dir` is the session-runtime spec's seam.

### 6.5 `src/orchestrator/git_ops.py`

**`_run_completion_pipeline`** (line 487) gains a third phase after `_phase_verify`: **`_phase_integrate`** — merge-slot acquire → rebase-in-slot → push → PR or local merge per policy → conflict handling per design §4.3 (release slot in a `finally`). `_phase_verify` (line 532) changes expectations under worktree mode: the agent finishes **on `aq/<task_id>` with everything committed**; the auto-merge-to-default remediation (lines 630–693) is *skipped* for worktree-mode tasks (integration is `_phase_integrate`'s job) and preserved for exclusive-clone. `GitManager.async_and_merge` / `amerge_branch` remain the exclusive-clone path.

### 6.6 `src/git/manager.py`

- **`aprepare_for_task`** (line 1230): its `is_worktree` branch is superseded by `reset_slot_for_task` (which owns fetch/reset/branch); the non-worktree branch remains the exclusive-clone path. Signature unchanged.
- **`acreate_worktree`** (line 1523, `worktree add -b <branch>`): **retired** in favor of `aworktree_add(..., detach=True)` — slot creation no longer claims a branch.
- **`aremove_worktree`** (line 1532): kept as-is; called only by `reap_slot` and the legacy cleanup.

### 6.7 `src/orchestrator/agent_reconciler.py`

The new-agent workspace gate (line 128, `count_available_workspaces`) must count acquirable capacity: under worktree mode that is `min(cap, existing free slots + creatable slots)`, not clone count — implemented by making `count_available_workspaces` mode-aware (bases with `mode='worktree'` count as `cap - locked_slots`). Without this the reconciler would refuse to create agents for a project with one base and zero pre-made slots.

### 6.8 `src/commands/agent_commands.py`

- `_cmd_list_workspaces` (line 175): annotate rows with `role`, `slot_index`, `mode`, branch, dirty flag (design §9).
- New `_cmd_workspace_doctor` and `_cmd_workspace_reap` implementing design §9, registered in `src/tools/definitions.py` and the API response models — surfaced as `aq workspace doctor|reap` by the aq-surface spec.

### 6.9 Events

Register `worktree.created|reset|reaped` and `merge.started|succeeded|conflict` payloads (design §8) with the event validator (`validate_events`), documented in `docs/specs/event-bus.md`.

---

## 7. Migration Path from Clone-Based Workspaces

1. The Alembic revision (§3) backfills `mode='exclusive-clone'` onto all existing kind rows — **no existing install changes behavior on upgrade**.
2. `WorkspaceKindStore` bootstrap does not rewrite existing markdown. Two consequences follow, and both are handled in code rather than left implicit:
   - An upgrading install's `project-repo.md` predates the `mode` key, so the parser must read an **absent** `mode:` as "leave the stored value alone" (`WorkspaceKind.mode = None`, coalesced in `upsert_workspace_kind`). Defaulting it would upsert `worktree` over the migration's backfill on the first daemon start and every start after, silently falsifying rule 1. `WorkspaceKindStore.backfill_mode_frontmatter` then injects the DB's value into such files once, so the file becomes the explicit source of truth it claims to be.
   - The migration's blanket `UPDATE` cannot distinguish rows it is upgrading from rows the preceding revision seeded moments earlier, so a **fresh** install would also come out on `exclusive-clone`. `WorkspaceKindStore._normalize_fresh_install_modes` corrects that — only when there are no projects, no workspaces, and no workspace-kind markdown at all, conditions no install with any history can meet.

   Operators opt an existing project in by editing the kind file to `mode: worktree`.
3. First worktree-mode acquisition for a project designates its base: the first enabled non-slot workspace row for the kind, clones preferred over links, then by id (`find_worktree_base` — the single definition of this rule; capacity counting and acquisition must both consult it rather than re-deriving it). Slots are created lazily beside it. Any **further** non-slot rows of that kind are *not* acquirable while the kind is in worktree mode (§2.3, §6.3) — they contribute neither inventory nor capacity, and `aq workspace doctor` flags them as redundant. This is the correct reading; earlier drafts of this list said such rows "stay acquirable clones", which contradicted §2.3 and §6.3.
4. Existing clones can be deleted by the operator once slots carry the load; `aq workspace doctor` flags redundant clones under worktree mode.
5. Rollback: set `worktrees.enabled: false` (or per-kind `mode: exclusive-clone`) — slot rows stop being candidates, clones resume. Reap slots at leisure via `aq workspace reap`.
6. **`workspace_mode='branch-isolated'` is deprecated on upgrade.** The branch-isolated worktree fallback is retired unflagged (§7.4 of the design spec), so the value is now an alias for `exclusive`: it required an explicit per-task opt-in that nothing sets by default, plus all clones locked, plus another branch-isolated task holding one. Tasks that set it now take the same PAUSED-with-60 s-backoff path exclusive tasks already take when clones are exhausted, emitting `task.paused` with `reason="no_workspace"`. The enum value stays accepted so existing rows and callers keep working; the tool schemas and `WorkspaceMode.BRANCH_ISOLATED` carry the deprecation note. Parallel work in one repo comes from slots, chosen by the kind's `mode`.

---

## 8. Phase Checklist

- [x] **Phase 0 — Schema & models.** Tables §3.1–3.3, Alembic revision (SQLite + PG), `models.py` dataclasses, parser fields, shipped `project-repo.md` update. `tests/test_worktree_migration.py` green on both dialects.
- [x] **Phase 1 — Slot manager.** `WorktreeSlotManager` (create/reset/salvage/exclude/sentinel), `GitManager` additions, `WorktreesConfig`. Unit-tested against real temp repos.
- [x] **Phase 2 — Acquisition & prepare.** Mode-aware `acquire_one_unlocked` + `count_available_workspaces`; `_prepare_workspace` worktree branch; retire `_create_branch_isolated_worktree` / `_get_worktree_base_path` / `find_branch_isolated_base`; `ensure_slots` lazily from `_prepare_workspace`. Both mode-aware counts bound candidates by the project's slot cap, so capacity and acquisition cannot disagree. `_recover_stale_state` must **not** delete slot rows: `_cleanup_worktree_workspace` short-circuits on `is_slot` for all three of its callers, and recovery rebuilds the `{slot_path -> base_path}` git-lock map from one `list_workspaces()` sweep so a task that was IN_PROGRESS across the restart keeps serializing. `_cleanup_workspace_for_next_task` routes slots to `restore_slot_after_task` (salvage → `reset --hard` → `clean -fd`) — its clone ladder stashes, `clean -fdx`s and checks out the default branch, all three of which are wrong in a worktree.
- [ ] **Phase 3 — Merge slot & pipeline.** `merge_slots` queries, `_phase_integrate`, verify-phase expectation changes, conflict → `rejection_reason` + files + (projected) `needs_attention`, events.
  - Carried in from the P2 review: `_phase_verify`'s auto-merge is *mostly* inert under worktree mode by accident, not design — it runs `git checkout <default_branch>` inside the slot, which git refuses while the base holds that branch (the normal topology), so it raises, is caught at `git_ops.py:661`, logs a `logger.warning`, and falls through to the not-merged path. It only actually merges un-serialized when the base is *not* on its default branch. Two things follow for P3: skip the auto-merge explicitly under worktree mode rather than relying on that refusal, and revisit the downstream branches (`git_ops.py:765/828/848`) that are written assuming a merge happened — today a task can take them after a merge that only warned.
- [ ] **Phase 4 — Reaper & adoption.** Cascade steps 7d/7e, `adopt_existing` in recovery, `_recover_stale_state` worktree-deletion removal, branch pruning, `--reset` escape hatch.
- [ ] **Phase 5 — Surface.** `workspace list` annotations, `workspace_doctor`, `workspace_reap`, event-registry docs, dashboard workspace view data.
- [ ] **Phase 6 — Flag flip & cleanup.** `worktrees.enabled: true` default; delete `_cleanup_worktree_workspace` legacy path after one minor version.

---

## 9. Rollout / Flags

`worktrees.enabled` gates everything (default `false` at first release). Burn-in on one project (`mode: worktree` on its project-scoped kind while the flag is on for that install), comparing task outcomes and workspace incidents against a clone project. Flip the default to `true` one minor version later; the flag then retires and markdown `mode` is the sole knob. The schema ships fully forward-compatible from Phase 0, so partial rollout never leaves mixed installs unreadable.

---

## 10. Test Plan

| File | Coverage |
|---|---|
| `tests/test_worktree_manager.py` (new) | exclude-block idempotency (create twice, foreign content preserved); slot create/reset against a real temp repo; dirty-slot salvage produces a patch `task_context` and a clean slot; `clean -fd` preserves gitignored caches; retry reuses branch; continuation resumes branch; fetch-failure-with-existing-ref proceeds with warning; sentinel round-trip; `setup_hash` re-run semantics |
| `tests/test_merge_slot.py` (new) | concurrent acquire — exactly one winner (both dialects); lease expiry + `break_expired_merge_slots`; release idempotency; survives simulated restart (row persists) |
| `tests/test_worktree_reaper.py` (new) | reap only retired slots; liveness guard blocks reap (fake `/proc` scan); ps-failure ⇒ skip; merged `aq/*` branch pruning; `retain_failed_days` honored |
| `tests/test_worktree_migration.py` (new) | Alembic upgrade/downgrade on SQLite and PostgreSQL; existing kind rows backfilled to `exclusive-clone`; idempotent re-run; existing workspaces untouched |
| `tests/test_workspace_attachments.py` (extend) | slot candidate filter (base excluded); multi-kind slot + vault acquisition keeps canonical order and all-or-nothing rollback |
| `tests/test_orchestrator.py` (extend) | restart adopts slots (no deletion); IN_PROGRESS recovery leaves directories/branches intact; reconciler creates agents with zero pre-made slots |
| `tests/test_git_manager_async.py` (extend) | `aworktree_add/prune/list`, `alist_merged_branches`, `adelete_local_branch` |
| `tests/test_git_command_handlers.py` / `tests/test_workspace_commands.py` (extend) | `workspace_doctor` finding matrix; `workspace_reap` refusal on live slot; `list_workspaces` annotations |
| End-to-end (extend `tests/test_orchestrator.py`) | two parallel tasks on one repo in two slots; both complete; merge slot serializes integration; injected conflict → BLOCKED + `rejection_reason` + conflict files + `merge.conflict` event; never a force-push to default (assert on command log) |

Run with `pytest tests/ -n auto`; git-heavy tests use temp repos with a bare "origin" — no network.

---

## 11. Risks

1. **`git worktree` quirks across git versions** (prune behavior, `--detach` + `switch -c` interplay). Mitigation: pin minimum git in `aq doctor` checks; all sequences behind `GitManager` so quirks are patched in one place.
2. **Reaper false-positive deleting live work.** Mitigation: liveness check is mandatory, skip-on-unknown, and reap triggers are narrow (retirement only); branches survive worktree removal regardless.
3. **Merge-slot starvation** from a crashed holder. Bounded by lease TTL + cascade breaker; `doctor` surfaces held slots.
4. **`clean -fd` vs. agent-created gitignored state** an operator wanted kept. Accepted: slots are cattle; durable outputs belong on the branch. Documented in the kind markdown.
5. **Submodule repos** silently broken in worktrees. Mitigation: doctor warns when `.gitmodules` exists in a worktree-mode kind without a submodule `worktree_setup` step; docs recommend `exclusive-clone`.
6. **Reconciler/capacity mismatch** (agents created with no acquirable slot or vice versa) — covered by §6.7 change plus test; failure degrades to the existing PAUSED backoff, not a crash.
7. **Two daemons on one repo** — explicitly unsupported (design §10); sentinel `daemon_epoch` makes collisions diagnosable but nothing prevents them.
