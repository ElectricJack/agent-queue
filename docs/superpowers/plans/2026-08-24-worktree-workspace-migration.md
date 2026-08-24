---
tags: [workspaces, worktrees, migration, plan]
status: proposed
date: 2026-08-24
---

# Migration Plan — Clone-Based Workspaces → Per-Slot Worktrees

Move project `agent-queue` from three separate clone directories
(`agent-queue2/3/4`) to one base clone plus N slot worktrees, per
[[specs/design/worktree-execution]].

## 1. Headline: the feature is built; only the opt-in is missing

The implementation-spec phase checklist claims Phases 3–6 are incomplete. **It is
stale.** Verified against source:

| Phase | Checklist | Actual |
|---|---|---|
| 0 — schema & models | ✅ | `workspaces.slot_index`, `base_workspace_id`, `workspace_kinds.mode` all present in DB |
| 1 — slot manager | ✅ | `src/orchestrator/worktree_manager.py` (27 defs) |
| 2 — acquisition | ✅ | `_recover_stale_state` short-circuits on `ws.is_slot` |
| 3 — merge slot | ⬜ | **done** — `src/orchestrator/merge_slot.py`, `merge_slots` table exists, `_phase_integrate` at `git_ops.py:541` |
| 4 — reaper & adoption | ⬜ | **done** — `adopt_existing` (`worktree_manager.py:992`), `reap_slot`, cascade step 7d |
| 5 — surface | ⬜ | **done** — `workspace_doctor`, `workspace_reap` in `src/commands/worktree_commands.py` |
| 6 — flag flip | ⬜ | **done** — `WorktreesConfig.enabled = True`; docstring: *"the rollout gate has retired, and the kind's markdown `mode` is the steady-state knob"* |

Only Phase 6's optional tail remains (delete the `_cleanup_worktree_workspace`
legacy path), which is cleanup, not a blocker.

`src/models.py:515` already declares `mode: str | None = KIND_MODE_WORKTREE` —
**the code default is worktree.** The vault opts out:

```yaml
# vault/workspace-kinds/project-repo.md
mode: exclusive-clone
worktree_setup: []
```

Confirmed in the DB: all three kinds (`project-repo`, `readonly-dir`, `vault`)
are `exclusive-clone`. That one line is why there are three clone directories.

> Update the checklist as part of this work. A stale checklist cost real time
> here, and it is the same failure mode as the stale compiled playbooks: a
> document asserting state that the code contradicts.

## 2. Current state

```
workspaces (project=agent-queue)
  ws-agent-queue-1   project-repo  /home/jkern/dev/agent-queue2   link  enabled=FALSE
  ws-agent-queue-3   project-repo  /home/jkern/dev/agent-queue3   link  enabled=true
  ws-agent-queue-4   project-repo  /home/jkern/dev/agent-queue4   link  enabled=true
  vault-agent-queue-33793117  vault  …/vault/projects/agent-queue link  enabled=true

projects.agent-queue: max_concurrent_agents = 2
                      repo_url = https://github.com/ElectricJack/agent-queue.git
```

All three are `source_type=link`, not `clone`. This matters: `find_worktree_base`
prefers clones over links, then orders by id — with no clones, id order decides.

## 3. What the switch produces

`find_worktree_base` picks the first **enabled** non-slot row of the kind:

- `ws-agent-queue-1` is `enabled=false` → skipped (good: that is this working
  directory and must never be an agent cwd)
- → **base = `ws-agent-queue-3` → `/home/jkern/dev/agent-queue3`**

Slots are created lazily beside it, count = `max_concurrent_agents` = **2**:

```
/home/jkern/dev/agent-queue3/                  ← base; fetch/merge only, never an agent cwd
  .git/info/exclude                            ← gains the managed /.aq/ marker block
  .aq/worktrees/slot-0/                        ← agent slot
  .aq/worktrees/slot-1/                        ← agent slot
```

`ws-agent-queue-4` (`/home/jkern/dev/agent-queue4`) becomes a **redundant
non-slot row**: not acquirable, contributing neither inventory nor capacity, and
flagged by `aq workspace doctor` (implementation spec §7.3).

Pre-flight facts verified on `agent-queue3`:

- clean tree, on `main`, `0 ahead / 0 behind origin/main`
- `.aq/hooks/` already present (harness hooks) — **`.aq/worktrees/` does not collide**
- `git worktree list` shows only the base — no stale registrations
- no `agent-queue managed` marker block in `.git/info/exclude` yet

## 4. Blocking prerequisite — push the merge first

Local `main` now carries the merge of six agent commits plus seven of mine, and
`origin/main` is still at `af3861b4`. Slot creation does
`git worktree add --detach <slot> origin/<default>` and every reset branches from
`origin/<default>`.

**Every slot would therefore be created from a base that predates today's fixes** —
including the `load_tools` fix and the `default_profile_id` backfill that agents
need in order to run correctly.

Push `main` to `origin` before enabling worktree mode. Not optional.

## 5. Open decisions

### 5.1 `worktree_setup` — RESOLVED: no pip install needed

**Verified empirically 2026-08-24. Slots need no Python install; `worktree_setup`
can stay `[]` for Python purposes.**

Only one checkout is pip-installed on this host:

```
agent-queue  0.1.0  /home/jkern/dev/agent-queue2     (editable)
```

`agent-queue3` and `agent-queue4` are **not** installed, yet pytest run inside
them imports *their own* `src`:

```
$ cd /home/jkern/dev/agent-queue3 && pytest tests/test_which_src_tmp.py
src.__file__ = /home/jkern/dev/agent-queue3/src/__init__.py   ✅
```

pytest's default `prepend` import mode puts rootdir at the front of `sys.path`,
ahead of the editable finder in site-packages. Independently corroborated: the
agent-authored test files (`test_costs.py`, `test_profile_default_selection.py`)
exist only in `agent-queue4` and pass there — they could not run against
`agent-queue2`'s tree.

The original worry — that `pip install -e` mutates the shared interpreter and two
slots would race — is therefore moot: **nothing needs to install.** Third-party
dependencies (pytest, sqlalchemy, …) resolve from the shared user site-packages,
which is correct: they are the same versions for every slot.

Two residual limitations, both **pre-existing under clone mode** and therefore
not regressions:

1. **A branch that adds a dependency** won't have it in the ambient environment.
   Handle it when it happens (`pip install` the new dep, or add a setup command
   at that point) rather than paying a per-slot venv cost up front.
2. **`packages/aq-client` is installed non-editable** into site-packages, so a
   slot's edits to it are invisible to imports. Identical under clone mode today.
   If agents start working on the generated client, that needs revisiting.

The `aq` console script always resolves to `agent-queue2`'s code regardless of
cwd. That is **correct** — `aq` is a thin HTTP client to the daemon, and the
daemon runs from `agent-queue2`.

### 5.2 Slot count is 2

`max_concurrent_agents = 2` means two slots, i.e. the same parallelism as today's
two enabled clones. Worktree mode makes the cap the only knob (raising it creates
slots lazily, no provisioning), so this is a good moment to decide whether 2 is
right.

### 5.3 Plan subtasks serialize

Design spec §4.4: subtasks share the parent's branch, and git allows a branch in
exactly one worktree, so sibling subtasks cannot run concurrently. **Accepted
behavior, not a bug** — but it is a change in parallelism characteristics worth
knowing before the switch.

## 6. Procedure

Each step is independently verifiable. Do not batch them.

### Step 0 — preconditions
- [ ] Daemon stopped (currently is)
- [x] `git push origin main` — done 2026-08-24 (`af3861b4..904fa68a`, 284 commits)
- [ ] Tag rollback point: `git tag pre-worktree-migration`
- [ ] Record current state: `aq workspace list --json > /tmp/ws-before.json`

### Step 1 — `worktree_setup` (§5.1) — RESOLVED, no action
- [x] Verified: slots need no pip install; leave `worktree_setup: []`

### Step 2 — flip the mode
```yaml
# vault/workspace-kinds/project-repo.md
mode: worktree
```
- [ ] **Verify the edit reaches the DB.** Playbook markdown silently failed to
      compile all day for a wiring bug; do not assume vault→DB sync works.
      Check: `SELECT mode FROM workspace_kinds WHERE id='project-repo';`
      If it does not sync, that is a bug to fix before continuing — not to
      work around by editing the DB directly.

### Step 3 — start the daemon, orchestrator paused
- [ ] `aq start`, then immediately `aq system orchestrator-control --action pause`
- [ ] Confirm the exclude marker block was written to
      `agent-queue3/.git/info/exclude`
- [ ] `aq workspace doctor` — expect `ws-agent-queue-4` flagged redundant

### Step 4 — provoke one slot creation
- [ ] Unpause; allow exactly one task to dispatch
- [ ] Verify `/home/jkern/dev/agent-queue3/.aq/worktrees/slot-0/` exists with
      `.aq-worktree.json`, and a `workspaces` row with `slot_index=0`,
      `base_workspace_id=ws-agent-queue-3`, `source_type=WORKTREE`
- [ ] Verify branch `aq/<task_id>` and that `git status` in the **base** is clean
- [ ] Verify the agent can import `src.*` and run `pytest` **inside the slot**

### Step 5 — restart survival (the highest-risk behavior)
- [ ] With a slot present, `aq stop && aq start`
- [ ] Verify the slot directory, its `workspaces` row, and its branch all survive
      (`adopt_existing`, not delete). This is exactly the class of bug that has
      bitten repeatedly today — in-memory state lost across restart.

### Step 6 — second slot + concurrency
- [ ] Drive two tasks concurrently; confirm `slot-1` is created and both run
- [ ] Confirm merge-slot serialization on integration (`merge.started` /
      `merge.succeeded` events, one at a time)

### Step 7 — retire the redundant clones
- [ ] Only after slots have carried real work: disable `ws-agent-queue-4`
- [ ] Delete `/home/jkern/dev/agent-queue4` once nothing references it
- [ ] Leave `ws-agent-queue-1` (`agent-queue2`) disabled and on disk — it is the
      human working directory

### Step 8 — housekeeping
- [ ] Update the Phase Checklist in `docs/specs/implementation/worktree-execution.md`
      to match reality (§1)
- [ ] Note the migration in the design spec's §9 rollout section

## 7. Rollback

Per implementation spec §7.5, at any point:

```yaml
mode: exclusive-clone     # in project-repo.md
```
or `worktrees.enabled: false` in `~/.agent-queue/config.yaml`.

Slot rows stop being acquisition candidates and the clones resume immediately.
Slots can be reaped later at leisure via `aq workspace reap`. **No data migration
is reversed** — the Alembic revision only backfills `mode`, and branches are the
durable artifact regardless of mode.

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| ~~`worktree_setup` empty → slots cannot run tests~~ | **resolved** | §5.1 — verified: pytest rootdir shadows the editable install; no install needed |
| Slots branch from stale `origin/main` | **high** | §4 — push first |
| vault→DB kind sync silently no-ops | medium | Step 2 explicit verification |
| Slot loss across daemon restart | medium | Step 5 is a dedicated gate |
| Base repo (`agent-queue3`) gets dirtied | medium | `.git/info/exclude` marker; verify in Step 4 |
| Submodules | none here | repo has none |
| Plan subtasks serialize | low | accepted design (§5.3) |

## 9. Explicitly out of scope

- Changing `max_concurrent_agents` (decide separately, §5.2)
- Migrating the `vault` or `readonly-dir` kinds — they are not git repos and stay
  as they are
- Deleting the `_cleanup_worktree_workspace` legacy path (Phase 6 tail)
