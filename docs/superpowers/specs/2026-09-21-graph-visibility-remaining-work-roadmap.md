# Remaining work after the graph-visibility branch: an index

**Date:** 2026-09-21 · **Status:** index, for decomposition into tasks and subtasks
**Supersedes as a to-do list:** §8 "Sequencing" of `2026-09-19-visibility-subtasks-and-evaluation-planning.md`
**Audience:** an agent that will break this into an `aq-graph` plan (phases, tasks, subtasks)

This document owns no design. It indexes every piece of work still open from the
graph-visibility programme, and for each one it says four things: where the design lives,
what state that design is in, what blocks it, and what counts as done. Where no design
exists, the item's first task is to write one. The design specs it points to remain the
authority. If this index and a spec disagree, the spec wins, and the index should be fixed.

---

## 0. How to use this document

### 0.1 Rules for the decomposing agent

1. **One work item (§3–§8) is one task or one epic, never less.** Items marked
   **Epic** have enough independent parts to justify a container. Where this document
   gives a *suggested breakdown*, treat it as a starting point, not a mandate.
2. **Design before code.** An item whose design state is *none* or *sketch* gets a
   design task first. The implementation tasks are gated on it with a `blocks` edge. A
   design task delivers a spec in `docs/superpowers/specs/` and gets an adversarial
   review before any implementation task is released. Every spec in this programme was
   materially changed by its review.
3. **Operator gates are real gates.** Items that need an operator decision (§2) must not
   be implemented on a guessed answer. Model each decision as a task assigned to the human,
   or leave the dependent work out of the graph until it is answered. Never encode a
   default and proceed.
4. **Use phases only where order is forced.** Phases here are the delivery order in §9.
   Most items within a phase are independent; do not chain them.
5. **Subtasks are for checklists inside one deliverable.** Examples are the leftover
   minors in §7 and the acceptance checks of a task. Anything that needs its own branch,
   review or agent is a task, not a subtask.
6. **Do not re-do finished work.** §1 lists what is done. If a task seems to need a part
   of it rebuilt, say so in the task and stop. Do not quietly re-implement it.
7. **Do not duplicate the `swift-orbit` epic.** Archiving tasks that have integration
   history already has its own agent-owned epic (§6.1). Index it, depend on it, do not
   re-file it.

### 0.2 Standing constraints that bind every task

Taken from `CLAUDE.md` and the operator's standing instructions. Each task inherits them.

- **Never migrate the operator's database.** No `alembic upgrade`, `alembic stamp` or
  `aq start` from a slot. A schema change ships as an idempotent, inspector-guarded
  revision, and only the operator runs `aq db upgrade`.
- **Testing:** use `aq test` with focused files, never a bare `pytest tests/`, and never
  raise `-n`. Latency budgets run only under `AQ_PERF_STRICT=1`, serially, on a quiet
  box. Run `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` after any change to
  claims, pools, formulas, the hierarchy or provider failover.
- **Generated artifacts:** after any change to `src/api/models` or a codegen router,
  regenerate with `./scripts/regenerate-api-client.sh --offline` and then
  `./scripts/regenerate-ts-client.sh --from-file`. Never hand-edit
  `packages/aq-client/`. After adding a CLI command, regenerate the CLI inventory. A
  profile edit that changes grants may drift reviewed-bundle fingerprints; run
  `tests/test_shipped_profile_capabilities.py` and the reviewed-bundle tests.
- **Vault profiles on this install are operator-edited** (`harness: codex` on purpose).
  Never blanket-reseed. Merge grants with `aq agent profile-reseed --profile-id <id> --grants-only`.
- **Policy in playbooks, mechanism in code.** A rule about *when* something happens
  belongs in a playbook or profile text. The code supplies the command it calls.
- **Git:** never run a bare `git stash`, `git checkout -- <path>` over files another task
  owns, or `ruff --fix` over `src/`. Push task branches to `origin`, never `darcyle`.
  Do not push `main`; whether local `main` is pushed is the operator's call (§8.3).

---

## 1. Done — do not re-implement

All of this is on local `main` as of `f5b6cf7dc`. Detail is in
`2026-09-19-graph-visibility-markers-subtasks-implementation.md` ("Implementation notes (as
built)") and in `CLAUDE.md`'s **Phases**, **Subtasks**, **Agent markers** and **Graph
layout** entries.

| Area | What shipped |
|---|---|
| Agent markers (B) | Markers docked from live attempts (`list_live_task_workers`); reconciler `reset_stale_busy_agent`; doctor `agents.dangling_current_task` with a state-preserving `--fix`. |
| Subtasks (C) | `task_subtasks` table (rev `a00000000010`); `task_subtask_*` commands and CLI; prime `## Subtasks`; close refusal `subtasks.open` / `--skip-open-subtasks`; graph node counts; task-panel checklist. |
| Phases (A2) | `phase_create` / `phase_list`; gating through `blocks` edges onto every earlier non-completed sibling; held-open childless containers; refused in `hierarchy`/`train`. |
| Standing parents | `parent_key` on `create_task` / `ensure_task` (mechanism only; no shipped playbook uses it). |
| Planning grammar | `subtasks:` and `phases:` in `aq-graph`, written in `write_plan`'s single transaction; validator findings incl. transitive `inverted_phase_edge`; supervisor profile prose ("Most epics have no phases…"). Planning spec slices 1–3. |
| Archive sweep robustness (A4) | Per-root catch, `archive_refusal` metadata, doctor `tasks.archive_blocked`, `integration_owned` refusal. 123 tasks archived on this install. |
| Profile grant drift | `profiles.system_drift` names missing grants; `profile-reseed --grants-only` (`merge_profile_grants`, atomic, `.bak-<epoch>`). |
| Landing view (A1/A5) | Show completed off by default, empty state, Density control, shared `<ProgressBar>`, finished containers dropped from the `active` variant unless still context. |
| Layout first slice | Aspect-balanced row target, tidy-seed ordering by `(phase_order, activity_class, created_at, id)`, convergence through the `layout_jobs` ledger, crossing-edge SQL filter. Layout spec §3 / §5. |
| Enter-only navigation | No inline expansion; Enter control and double click; breadcrumbs; `variant_applied`; `root` on list/locate; entered frame cropped to its children with docked boundary stubs and a once-per-entry fit (`f5b6cf7dc`). |

**Already resolved since the last status report. Verify, then drop:**
- Daemon health was "degraded" by a broken `default-assignment-routing` playbook. On
  2026-09-21 `/health` reports `healthy` and every required playbook `ok`. Nothing is owed
  unless it regresses.
- The broken ref `refs/remotes/origin/aq/development-repair-255224b22377c9aa6145` no
  longer resolves, and `git fsck` reports nothing about it.

---

## 2. Operator decisions (gates)

Each decision below blocks the items listed. File each as a task assigned to the human, or
keep the dependent items out of the graph until it is answered. The recommendation column
is the author's view, not a decision.

| # | Decision | Blocks | Recommendation | Source |
|---|---|---|---|---|
| OD1 | Do phases stay refused in `hierarchy`/`train` projects, or should they be designed to work there? | §3.5 | Keep refused. No project on this install runs those modes, so any design there cannot be checked against real data. | Planning spec §3.1; impl spec |
| OD2 | Does a FAILED task inside phase *N* hold phase *N+1* closed forever (today: yes, because the container never completes), or does the phase need a "settle with failures" rule? | §3.4 | Needs an answer before real work uses phases. Today one failure deadlocks every later phase until an operator deletes or retries it. | Impl spec, open decisions |
| OD3 | Under an active search or status filter, the server force-opens the ancestors of every match. That is the one exception to enter-only navigation. Keep it, or replace it with count badges on the tiles? | §4.4 | Keep it for now; revisit after using enter-only for a while. | `CLAUDE.md` Graph layout |
| OD4 | Add a "take me to running work" control (jump into the container that holds the running task)? | §4.5 | Yes, it is cheap. The old "Focus active" was removed with inline expansion. | Session discussion |
| OD5 | The six layout questions in the layout spec §7 (does a finished stub sink; `ROW_ASPECT = 1.3`; a finer growth ladder; the ~5 h silent convergence; manual pins; ordering between tidies). | §4.1, §4.2 | Answer 5 (pins) and 6 (ordering) first; they decide whether §4.1 exists at all. | Layout spec §7 |
| OD6 | The archive spec's decisions D1–D5 (drop the verifier foreign key; the delivery rule for every development archive; leave `delivery_target_fixed`; supervisor authority for the integration controls, plus `release-owner`; `abandon_undelivered`). | §6.1 | See that spec §13. | Archive spec §13 |
| OD7 | End the three obsolete integration operations (`4caf5ff3…`, `096374ac…`, `b02de174…`) with `aq integration cancel-preserving`, as the archive spec §11.3 proposes. | §6.1, §6.2 | Yes, before anything is archived. It is an operator action, not an agent task. | Archive spec §11.3 |
| OD8 | Which benchmark project the evaluation uses, and the monthly budget ceiling per arm. | §5 | Choose during §5's design task, then confirm. | Planning doc §6 D1 |

---

## 3. Workstream A2/C follow-ups — making phases and subtasks used for real

### 3.1 Supervisor plans real work with phases and subtasks — *Task*

- **Goal:** the next real epic on `agent-queue` is planned by the supervisor as a phased
  `aq-graph` with subtasks, and the result reads correctly on the dashboard.
- **Design state:** complete; this is the acceptance test of the planning spec (§8 there).
- **Depends on:** nothing in code. Benefits from OD2 being answered first.
- **Done when:** a real epic has at least two phases, and phase 2 stays DEFINED until
  phase 1 completes. At least one task carries subtasks that a worker ticks off. The
  operator confirms the graph reads well, and any friction is filed as new tasks.

### 3.2 End-to-end `phased-graph` smoke scenario — *Task*

- **Goal:** `scripts/e2e-smoke.sh` gains a scenario that cooks a phased document with
  subtasks through the real CLI. It asserts: phase *N+1* withheld until phase *N*
  completes; subtasks listed in prime; a close refused on open subtasks and then passed
  with `--skip-open-subtasks`; and, in `development` mode, the inter-phase gate released
  on COMPLETED alone.
- **Design state:** named in the planning spec §8; no scenario exists yet (`grep phased
  scripts/e2e-smoke.sh` is empty).
- **Done when:** the scenario passes in the isolated e2e kit and is listed in
  `docs/guides/e2e-swarm.md`.

### 3.3 Repair the planner agent type — *Epic*

- **Goal:** the shipped `planner` profile can create a graph. Today it can create only
  children of its own planning task, because `create_task_graph` is not in
  `AGENT_COMMAND_SET`.
- **Design state:** sketch, in the planning spec §7.4. It needs a design task.
- **What the design must settle:** three safety properties must exist before
  `create_task_graph` may join `AGENT_COMMAND_SET`:
  1. a filing quota through `reserve_filing`;
  2. containment under the held task (`hierarchy.parent_out_of_scope`);
  3. provenance, as a `discovered-from` edge.
  Only after all three: the planner profile grant and Rules, and a check that
  `EXPECTED_UNREACHABLE["planner"]` stays `set()`.
- **Suggested breakdown:** design + review → the three safety properties (one task each,
  or one task with three subtasks) → the scope change + profile grant → the skill-text
  correction (§3.2(d) there) → tests.
- **Security note:** this widens what a session token can create. The design review must
  treat it as security-sensitive.

### 3.4 A phase containing a FAILED task — *Task, gated on OD2*

- **Goal:** implement whatever OD2 decides. If the answer is "no rule", the task is to
  document the remedy (retry or delete the failed task) where the operator will find it:
  `aq task explain` output and the phase header on the dashboard.
- **Design state:** none.

### 3.5 Phases in `hierarchy`/`train` — *Design task, only if OD1 says so*

- If OD1 keeps the refusal, drop this item.

### 3.6 Standing parents used by a playbook — *Design task*

- **Goal:** planning-doc item A3, "dynamic tickets never land in the root". The mechanism
  (`parent_key`) exists, but no playbook uses it, and worker-filed work still reaches the
  root in some paths.
- **Design state:** open questions only (planning doc §3 A3). It needs a design:
  - which creators (CI sentinel repairs, recovery incidents, supervisor maintenance)
    should file under a standing per-project "maintenance" parent;
  - how that interacts with the `development` integration mode.
  `parent_key` is refused in `hierarchy`/`train`, and the CI sentinel deliberately files
  at the root.
- **Done when:** a reviewed spec names each creator's placement, and a follow-up
  implementation task exists for each playbook change.

### 3.7 Structure in the planning proposal (Tier 3) — *Deferred; do not schedule*

Planning spec §3.4. It is only worth doing if the operator turns on `default-pipeline`.
It stays listed here so it is not lost.

---

## 4. Workstream A1/A5 follow-ups — the graph view

### 4.1 Layout second slice — *Epic, gated on OD5*

- **Source:** layout spec §6. Each of the five items there is recorded with the reason
  it is **not shippable as drafted**. Every one needs a fresh design, not a
  transcription of §6.
- **Items and what the design must solve:**

  | Item | Blocking problem recorded in §6 |
  |---|---|
  | 6.1 `reorder` mode | No dirty mark is written on `READY → IN_PROGRESS`, so work cannot float when it starts. In `active`, settled containers are already dropped. It would sort on stale aggregates. Mode precedence in `_drain` must be specified. Depends on OD5 Q6. |
  | 6.2 Filling the hole a finished leaf leaves (`reflow`) | `clear_layout_dirty` deletes by `seq` with no reason filter, so deferred marks would be deleted before the sweep sees them. Needs a reason-aware clear or a separate table. |
  | 6.3 Banner offering to reset pinned positions | The client cannot tell a rebuild from an incremental publish, because `layout_version` bumps every pass. Needs a distinguishable signal (e.g. the `layout_jobs` row via `extent`). Depends on OD5 Q5; if pins are retired, this item disappears. |
  | 6.4 Phase rank floor | A phase created after its predecessors completed gets no gate edge and lands at rank 0 beside them. Needs `rank >= phase_order` in layering. |
  | 6.5 Finer growth ladder | About 2.4× more band crossings; measured by `test_root_band_crossing_publish_under_1s`. Depends on OD5 Q3. |

- **Suggested breakdown:** one design task covering the items OD5 keeps, reviewed. Then
  one implementation task per kept item. 6.4 is independent of the rest and can go
  first.

### 4.2 Layout spec is self-contradictory in two places — *Task (docs)*

- Layout spec §6's Task-2 block still names the removed `layout_job_exists` and its test.
- The G3 sentence (around lines 342–347) says the clamped target is not ordering-dependent,
  but it is.
- **Done when:** the spec describes what shipped. This is docs only.

### 4.3 Mobile list parity with the canvas — *Task*

- The portrait phone list (`MobileLayoutList.tsx`) still says "No tasks match these
  filters." for a project whose work is all finished, and offers no Show-completed
  affordance.
- It also ignores `variant_applied`, so it never shows the "completed work is shown"
  banner the canvas shows.
- **Done when:** the phone view has the same empty state and banner as the canvas, with
  tests.

### 4.4 The search exception — *Task, gated on OD3*

- Only if OD3 chooses count badges: replace `forced_expansion_for` in the dashboard path
  with badges showing match counts on containers, and enter on click. Otherwise drop this
  item.

### 4.5 "Take me to running work" — *Task, gated on OD4*

- A toolbar control that enters the container holding the highest-priority running task
  (via the `focus` URL param, so Back works) and pans to it.

### 4.6 Tall single-chain containers — *Design note, low priority*

- A container whose children form one long dependency chain is laid out tall and narrow.
  After enter-and-fit, the cards get small in a short window.
- The serpentine `chain_target` floor already wraps long chains at the root. Check
  whether it applies inside containers, and file a task if not.

---

## 5. Workstream D/E — the comparative evaluation — *Epic, design-first*

- **Source:** planning doc §6 (D1–D4) and §7 (E). These are goals and open questions only.
  That doc says every workstream gets its own design spec; D and E never got one.
- **Goal:** one command (`aq eval run <benchmark>`) runs a fixed project three ways (Agent
  Queue, Claude Code alone, Codex alone) in a disposable environment. It produces a scored
  report that can be compared month to month. Headline metrics are wall-clock time and
  usage spend, counted only when the run passes correctness.
- **What the design must settle:** everything planning doc §6 lists as open:
  - **Benchmark:** the choice (OD8), whether to have a smoke size and a full size, the
    hidden acceptance suite, and pinned repo and model versions.
  - **Measurement:** the same accounting for spend across all arms; a scripted operator
    for counting autonomy; an LLM judge for blind quality review, with human
    calibration.
  - **Variance and leakage:** how many repetitions, and a fresh vault and memory for each
    run.
  - **A possible fourth arm:** Agent Queue with learned memory retained.
  - **Repo cleanliness (E):** score only signals that are comparable across arms; the rest
    is an Agent Queue-internal trend.
  - **Storage and reporting:** results stored with the commit SHA, config and benchmark
    version; a report view.
- **Suggested breakdown:**
  1. Design spec plus adversarial review.
  2. Benchmark project and hidden suite. This is the expensive, creative part; keep it a
     separate task.
  3. Harness skeleton:
     - a disposable environment built from the e2e kit and throwaway-container
       precedents;
     - no contact with the operator's database;
     - a hard budget ceiling for each arm.
  4. Metric collectors (one task per metric family).
  5. Results storage and report view.
  6. A first baseline run. It is the reference every later run is compared against.
- **Sequencing note:** the planning doc wanted this started early "so the rest can be
  measured rather than asserted". It is independent of §3, §4 and §6 and can run in
  parallel with them.

---

## 6. Integration health and archival

### 6.1 Archive tasks that carry integration history — *Existing epic `swift-orbit`; index only*

- **Source:** `2026-09-20-archive-tasks-with-integration-history-design.md`, revised
  2026-09-21 and **awaiting operator approval** (OD6). Nothing may be implemented until it
  is approved: it gates a schema change on a production database.
- **Prerequisites already filed there:**
  - P1 `swift-orbit.4`: the delivery predicate does not recognise operator adoption.
  - P2 `swift-orbit.5`: `integration.stranded_delegates` misses a stopped session with a
    leftover `claim_phase`.
  - The optional D4 `aq integration release-owner`.
- **Sequence (that spec §13):** P1 → P2 → D4 if chosen → OD7 → the change → §10 there.
- **For the decomposing agent:** do not re-file any of this. If the plan needs something to
  depend on it, depend on the `swift-orbit` tasks.

### 6.2 The three `active` operations on COMPLETED parents — *Operator action (OD7)*

- These are the three operations above, on `nimble-dune`, `smart-dune` and
  `sound-current`. Each has zero stages, has never advanced, and was reserved under
  hierarchy mode.
- The remedy and its reasoning are in the archive spec §11.3.
- `sound-current` also needs its attached owner row released, which is D4 in that spec.
- An agent may prepare the exact commands and checks, but must not run them.

### 6.3 Delivery branch cleanup — *Existing spec; check status*

- `2026-09-21-delivery-branch-cleanup-design.md` (task `quick-ridge`) is recent work in
  the same area; `CLAUDE.md` describes it as shipped.
- It is listed so the decomposing agent does not file an overlapping cleanup task.

---

## 7. Leftover minors from review — *one Task, with a subtask per line*

These were each deferred as minor during task reviews on the graph-visibility branch
(ledger: `.superpowers/sdd/2026-09-19-graph-visibility-markers-subtasks-implementation/progress.md`).
None is urgent. Group them into one or two tasks with a subtask per line, and confirm each
is still present before fixing it.

**Performance / correctness**
- Tighten `RECT_MS_PER_NODE` in `tests/perf/test_layout_api_statements.py` from `2.0`
  to about `1.5`. At 2.0 it would not catch a return to the measured 185 ms at 109 nodes
  (1.70 ms/node). Fix the docstring's claim and guard the per-node division.
- `add_task_subtasks` takes no lock on `max(ordinal)`, so a concurrent append surfaces
  `IntegrityError` instead of `subtask_limit`.
- `phase_create` takes no lock across read-max-order → create, so concurrent calls can
  produce a duplicate `order`.
- `phase_create`: an `add_dependency` failure after the metadata commit leaves a flagged
  but ungated phase (reported in the response, untested).
- `phase_create`: `_phase_siblings` hydrates every task in the project, and there is a
  `get_task_meta` round trip per phase.
- The subtask flip at close runs after `complete_session_task`. A database error there
  would leave rows open on a COMPLETED task.
- The layout ledger read filters on `kind` with no index, on a table that is never
  trimmed (one sequential scan per 900 s sweep).
- `variantApplied` stays stale for one round trip after a scope change.

**Tests**
- There is no server test for a finished container with an unfinished descendant that is
  entered in the `active` variant.
- There is no CLI-level test threading `--skip-open-subtasks` (or `--abandon-children`)
  through `agent_surface.py`.
- There is no FAILED-status test for `reset_stale_busy_agent`, and no stubbed-db test for
  the "task missing" branch of `agents.dangling_current_task`.
- Marker tests still set `Agent.current_task_id`, which docking ignores. The helper that
  seeds sessions and attempts is duplicated across three test files.
- The plan-mandated test that aggregate-dict order can't change the result is close to
  vacuous.

**Docs / code hygiene**
- `_check_phases` docstring (`src/task_graph/validator.py`, around 320–325) still says
  "memoised DFS … cuts back edges". The walk is Kahn's. Nodes upstream of a gating
  cycle lose their `inverted_phase_edge` finding until the cycle is fixed; document it.
- `phase_create`'s `blocked_by` can name a phase that no longer gates (`blocked_by_all`
  is correct). Reword or deprecate it.
- `subtasks:` on an `aq-graph` `parent:` block is silently ignored; make it a finding.
- `_check_subtasks_reportable` skips nodes with no explicit profile.
- `archive_task` refusals bypass `_hierarchy_failure` and carry no `references`.
- Stale `archive_refusal` metadata survives on roots that stop being eligible. It is
  filtered from the report, but never cleared.
- `BranchDiscardPrompt` never fires for a subtree that is also integration-held, and no
  dashboard surface renders `integration_owned` (archive spec, Q4).
- `scripts/regenerate-api-client.sh --offline` calls `python`, which is absent on this
  box; use the venv interpreter or `python3`.
- Add `agents.dangling_current_task` to the check lists in `docs/guides/worker-pools.md`
  and `session-troubleshooting.md`.

---

## 8. Housekeeping

### 8.1 Retire the implementation ledger — *Task, last*

- Once §7 is done, delete the graph-visibility SDD workspace. It is git-ignored.
- Then remove the `.worktrees/graph-visibility` and `.worktrees/graph-layout` worktrees,
  after confirming that every commit on their branches is an ancestor of `main`.

### 8.2 Scratch PostgreSQL — *not an agent task*

The session that built this branch ran a scratch PostgreSQL on port 5544 under its own
scratchpad. That session stops it, not a queue agent.

### 8.3 Pushing local `main` — *operator decision*

On 2026-09-21, local `main` is five commits ahead of `origin/main`: `f5b6cf7dc` plus four
merge commits. Only the operator decides whether to push. Agents never push `main`.

---

## 9. Suggested phases

This order is forced by dependencies; nothing else is. Items within a phase are
independent of each other.

| Phase | Contents | Why here |
|---|---|---|
| 1 — Decisions | OD1–OD8 as human tasks; §4.2 (docs); §6.1's P1/P2 (already filed) | Cheap, and they unblock or delete most of the rest. |
| 2 — Use it and measure it | §3.1 real phased planning; §3.2 e2e scenario; §4.3 mobile parity; §7 minors; §5 design task + review | Exercises what shipped before building more. Starts the evaluation early, as the planning doc asked. |
| 3 — Build on answers | §3.3 planner repair; §3.4 FAILED-in-phase; §3.6 standing parents; §4.1 layout second slice; §4.4 / §4.5 if chosen; §5 benchmark + harness | Each is gated on a phase-1 decision or a phase-2 design. |
| 4 — Close out | §5 first baseline run; §6.1 archival once approved; §8.1 | Depends on everything above. |

The evaluation epic (§5) has its own internal order and should run in parallel with
phases 2–3, not wait for them.

---

## 10. Source documents

| Document | Role |
|---|---|
| `2026-09-19-visibility-subtasks-and-evaluation-planning.md` | Original goals (workstreams A–E). Authority for §5. |
| `2026-09-19-graph-visibility-markers-subtasks-implementation.md` | What shipped for A, B, C. |
| `2026-09-20-planning-emits-phases-and-subtasks-design.md` | Planning slices (done); §7.4 planner repair; §3.4 Tier 3. |
| `2026-09-20-graph-layout-reorganisation-design.md` | Layout first slice (done); §6 second slice; §7 questions. |
| `2026-09-20-archive-tasks-with-integration-history-design.md` | `swift-orbit`; awaiting approval. |
| `2026-09-20-integration-delegate-release-design.md` | Delegate release (implemented except §2/§3). |
| `2026-09-21-delivery-branch-cleanup-design.md` | Branch cleanup; overlap check for §6.3. |
| `2026-09-01-task-graph-spatial-layout-design.md` | The layout engine's base spec. |
