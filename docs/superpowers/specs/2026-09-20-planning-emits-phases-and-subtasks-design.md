# Planning Emits Phases and Subtasks — Design

**Date:** 2026-09-20 · **Branch:** `feat/graph-visibility` · **Status:** design, no code

**Problem.** The `feat/graph-visibility` branch built the mechanisms — `phase_create`/`phase_list` (`src/commands/phase_commands.py`) and in-task subtasks (`src/commands/task_subtask_commands.py`, table `task_subtasks`) — and nothing produces either. No shipped prompt mentions a phase. No creation path can express one. Until planning emits the structure, a new epic looks exactly like an old one: a flat container with N children.

**Operator's target shape.** Phases (ordered, gating) around and inside epics; epics with children; children with "possibly a ton of little subtasks that might not even be handed to separate agents… tackled in some order by a single agent, quickly, but used to track progress within a larger body of work." Sequential execution by one agent is often faster than a handoff — *the only thing wanted from a subtask is visibility.*

---

## 1. How planning works today, from the code

There are five paths from intent to tasks. Two of them actually create structure; three cannot.

### 1.1 `create_task_graph` / `aq-graph` — the real graph path

`aq task create --from-spec <path>` and `--graph <file>` (`src/cli/tasks.py:125-160`, `:356`) call `create_task_graph` (`src/commands/task_commands.py:2966`). A fenced ` ```aq-graph ` block is extracted (`src/task_graph/parser.py:381`), parsed structurally (`:294`), validated semantically (`src/task_graph/validator.py:552`), planned (`src/task_graph/creator.py:205`) and written in **exactly one transaction** (`:435`) — whole or not at all.

What it can express today:

| | |
|---|---|
| **Container** | one `parent:` block → one task row (`creator.py:230-250`), `status=IN_PROGRESS`, flagged a container |
| **Nodes** | `nodes:` → task rows, `status=DEFINED`, ids `<container>.1..N` (`assign_child_ids`, `creator.py:64`) |
| **Edges** | per-node `needs:` with `dep_type` (`models.py:61`) |
| **Per node** | `title`, `description`, `acceptance`, `deliverables`, `context`, `labels`, `priority`, `profile`, `intelligence_class`, `task_type` (`models.py:103-136`) |
| **Nesting** | **exactly two levels.** `GraphNode` (`models.py:103`) has no `children` and **no metadata map**. Every node is linked to the one container by a single `set_parent_bulk` call (`creator.py:525`). |
| **Under an existing parent** | `parent_id` → *no container row is planned at all* (`creator.py:229`, `parent_row` stays `None`); the nodes hang directly off the given parent |

So a graph is `container → node`, or `existing-parent → node`. It cannot produce three levels, and it cannot stamp `task_metadata` on anything except formula provenance on the container (`creator.py:560-581`).

**Who can call it.** Nobody but the supervisor and the loopback CLI. `create_task_graph` is **not** in `AGENT_COMMAND_SET` (`src/api/scope.py:13-76`), so a session token is refused with `out of scope: create_task_graph` (`scope.py:206`). The only elevated session minted anywhere is the supervisor (`src/messages/session_lens.py:356-368`, `elevated=True`); task sessions (`src/orchestrator/execution.py:819`) and pool sessions (`src/orchestrator/pools.py:895`) mint non-elevated tokens, as does the manual terminal (`src/agents/terminals.py:195-200`).

### 1.2 The planner profile — instructs a command it cannot run

`src/profiles/defaults/planner/profile.md:25-31` tells the planner to run `aq task create --from-spec <path> --dry-run` and then without it. Its `## Capabilities.aq_commands` (`:63-88`) grants `create_task`, `phase_create`, `phase_list`, `task_subtask_*` — **but not `create_task_graph`**, and even if it did, §1.1's scope gate answers first (the planner is `lifecycle: task`, `profile.md:40`, so its token is non-elevated).

This is a live defect, not a subtlety: **the planner profile's step 4 is unexecutable.** `tests/test_shipped_profile_capabilities.py:50` records `"planner": set()` for unreachable names only because the profile does not *declare* the command its Role prose calls.

Its `## Rules` (`:96-115`) say nothing about phases and nothing about subtasks.

### 1.3 Spec ingest → proposal → human gate → batch commit

`spec_approve` (`src/commands/spec_commands.py:30`) flips a vault spec's frontmatter and emits `spec.approved` (`:83`). The shipped `default-pipeline` (`src/prompts/default_playbooks/default-pipeline.md:27-41`) `ensure_task`s a `spec-ingest` task; that agent (`src/profiles/defaults/spec-ingest/profile.md:16-28`) reads the spec and calls `task_batch_propose`; `proposal.ready` opens a human gate (`:45-51`); an approving `gate.resolved` calls `task_batch_commit` (`:53-69`).

What a proposal can express (`src/commands/proposal_commands.py:45-77`): `tempId`, `title`, `description` (required); `priority`, `deliverables`, `profile_id`, `intelligence_class` are read at commit (`:643-654`, `:657-681`); edges of any `TASK_DEP_TYPES` including `parent-child`. In a hierarchy/train project `parent-child` really nests (`_commit_hierarchical_proposal:506-605`); in a legacy project it is passed to `add_dependency` like any other edge (`:445-455`).

No phase. No subtasks. No metadata — `_create_one_task` (`:674`) passes `"metadata": {"proposal_source": source}` to `create_task`, and **`create_task` has no `metadata` argument at all**, so that key is silently dropped on the legacy path (pre-existing, out of scope here; the hierarchical path writes it explicitly at `:574`).

**This path is opt-in.** `default-pipeline` is not in `REQUIRED_SYSTEM_PLAYBOOK_IDS` or `DEFAULT_SYSTEM_PLAYBOOK_IDS` (`src/playbooks/required.py:25-33`) and `src/prompts/reviewed_playbooks/` ships only `blocked-task-escalation`, `default-assignment-routing`, `provider-usage-probe`. A fresh install does not run it.

### 1.4 Formulas

`src/task_graph/formulas.py` resolves a vault template's `extends` chain and vars into one `aq-graph` document, cooked by `formula_cook` (`src/commands/formula_commands.py:262`, elevated-only). It inherits §1.1's grammar exactly, so it inherits its limits exactly — as `docs/specs/design/work-graph.md:340` already states: *"formulas cannot declare phases — an `aq-graph` node carries no metadata or nesting."*

### 1.5 Supervisor-created work and worker filing

The supervisor (`src/profiles/defaults/supervisor/profile.md`) is elevated, holds `phase_create`/`phase_list` (`:94-95`) and `task_subtask_*` (`:105-108`), and is told *"Create graphs, not loose tasks"* (`:170-173`) — but nothing tells it to create a phase, and its graphs are still §1.1 two-level graphs.

Worker filing (`emergent_work.md`, rendered when the profile grants `create_task` — `src/prime/sections.py:610-612`) files one task per confirmed finding, child-of-held-task by default. It is deliberately single-task and must stay that way.

---

## 2. The gap, per path

| Path | What blocks phases | What blocks subtasks |
|---|---|---|
| `aq-graph` / `create_task_graph` | grammar has no phase concept and exactly two levels (`models.py:103`, `creator.py:64`); no metadata map on any node | no `subtasks` field anywhere in `GraphNode` |
| planner session | cannot reach `create_task_graph` at all (`scope.py:206`); *can* call `phase_create` (it is in `AGENT_COMMAND_SET`, `scope.py:63`) but then has no way to file a graph into the phase | **`_task_findings_write_fence` (`src/commands/task_comment_commands.py:68-99`) refuses a non-elevated session writing subtasks on a task it does not hold.** A planner cannot seed a checklist on the tasks it just created — at all, by design |
| spec-ingest → proposal | `_validate_shape` (`proposal_commands.py:45`) has no phase field; commit creates no metadata | no subtask field in the payload; the spec-ingest session is also fenced out of `task_subtask_add` on tasks it does not hold |
| formulas | inherits the `aq-graph` limits | same |
| supervisor | none technically (elevated, holds both commands) — **prompt silence** is the whole gap | none technically — prompt silence |
| a playbook | `phase_create` and `task_subtask_add` are **not in the contract registry** (`src/commands/contracts/builtin.py:682-1060` — the contracted set is `list_projects, get_task, render_prompt, read_project_memory_file, count_project_memory_files, git_diff, memory_save, memory_search, create_task, ensure_task, edit_task, add_dependency, gate_create, gate_resolve, list_tasks, get_downstream_tasks, task_batch_commit, stop_task, ci_baseline_status, provider_usage_probe, message_send, task_recovery_notify, task_route_options, task_route`) | same; and `CreateTaskArgs`/`CommandArgs` are `extra="forbid"` (`src/commands/contracts/models.py:22`), so a playbook cannot smuggle `metadata` through `create_task` either |
| depth | `MAX_STRUCTURAL_DEPTH = 3` (`src/task_names.py:93`) — phase → epic → task already spends the whole budget | subtasks cost **zero** depth: `task_subtasks` is its own table, not `tasks` |

**The fence in row 2 is the decisive fact of this design.** Seeding subtasks by having the planner call `task_subtask_add` N times is not merely slow and non-atomic; it is refused. Only a caller that is local, elevated, or *holds the task* may write a subtask row. That rules out step-by-step seeding for every planner-shaped actor and points at writing the rows inside the creation transaction instead.

---

## 3. Proposed design

Smallest-first, in three tiers. Tier 1 alone changes what the supervisor produces today. Tier 2 is the mechanism everything else needs. Tier 3 is optional.

### Tier 1 — pure text (no code)

**(a) `src/profiles/defaults/planner/profile.md:20-24`, replace:**

> 2. **Write the spec.** Author a markdown spec in
>    `vault/projects/<pid>/specs/<slug>.md` that names the problem, the
>    decisions, the acceptance criteria, and the graph. Include a fenced
>    `aq-graph` block with hierarchical task ids, `needs` edges, `spec_ref`
>    contexts, and per-node acceptance criteria.

**with:**

> 2. **Write the spec.** Author a markdown spec in
>    `vault/projects/<pid>/specs/<slug>.md` that names the problem, the
>    decisions, the acceptance criteria, and the graph. Include a fenced
>    `aq-graph` block with `needs` edges, `spec_ref` contexts, and per-node
>    acceptance criteria. Give the graph the shape the work actually has:
>    - A `parent:` block — the **epic**. One graph is one epic.
>    - A `phases:` list **only** when there is a real ordering: nothing in
>      phase *N+1* may start until everything in phase *N* is done. A phase
>      gates; it is not a label. Two to five per epic. Work that can run
>      concurrently does not get a phase — `needs` edges say that better and
>      cost no depth.
>    - `subtasks:` on any node whose work is more than one sitting. A subtask
>      is a checklist row **inside** one task, worked in order by the one
>      agent that holds it. It is never scheduled, never gets its own agent,
>      never gets its own branch. Three to fifteen per node; a node that
>      wants more than fifteen is two nodes.

**(b) `planner/profile.md` `## Rules`, add:**

> - **Phases gate; subtasks only show.** Split work into a separate task
>   only when it needs a different agent, a different branch, or separate
>   scheduling. Everything else worth seeing is a subtask on the task that
>   does it — one agent doing five steps in order is usually faster than
>   five handoffs, and the only thing that costs you is visibility, which is
>   exactly what a subtask buys back. Never create a task merely to make
>   progress legible.
> - **Three levels, no more.** Structural depth is capped at 3. `epic →
>   phase → task` and `phase → epic → task` are both legal and both spend
>   the whole budget; you cannot have phases on both sides of an epic. When
>   a plan wants a fourth level, push it into `subtasks:` (free) or into a
>   second epic under the same phase with `needs` edges between them.

**(c) `src/profiles/defaults/supervisor/profile.md:170-173`, replace:**

> - **Create graphs, not loose tasks.** Any request that decomposes into more
>   than one task becomes a spec in `specs/` plus `aq task create --from-spec`
>   (or `--graph`). Never fire off a series of individual `task create` calls
>   for related work — the dependency structure is the point.

**with:**

> - **Create graphs, not loose tasks.** Any request that decomposes into more
>   than one task becomes a spec in `specs/` plus `aq task create --from-spec`
>   (or `--graph`). Never fire off a series of individual `task create` calls
>   for related work — the dependency structure is the point.
> - **Give the graph phases when the work has stages.** If the request has a
>   "first this, then that" shape, declare a `phases:` list in the `aq-graph`
>   block rather than wiring task-to-task `blocks` edges across the boundary:
>   a phase gates every task under it at once, survives a task being added
>   later, and is what the graph view reads as progress. Two to five phases.
>   Concurrent work gets no phase.
> - **Decompose inside a task with subtasks.** When a node is several steps
>   for one agent, list them as `subtasks:` on that node instead of splitting
>   it. The agent works them in order and ticks them off; you get the
>   progress bar without paying for a handoff, a branch and a schedule.

**(d) `src/skills/aq-tasks/SKILL.md:136-137`, replace:**

> Add your own with `subtask-add` when a task has several distinct steps worth
> tracking individually — it beats losing the breakdown in prose.

**with:**

> Add your own with `subtask-add` when a task has several distinct steps worth
> tracking individually — it beats losing the breakdown in prose. If the task
> arrived with a checklist, work that one; do not re-decompose it. Add rows for
> steps you discover as you go, and settle each as you finish it, so a reader
> can see where you are without reading your transcript.
>
> A subtask is not a place to put work that belongs to someone else. Something
> outside this task's scope is still emergent work: file it as a task
> (`aq task create … --reason "…"`), not as a checklist row.

**(e) `src/skills/aq-tasks/SKILL.md`, `## Phases (planner)` (`:335-352`) — add a first paragraph:**

> Use a phase when phase *N+1* genuinely must not begin until every task in
> phase *N* is COMPLETED. That is the whole semantic: a phase is a container
> with a `blocks` edge onto every earlier open sibling phase, and a blocked
> container withholds its children, so one edge gates a whole stage. Two to
> five phases per project or per epic. If the two groups could overlap, they
> are not phases — use `needs` edges between the individual tasks.

Tier 1 makes the **supervisor** (the only actor that can actually create a graph today, §1.1) produce phases by hand-sequencing `phase_create` + `create_task_graph --parent <phase-id>`, and subtasks by `task_subtask_add` (it is elevated, so the fence at `task_comment_commands.py:74` returns early for it). That is real value for zero code. It does **not** help the planner or spec-ingest, and it is not atomic.

### Tier 2 — the grammar extension (recommended)

Let one `aq-graph` document declare the whole structure, so one validated, reviewable, atomic call creates it.

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
    subtasks:
      - "Wire the cascade step behind messages.enabled"
      - "Parking for stale session rows"
```

Creates: epic `<id>` (depth 1) → `<id>.1` "Phase 1", `<id>.2` "Phase 2" (depth 2) → `<id>.1.1` tables, `<id>.1.2` queries, `<id>.2.1` cascade (depth 3). `<id>.2` carries a `blocks` edge onto `<id>.1`. `<id>.1.1` gets three `task_subtasks` rows, `<id>.2.1` two. All in `write_plan`'s single transaction.

#### Parser / creator changes this implies

- **`src/task_graph/models.py`**
  - new `@dataclass GraphPhase: key, title, label: str | None` and `@dataclass GraphSubtask: title, context: str = ""`, both with `to_dict()`;
  - `GraphNode` (`:103`) gains `phase: str | None = None` and `subtasks: list[GraphSubtask] = field(default_factory=list)`, both added to `to_dict()` (`:122`);
  - `TaskGraph` (`:159`) gains `phases: list[GraphPhase] = field(default_factory=list)`, added to `to_dict()` (`:179`).
- **`src/task_graph/parser.py`**
  - `_parse_phase` beside `_parse_parent` (`:220`), findings `bad_phase` / `missing_phase_key`;
  - `_parse_node` (`:115`) reads `phase` (string, `bad_field_type` otherwise) and `subtasks` (list of `str` or `{title, context}`; finding `bad_subtask`). Bounds come from the existing constants, imported rather than re-declared: `MAX_SUBTASKS_PER_TASK = 200` (`src/database/queries/task_subtask_queries.py:15`), `MAX_SUBTASK_TITLE = 300` / `MAX_SUBTASK_CONTEXT = 16000` (`src/commands/task_subtask_commands.py:23-24`);
  - `parse_graph` (`:294`) reads `data.get("phases")` into `TaskGraph.phases`, preserving document order.
- **`src/task_graph/validator.py`** — new findings beside `_check_keys` (`:260`):
  - `duplicate_phase_key`, `unknown_phase` (a node names a phase key not declared) — errors;
  - `phase_without_nodes` — warning (an empty phase is legal but is held open by `childless_held_open_container()` and must be deleted by hand);
  - `redundant_phase_edge` — warning when a `blocks` need points at a node in an earlier phase; the phase gate already covers it;
  - `subtasks_unreportable` — warning when a node carries `subtasks` and its resolved profile's policy lacks `task_subtask_update`, naming `aq agent profile-reseed --profile-id <id> --grants-only`. Reuse `profile_allows_command` (`src/prime/sections.py:575`);
  - `phases_need_root` — error when `phases` is non-empty and `parent_id` was supplied (see §4).
- **`src/task_graph/creator.py`**
  - `assign_child_ids` (`:64`) becomes two-level: phase *i* → `<epic>.<i>`; a node in phase *i* → `<epic>.<i>.<j>`; an unphased node keeps `<epic>.<k>`, numbered after the phases. The container row's `next_child_ordinal` (`:247`) becomes `len(phases) + len(unphased nodes) + 1`.
  - `build_plan` (`:205`) additionally emits `phase_rows` (task rows, `status=DEFINED`, `task_type="plan"`, description = label — mirroring `phase_commands.py:114-124`), `subtask_rows`, and the inter-phase `blocks` dependency rows: **every earlier phase, not only the predecessor**, exactly as `phase_commands.py:163-166` does and for the same reason (deleting an abandoned middle phase must not release its successor).
  - `write_plan` (`:435`) ordering is load-bearing. `set_parent_bulk` (`src/database/queries/hierarchy_queries.py:1090-1106`) asserts *freshly inserted childless leaves with no blocking out-edges*. So: insert epic → insert phase rows → `set_parent_bulk(phase_ids, epic_id)` → per phase, `set_parent_bulk(node_ids, phase_id)` → per phase, `mark_container(phase_id, conn=conn)` + `_upsert_meta(phase_id, PHASE_KEY, {"order": i, "label": …}, conn=conn)` → **then** the existing dependency insert (`:529`), which now carries the inter-phase edges. Writing an inter-phase edge before the links would trip `cycle_check_skipped`.
  - subtasks: `add_task_subtasks` (`src/database/queries/task_subtask_queries.py:44-56`) opens its own `self._engine.begin()`. It needs the codebase's usual `*, conn=None` parameter so `write_plan` can call it on the graph's connection; a graph that half-creates its checklists is the partial-graph failure `creator.py`'s module docstring forbids.
  - `build_report` (`:585`) gains `phases: [{key, task_id, order, title}]` and per-node `subtasks: <count>`, so `--dry-run` shows the structure it would build.
  - `_file_hierarchical_plan` (`:380`) would need one `file_prepared_children_on` per phase. **First cut: refuse instead.** A phase that owns its children's delivery is the hazard `work-graph.md:360` already names for standing parents — the children are based on the container's checkpoint and reach the default branch only when the container settles, which one FAILED sibling prevents forever. Refuse `phases` in `HIERARCHY_MODES` with `graph.phases_unsupported_mode`, worded like `hierarchy.parent_key_unsupported_mode`.
- **`src/commands/task_commands.py:2920-2964`** — `_validate_graph_parent`'s depth check (`:2951`, `depth + 1 > MAX_STRUCTURAL_DEPTH`) is written for a one-level graph. A phased graph is two levels below its parent. First cut: forbid the combination (`phases_need_root`) rather than generalise the check.

#### Why the grammar, not step-by-step calls

1. **The fence forbids the alternative.** `_task_findings_write_fence` (`task_comment_commands.py:79-92`) requires the caller's session to *own* the task. A planner cannot seed a subtask on a task it created. Only an elevated supervisor or the loopback CLI can, so "the planner calls `task_subtask_add` per node" is not a slower design, it is a refused one.
2. **Atomicity.** `write_plan` is one transaction by contract (`creator.py:6-13`). `phase_create` + N × `create_task` + M × `task_subtask_add` is 1+N+M separate transactions with no rollback; a failure halfway leaves a half-built epic and an empty phase that must be hand-deleted to release its successor.
3. **Reviewability.** `--dry-run` already reports every finding at once, and the planner is already required to run it (`planner/profile.md:104-106`). A phase that only exists after the eleventh call cannot be dry-run.
4. **Zero contract churn.** `create_task_graph` is not in the contract registry and no playbook calls it, so nothing under `tests/fixtures/playbooks/v2/` or `src/prompts/reviewed_playbooks/` moves (§6).

### Tier 3 — the proposal payload (optional, follow-on)

Give `task_batch_propose` the same vocabulary: an optional top-level `phases: [{key, title, label}]` and per-task `phase: <key>` / `subtasks: [...]` in the payload, validated in `_validate_shape` (`proposal_commands.py:45`) and materialised in `_cmd_task_batch_commit` (`:295`). This costs **no contract change** — `task_batch_commit`'s contracted args are `proposal_id`/`gate_id`/`project_id` and the payload is opaque JSON in `task_proposals.payload`. Pair it with a `spec-ingest/profile.md:16-28` Role edit naming the same policy as Tier 1(a).

Deferred because `default-pipeline` is not activated on a fresh install (`src/playbooks/required.py:25-33`) — it buys structure on a path most installs do not run.

### Where the policy lives

Mechanism in code, policy in prompts and playbooks, per the standing rule. Concretely:

- **"When is something a phase"** → `planner/profile.md` Rules + `supervisor/profile.md` Rules + `aq-tasks/SKILL.md` `## Phases`. Never in the validator: `phase_without_nodes` and `redundant_phase_edge` are *warnings* precisely so the code states a fact and the prompt states the judgement.
- **"How granular is a subtask"** → the same three places (Tier 1 a/d/e). The code's only opinion is a bound: 200 per task, 300-char titles.
- **"Which key does a standing parent use"** → already playbook policy (`work-graph.md:354`); unchanged.

---

## 4. Depth budget

`MAX_STRUCTURAL_DEPTH = 3` (`src/task_names.py:93`), root = 1, enforced at `hierarchy_queries.py:1030`/`:1178` and `task_commands.py:2951`. `MAX_NAMING_DEPTH = 3` (`task_names.py:89`) matches, so dotted ids stay derivable.

**Legal shapes:**

| Shape | How | Depth |
|---|---|---|
| `epic → phase → task` | Tier 2 `phases:` in one document | 3 — full |
| `phase → epic → task` | `phase_create` at the project root, then `create_task` for the epic under it, then `create_task_graph --parent <epic-id>` | 3 — full |
| `phase → task` | `phase_create` at the root, then `create_task_graph --parent <phase-id>` (today's `parent_id` path files nodes directly under the phase) | 2 |
| `epic → task` | today's plain graph | 2 |
| `task + subtasks` | any of the above; subtasks live in `task_subtasks`, **not** in `tasks` | +0 |

**Illegal:** `phase → epic → phase → task` (4). Refused by `hierarchy.depth`, and by `graph.phases_need_root` before a row is written.

**What the planner does when the plan wants more nesting** — in this order:

1. Push the extra level into `subtasks:`. A group of five one-sitting steps under a task is a checklist, not a level. This is free.
2. Split the epic. Two sibling epics under the same phase, with a `needs` edge between them, expresses "A then B" at no depth cost.
3. Drop the outer phase. If the work is one stage, the phase is decoration.

Never: reparent to fake depth, or file a fourth level and let `hierarchy.depth` refuse it at creation.

---

## 5. Worker side

**Already built, nothing to add.** Prime renders `## Subtasks` from `build_task_subtasks_summary` (`src/prime/sections.py:179-208`): titles only, glyphs `[x]/[-]/[~]/[ ]`, and the line *"Full context for one: `aq task subtask-show N`. Work them in order unless the task says otherwise."* When the held profile's policy lacks `task_subtask_update` the "Report progress as you go" half is omitted (`allow_updates=False`, gated on `profile_allows_command` / `SUBTASK_UPDATE_COMMAND`, `sections.py:572-575`).

**When a worker adds its own** — Tier 1(d). Short version: work the checklist you were given; add rows for steps you discover; file anything outside the task's scope as a task, not a row.

**Interaction with the close refusal.** `task_close --outcome pass` is refused `subtasks.open` while any row is `pending`/`in_progress`; `--skip-open-subtasks` flips them to `skipped`; a failing close is never gated. A planner-seeded checklist therefore turns "did you actually do the five things" into a close-time question — which is the point — and never into an unclosable task, because the escape hatch exists.

**Existing installs without the grant.** `vault.ensure_default_profiles` is write-if-absent, so a vault upgraded from before the grants existed has worker profiles with no `task_subtask_update`. Such a worker sees the checklist, is *not* told to report progress, and can still close with `--skip-open-subtasks`. The repair is the documented one (`docs/guides/worker-pools.md` §7b, "A shipped profile that gained a new grant"): `aq doctor --check profiles.system_drift` names the missing grants, `aq agent profile-reseed --profile-id <id> --grants-only` merges them in without touching other edits. Tier 2 adds one thing: the validator's `subtasks_unreportable` **warning** at `--dry-run` time, so a planner learns *before* creating the graph that the target profile cannot tick the boxes, and the operator gets the reseed command in the finding. A warning, not an error — the checklist is still worth writing.

---

## 6. Reviewed-bundle impact

The precise rule: a bundle's `contract_fingerprint` is taken over `compiled_against.commands` **only**, and `src/playbooks/definition.py:742-748` says so in as many words — *"Profiles are excluded."* The shipped bundles compile against:

| bundle | commands |
|---|---|
| `blocked-task-escalation` | `task_recovery_notify` |
| `ci-main-sentinel` | `ci_baseline_status`, `ensure_task`, `escalation_create` |
| `default-assignment-routing` | `task_route`, `task_route_options` |
| `default-pipeline` | `ensure_task`, `gate_create`, `task_batch_commit` |
| `provider-usage-probe` | `provider_usage_probe` |

Therefore:

- **Tier 1 (profile + skill text): no bundle rebuild.** Editing `planner/profile.md` or `supervisor/profile.md` cannot move a contract fingerprint, and `profiles_referenced` lists ids, which do not change. What *does* guard these files is `tests/test_shipped_profile_capabilities.py` — its `EXPECTED_UNREACHABLE` literal (`:50-61`) fails if a profile gains a name its own token cannot dispatch. That is the guard this branch tripped on, and it is the one to run after any profile edit.
- **Tier 2 (grammar): no bundle rebuild.** `create_task_graph` has no contract registration and no playbook step calls it.
- **Tier 3 (proposal payload): no bundle rebuild.** `task_batch_commit`'s contracted args are unchanged; the payload is opaque JSON.
- **The thing that would force one:** adding a field to `CreateTaskArgs` or `EnsureTaskArgs` (e.g. a `phase_key` convenience for playbooks). `CommandArgs` is `extra="forbid"` (`contracts/models.py:22`), so such a field is the only way a playbook could ever pass phase intent — and it would move `ensure_task`'s fingerprint and require rebuilding **`default-pipeline` and `ci-main-sentinel`** via `scripts/rebuild-reviewed-playbook-artifacts.py`, then hand-copying into `src/prompts/reviewed_playbooks/` and `docs/playbooks/integration-only/` and updating each `manifest.md`'s digests. **This design deliberately avoids that.** If a playbook must create phased work later, contract `phase_create` as its own command rather than widening `create_task`.

---

## 7. Task breakdown

Numbered smallest-first; 1–3 are independently shippable.

### Task 1 — Make the planner's own instructions executable
**Files:** `src/api/scope.py:13-76` (add `create_task_graph` to `AGENT_COMMAND_SET` with a comment stating the project pin, beside the `phase_create` entry at `:63`); `src/profiles/defaults/planner/profile.md:63-88` (`aq_commands` += `create_task_graph`). **Tests:** `tests/test_api_scope.py` — a session token in project P may call `create_task_graph` for P and is refused `project_id mismatch` for Q; `tests/test_shipped_profile_capabilities.py` — `planner` stays `set()` in `EXPECTED_UNREACHABLE`.
**Note:** `check_command_scope` (`scope.py:206-221`) injects `task_id` from the token for every non-`_TASK_ID_UNPINNED` command; `_cmd_create_task_graph` does not read `task_id`, so the injection is inert — assert that in the test so it stays inert. The alternative (minting planner tokens elevated) is larger and grants far more; see open question Q2.

### Task 2 — Tier 1 prompt and skill text
**Files:** `planner/profile.md` (:20-24 replace, Rules append), `supervisor/profile.md` (:170-173 replace), `src/skills/aq-tasks/SKILL.md` (:136-137 replace, `## Phases` prepend). **Tests:** `aq test tests/test_shipped_profile_capabilities.py tests/test_default_profiles.py`. No bundle rebuild (§6). Depends on nothing; ship first.

### Task 3 — `phases:` and `subtasks:` in the model and parser
**Files:** `src/task_graph/models.py` (`GraphPhase`, `GraphSubtask`, two `GraphNode` fields, one `TaskGraph` field); `src/task_graph/parser.py` (`_parse_phase`, `_parse_node` additions, `parse_graph`). **Tests:** `tests/test_task_graph.py` — a document with `phases` round-trips; an unknown-typed `phase` is `bad_field_type`; a subtask over 300 chars is `bad_subtask`; 201 subtasks on one node is `bad_subtask`; a document with neither field parses byte-identically to today.

### Task 4 — Validator findings
**Files:** `src/task_graph/validator.py`. **Tests:** `tests/test_task_graph_validator.py` (or the existing validator test file) — one case per finding in §3 Tier 2, plus: a clean phased graph produces zero errors; `subtasks_unreportable` is severity `warning` and its detail contains `profile-reseed`.

### Task 5 — Creator: phase rows, links, gate edges
**Files:** `src/task_graph/creator.py` (`assign_child_ids`, `build_plan`, `write_plan`, `build_report`); refusal in `_cmd_create_task_graph`/`_validate_graph_parent` for `phases` + `parent_id` and for `HIERARCHY_MODES`. **Tests:** `tests/test_create_task_graph_command.py` — ids are `<epic>.<i>.<j>`; phase 2 has `blocks` edges onto phase 1 **and** every earlier open phase in a 3-phase graph; each phase carries `task_metadata.phase = {order, label}` and `is_container`; phase 2's tasks are withheld while phase 1 is open; a patched failure mid-write leaves **zero** rows (the existing single-transaction test pattern at `creator.py:336`); `--dry-run` reports `phases` and creates nothing; `phases` + `parent_id` → `graph.phases_need_root`; a hierarchy-mode project → `graph.phases_unsupported_mode`.

### Task 6 — Creator: subtask rows in the same transaction
**Files:** `src/database/queries/task_subtask_queries.py:44` (add `*, conn=None`, the standard split); `src/task_graph/creator.py` (`write_plan` writes `subtask_rows`). **Tests:** `tests/test_task_subtasks.py` — `add_task_subtasks(..., conn=conn)` participates in the caller's transaction and rolls back with it; `tests/test_create_task_graph_command.py` — a node's three subtasks exist ordinal-ordered after create, and none exist after a mid-write failure; `aq task subtasks <id>` shows them; prime renders the `## Subtasks` block for the created task.

### Task 7 — End-to-end scenario
**Files:** `scripts/e2e-smoke.sh` (new scenario), a fixture spec under `scripts/` or `tests/fixtures/`. See §8.

### Task 8 (optional, Tier 3) — Proposal payload
**Files:** `src/commands/proposal_commands.py:45` (`_validate_shape`), `:295`/`:496` (both commit paths), `src/profiles/defaults/spec-ingest/profile.md:16-28`. **Tests:** `tests/test_task_proposals.py` — a payload with `phases` and per-task `phase`/`subtasks` commits into the same shape Task 5 produces; an unknown `phase` key is refused at propose time, not at commit; `task_batch_commit`'s contract fingerprint is unchanged (assert against `tests/fixtures/playbooks/v2/default-pipeline/manifest.md`).

---

## 8. Acceptance test

**Claim to prove:** a realistic spec, ingested through the real pipeline, yields phases, epics, tasks and subtasks, and the phase gate actually holds.

**Can `scripts/e2e-smoke.sh` carry it? Half of it — the important half.** The kit runs a real daemon on real PostgreSQL through the real CLI with `sessions.provider: fake` and no LLM (`scripts/e2e-env.sh:348`, `:452-457`). Everything in Tier 2 is CLI-and-daemon only, so it fits:

New scenario `phased-graph`:
1. Write a fixture spec with a `parent:`, two `phases:`, three nodes and a node carrying three `subtasks:`.
2. `aq task create --from-spec <path> --dry-run` → exit 0, report lists two phases, three nodes, three subtasks, zero errors.
3. Drop `--dry-run`; capture the epic id.
4. `aq task phase-list --project-id $PID --parent-id <epic>` → two rows, `order` 1 and 2, phase 2 `is_blocked: true`.
5. `aq task get-tree --task-id <epic>` → three levels.
6. `aq task subtasks <epic>.1.1` → three rows, all `pending`.
7. `aq task claim --next` twice, close both phase-1 tasks (`--skip-open-subtasks` on the one with a checklist), then assert phase 2's task becomes claimable and phase 1 settles COMPLETED.
8. Delete the epic (`--cascade`) so the scenario is re-runnable.

**What the kit cannot carry:** the spec-ingest leg. A fake session never reads a profile's Role, so no agent will actually author a proposal. Cover that leg with a non-LLM pytest test in `tests/test_task_proposals.py`: emit `spec.approved` → assert the pipeline's `ensure_task` fired → feed a **recorded** `task_batch_propose` payload (the one a planner would have written) → `gate_create` → `gate_resolve approve` → `task_batch_commit` → assert the same tree shape as step 4–6 above. That proves the pipeline's plumbing end to end; the only thing not proven by machine is that a real LLM planner *chooses* to emit phases, which is what Tier 1's prompt text is for and which belongs in `tests/llm/` if anywhere.

---

## 9. Open questions for the operator

1. **Phases in hierarchy/train projects.** This design refuses them (`graph.phases_unsupported_mode`), by the same argument `work-graph.md:360` uses against standing parents: a phase would own its children's delivery, and one FAILED sibling would keep the whole stage off the default branch forever. Is refusing acceptable for now, or is phase-aware delivery (each phase's branch merging into the epic's on settle) wanted in this pass?
2. **How the planner gets to reach `create_task_graph`** — add it to `AGENT_COMMAND_SET` with the project pin (Task 1, matches the `phase_create` precedent), or mint planner tokens `elevated` (one flag, far broader grant)? Recommendation: the scope list.
3. **A failed task in phase *N* holds phase *N+1* forever**, because a phase settles only when every child is COMPLETED. Operator said phases "should gate" — is that the desired behaviour for a failure, or should there be an explicit release (e.g. `aq task phase-release`, or closing the failed child with `--abandon-children`)?
4. **Phases at the project root versus inside an epic.** Tier 2 gives `epic → phase → task` in one call. Root-level phases still need `phase_create` first and one graph per phase. Worth a follow-on that lets `create_task_graph --parent <phase-id>` mint its own container (epic) under the phase, or is the two-step fine?
5. **`subtasks_unreportable`: warning or error?** Proposed as a warning so a checklist is still written for a profile that cannot tick it (prime shows it either way). Should a planner instead be blocked until the operator reseeds?
6. **Is Tier 3 wanted at all?** `default-pipeline` is not activated on a fresh install (`src/playbooks/required.py:25-33`), so the proposal path is opt-in. If it is not in use here, Tier 3 is dead weight.
7. **Subtask granularity numbers.** Tier 1 proposes "three to fifteen per node, two to five phases per epic" as the first-cut wording. These are guesses at the operator's taste and should be corrected before they become the fleet's habit.
