---
tags: [design, workspaces, worktrees, git, parallelism, merge-slot]
---

# Worktree Execution — Per-Slot Worktrees, Merge Slot, Branch Lifecycle

**Status:** Draft — approved direction (2026-08-19)
**Principles:** [[guiding-design-principles]] (#1 files as source of truth, #2 visible and editable, #5 human judgment, #7 events, #10 fewer moving parts)
**Related:** [[workspaces-v2]] (this spec amends it), `docs/analysis/framework-overhaul-todo.md` (Workstream W, §3b), session-runtime spec (session env / `work_dir` wiring), work-graph spec (work-state metadata schema), aq-surface spec (CLI plumbing)

---

## 1. Problem Statement

Today parallelism per project is bounded by how many pre-provisioned clones exist. `workspaces` rows are full clones with one exclusive lock each; `branch-isolated` mode is a *fallback* that carves a worktree out of an already-locked clone (`_create_branch_isolated_worktree` in `src/orchestrator/workspace.py`, path convention `<parent>/.worktrees-<base>/<slug>/`); `_recover_stale_state` in `src/orchestrator/core.py` deletes every `source_type=WORKTREE` workspace on daemon boot; nothing serializes integration, so two tasks merging to the default branch race each other in `_phase_verify`'s auto-merge; and a clone per parallel stream costs a full checkout plus a full dependency install per stream.

Target (decisions D5 + Workstream W, 2026-08-19): **one base clone per project; a reusable worktree per agent slot; a fresh branch per task; explicit crash-safe lifecycle; serialized integration through a per-project merge slot.** Parallelism becomes cap-bounded (`max_concurrent_agents`), not clone-bounded. The branch — not the worktree — is the durable artifact.

This spec owns the worktree/slot model, lifecycle, merge slot, reaping, and `aq workspace` semantics. The work-graph spec owns the schema mechanics of the task work-state metadata keys (`work_dir`, `branch`, `pr_url`, `rejection_reason`, `merged_at`) that this spec writes. The session-runtime spec owns how `work_dir` reaches the agent session environment. The aq-surface spec owns CLI plumbing for the commands whose semantics are defined here.

---

## 2. Model

### 2.1 Kind modes

`workspace_kinds` gains a `mode` field, meaningful for `is_git_repo=true` kinds:

| Mode | Semantics |
|---|---|
| `worktree` (default) | One **base workspace** (the project clone) plus N **slot worktrees** under `<base_repo>/.aq/worktrees/<slot>/`. Agents run in slots; the base is never an agent cwd while worktrees exist. |
| `exclusive-clone` (legacy) | Today's behavior: each workspace row is a full clone, exclusively locked per task. Existing rows keep working under this mode with zero changes. |
| `directory-isolated` (deferred) | Same branch, different directories (monorepos). Stub only, as today. |

`mode` replaces `default_lock_mode` as the primary sharing knob for git kinds; `default_lock_mode` stays for `exclusive-clone` kinds and non-repo kinds. The `branch-isolated` *fallback* path is retired outright: worktree mode is its principled replacement (worktrees are first-class slots, not emergency derivatives of a locked clone).

### 2.2 Slots

A **slot** is a reusable worktree identified by `(base_workspace_id, slot_index)` and named `slot-<n>` (`n` starting at 0). Slot count per base equals the project's `max_concurrent_agents` — concurrency per project = agent cap = slot count. Slots live at:

```
<base_repo>/.aq/worktrees/slot-0/
<base_repo>/.aq/worktrees/slot-1/
...
```

**Slot ⇄ agent binding.** "Per-agent slot" means *cardinality and exclusive occupancy*, not a durable name binding. `AgentReconciler` (`src/orchestrator/agent_reconciler.py`) lazily creates agent rows up to `max_concurrent_agents`, reassigns them across profiles, and `_recover_stale_state` deletes excess idle agents when caps shrink — agent ids are fungible and volatile. Embedding agent ids in directory names would strand a directory on every reap or reassignment. Instead, slots are ordinal and the binding is lock-scoped: while a task runs, the existing `locked_by_agent_id` / `locked_by_task_id` columns on the slot's `workspaces` row *are* the binding; between tasks the slot is unowned and any agent may claim it. Warm caches (`node_modules`, build artifacts) belong to the slot directory, so reuse amortizes setup cost regardless of which agent occupies it next.

Each slot is a `workspaces` row (`kind_id='project-repo'` or another git kind, `source_type=WORKTREE`, `slot_index` set, `base_workspace_id` pointing at the base clone's row). Acquisition is therefore **unchanged in shape**: `acquire_for_task` / `acquire_one_unlocked` pick a free slot exactly as they pick a free clone today, preserving workspaces-v2 §6 all-or-nothing multi-kind acquisition in canonical `(kind_id, position)` lock order. `vault`, `readonly-dir`, and package kinds are untouched; a multi-repo task gets one slot per git kind it requires.

### 2.3 Base workspace

The base workspace is the project's normal clone with the default branch checked out — the existing `workspaces` row for the project. Under worktree mode it is used **only** for `fetch`, branch queries, `git worktree add/prune/remove`, and merge-slot integration; it is excluded from agent acquisition (rows with `slot_index IS NULL` whose kind resolves to `mode: worktree` are never handed to agents). The per-base-repo `asyncio.Lock` git mutex (`Orchestrator._git_mutex`) stays and serializes every git operation that touches the base or the shared `.git/worktrees/` registry.

**As landed (2026-09-01), enforced rather than assumed.** "Never handed to agents" was a property of the acquisition path, not a check, and one path broke it: `acquire_for_task(read_only=True)` attached a lockable kind's *first* workspace without a lock, which is exactly the base row. Read-only agents therefore ran their full tool surface in the base — routinely a `LINK` row pointing at a developer's own checkout — and, because the caller still wrote `.agent-queue-lock` there, serialized on the base's sentinel while their slots sat free. Three changes close it:

- **No read-only acquisition path.** `profile.read_only` is a declarative statement of write intent (no write tools in the profile's tool list) and no longer touches acquisition. A read-only task takes a disposable slot like any other task.
- **A launch guard.** `src/orchestrator/base_workspace.py` refuses any session whose `work_dir` resolves to a base, on both the task-launch (`_launch_session_for_task_locked`) and pool-launch paths, unless its profile sets `allow_base_checkout: true`. A base is identified structurally — a non-slot row that at least one slot names as its base — so the guard is inert on installs with `worktrees.enabled: false`, where no slots and therefore no bases exist.
- **A doctor check.** `workspaces.base_sessions` (error) reports any live session sitting in a base, covering launches the guard never saw.

`pr_merge` also stopped borrowing the base: `gh pr merge` resolves owner/repo/number from a full PR URL, so it runs in the daemon's data dir instead of a checkout.

### 2.4 Hidden and ignored: `.git/info/exclude`

`.aq/` is ignored via an idempotent marker block appended to `<base>/.git/info/exclude` — no commit, so this works for repos we don't own and never dirties the tree:

```
# >>> agent-queue managed — do not edit between markers >>>
/.aq/
# <<< agent-queue managed <<<
```

The block is written if absent, rewritten if drifted, and left alone if present (idempotent under repeated daemon starts). Once ignored, `git worktree add` inside the repo's own directory is safe: `git status` in the base stays clean, and slot checkouts never contain `.aq/` because it is untracked.

### 2.5 Sentinel: `.aq-worktree.json`

Every slot carries a sentinel at `<slot>/.aq-worktree.json`:

```json
{
  "slot": "slot-1",
  "slot_index": 1,
  "base_workspace_id": "ws-a1b2",
  "project_id": "atom-claude",
  "task_id": "tsk-9f3e",
  "branch": "aq/tsk-9f3e",
  "created_at": 1755590400.0,
  "assigned_at": 1755612300.0,
  "daemon_epoch": "2026-08-19T10:00:00Z",
  "setup_hash": "sha256 of worktree_setup commands"
}
```

Written at slot creation, updated at every assignment. It is the filesystem half of adoption and doctoring: a directory with a sentinel but no DB row is an orphan; a DB row with no sentinel is a broken slot; a `setup_hash` mismatch means `worktree_setup` changed and the setup commands re-run at next reset. The sentinel is data for recovery — the DB row remains authoritative for locks.

---

## 3. Lifecycle

### 3.1 Create (first use of a slot)

Under the base git mutex:

1. Ensure the `.git/info/exclude` marker block (§2.4).
2. `git worktree prune` (clear stale registrations from crashes).
3. `git fetch origin` in the base (failure handling per §3.5).
4. `git worktree add --detach <base>/.aq/worktrees/slot-<n> origin/<default>` — detached, so no branch is claimed at creation; branches are per task.
5. Run the kind's `worktree_setup` commands (§3.6) inside the slot.
6. Write the sentinel; insert the slot's `workspaces` row; emit `worktree.created`.

Slots are created lazily up to the cap, on demand when acquisition finds fewer free slots than the cap allows.

**The dispatch that grows the pool takes the slot it grew.** Growth runs immediately before acquisition, with no intervening await, and the new slot's `workspaces` id is passed to acquisition as its preference. Otherwise the growth-triggering task funds a slot and then loses it to whichever concurrent dispatch reaches `acquire_one_unlocked` first — observed 2026-09-02 as a priority-3 task starving for ~40 minutes behind lower-priority work that kept taking the slots it provisioned. The preference is still soft: an explicit `preferred_workspace_id`, and the branch-affinity hint of §3.4, both outrank it, because those are correctness constraints and this is a fairness one.

A dispatch that finds nothing to acquire is PAUSED with a backoff, and the reason it waited decides what happens next:

| Wait | Meaning | Handling |
|---|---|---|
| `slot_lost_race` | this dispatch created a slot and another took it | short backoff (one cycle) — nothing is being built, the task just has to be visible to priority ordering again |
| `slot_stalled` | growth was needed and produced no row | full backoff, logged loudly — unlike a ramp this never clears itself |
| `slot_warming` | the pool is genuinely mid-ramp | full backoff, logged quietly |
| `slots_full` | the pool is at cap, every slot busy | full backoff, logged quietly — honest contention, and not something `/add-workspace` can fix |

All four end the moment *any* in-cap slot frees, so the cascade returns every task on one of them to READY in that same cycle rather than waiting out its timer. The scheduler then picks by `(priority, id)` as it always does — which is what makes the highest-priority waiter, not the one whose backoff happened to expire first, the one that takes a freed slot. Waits a free slot does **not** resolve (a branch checked out in another worktree, an exhausted clone pool) keep their timer.

### 3.2 Reset (every assignment)

A slot is reused across tasks; per assignment it is reset to a pristine per-task state:

0. **Clear any interrupted git operation.** `merge|rebase|cherry-pick --abort` plus a stale-lock sweep, exactly as the clone path has always done. A slot left mid-rebase by a killed agent fails both `reset --hard` and `switch`, which would send every task that lands on it into the PAUSED backoff indefinitely. Note the lock files live under `<base>/.git/worktrees/<slot>/`, not `<slot>/.git` — a worktree's `.git` is a *file*.
1. **Salvage dirty state.** If the worktree is dirty (a crashed or sloppy predecessor), do **not** stash — the stash stack is **repository-wide**, shared by the base and every sibling slot, so a `pop` in one slot can restore another slot's work. Instead: `git add -A`, capture `git diff --cached --binary HEAD` as a patch, archive it as `task_context(type=worktree_salvage)` on the *previous* task (from the sentinel's `task_id`; on the incoming task if unknown), then hard-reset. `--binary` is required: without it git emits only `Binary files … differ` and the reset destroys the bytes. Patches larger than `worktrees.salvage_max_bytes` (default 5 MiB) are replaced by their `--stat` summary — `task_contexts` is not a blob store. Work is preserved in a visible, per-task place (principle #2) and the slot is deterministic.
2. `git fetch origin` (via the base mutex; §3.5 on failure).
3. `git reset --hard` + `git clean -fd` — **without `-x`**, so gitignored caches (`node_modules`, build dirs) survive and slot reuse keeps its amortization value. The sentinel is exempted (`-e .aq-worktree.json`).
4. Fresh branch: `git switch -c aq/<task_id> origin/<default>` (or `origin/<base_branch>` when the task specifies `base_branch`). If the branch already exists (task retry), `git switch aq/<task_id>` followed by a rebase onto the start point — **unless the previous attempt already pushed it**. `origin/aq/<task_id>` that the start point does not contain is published work, very often with a PR open on it; discarding it locally unpublishes nothing, so the retry would hand the agent an empty branch and a non-fast-forward on its next push. In that case the branch is re-pointed at `origin/aq/<task_id>` instead, and the agent picks up where the last attempt left off. Once the push has been merged (the start point contains it), the start point wins again. A continuation task (§4.3) resumes its predecessor's branch instead: `git switch aq/<orig_task_id>`, no reset of the branch tip.
5. Re-run `worktree_setup` only if `setup_hash` changed.
6. Update the sentinel; record `work_dir` (slot path) and `branch` on the task (work-state contract); emit `worktree.reset`.

Branch naming is fixed at `aq/<task_id>` — no title slug, so the branch is derivable from the task id alone (crash recovery, `aq task branch`, reaper matching by `aq/*` prefix).

### 3.3 While running

The slot is the session `work_dir` (delivery owned by the session-runtime spec). Hooks, overlays, and `.aq/prompt.md` live inside the slot. Nothing else touches the slot while `locked_by_task_id` is set.

### 3.4 On close: the branch is the artifact

- `shipped` → the branch is pushed and PR'd or merged (§4). The slot is *not* torn down; it is reset by the next assignment.
- `blocked | abandoned | failed` → the branch is kept for `retain_failed_days` (default 7) for forensics and retry, then pruned. The slot itself returns to the free pool immediately — the branch survives independently of the worktree, which is the point. Before the slot returns to the pool the *same* salvage → `reset --hard` → `clean -fd` sequence as §3.2 runs (`WorktreeSlotManager.restore_slot_after_task`), and the slot deliberately stays on its task branch. The clone-mode cleanup ladder — commit, else **stash**, else `clean -fdx`, then `checkout <default_branch>` — must never run against a slot: the stash is shared (§3.2), `-x` destroys the warm caches that are the whole point of slot reuse, and the checkout fails outright because the base already holds the default branch.
- Merged `aq/*` branches are pruned locally in the base; remote pruning per `prune_remote_branches` policy.

**Slot affinity is the price of that choice.** A slot left on `aq/<task_id>` means the *next* dispatch of that same task must land on that slot or it cannot check the branch out at all: git allows a branch in exactly one worktree, nothing moves the old slot off it, and the collision therefore repeats on every retry forever. So acquisition **prefers the slot that already holds the task's branch** (`WorkspaceMixin._slot_branch_affinity` → `acquire_for_task(preferred_workspaces=...)` → the existing `prefer_workspace_id` seam).

Three decisions make that a hint rather than a new piece of state:

- **Derived, never recorded.** The holder is read per acquisition from `git worktree list --porcelain` in the base — the same state git consults when it refuses the checkout, so the hint cannot disagree with the refusal it exists to avoid. No column, no field on the task, nothing to migrate, nothing to go stale: a manual `git switch` in a slot, a restarted daemon or a reaped slot all resolve correctly on the next look.
- **Soft, never a wait.** If the preferred slot is locked, the task takes any free slot instead of queueing behind it. A hard wait would trade a self-inflicted stall for an unrelated one — parked behind whatever long task now holds the slot — and the branch-held reporting (§4.4) still covers the collision if one follows. In practice a locked holder is only reachable through the shared plan branch: a slot locked by another task was reset onto *that* task's branch, so it no longer holds ours.
- **Outranked by an explicit pin.** `Task.preferred_workspace_id` is an operator instruction; the hint is an optimization, and it is only consulted for requirements that do not already carry one. The `worktree_slot_cap` bound still applies: an out-of-cap slot is never hinted, because it is never acquirable.

The cases the hint cannot fix are exactly the ones `aq doctor --check worktrees.orphans` reports: the branch is held by the base, by a foreign worktree, or by a slot that a shrunk cap has retired. Those need an operator, not a scheduler.

### 3.5 Failure modes

- **Fetch failure at reset** (base offline, auth broken): non-fatal *if* the slot already has an `origin/<default>` ref — the task branches from the last-known ref and a warning is notified. Slot **creation** requires one successful fetch or an existing up-to-date base; otherwise acquisition fails for that kind and the task takes the existing no-workspace PAUSED backoff path in `_execute_task`.
- **Base clone missing** (deleted out-of-band): recreate via `GitManager.acreate_checkout` from `kind.repo_url` / `project.repo_url`, then recreate slots lazily.
- **Force-push protection:** agents and the completion pipeline only ever push `aq/*` branches; `--force-with-lease` is permitted only against the task's own `aq/<task_id>`; nothing in this system runs any force variant against `origin/<default>`. This is an invariant of the pipeline code, asserted in tests — server-side protection (branch rules) is recommended but not assumed.
- **Submodules:** git worktrees share superproject config and handle submodules poorly. v1 policy: repos needing submodules add `git submodule update --init --recursive` to `worktree_setup`, or pin the kind to `mode: exclusive-clone`. Documented limitation, not solved here.
- **Dirty worktree at reset:** handled by salvage (§3.2 step 1) — hard-reset plus archived patch, never silent loss, never a stash.

### 3.6 `worktree_setup`

Per-kind list of shell commands in the kind's markdown frontmatter (e.g. `["npm ci", "ln -s ~/.cache/aq-pip .pipcache"]`), run once at slot creation and again only when the command list changes (`setup_hash`). This is the Gas City `session_setup` shape: slot reuse amortizes installs; no global cache system in v1. Commands run with the slot as cwd, a bounded timeout, and never with task- or chat-derived text interpolated (trust rule G.3).

---

## 4. Integration: the Merge Slot

### 4.1 Why a DB row, not an advisory lock

Integration must be serialized per project so only one task lands on the default branch at a time. Two candidate mechanisms:

- **Postgres advisory lock** — connection-scoped, dies with the daemon connection, does not exist on SQLite (the default backend), and is invisible to the dashboard.
- **DB row with lease** — survives daemon restart, works identically on SQLite and PostgreSQL, is visible and editable (principle #2), queryable by `aq workspace doctor` and the dashboard, and breakable by an explicit expiry rule rather than a connection drop.

**Decision: DB row.** A `merge_slots` table with one row per project: `holder_task_id`, `acquired_at`, `expires_at`. Acquire is a dialect-appropriate atomic conditional `UPDATE` (holder is NULL or lease expired); the pipeline renews the lease while working; release nulls the holder. A cascade housekeeping step breaks expired leases and emits an event, so a daemon crash mid-merge stalls integration for at most one lease TTL (default 600 s).

### 4.2 Merge flow (rebase-before-merge, serialized)

The completion pipeline stays algorithmic. After verification, integration runs under the merge slot and the base git mutex:

1. Acquire merge slot → emit `merge.started`.
2. In the **slot worktree** (the branch's home — a branch checked out in one worktree cannot be checked out in another): `git fetch origin`, rebase `aq/<task>` onto `origin/<default>` (the existing rebase-before-merge behavior), push with `--force-with-lease`.
3. Per the task's effective integration mode (task override → project policy → `integration.default_mode`): in `pull_request` mode, open a PR via `gh` (record `pr_url` on the task; the task completes unmerged and the review pipeline's `pr-merged` gate sweep takes over), **or** in `direct` mode, local merge: in the **base** — `checkout <default>`, `reset --hard origin/<default>`, merge the branch, push.
4. Success → emit `merge.succeeded`, record `merged_at`; release the slot.

### 4.3 Conflicts

Rebase or merge conflict → abort cleanly, release the merge slot, and:

- Set `rejection_reason` (e.g. `"merge_conflict: rebase onto origin/main failed"`) and the conflicting file list in the task's work-state metadata (schema owned by the work-graph spec).
- Transition the task to `needs_attention` (until the work-graph state lands, the projection is `BLOCKED` with the same metadata).
- Emit `merge.conflict` with `{task_id, branch, target, files}`.
- Optionally (per project policy `spawn_conflict_continuation`) auto-create a continuation task that resumes the **same branch** (§3.2 step 4, continuation path) with the rejection reason in its context — the rejection-aware-resume pattern.

The branch is untouched by the failure; it remains the durable artifact for whoever resolves the conflict.

### 4.4 Plan subtasks share one branch, and therefore serialize

Subtasks of a plan all resume the parent's branch (`resume_branch = parent.branch_name`) so the whole plan lands as one PR. Git allows a branch to be checked out in exactly one worktree, so two sibling subtasks dispatched into two slots cannot both hold it: the second `git switch` is refused, its slot is released, and the task takes the ordinary 60 s PAUSED backoff until the sibling finishes. **A parallel plan therefore executes serially.**

**Decision: accept the serialization; keep the shared branch.** The alternative — a branch per subtask, folded into the parent branch at integration — is a real design change, not a bug fix: it needs somewhere to do the folding, and that place is the merge slot, which does not exist until Phase 3. Building a second integration path in Phase 2 for Phase 3 to replace is the wrong order, and "one plan, one branch, one PR" is a property the plan model deliberately has. Revisit in Phase 3 with `_phase_integrate` in hand; until then, plans get isolation and atomicity rather than parallelism, which is the trade the shared branch was always making.

Affinity (§3.4) applies here too, on the branch the subtask *resumes* rather than on `aq/<subtask_id>`: while a sibling is running it holds its slot locked, so the hint cannot be taken and the wait below is unchanged; when no sibling is running the next subtask lands back on the slot that last advanced the plan branch, which is the difference between resuming a warm worktree and colliding with a cold one.

What *is* fixed for the sibling case itself is the reporting. The refusal was surfacing as a per-attempt Discord "Git Error", which described a scheduling wait as a fault. It is now recognised (`_is_branch_busy_error`) and logged as the expected wait it is. The same applies to the pool ramp: slots are created one per dispatch, so a cold cap-N project needs N−1 dispatch rounds to warm up, each previously costing a "No Workspace — use /add-workspace" notice for a condition the operator can neither cause nor fix. Both now pause quietly; genuine exhaustion still notifies.

**Only a *resume* has a sibling.** `_is_branch_busy_error` alone does not make a refusal a sibling wait: without a `resume_branch` the task owns `aq/<task_id>` by itself, and the same refusal means its own branch is still checked out somewhere — a released slot stays on its last task's branch (§3.4), so a retry landing on a *different* slot collides with its own predecessor, and a slot left on a deleted task's branch pins it for good. Neither clears itself. The wait is therefore classified on `resume_branch`, not on the git text: with one it is the sibling wait above (quiet, self-clearing); without one it logs a warning naming the real branch and notifies the operator, who can find the holding slot with `aq doctor --check worktrees.orphans`. Classifying the second case as the first logged the literal text `waits for branch None — a sibling holds it in another slot` and looped silently forever (observed live: calm-flare, 2026-09-01).

---

## 5. Reaping — slots, not tasks

The reaper is a cascade housekeeping step that acts on **slots**. A slot worktree is removed only when the slot is *retired*: project caps shrank below its index, the project was archived or removed, the base workspace was removed, or the kind's mode flipped to `exclusive-clone`. Task completion never reaps a slot.

Before removal, a **liveness check** against the process table: any process whose environment carries a matching `AQ_TASK_ID` or whose cwd is inside the slot path blocks the reap. If the process table cannot be read, the reaper *skips* — never reap on partial information. Removal sequence, under the base mutex: `git worktree remove <slot>` (`--force` fallback), `git worktree prune`, delete the slot's `workspaces` row, emit `worktree.reaped`.

Branch pruning is a separate reaper concern: merged `aq/*` branches (`git branch --merged origin/<default>`, filtered to the `aq/` prefix) are deleted locally; remote deletion per policy; unmerged `aq/*` branches whose tasks are terminal-failed are pruned only after `retain_failed_days`.

---

## 6. Daemon Restart: Adopt, Don't Delete

`_recover_stale_state` today removes every `source_type=WORKTREE` workspace on boot. **That stops.** Slot worktrees belong to agent slots, not to a daemon run. On restart the daemon *adopts*: it cross-checks `git worktree list --porcelain` in each base against slot rows and sentinels, repairs the exclude block, runs `git worktree prune` for stale registrations, and re-registers rows for intact directories. Lock release for genuinely dead tasks follows the existing recovery logic (and, once the session-runtime spec lands, its liveness-checked adoption) — but the directories and branches survive. `--reset` remains the admin escape hatch for a wholesale wipe.

Full adoption is Phase 4. Phase 2 ships the half that makes the interim survivable: `_cleanup_worktree_workspace` short-circuits on `is_slot`, so recovery unlocks slot rows and leaves both directory and branch alone, and every slot's `{slot_path -> base_path}` entry is rebuilt from one `list_workspaces()` sweep during recovery. That second half matters on its own — the map is what `_resolve_git_lock` reads to serialize a slot's `fetch` against the base's shared object store, and a task that was IN_PROGRESS across a restart never re-enters `_prepare_workspace`, so building the map only there would leave it permanently unregistered. The map is a startup projection of the DB, not an incrementally-built cache.

---

## 7. Amendments to Workspaces v2

This spec amends [[workspaces-v2]] as follows; everything not listed is unchanged.

1. **§3.1** `workspace_kinds` gains `mode` and `worktree_setup` (§2.1, §3.6).
2. **§3.2** `workspaces` gains `slot_index` and `base_workspace_id`; slot rows use `source_type=WORKTREE`.
3. **§6** Acquisition semantics (all-or-nothing, canonical `(kind_id, position)` order, per-dialect strategies, path-conflict checks) are preserved verbatim. Under worktree mode the candidate set for a git kind is its slot rows; the base row is excluded.
4. **§6.6 step 4** — the branch-isolated worktree *fallback* (`_create_branch_isolated_worktree`, the `.worktrees-<base>/<slug>/` convention, and `_get_worktree_base_path` path parsing) is **retired**. Base derivation becomes a DB lookup via `base_workspace_id`.
5. **§9** Migration: existing clone rows keep working under `mode: exclusive-clone`; no data rewrite of existing workspaces is required (see the implementation spec).
6. Shared-workspace tasks that must run in place (deploy scripts) remain expressible: pin the kind to `exclusive-clone` or use an explicitly locked LINK workspace, exactly as today.

---

## 8. Events

All dot-namespaced, following `git.push` / `task.*` conventions:

| Event | Payload (minimum) | When |
|---|---|---|
| `worktree.created` | `project_id, workspace_id, slot, path, base_workspace_id` | Slot creation completes (after `worktree_setup`) |
| `worktree.reset` | `project_id, workspace_id, slot, task_id, branch, salvaged: bool` | Per-assignment reset completes |
| `worktree.reaped` | `project_id, workspace_id, slot, path, reason` | Slot retired and removed |
| `merge.started` | `project_id, task_id, branch, target` | Merge slot acquired |
| `merge.succeeded` | `project_id, task_id, branch, target, pr_url?, merged_at` | Integration landed (or PR opened) |
| `merge.conflict` | `project_id, task_id, branch, target, files, rejection_reason` | Rebase/merge aborted on conflict |

---

## 9. `aq workspace` Command Semantics

Semantics owned here; CLI plumbing (flags, `--json` envelope) owned by the aq-surface spec. All are `CommandHandler` commands returning `{"success": bool, ...}`.

- **`aq workspace list`** — every workspace row grouped by project and kind, annotated: role (`base` / `slot-<n>` / `clone` / `link`), mode, lock holder (task + agent), current branch, dirty flag, sentinel status.
- **`aq workspace doctor`** — read-only diagnosis: directories with sentinels but no rows (orphans), rows without directories, stale `.git/worktrees` registrations (fix: `git worktree prune`), missing/drifted exclude blocks, dirty unlocked slots, expired merge-slot leases, `aq/*` branches past retention. Reports findings with the exact remediation each needs; `--fix` applies the safe subset (prune, exclude repair, row re-registration) and never deletes work.
- **`aq workspace reap`** — explicit reap of retired slots and prunable branches, same liveness guard as the cascade reaper; `--slot`, `--branches-only` narrowing. Refuses live slots.

---

## 10. Edge Cases and Explicit Non-Goals

- **Two daemons pointing at one repo** — out of scope. The design assumes one daemon owns a base repo; the sentinel's `daemon_epoch` lets `doctor` *detect* foreign-epoch activity, but no cross-daemon coordination is attempted. Do not do this.
- **Repos we don't own** — fully supported: `.git/info/exclude` requires no commit and no upstream cooperation.
- **Windows** — the daemon targets Linux/WSL2 (Workstream A constraint); process-table liveness uses `/proc`. No Windows-native support.
- **Global shared caches across slots** — non-goal in v1; `worktree_setup` plus slot reuse is the mechanism.
- **`directory-isolated`** — remains deferred, as in workspaces-v2 §11.
- **Cross-project and multi-kind acquisition** — unchanged; a task spanning `project-repo` + `package-foo` + `vault` acquires one slot per git kind under the same canonical-order transaction semantics as workspaces-v2 §6.

---

## 11. Open Questions

None blocking. Two items logged for later: whether `prune_remote_branches` should default on once PR-based projects dominate; and whether slot count should be allowed to exceed `max_concurrent_agents` for pre-warming (currently: no — slots = cap, by decision).
