---
tags: [analysis, plan, overhaul, execution]
---

# Framework Overhaul — Execution Plan, Waves 0–2

**Status:** Active (2026-08-19). Scope: everything buildable **without WSL/tmux**.
Direction: [framework-overhaul-todo.md](framework-overhaul-todo.md) ·
Specs: [design](../specs/design/README.md) · [implementation](../specs/implementation/README.md)

Verified on the dev machine before writing this plan:

- Native Windows Python 3.12.4; `pytest tests/test_database.py tests/test_orchestrator.py tests/test_config.py -q` → **128 passed**.
- `git worktree add` + `.git/info/exclude` marker verified working (git 2.33) — `git status` stays clean.
- Only 7 files in the tree touch `sys.platform` / `platform.system`.
- WSL2 Ubuntu is installed but **cannot boot**: `VirtualizationFirmwareEnabled: False` (SVM disabled in BIOS on the X570 board).
- Alembic single head `e252a41eb210`; 25 tables; 195 test files; CI has **no test workflow** (only `docs.yml`).

## 0. The WSL boundary

Everything in Waves 0–2 is Windows-native **except** one file cluster in session-runtime S1:
`providers/tmux.py`, `proctable.py`, `dialogs.py` and their tmux-marked tests. Those are
deferred to Wave 2-T (post-BIOS). Checkpoint **C1 — full task lifecycle on `FakeProvider`** is
reachable without WSL and is the target of this plan.

## 1. Parallelization strategy

Work lands on `main` **behind flags that default off**, in short-lived lane branches merged
daily. Long-lived branches are forbidden — every spec already defines its rollout flag.

### 1.1 The substrate rule

Four workstreams add tables, six add config dataclasses, five add cascade steps, six register a
command mixin, and Alembic's revision chain is linear. Those files are landed **once, serially,
in Wave 0**, so lanes never touch them again:

| Shared file | Substrate lands | Lanes then only… |
|---|---|---|
| `src/config.py` | all 8 config dataclasses, flags off | read their own flag |
| `src/database/tables.py` | all new tables/columns/indexes | — (no further edits) |
| `migrations/versions/` | **one** revision, DDL only | add their own data-step revision |
| `src/orchestrator/core.py` | no-op `async def` cascade stubs, called in order | fill in their own stub body |
| `src/commands/handler.py` | empty mixin classes registered in bases | add methods to their own mixin file |

**Rule: the substrate revision contains DDL only.** Data steps (e.g. work-graph's `is_blocked`
backfill) ship with the owning lane, so a lane can iterate on its predicate without rewriting a
merged migration. The one exception is `workspace_kinds.mode` → `exclusive-clone`, which must be
in the same transaction as the column add or existing installs silently change behavior.

**Deviation from the specs, deliberate:** work-graph §2 asks for four separate revisions and
worktree-execution §3 for its own. The substrate merges their **DDL** into one revision to keep
the chain single-headed across five parallel agents. Each spec's remaining data/behavior steps
keep their own revisions.

### 1.2 Worktrees

Lane agents work in dedicated git worktrees outside the repo tree (never under `.aq/`, which
would put a second copy of `tests/` in pytest's collection path):

```
<parent>/agent-queue-wt/<lane>      branch  wave<N>/<lane>
```

Tests import `from src.…`, so pytest run from a worktree root shadows the editable install and
exercises the worktree's code. Merges are sequential into `main` with the suite green after each.

## 2. Wave 0 — Substrate (serial, 1 opus agent)

Blocks everything. Single agent, no parallelism.

- [ ] `src/config.py`: `PlaybooksConfig`, `SessionsConfig`, `WorktreesConfig`, `SecurityConfig`,
      `PricingConfig`/`ModelPricing`, `SupervisorAgentConfig`, `SurfaceConfig`, `WorkGraphConfig`;
      wired into `AppConfig`, `validate()`, `load_config()`, `RESTART_REQUIRED_SECTIONS`,
      `_SECTION_FIELDS`; `src/config_editor.py` schema entries. All new flags default **off**.
- [ ] `src/database/tables.py`, DDL only:
      `task_dependencies.dep_type` + widened PK + check + composite indexes ·
      `tasks.is_blocked` + `archived_tasks.is_blocked` + `idx_tasks_project_status_blocked` +
      `idx_tasks_parent` · `gates` · `task_gates` · `task_labels` ·
      `workspace_kinds.mode`/`worktree_setup` · `workspaces.slot_index`/`base_workspace_id` +
      partial unique index · `merge_slots` · `sessions` · `messages` ·
      `agent_profiles` supervisor columns · `api_session_tokens` ·
      `token_ledger.model`/`input_tokens`/`output_tokens`.
- [ ] **One** Alembic revision off `e252a41eb210`; SQLite batch-recreate for the
      `task_dependencies` PK widen; partial index written by hand; the single
      `workspace_kinds` data step. `alembic upgrade head` **and** `downgrade -1` green.
- [ ] `src/orchestrator/core.py`: no-op cascade stubs in `run_one_cycle` —
      `_sweep_gates`, `_reap_worktree_slots`, `_reconcile_sessions`, `_deliver_messages`,
      `_revoke_expired_tokens` — each returning immediately, each flag-gated.
- [ ] `src/commands/`: empty mixins `gate_commands.py`, `message_commands.py`,
      `session_commands.py`, `surface_commands.py`, `ops_commands.py`, `worktree_commands.py`,
      registered in `CommandHandler`'s bases.
- [ ] `.github/workflows/tests.yml`: `pytest tests/ -n auto` + `alembic upgrade head` on push/PR.
- [ ] `pytest tests/ -n auto` green.

**Checkpoint C0:** suite green, migration round-trips on SQLite, daemon boots unchanged.

## 3. Wave 1 — 4 lanes in parallel

| Lane | Scope | Model |
|---|---|---|
| **1A** | `feature-pauses` full checklist — memory + playbooks paused, command gate, startup logs | opus |
| **1B** | `messaging-rework` M0 strip — delete `src/telegram/`, `discord/commands.py`, `project_wizard.py`, notes threads; `NullMessagingAdapter`; decouple handler/supervisor from the adapter | sonnet → opus review |
| **1C** | `trust-and-ops` — `src/env_scrub.py`, `isolated_env` delegation, `_validate_ref` across `GitManager`, `src/doctor/` skeleton + runner + builtin checks | opus |
| **1D** | `aq-surface` S0 — `src/cli/envelope.py`, `--brief`, `get_schema`/`aq schema`, `CLIClient` env vars, fix stale `src/cli/CLAUDE.md` | sonnet |

Conflict surface after substrate: 1A and 1B both touch `src/main.py`; 1B lands first, 1A rebases.

**Checkpoint C1a:** daemon boots with memory + playbooks paused and `messaging_platform: "none"`.

## 4. Wave 2 — 5 lanes in parallel

| Lane | Scope | Model |
|---|---|---|
| **2A** | `session-runtime` S0 + S1-minus-tmux + S2 — provider ABC, `FakeProvider`, conformance suite, `SubprocessProvider`, harness parser/registry + `vault/harnesses/claude.md`, `SessionSpecBuilder`, `SessionReconciler`, `_cmd_task_close`/`_cmd_task_heartbeat`/`_cmd_session_*`, events | opus |
| **2B** | `worktree-execution` P0–P2 — models/parser, `WorktreeSlotManager`, `GitManager` additions, mode-aware acquisition, `_prepare_workspace` worktree branch | opus |
| **2C** | `work-graph` WG-1 + WG-2 — `blocked_state.py` recompute + backfill revision, single-transaction rewrites, typed edges through the command surface, plan-parser edges, `task_labels` | opus |
| **2D** | `supervisor-agent` P0–P2 — `Message` model + queries, `MessageCommandsMixin`, `/api/messages`, `aq message`/`reply`, `src/task_graph/` parser/validator/creator, `_cmd_create_task_graph` | opus |
| **2E** | `aq-surface` S1 — `src/prime/` renderer + sections + overrides, `prime`/`task_handoff` commands, `aq prime|handoff`, hook templates, tool definitions | sonnet → opus review |

Ordering inside the wave: 2A and 2B integrate at the end (a session's `work_dir` is its slot),
so they merge as a pair. 2D's delivery engine (P3) is **out of scope** — it needs named sessions.

**Checkpoint C1 (wave goal):** orchestrator → `FakeProvider` session → `aq task close` →
drain-ack → DONE, with the task running in a real worktree slot. No tmux, no WSL.

## 5. Deferred

**Wave 2-T (done):** `TmuxProvider`, `proctable.py`, `dialogs.py`, tmux conformance tests.
**Wave 3 (done):** work-graph WG-3/4/5 (gates sweep, explain, outcomes), session-runtime S3
(transcripts/SSE), worktree P3–P6 (merge slot, reaper, adoption), supervisor P3–P5.
**Wave 4 (MVP surface):** dashboard D1–D4 (Sessions, Task explain/graph, Gates inbox,
Supervisor chat), in-process Discord verification against the new messaging paths,
aq-surface S2 (auth tokens; also fixes session-scoped `aq prime`).
**Post-MVP (do not drop):** dashboard **D5 Worktrees · D6 Harness editor · D7 Doctor ·
D8 Costs** (messaging-rework §6 M5 order), aq-surface S3 (task-scoped MCP) + S4
(measurement), messaging M2–M4 (out-of-process `packages/aq-discord/` bridge).

## 6. Manual, not automatable

- **Rotate the GitHub PAT** embedded in plaintext in `.git/config`'s `origin` URL, and switch
  the remote to SSH or a credential helper.
- **Enable SVM Mode** in BIOS (Tweaker → Advanced CPU Settings) to unblock Wave 2-T.
