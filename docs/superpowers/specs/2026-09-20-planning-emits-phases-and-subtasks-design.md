# Planning Emits Phases and Subtasks — Design

**Date:** 2026-09-20 (rev. 2, post-review) · **Branch:** `feat/graph-visibility` · **Status:** design, no code

**Problem.** The `feat/graph-visibility` branch built the mechanisms — `phase_create`/`phase_list` (`src/commands/phase_commands.py`) and in-task subtasks (`src/commands/task_subtask_commands.py`, table `task_subtasks`) — and nothing produces either. No shipped prompt mentions a phase. No creation path can express one atomically. Until planning emits the structure, a new epic looks exactly like an old one: a flat container with N children.

**Operator's target shape.** Phases (ordered, gating) around and inside epics; epics with children; children with "possibly a ton of little subtasks that might not even be handed to separate agents… tackled in some order by a single agent, quickly, but used to track progress within a larger body of work." Sequential execution by one agent is often faster than a handoff — *the only thing wanted from a subtask is visibility.*

**Revision note.** This is rev. 2. The first draft proposed adding `create_task_graph` to `AGENT_COMMAND_SET` as task 1; adversarial review established that the planner is broken more deeply than reported and that the fix as written would be unsafe. That work is now a **deferred follow-up** (§7.4), not part of the first slice. The first draft also proposed refusing phases in `hierarchy`/`train` on the graph path only, which would have contradicted shipped behaviour; §3.1 now states one policy at both doors, and §3.0 states what happens in every integration mode — including `development`, which is what the operator's projects actually run and where phases are safe today.

---

## 1. How planning works today, from the code

Five paths lead from intent to tasks. Two create structure; three cannot.

### 1.1 `create_task_graph` / `aq-graph` — the real graph path

`aq task create --from-spec <path>` and `--graph <file>` (`src/cli/tasks.py:125-160`, `:356`) call `create_task_graph` (`src/commands/task_commands.py:2966`). A fenced ` ```aq-graph ` block is extracted (`src/task_graph/parser.py:381`), parsed structurally (`:294`), validated semantically (`src/task_graph/validator.py:552`), planned (`src/task_graph/creator.py:205`) and written in **exactly one transaction** (`:435`) — whole or not at all.

What it can express today:

| | |
|---|---|
| **Container** | one `parent:` block → one task row (`creator.py:230-250`), `status=IN_PROGRESS`, flagged a container |
| **Nodes** | `nodes:` → task rows, `status=DEFINED` (`creator.py:56`), ids `<container>.1..N` (`assign_child_ids`, `:64`) |
| **Edges** | per-node `needs:` with `dep_type` (`models.py:61`) |
| **Per node** | `title`, `description`, `acceptance`, `deliverables`, `context`, `labels`, `priority`, `profile`, `intelligence_class`, `task_type` (`models.py:103-136`) |
| **Nesting** | **exactly two levels.** `GraphNode` (`models.py:103`) has no `children` and **no metadata map**. Every node is linked to the one container by a single `set_parent_bulk` (`creator.py:525`). |
| **Under an existing parent** | `parent_id` → *no container row is planned at all* (`creator.py:229`, `parent_row` stays `None`); the nodes hang directly off the given parent |

So a graph is `container → node`, or `existing-parent → node`. It cannot produce three levels and cannot stamp `task_metadata` on anything except formula provenance on the container (`creator.py:560-581`).

**Who can call it.** The loopback CLI and the elevated supervisor, and nobody else. `create_task_graph` is **not** in `AGENT_COMMAND_SET` (`src/api/scope.py:13-76`), so a session token is refused `out of scope: create_task_graph` (`scope.py:206`). The only elevated session minted anywhere is the supervisor (`src/messages/session_lens.py:356-368`, `elevated=True`); task sessions (`src/orchestrator/execution.py:819`), pool sessions (`src/orchestrator/pools.py:895`) and manual terminals (`src/agents/terminals.py:195-200`) all mint non-elevated tokens. `formula_cook` is the same story — elevated-only (`src/commands/formula_commands.py:262`) and sharing `_validate_graph_parent` with `create_task_graph`, so anything said here about the graph parent applies to it unchanged.

### 1.2 The shipped planner agent type is non-functional — a production bug

Not a subtlety and not a gap this design introduces. Two independent walls:

1. **It cannot create a graph.** `planner/profile.md:25-31` tells it to run `aq task create --from-spec <path> --dry-run` and then without it. `create_task_graph` is neither in its `## Capabilities.aq_commands` (`:63-88`) nor in `AGENT_COMMAND_SET`, and the planner is `lifecycle: task` (`:40`), so its token is non-elevated. Step 4 of the shipped Role is unexecutable.
2. **It cannot file work into a phase it creates.** `phase_create` *is* in `AGENT_COMMAND_SET` (`scope.py:63`) and does work from a planner session — it passes `root: True` (`phase_commands.py:128`), and a worker-filed root task is allowed. But the next step is not. A non-elevated session's `create_task` goes down the worker-filing path (`task_commands.py:2047`), where the parent must be the held task, one of its descendants, or the held task's own parent (`_PARENT_SCOPE_ERROR`, `:333-337`; enforced at `:1525-1528` in-transaction and `:1371-1378` for reparent, code `hierarchy.parent_out_of_scope`). A root-level phase is none of those. So `aq task create --parent <phase-id>` — which `src/skills/aq-tasks/SKILL.md:347` explicitly instructs — is refused.

**Net:** the shipped planner can produce nothing but children of its own planning task. Every graph in this install is created by the supervisor or by a human at the loopback CLI. The fix is designed in §7.4 and is **deferred**: `_cmd_create_task_graph` (`task_commands.py:2966-3080`) reads no session principal at all, so exposing it to session tokens as-is would hand any granted profile unmetered root-task creation.

### 1.3 Spec ingest → proposal → human gate → batch commit

`spec_approve` (`src/commands/spec_commands.py:30`) flips a vault spec's frontmatter and emits `spec.approved` (`:83`). The shipped `default-pipeline` (`src/prompts/default_playbooks/default-pipeline.md:27-41`) `ensure_task`s a `spec-ingest` task; that agent (`src/profiles/defaults/spec-ingest/profile.md:16-28`) reads the spec and calls `task_batch_propose`; `proposal.ready` opens a human gate (`:45-51`); an approving `gate.resolved` calls `task_batch_commit` (`:53-69`).

A proposal can express (`src/commands/proposal_commands.py:45-77`) `tempId`, `title`, `description` (required), plus `priority`, `deliverables`, `profile_id`, `intelligence_class` read at commit (`:643-654`, `:657-681`), and edges of any `TASK_DEP_TYPES` including `parent-child`. In a hierarchy/train project `parent-child` really nests (`_commit_hierarchical_proposal:506-605`); in every other mode it is handed to `add_dependency` like any other edge (`:445-455`). No phase, no subtasks, no metadata.

**This path is opt-in.** `default-pipeline` is in neither `REQUIRED_SYSTEM_PLAYBOOK_IDS` nor `DEFAULT_SYSTEM_PLAYBOOK_IDS` (`src/playbooks/required.py:25-33`). A fresh install does not run it.

### 1.4 Formulas

`src/task_graph/formulas.py` resolves a vault template's `extends` chain and vars into one `aq-graph` document, cooked by `formula_cook`. It inherits §1.1's grammar exactly, so it inherits its limits exactly — as `docs/specs/design/work-graph.md:340` already states: *"formulas cannot declare phases — an `aq-graph` node carries no metadata or nesting."* Any grammar extension here reaches formulas for free.

### 1.5 Supervisor-created work and worker filing

The supervisor (`src/profiles/defaults/supervisor/profile.md`) is elevated, holds `phase_create`/`phase_list` (`:94-95`) and `task_subtask_*` (`:105-108`), and is told *"Create graphs, not loose tasks"* (`:170-173`) — but nothing tells it to create a phase, and its graphs are still §1.1 two-level graphs. **The supervisor is the one actor for whom phases and subtasks already work end to end today** (in a non-hierarchy project): elevated, so no filing scope applies and no subtask fence applies. That is why the first slice is prose aimed at it.

Worker filing (`emergent_work.md`, rendered when the profile grants `create_task` — `src/prime/sections.py:610-612`) files one task per confirmed finding, child-of-held-task by default. Deliberately single-task; unchanged by this design.

### 1.6 Unrelated cosmetic defect, out of scope

`_create_one_task` (`proposal_commands.py:674`) passes `"metadata": {"proposal_source": source}` to `create_task`, which has no `metadata` argument — the key is silently dropped on the non-hierarchical commit path. The hierarchical path writes it explicitly (`:574`). Two-line fix (an explicit `set_task_meta` after the create, mirroring `:440`); not part of this work and not related to §1.2.

---

## 2. The gap, per path

| Path | What blocks phases | What blocks subtasks |
|---|---|---|
| `aq-graph` / `create_task_graph` | grammar has no phase concept and exactly two levels (`models.py:103`, `creator.py:64`); no metadata map on any node | no `subtasks` field anywhere in `GraphNode` |
| planner session | §1.2 — cannot reach `create_task_graph`; *can* call `phase_create` but is then refused `hierarchy.parent_out_of_scope` when filing into it | the write fence (below) — a planner may write subtasks only on its own planning task |
| spec-ingest → proposal | `_validate_shape` (`proposal_commands.py:45`) has no phase field; commit writes no metadata | no subtask field in the payload; same fence |
| formulas | inherits the `aq-graph` limits | same |
| supervisor | **nothing but prompt silence** (elevated; no filing scope, no fence) | **nothing but prompt silence** |
| a playbook | `phase_create` and `task_subtask_add` are **not in the contract registry** — the contracted set is `list_projects, get_task, render_prompt, read_project_memory_file, count_project_memory_files, git_diff, memory_save, memory_search, create_task, ensure_task, edit_task, add_dependency, gate_create, gate_resolve, list_tasks, get_downstream_tasks, task_batch_commit, stop_task, ci_baseline_status, provider_usage_probe, message_send, task_recovery_notify, task_route_options, task_route` (`src/commands/contracts/builtin.py:682-1060`) | same; and `CommandArgs` is `extra="forbid"` (`src/commands/contracts/models.py:22`), so a playbook cannot smuggle `metadata` through `create_task` either |
| depth | `MAX_STRUCTURAL_DEPTH = 3` (`src/task_names.py:93`) — phase → epic → task spends the whole budget | **zero** — `task_subtasks` is its own table, not `tasks` |

### 2.1 The subtask write fence, and exactly who it stops

`_task_findings_write_fence` (`src/commands/task_comment_commands.py:68-99`) returns early — no ownership check at all — when the caller is not a session or is elevated (`:74`). Otherwise it requires the caller's session to *own the named task*: right claim epoch, right session, right agent, session still live (`:79-92`). Consequences, stated plainly because they decide the design:

- **Loopback CLI and the elevated supervisor** may add subtasks to any task, any time.
- **A planner session** may add subtasks only to its own planning task — never to the tasks it plans.
- **A worker filing emergent work with a checklist** is refused: the new task is not the one it holds.

Seeding subtasks by looping `task_subtask_add` is therefore not a slower design for planner-shaped actors; it is a refused one. Subtask rows must be written by whoever writes the task row, inside the same transaction. That is the single strongest argument for putting the checklist in the creation payload.

### 2.2 What `phase_create` cannot do about its own windows

`_cmd_phase_create` is three separate transactions plus N more:

1. `_cmd_create_task` (`phase_commands.py:130`) — its own transaction. The phase exists as an ordinary task.
2. `mark_container` + `_upsert_meta(PHASE_KEY, …)` (`:169-174`) — a second transaction. **Between 1 and 2 the phase is an unflagged task**; only its `DEFINED` birth status (`:121`) keeps the 5-second promotion cascade off it.
3. One `add_dependency` per earlier open sibling (`:176-188`), each its own transaction and each able to fail independently. **Between 2 and the last of these, phase *N+1* exists ungated** — a cascade tick in that window can release it and start its children while phase *N* is still open. The command's own error path admits this: *"phase '<id>' was created but is not gated behind '<gate>'; add the edge with 'aq task deps'"* (`:184-187`).

A phased graph written by `write_plan` has none of these windows: rows, flags, metadata and gate edges commit together or not at all (`creator.py:6-13`).

---

## 3. Proposed design

### 3.0 Behaviour per integration mode

The column is `projects.hierarchical_integration_mode`, constrained to `disabled | observe | hierarchy | train | development` (`src/database/tables.py:85`). `HIERARCHY_MODES = ("hierarchy", "train")` (`src/database/queries/hierarchy_queries.py:162`). This table is load-bearing and was missing from rev. 1.

| Mode | Graph writer | What a phase container is | Phases |
|---|---|---|---|
| `None` / `disabled` | legacy (`_graph_route` returns `None` unless the mode is in `{"hierarchy","train"}` — `creator.py:426-429`) | a plain task row; no branch, no origin, no episode | **safe** |
| `observe` | legacy, same check | same | **safe** |
| `development` | legacy, same check | same. Delivery is flat and per completed task — `DevelopmentIntegration.sweep` replays completed task rows onto the default branch (`src/integration/development.py:413-430`); a container is never a delivery unit | **safe and useful — this is the operator's mode** |
| `hierarchy` | `HierarchyIntegration` via `_file_hierarchical_plan` (`creator.py:380`) | a real branch owner: children are based on the container's checkpoint and deliver to the container's branch | **refused** (§3.1) |
| `train` | same | same | **refused** (§3.1) |

**The one load-bearing detail in `development`.** A `blocks` edge is unsatisfied while the prerequisite is not COMPLETED *or* `_development_delivery_pending(prerequisite)` (`src/database/queries/blocked_state.py:169-179`). That predicate requires `task.branch_name IS NOT NULL` (`:147`). A phase container never gets a `branch_name`, so the delivery half is always false for an inter-phase edge and the gate releases on COMPLETED alone — which for a container means every child COMPLETED. **This is an untested escape today** and Slice 3 must pin it (§7.3, test 6). If a future change gave containers a `branch_name` in development mode, every inter-phase gate would silently stop releasing.

### 3.1 One phase policy, at both doors

Rev. 1 proposed refusing phases in hierarchy/train **on the graph path only**. That contradicts shipped behaviour: `phase_create` works in those modes today and `tests/test_phases.py::TestHierarchyModeCreation` (`:714-…`) pins it, filing through `file_root_on` / `file_prepared_child_on`.

**Ruling (provisional; open question Q1):** phases are refused in `HIERARCHY_MODES` at **both** doors, through **one** shared check and **one** refusal code, `hierarchy.phases_unsupported_mode`, worded after the existing `hierarchy.parent_key_unsupported_mode` (`work-graph.md:360`). Rationale: in those modes a phase container owns a branch and its children deliver to it, so phase *N+1* can open on a base that lacks phase *N*'s work, and one FAILED child strands a whole phase's delivery indefinitely — the identical hazard that already bars a standing parent there. `TestHierarchyModeCreation` is amended from "phases are created and gated under hierarchy filing" to "phase creation is refused in hierarchy and train mode". Cost if the ruling is wrong: hierarchy/train projects get no phases until phase-aware delivery is designed. They lose nothing they use today — no shipped prompt asks for a phase.

### 3.2 Tier 1 — text where text lands

**Honesty about reach (B4).** `vault.ensure_default_profiles` is write-if-absent and `aq agent profile-reseed --grants-only` merges *grant names* into `## Capabilities` only — it never touches `## Role` or `## Rules`. On this operator's box the vault profiles are hand-maintained (`harness: codex`), so **new profile prose reaches an existing install only by a full `profile-reseed` (which overwrites local edits) or by hand.** `src/skills/aq-tasks/SKILL.md` is different: it is read from the checkout and lands on restart with no vault step. Therefore:

- The **skill** carries as much of the policy as it can.
- The **profile** edits are shipped for fresh installs, and the operator step is explicit and listed in §7.1: hand-merge the new `## Rules` bullets into the vault's `supervisor` (and, once §7.4 lands, `planner`) profile, preserving `harness` and every other local edit, writing a `.bak-<epoch>` first.
- No `--prose-only` reseed mode is proposed. YAGNI for one hand-merge.

**(a) `src/profiles/defaults/supervisor/profile.md:170-173`, replace:**

> - **Create graphs, not loose tasks.** Any request that decomposes into more
>   than one task becomes a spec in `specs/` plus `aq task create --from-spec`
>   (or `--graph`). Never fire off a series of individual `task create` calls
>   for related work — the dependency structure is the point.

**with:**

> - **Create graphs, not loose tasks.** Any request that decomposes into more
>   than one task becomes a spec in `specs/` plus `aq task create --from-spec`
>   (or `--graph`). Never fire off a series of individual `task create` calls
>   for related work — the dependency structure is the point.
> - **Most epics have no phases.** Add one only when you can name what must
>   finish before the next stage may begin. A phase gates every task under it
>   at once and survives a task being added later, which `blocks` edges
>   between individual tasks do not — but work that could overlap is not a
>   stage, and a phase you cannot justify in one sentence is decoration. Stop
>   at five; more than that is a second epic.
> - **Most tasks have no subtasks.** Add them when the task is more than one
>   work session and a reader would otherwise have to read the agent's
>   transcript to know where it got to. A subtask is a checklist row inside
>   one task, worked in order by the one agent that holds it — never
>   scheduled, never its own agent, never its own branch. Use them to make
>   sequential work legible, never to create parallelism. Stop at fifteen; a
>   task wanting more than that is two tasks.

**(b) `src/skills/aq-tasks/SKILL.md:136-137`, replace:**

> Add your own with `subtask-add` when a task has several distinct steps worth
> tracking individually — it beats losing the breakdown in prose.

**with:**

> Most tasks have no subtasks. Add them with `subtask-add` when the task is
> more than one work session and a reader would otherwise have to read your
> transcript to know where you got to; stop at fifteen. If the task arrived
> with a checklist, work that one — do not re-decompose it — and add rows for
> steps you discover as you go, settling each as you finish it.
>
> A subtask is not a place to put work that belongs to someone else. Something
> outside this task's scope is still emergent work: file it as a task
> (`aq task create … --reason "…"`), not as a checklist row.

**(c) `src/skills/aq-tasks/SKILL.md`, `## Phases (planner)` (`:335-352`) — prepend:**

> Most projects and most epics have no phases. Create one only when you can
> name what must finish before the next stage may begin: phase *N+1* stays
> blocked until every task in phase *N* is COMPLETED. That is the whole
> semantic — a phase is a container with a `blocks` edge onto every earlier
> open sibling phase, and a blocked container withholds its children, so one
> edge gates a whole stage. Stop at five per level. If two groups could
> overlap, they are not phases; use `needs`/`blocks` edges between the
> individual tasks.

**(d) `src/skills/aq-tasks/SKILL.md:347`, correct the instruction that cannot work (B1):**

> File tasks into a phase the ordinary way, `aq task create --parent <phase-id>`.

**with:**

> File tasks into a phase with `aq task create --parent <phase-id>` — from the
> loopback CLI or a supervisor session. A **worker or planner session cannot**:
> a non-elevated filing's parent must be the task it holds, one of that task's
> descendants, or that task's own parent, and a root-level phase is none of
> those (`hierarchy.parent_out_of_scope`). From a session, create the phase's
> children through a graph (`--from-spec` / `--graph`) instead.

Tier 1 makes the supervisor — the only actor for which this already works (§1.5) — produce phases and subtasks in a `development`-mode project with zero code. It is not atomic (§2.2) and does not help the planner or spec-ingest.

### 3.3 Tier 2 — the grammar extension (recommended)

Let one `aq-graph` document declare the whole structure, so one validated, dry-runnable, atomic call creates it. Slices 2 and 3 of §7 build this.

#### Proposed block

```aq-graph
version: 1
spec: vault/projects/agent-queue/specs/messages-table.md
defaults: { profile: standard-high-claude }
parent:
  title: "Messages table + delivery engine"      # the epic
phases:
  - key: schema
    title: "Phase 1 — schema and queries"
    label: "schema"
  - key: engine
    title: "Phase 2 — delivery engine"
nodes:
  - key: tables
    phase: schema
    title: "Add the messages table and its migration"
    acceptance:
      - "alembic upgrade head is clean on a fresh database"
      - "tests/test_database.py green"
    context: [{ type: spec_ref, section: "3. Schema" }]
    subtasks:
      - "Add the Table() to src/database/tables.py"
      - title: "Write the guarded alembic revision"
        context: "Inspector guard, not autogenerate — see CLAUDE.md, migrations must be idempotent."
      - "Round-trip test on a fresh database"
  - key: queries
    phase: schema
    title: "Message queries module"
    needs: [{ on: tables, dep_type: blocks }]
  - key: cascade
    phase: engine
    title: "Delivery engine cascade step"
```

Creates: epic `<id>` (depth 1) → `<id>.1` "Phase 1", `<id>.2` "Phase 2" (depth 2) → `<id>.1.1` tables, `<id>.1.2` queries, `<id>.2.1` cascade (depth 3). `<id>.2` carries a `blocks` edge onto `<id>.1`. `<id>.1.1` gets three `task_subtasks` rows. All in `write_plan`'s single transaction. A document with neither `phases:` nor `subtasks:` behaves byte-identically to today.

#### Why the grammar, not step-by-step calls

1. **The fence forbids the alternative** for every planner-shaped actor (§2.1).
2. **Atomicity.** `write_plan` is one transaction by contract; `phase_create` + N × `create_task` + M × `task_subtask_add` has the windows catalogued in §2.2, including an ungated phase *N+1*.
3. **Reviewability.** `--dry-run` already reports every finding at once and the planner is already required to run it (`planner/profile.md:104-106`). A phase that only exists after the eleventh call cannot be dry-run.
4. **Zero contract churn** (§6), and formulas inherit it for free (§1.4).

### 3.4 Tier 3 — the proposal payload (deferred)

Give `task_batch_propose` the same vocabulary: an optional top-level `phases: [{key, title, label}]` and per-task `phase: <key>` / `subtasks: [...]`, validated in `_validate_shape` (`proposal_commands.py:45`) and materialised in both commit paths. Costs no contract change — `task_batch_commit`'s contracted args are `proposal_id`/`gate_id`/`project_id` and the payload is opaque JSON in `task_proposals.payload`.

**Out of the first slice.** `default-pipeline` is not activated on a fresh install (`src/playbooks/required.py:25-33`), so this buys structure on a path most installs never run. Revisit if the operator turns the pipeline on.

### 3.5 Where the policy lives

Mechanism in code, policy in prompts and playbooks. Concretely: "when is something a phase" and "how granular is a subtask" live in `supervisor/profile.md` Rules and `aq-tasks/SKILL.md` — never in the validator, which is why `phase_without_nodes` and `redundant_phase_edge` are *warnings*: the code states a fact, the prompt states the judgement. The code's only opinions are bounds (§7.2) and the mode refusal (§3.1), which is a correctness constraint, not taste.

---

## 4. Depth budget

`MAX_STRUCTURAL_DEPTH = 3` (`src/task_names.py:93`), root = 1, enforced at `hierarchy_queries.py:1030`/`:1178` and `task_commands.py:2951`. `MAX_NAMING_DEPTH = 3` (`task_names.py:89`).

| Shape | How | Structural depth | Naming depth of the leaves |
|---|---|---|---|
| `epic → phase → task` | Tier 2 `phases:` in one document | 3 — full | 3 — at cap |
| `phase → epic → task` | `phase_create` at root, `create_task` for the epic under it, then `create_task_graph --parent <epic-id>` | 3 — full | 3 — at cap |
| `phase → task` | `phase_create` at root, then `create_task_graph --parent <phase-id>` (today's `parent_id` path files nodes directly under the phase) | 2 | 2 |
| `epic → task` | today's plain graph | 2 | 2 |
| `task + subtasks` | any of the above | **+0** — `task_subtasks` is its own table | n/a |

**Illegal:** `phase → epic → phase → task` (4). Refused by `hierarchy.depth`, and by `graph.phases_need_root` before any row is written (§7.3).

**Consequence of being at the naming cap.** At depth 3 the leaves are `<root>.<i>.<j>`. `_validate_graph_parent` refuses a graph under such a task (`naming_depth(parent_id) >= MAX_NAMING_DEPTH`, `task_commands.py:2955-2963`), and a worker filing emergent work under one gets a **root** id plus a `discovered-from` edge rather than a dotted child (`task_names.py:85-89`) — structural depth would refuse a real child anyway. So a fully phased epic's leaves are terminal: nothing nests below them. That is the intended trade and it is exactly why subtasks are the fourth level.

**What a planner does when the plan wants more nesting**, in order: (1) push the extra level into `subtasks:` — free, and usually what the level actually was; (2) split into two sibling epics under the same phase with a `blocks` edge between them; (3) drop the outer phase, because one-stage work does not need one. Never file a fourth level and let `hierarchy.depth` refuse it.

---

## 5. Worker side

**Already built.** Prime renders `## Subtasks` from `build_task_subtasks_summary` (`src/prime/sections.py:179-208`): titles only, glyphs `[x]/[-]/[~]/[ ]`, plus *"Full context for one: `aq task subtask-show N`. Work them in order unless the task says otherwise."* When the held profile's policy lacks `task_subtask_update`, the "Report progress as you go" half is omitted (`allow_updates=False`, gated on `profile_allows_command` / `SUBTASK_UPDATE_COMMAND`, `sections.py:572-575`).

**When a worker adds its own** — §3.2(b). Work the checklist you were given; add rows for what you discover; file anything outside scope as a task, not a row.

**Close refusal.** `task_close --outcome pass` is refused `subtasks.open` while any row is `pending`/`in_progress`; `--skip-open-subtasks` flips the remainder to `skipped`; a failing close is never gated. A seeded checklist therefore becomes a close-time question — the point — and never an unclosable task.

**Existing installs without the grant.** A vault upgraded from before the grants existed has worker profiles with no `task_subtask_update`. Such a worker sees the checklist, is not told to report progress, and can still close with `--skip-open-subtasks`. Repair is the documented one (`docs/guides/worker-pools.md` §7b): `aq doctor --check profiles.system_drift`, then `aq agent profile-reseed --profile-id <id> --grants-only`. Slice 2 adds one thing: a **warning** at `--dry-run` time (`subtasks_unreportable`) when a node carries `subtasks` and its resolved profile cannot update them, naming the reseed command — so the planner learns before creating the graph. A warning, not an error: the checklist is still worth writing, and prime still shows it.

---

## 6. Reviewed-bundle impact

A bundle's `contract_fingerprint` is taken over `compiled_against.commands` **only** — `src/playbooks/definition.py:742-748` says so: *"Profiles are excluded."* The bundles and where they live:

| bundle | location | compiled against |
|---|---|---|
| `blocked-task-escalation` | `src/prompts/reviewed_playbooks/` **and** `tests/fixtures/playbooks/v2/` | `task_recovery_notify` |
| `default-assignment-routing` | `src/prompts/reviewed_playbooks/` **and** `tests/fixtures/` | `task_route`, `task_route_options` |
| `provider-usage-probe` | `src/prompts/reviewed_playbooks/` **and** `tests/fixtures/` | `provider_usage_probe` |
| `ci-main-sentinel` | `tests/fixtures/` only (import per install) | `ci_baseline_status`, `ensure_task`, `escalation_create` |
| `default-pipeline` | `tests/fixtures/` + `docs/playbooks/integration-only/` (import per install) | `ensure_task`, `gate_create`, `task_batch_commit` |

- **Slice 1 (profile + skill text): no rebuild.** A profile edit cannot move a contract fingerprint, and `profiles_referenced` lists ids, which do not change. The guard that *does* bite is `tests/test_shipped_profile_capabilities.py` — its `EXPECTED_UNREACHABLE` literal (`:50-61`) fails if a profile gains a name its own token cannot dispatch. Run it after any profile edit. (Slice 1 adds no capability names, so it should pass unchanged.)
- **Slices 2 and 3 (grammar): no rebuild.** `create_task_graph` has no contract registration and no playbook step calls it.
- **Tier 3, if ever built: no rebuild.** `task_batch_commit`'s contracted args are unchanged; the payload is opaque JSON.
- **What would force one:** adding a field to `CreateTaskArgs` or `EnsureTaskArgs` (e.g. a `phase_key` convenience for playbooks). `CommandArgs` is `extra="forbid"`, so that is the only way a playbook could ever pass phase intent — and it would move `ensure_task`'s fingerprint and require rebuilding **`default-pipeline` and `ci-main-sentinel`** with `scripts/rebuild-reviewed-playbook-artifacts.py`, hand-copying into the other trees and updating each `manifest.md`'s digests. **This design avoids it.** If a playbook must create phased work later, contract `phase_create` as its own command instead of widening `create_task`.

---

## 7. Task breakdown — the first slice

Three tasks, in order. Each is self-contained: an implementer who sees only that task has the files, names and tests it needs. §7.4 and §3.4 are **deferred and not part of this slice**.

### 7.1 Slice 1 — text where text lands

**Files (edit only; no new files):**
- `src/profiles/defaults/supervisor/profile.md` — replace the bullet at `:170-173` with the three bullets in §3.2(a).
- `src/skills/aq-tasks/SKILL.md` — replace `:136-137` per §3.2(b); prepend the paragraph in §3.2(c) to `## Phases (planner)` at `:335`; replace the sentence at `:347` per §3.2(d).

**No capability lists change. No code changes. No bundle rebuild (§6).**

**Tests:** `aq test tests/test_shipped_profile_capabilities.py tests/test_default_profiles.py` — both must pass unchanged. There is no test asserting prose; the guard is that the capability invariants still hold.

**Operator step (must be listed in the commit message and the task report, B4):** the new `## Rules` bullets do **not** reach this install automatically. After merge, hand-merge them into `~/.agent-queue/vault/agent-types/supervisor/profile.md`, preserving `harness` and every other local edit, writing a `.bak-<epoch>` first. The skill text lands from the checkout on the next daemon restart and needs no vault step.

**Why this is first:** the supervisor is elevated and the operator's projects run `development` mode, so `phase_create` + `create_task_graph --parent <phase-id>` + `task_subtask_add` all already work for it (§1.5, §3.0). This slice is the only one that changes behaviour with no code.

### 7.2 Slice 2 — `subtasks:` in the `aq-graph` grammar

**Grammar key:** `subtasks:` on a node — a list whose entries are either a plain string (the title) or `{title: str, context?: str}`. Not valid anywhere else in the document.

**Bounds — import both, enforce one.** `MAX_SUBTASKS_PER_CALL = 50` (`src/commands/task_subtask_commands.py:21`, the per-authoring-act cap) and `MAX_SUBTASKS_PER_TASK = 200` (`src/database/queries/task_subtask_queries.py:15`, the durable per-task ceiling), plus `MAX_SUBTASK_TITLE = 300` / `MAX_SUBTASK_CONTEXT = 16000` (`task_subtask_commands.py:23-24`). **The parser enforces `MAX_SUBTASKS_PER_CALL` per node**: a graph node's list is one authoring act, and the 200 ceiling is unreachable from a graph alone (the task is brand new, base ordinal 0) — it remains what `add_task_subtasks` raises if a later `subtask-add` pushes an already-seeded task past it. Import both constants; do not restate either number.

**Files and interfaces:**

- `src/task_graph/models.py`
  - `@dataclass GraphSubtask: title: str; context: str = ""` with `to_dict() -> dict`.
  - `GraphNode` (`:103`) gains `subtasks: list[GraphSubtask] = field(default_factory=list)`; add `"subtasks": [s.to_dict() for s in self.subtasks]` to `to_dict()` (`:122`).
- `src/task_graph/parser.py`
  - `_parse_subtask(raw, node_key) -> tuple[GraphSubtask | None, list[GraphError]]` beside `_parse_context` (`:95`); finding rule `bad_subtask` for: a non-string/non-object entry, a missing or non-string `title`, a title outside 1–300 characters, a non-string or over-16000-character `context`.
  - In `_parse_node` (`:115`), after the `context` block (`:203-215`): read `raw.get("subtasks", defaults.get("subtasks"))`, coerce a bare string/dict to a one-element list, emit `bad_subtask` when the value is not a list, and emit `bad_subtask` with detail `"node has N subtasks; at most {MAX_SUBTASKS_PER_CALL} may be declared on one node"` when the list is longer.
- `src/task_graph/validator.py`
  - `subtasks_unreportable`, severity **`warning`** (not an error): emitted per node that carries `subtasks` and whose resolved profile's policy lacks `task_subtask_update`. Resolve the profile the same way `_check_profiles` (`:410`) does; test the grant with `profile_allows_command` (`src/prime/sections.py:575`). Detail must contain `aq agent profile-reseed --profile-id <id> --grants-only`.
- `src/database/queries/task_subtask_queries.py`
  - `add_task_subtasks(self, task_id, project_id, items, *, conn=None)` — split the body the way the other mixins do: keep the current logic in a private `_add_task_subtasks_on(conn, …)` and have the public method open `self._engine.begin()` only when `conn is None`. Same return value and same `ValueError("subtask_limit")`.
- `src/task_graph/creator.py`
  - `build_plan` (`:205`) appends to a new `GraphPlan.subtask_rows: list[dict]` — `{"_key": node.key, "title": …, "context": …}`, document order preserved. `_rewrite_ids` (`:345`) must resolve `_key` for these rows exactly as it does for `context_rows`/`criteria_rows` (`:358-360`), or a provisional plan writes the checklist to the wrong id.
  - `write_plan` (`:435`) — after `criteria_rows` (`:544`) and before the label insert: group `subtask_rows` by resolved `task_id` and call `db.add_task_subtasks(task_id, plan.project_id, items, conn=conn)` once per task.
  - `build_report` (`:585`) — each node dict gains `"subtasks": <count>`.

**Tests:**
1. `tests/test_task_graph.py` — a node with three string subtasks parses to three `GraphSubtask`s; a mixed string/object list parses; a 301-character title, a 16001-character context, a non-list value and 51 entries each produce exactly one `bad_subtask`; a document with no `subtasks` key round-trips byte-identically to today.
2. `tests/test_task_subtasks.py` — `add_task_subtasks(..., conn=conn)` participates in the caller's transaction: roll the caller's transaction back and assert zero rows; called with `conn=None` it still commits on its own.
3. `tests/test_create_task_graph_command.py` — after a real create, the node's three rows exist ordinal-ordered with the right titles and contexts; `--dry-run` reports `subtasks: 3` and writes **zero** rows; patching `_insert_task` to fail after the first node (the existing single-transaction pattern at `creator.py:336`) leaves zero subtask rows; a graph created under an existing container (provisional ids) writes the checklist to the reserved id, not the `.?` placeholder.
4. `tests/test_create_task_graph_command.py` — `subtasks_unreportable` is severity `warning`, the graph is still created, and the detail names `profile-reseed`.
5. Prime renders the `## Subtasks` block for a task created this way (assert via `build_task_subtasks_summary` or the prime renderer test already covering that section).

### 7.3 Slice 3 — `phases:` in the `aq-graph` grammar

**Grammar keys:** top-level `phases: [{key: str, title: str, label?: str}]` (document order is phase order, 1..N) and per-node `phase: <phase key>`. A node without `phase` stays a direct child of the epic.

**Refusal codes:** `hierarchy.phases_unsupported_mode` (shared, §3.1) and `graph.phases_need_root` (a document declaring `phases` was given a `parent_id`).

**Files and interfaces:**

- `src/database/queries/hierarchy_queries.py` — the **one** shared check, beside `HIERARCHY_MODES` (`:162`):
  ```python
  PHASES_UNSUPPORTED_MODE_CODE = "hierarchy.phases_unsupported_mode"

  async def phase_mode_refusal(self, project_id: str, *, conn=None) -> dict | None:
      """``{"success": False, "code", "error"}`` when *project_id*'s
      ``hierarchical_integration_mode`` is in ``HIERARCHY_MODES``, else ``None``.

      A phase container there owns a branch and its children deliver to it, so
      phase N+1 can open on a base without phase N's work and one FAILED child
      strands the whole phase — the hazard ``parent_key_unsupported_mode``
      already bars for standing parents.
      """
  ```
  A method on `HierarchyQueryMixin` so both callers reach it: `phase_commands.py` as `self.db.phase_mode_refusal(project_id)`, and `creator.write_plan` as `db.phase_mode_refusal(plan.project_id, conn=conn)`.
- `src/commands/phase_commands.py` — call it in `_phase_scope` (`:56`), after the project lookup and **before** `_cmd_create_task`, returning the refusal as the third element.
- `src/task_graph/models.py` — `@dataclass GraphPhase: key: str; title: str = ""; label: str | None = None` with `to_dict()`; `GraphNode` gains `phase: str | None = None`; `TaskGraph` (`:159`) gains `phases: list[GraphPhase] = field(default_factory=list)`; both added to the respective `to_dict()`s.
- `src/task_graph/parser.py` — `_parse_phase(raw, index)` beside `_parse_parent` (`:220`): rules `bad_phase` (not an object) and `missing_phase_key`. `_parse_node` reads `phase` (rule `bad_field_type` when not a string). `parse_graph` (`:294`) reads `data.get("phases")` into `TaskGraph.phases`, preserving order; a non-list value is `bad_phase`.
- `src/task_graph/validator.py` — new findings beside `_check_keys` (`:260`):
  - `duplicate_phase_key` — error;
  - `unknown_phase` — error, a node names a key not declared in `phases`;
  - `phase_without_nodes` — **warning**; the phase is still created and is held open by `childless_held_open_container()` until deleted;
  - `redundant_phase_edge` — **warning**, a `blocks` need whose target node is in an earlier phase (the phase gate already covers it).
- `src/commands/task_commands.py` — in `_cmd_create_task_graph` (`:2966`), after parsing and before validation: if `graph.phases` and `args.get("parent_id")` → `{"success": False, "code": "graph.phases_need_root", "error": "a graph that declares phases must be created at the project root; the phases are its second level"}`. (`_validate_graph_parent`'s `depth + 1 > MAX_STRUCTURAL_DEPTH` check at `:2951` is written for a one-level graph; forbidding the combination is smaller and clearer than generalising it.)
- `src/task_graph/creator.py`
  - `assign_child_ids` (`:64`) becomes two-level. Phase *i* (1-based, document order) → `<epic>.<i>`; a node in phase *i*, *j*-th within that phase → `<epic>.<i>.<j>`; an unphased node → `<epic>.<k>`, numbered after the phases. Keep the provisional (`.?`) behaviour for the no-phases case untouched; a phased graph is root-only so it is never provisional.
  - `GraphPlan` gains `phase_rows: list[dict]` and a `phase_ids` property. The container row's `next_child_ordinal` (`:247`) becomes `len(phases) + len(unphased nodes) + 1`.
  - `build_plan` emits `phase_rows` — task rows with `status="DEFINED"`, `task_type="plan"`, `title` from the phase, `description` = label or title, mirroring `phase_commands.py:114-124` — and the inter-phase dependency rows: for phase *i*, one `blocks` row onto **every** earlier phase, not only *i−1*, matching `phase_commands.py:163-166` and for the same reason (deleting an abandoned middle phase must not release its successor).
  - `write_plan` (`:435`) — **ordering is load-bearing.** `set_parent_bulk` (`src/database/queries/hierarchy_queries.py:1090-1106`) asserts *freshly inserted childless leaves with no blocking out-edges*:
    1. insert the epic row (existing `:484-498`);
    2. insert the phase rows;
    3. `set_parent_bulk(plan.phase_ids, plan.parent_id, conn=conn)` — phases are still childless with no out-edges;
    4. insert the node rows (existing loop `:507-519`) — **node rows must be inserted before step 5**, as they are today;
    5. per phase, `set_parent_bulk(node_ids_of_that_phase, phase_id, conn=conn)`; unphased nodes keep today's single call onto the epic;
    6. **for every phase, including one with zero nodes**, `db.mark_container(phase_id, conn=conn)` and `db._upsert_meta(phase_id, PHASE_KEY, {"order": i, "label": …}, conn=conn)`. `set_parent_bulk` already flags a phase that got children; the explicit `mark_container` is what stops an empty phase being a claimable task, and the loop must therefore not be inside the per-phase-children branch. Both writes are idempotent;
    7. insert the dependency rows (existing `:529-540`) — node `needs` **and** the inter-phase `blocks` edges. Writing an inter-phase edge before step 3 would trip `cycle_check_skipped`;
    8. widen the final projection: `recompute_blocked(set(plan.task_ids) | set(plan.phase_ids), conn=conn)` (`:582`). `GraphPlan.task_ids` (`:95`) is `[row["id"] for row in self.node_rows]` and is also the report's `task_ids` (`:602`); the phase ids must reach `recompute_blocked` or the inter-phase gate is never projected and phase 2 is claimable. Widening the `recompute_blocked` argument rather than `task_ids` itself keeps the report's meaning ("the tasks this graph created") honest — the review's requirement is that `recompute_blocked` covers the phases, and this satisfies it.
  - `write_plan` also calls `db.phase_mode_refusal(plan.project_id, conn=conn)` first when `plan.phase_rows` is non-empty and raises on a refusal, so the creator cannot be bypassed by a caller that skipped the command layer.
  - `build_report` (`:585`) gains `"phases": [{"key", "task_id", "order", "title"}]`.
  - `_file_hierarchical_plan` (`:380`) is **not** extended: phases are refused in the only modes that reach it (§3.1).
- `tests/test_phases.py::TestHierarchyModeCreation` (`:714`) — amend. Replace `test_phases_are_created_and_gated_under_hierarchy_filing` with a test asserting `phase_create` in `hierarchy` mode and in `train` mode returns `success=False`, `code == "hierarchy.phases_unsupported_mode"`, and creates no task. Keep the `_enable` helper.

**Tests:**
1. `tests/test_task_graph.py` — `phases` parses in order; a duplicate key, an unknown `phase` on a node, a non-list `phases` and a phase without a `key` each produce exactly one finding of the right rule; a document without `phases` round-trips byte-identically.
2. `tests/test_create_task_graph_command.py` — ids are `<epic>.<i>.<j>` for phased nodes and `<epic>.<k>` for unphased ones; each phase carries `task_metadata.phase = {"order", "label"}` and is flagged a container; in a **three**-phase graph, phase 3 has `blocks` edges onto phase 1 **and** phase 2.
3. `tests/test_create_task_graph_command.py` — a phase declared with zero nodes is still flagged a container and still carries its metadata (the step-6 loop); it is not claimable.
4. `tests/test_create_task_graph_command.py` — atomicity: patching `_insert_task` to fail on the second node leaves zero tasks, zero phases, zero edges, zero subtask rows.
5. `tests/test_create_task_graph_command.py` — `phases` plus `parent_id` → `graph.phases_need_root`, nothing created; `--dry-run` reports `phases` and creates nothing.
6. **`tests/test_phases.py` — the development-mode gate (B3, currently untested anywhere).** In a project with `hierarchical_integration_mode="development"`, create a two-phase graph; assert phase 2 `is_blocked` while phase 1 has an open child; COMPLETE every child of phase 1; assert phase 1 settles COMPLETED and phase 2 becomes unblocked and its children claimable. This pins the escape at `blocked_state.py:147`: `_development_delivery_pending` is false for the phase container only because a container has no `branch_name`. Add a comment in the test saying so, naming the line.
7. `tests/test_phases.py` — the amended `TestHierarchyModeCreation` (above), plus: `create_task_graph` with `phases` in a `hierarchy`-mode project returns `hierarchy.phases_unsupported_mode` and creates nothing — one code, both doors.
8. `tests/test_create_task_graph_command.py` — a phased graph in `disabled`/`None` mode and in `observe` mode is created normally (the table in §3.0 is real).

### 7.4 Deferred — repair the planner agent type (its own task, not this slice)

Recorded here so it is not lost. `create_task_graph` may only be exposed to session tokens **after** `_cmd_create_task_graph` learns the session principal. Sketch, not a commitment:

- **Already true, do not re-implement:** `check_command_scope` pins/injects `project_id` for a session token (`scope.py:207-221`), so cross-project creation is already impossible once the name is in `AGENT_COMMAND_SET`; and graph nodes are already born `DEFINED` (`creator.py:56`) while the container is a container and therefore unclaimable. Rev. 1's framing of "forced DEFINED" as missing was wrong.
- **Genuinely missing:** (1) no filing quota — `reserve_filing(conn, held_id, max_filings=config.swarm.max_filings_per_task)` (`task_commands.py:1529-1531`) is never consulted, so a granted session could mint unbounded tasks; (2) no containment — a session-created graph should be filed under the task the session holds (or a descendant), reusing `_PARENT_SCOPE_ERROR`/`hierarchy.parent_out_of_scope` (`:333-337`), never at the project root; (3) no provenance — a session-created graph should carry the `discovered-from` edge a worker filing gets.
- Only once those three exist does `create_task_graph` join `AGENT_COMMAND_SET`, and only then does `planner/profile.md` gain it in `## Capabilities.aq_commands` and gain the §3.2-style Rules. `tests/test_shipped_profile_capabilities.py`'s `EXPECTED_UNREACHABLE["planner"]` must stay `set()`.
- Until then, §3.2(d)'s skill correction is what stops an agent following an instruction that cannot work.

---

## 8. Acceptance test

**Claim:** a realistic spec creates phases, epics, tasks and subtasks in one atomic call, and the phase gate actually holds in the mode the operator runs.

**Can `scripts/e2e-smoke.sh` carry it? The whole of Slices 2 and 3 — yes.** The kit runs a real daemon on real PostgreSQL through the real CLI with `sessions.provider: fake` and no LLM (`scripts/e2e-env.sh:348`, `:452-457`). Everything in Tier 2 is CLI-and-daemon only.

New scenario `phased-graph`, **run in a `development`-mode project** (S8 — that is the operator's mode and the one with the `_development_delivery_pending` escape):

1. Write a fixture spec with a `parent:`, two `phases:`, three nodes, one node carrying three `subtasks:`.
2. `aq task create --from-spec <path> --dry-run` → exit 0; report lists two phases, three nodes, three subtasks, zero errors; **assert nothing was created**.
3. Drop `--dry-run`; capture the epic id.
4. `aq task phase-list --project-id $PID --parent-id <epic>` → two rows, `order` 1 and 2, phase 2 `is_blocked: true`.
5. `aq task get-tree --task-id <epic>` → three levels, ids `<epic>.<i>.<j>`.
6. `aq task subtasks <epic>.1.1` → three rows, all `pending`.
7. `aq task claim --next` twice; close both phase-1 tasks (`--skip-open-subtasks` on the one with the checklist). Assert phase 1 settles COMPLETED **and** phase 2's task becomes claimable — the live proof of the `branch_name IS NULL` escape under a real development-mode project.
8. `aq task delete --task-id <epic> --cascade` so the scenario is re-runnable.

**What the kit cannot carry:** the spec-ingest leg — a fake session never reads a profile's Role, so no agent authors a proposal. That leg is Tier 3 and deferred anyway; if built, cover it with a non-LLM pytest test in `tests/test_task_proposals.py` replaying a recorded payload through `spec.approved` → `ensure_task` → `task_batch_propose` → `gate_create` → `gate_resolve approve` → `task_batch_commit`. What no machine test can prove is that a real planner *chooses* to emit phases — that is what §3.2's wording is for, and it belongs in `tests/llm/` if anywhere.

---

## 9. Open questions for the operator

1. **Phases in `hierarchy`/`train`.** §3.1's ruling is provisional: refuse at both doors with one code, and amend `TestHierarchyModeCreation` (which currently pins the opposite). Reason: there a phase container owns a branch and its children deliver to it, so phase *N+1* can open on a base lacking phase *N*'s work and one FAILED child strands the stage — the hazard `parent_key_unsupported_mode` already bars. **Your projects run `development`, where phases are safe and useful and none of this applies** (§3.0). Confirm the refusal, or ask for phase-aware delivery to be designed instead. Cost of confirming: hierarchy/train projects get no phases for now, and lose nothing they use today.
2. **A FAILED task in phase *N* holds phase *N+1* forever**, because a phase settles only when every child is COMPLETED. Is that the wanted behaviour for a failure, or should there be an explicit release (`aq task phase-release`, or closing the failed child with `--abandon-children`)?
3. **The prose wording.** §3.2 uses "most epics have no phases / most tasks have no subtasks", ceilings only (five phases, fifteen subtasks), no floors. Confirm before it becomes the fleet's habit.
4. **The profile hand-merge.** Slice 1's `supervisor` Rules reach this box only by your hand-merge (§7.1). Confirm you want that rather than a full `profile-reseed` (which would overwrite `harness: codex` and every other local edit).
5. **Root-level phases still need three calls** (`phase_create`, `create_task` for the epic, `create_task_graph --parent <epic>`). Tier 2 gives `epic → phase → task` in one call. Worth a follow-on letting `create_task_graph --parent <phase-id>` mint its own epic container under the phase, or is the supervisor's three-call sequence fine?
6. **`subtasks_unreportable`: warning or error?** Proposed as a warning so the checklist is still written for a profile that cannot tick it (prime shows it either way). Should a planner instead be blocked until you reseed the grants?
7. **The planner repair (§7.4) — priority?** The shipped planner agent type is non-functional today (§1.2). Nothing depends on it, since the supervisor does the planning, so it is deferred. Say if it should be pulled forward.
8. **Tier 3 (proposal payload) — wanted at all?** `default-pipeline` is not activated on a fresh install, so the spec-ingest path is opt-in. If you do not run it, Tier 3 is dead weight.
