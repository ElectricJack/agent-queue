# aq task graphs — hierarchy, phases, graph filing, dependencies, formulas

Reference for [the aq-tasks skill](../SKILL.md). Planners, supervisors and
operators use most of this; a plain worker needs only the hierarchy rules
that explain why a filing or a close was refused.

## Contents

- Filing a graph (planners)
- Hierarchy
- `hierarchy.*` refusal codes
- Phases (planner)
- Dependencies
- Cooking a formula

## Filing a graph (planners)

A session whose profile grants `create_task_graph` (a planner) may file a whole
graph with `--graph` / `--from-spec` and a `--reason`. Its nodes land under the
task it holds. A document-level `parent:` block creates the graph's own container
in the same transaction: under the held task by default, or at the project root
with `--root`, where the container carries the routing gate a root filing gets
and its children wait behind it. A root graph must declare that `parent:` block.
In hierarchical/train projects, a worker graph cannot declare its own document
parent (`hierarchy.parent_out_of_scope`).
`--after-review <review-id>` gates every node of the graph on that document
review until it is approved; an unknown or withdrawn review creates nothing.
Never file an epic as a plain task: a pool worker can claim it before its
children arrive. The supervisor or an operator can file a single epic with
`aq task create --container` and add its children afterwards; a worker session
is refused (`hierarchy.container_not_for_sessions`) and uses the graph instead.

Graph nodes take the same routing hints as a single task (`type`,
`intelligence_class`); a `profile` or `pin` key is refused with
`routing.choice_forbidden`.

```bash
# From a spec (preferred for multi-task graphs): dry-run first, then file.
aq task create --project <pid> --from-spec projects/<pid>/specs/<slug>.md --reason "why" --dry-run
aq task create --project <pid> --from-spec projects/<pid>/specs/<slug>.md --reason "why"
aq task create --project <pid> --from-spec <path> --reason "why" --after-review <review-id>
# Under an existing container
aq task create --project <pid> --from-spec <path> --parent <container_task_id> --reason "why"
```

## Hierarchy

Any task can become a **container** just by gaining a child — there is no
separate group entity, and progress is always computed live from the graph,
never stored. Ids are hierarchical and **immutable**: a child created under
`swift-falcon` gets `swift-falcon.1`, its own child gets `swift-falcon.1.1`,
and so on — an id never changes once assigned, and structural depth is
capped at 3 (root = 1).

```bash
aq task reparent --task-id <task_id> --parent-id <new_parent_id>  # move under another container
aq task reparent --task-id <task_id> --root           # detach to root (clears parent)
# On a worker token, reparent moves only unclaimed tasks you filed from the task
# you hold, to the parents you could have filed under (your task, a descendant,
# its immediate parent, or --root, which attaches the routing gate). Use it only
# when a filing was misplaced — never to move a child aside so your task can close.
# Operator/supervisor deletion: preview before confirming.
aq task delete --task-id <task_id> --recursive --preview
aq task delete --task-id <task_id> --recursive --branches keep --yes
# --task-only instead reparents surviving children without renaming their ids.
aq task close <id> --abandon-children \
  --outcome pass|fail --summary "..."                 # close a container, abandoning
                                                        # any still-open descendants
```

A container with open children refuses a plain passing close and unforced
transition to COMPLETED. `--abandon-children` refuses live or manually paused
descendants. Adding a child under a COMPLETED container is refused. Ordinary
container settlement requires every child COMPLETED; managed integration parents
also require the train's completion proof.

Unified deletion is operator/supervisor-only. It stops selected running work
before deleting; `--stop-running` acknowledges the warning, rather than enabling
the stop. `--branches keep` is the default; `delete` requests guarded branch
cleanup. `--cascade` is a deprecated alias for `--recursive`. These deletion
semantics supersede the older refuse-on-live-descendants/branch-exists contract.

A successful `reparent` emits **`task.reparented`** on the bus, carrying
`task_id`, `project_id`, `title`, `old_parent` and `new_parent` (either parent
is `null` at the root). Playbooks can trigger on it.

In a hierarchical or train project a reparent also checks ownership: a task
with a claim, a live session or workspace, an active integration batch, a
branch carrying commits, or landed or completed work answers
`needs_quiescence` with the `owner` and the `safe_continuation` to follow
instead of rewriting it.

## `hierarchy.*` refusal codes

Failures come back as `hierarchy.<code>`. Common codes (the complete enum is in `aq schema`):


| Code | Meaning |
|---|---|
| `not_found` | the task or the requested parent does not exist |
| `self_parent` | a task cannot be its own parent |
| `cross_project` | parent and child live in different projects |
| `container_closed` | the parent is COMPLETED and cannot take children |
| `cycle` | the move would close a loop over blocking edges |
| `depth` | structural or naming depth would exceed 3 |
| `open_children` | close/complete refused: a direct child is non-terminal |
| `open_descendants` | archive refused: a *deeper* descendant is non-terminal |
| `has_children` | legacy hierarchy writer requires explicit subtree handling; unified delete uses `--recursive` / `--task-only` |
| `live_descendants` | abandon refused: a descendant has a live session |
| `manually_paused_descendants` | abandon refused: resume hand-paused descendants first |
| `dependency_on_ancestor` | reparent refused: the moved subtree has a blocking edge onto its new parent chain — drop the edge first (returned bare, not `hierarchy.`-prefixed) |
| `cycle_check_skipped` | internal: the bulk graph-creation path was handed a task that is not a fresh leaf |
| `parent_conflict` | `parent_key` was combined with `parent_id` or `root` — pass exactly one |
| `parent_key_not_for_sessions` | `parent_key` is for automated creators; a worker files under the task it holds |
| `parent_key_busy` | another creator holds the standing-parent lock for that key — retry |
| `parent_key_unavailable` | the standing container could not be created (the underlying refusal is returned when there is one) |
| `parent_key_unsupported_mode` | the project delivers hierarchically, where a standing parent would own its children's delivery — use `parent_id` or the root |
| `parent_key_invalid` | `parent_key` must be 1–64 characters matching `[a-z0-9][a-z0-9_-]*` |
| `reserved_dedup_key` | a `dedup_key` starting `parent:` belongs to the standing-parent mechanism — pass `parent_key` instead |

## Phases (planner)

Most projects and most epics have no phases. Create one only when you can
name what must finish before the next stage may begin: phase *N+1* stays
blocked until every task in phase *N* is COMPLETED. That is the whole
semantic — a phase is a container with a `blocks` edge onto every earlier
open sibling phase, and a blocked container withholds its children, so one
edge gates a whole stage. Stop at five per level. If two groups could
overlap, they are not phases; use `needs`/`blocks` edges between the
individual tasks. Phases are refused outright in a project whose integration
mode is `hierarchy` or `train` — there a phase container would own its
children's delivery branch — with one code,
`hierarchy.phases_unsupported_mode`, from `phase_create` and from a graph
alike.

**Prefer declaring them in the graph.** One `aq-graph` document can carry
its phases, so the containers, their metadata, the gate edges, the tasks and
their subtasks are created in a single transaction that a `--dry-run`
reviews first:

```yaml
phases:
  - key: schema
    title: "Phase 1 — schema"
    label: schema
  - key: engine
    title: "Phase 2 — engine"
nodes:
  - key: tables
    phase: schema
    title: "Add the messages table"
```

That creates `<epic>.1`/`<epic>.2` for the phases and `<epic>.1.1…` for
their work; a node that names no phase stays a direct child of the epic.
Never make a node depend on work in a *later* phase — that deadlocks (the
later phase waits for this one to finish, and this one waits for that task),
and it is refused as `inverted_phase_edge` — whatever the `dep_type`,
`waits-for` included. That includes reaching it through an unphased node: a node with no `phase` is not held back by any
phase, but it is still held back by its own `needs`, so it passes the
deadlock along. If the work really runs in that order, the two are in the
wrong phases.
Because epic → phase → task is the whole depth budget, such a document must
be created at the project root — `phases:` with `--parent` is refused with
`graph.phases_need_root`. Filing phases one `phase-create` call at a time is
still available and is what you use to add a phase to an epic that already
exists.

A phase is an ordered container that gates implicitly: phase *N+1* stays
blocked until every child of phase *N* is COMPLETED, with no extra edges to
manage per task. Available if your profile lists `phase_create`/`phase_list`
(planner, supervisor, operator — not a plain worker):

```bash
aq task phase-create --project-id <pid> --title "..." [--label "..."] [--parent-id <id>]
aq task phase-list --project-id <pid> [--parent-id <id>]
```

**Supervisor or loopback workflow.** An elevated supervisor or the loopback
CLI owns root-level phased planning: it may create a phase and place work in
it with `aq task create --parent <phase-id>`, or create a root graph that
declares `phases:`. That is the supported path for a phase → task plan.

**Planner workflow.** A planner granted `create_task_graph` files a graph with
`aq task create --project <pid> --from-spec <path> --dry-run --reason "..."`,
then repeats without `--dry-run`. With no document parent, its graph is
fenced to the planning task it holds: nodes become direct children of that task.
A document `parent:` creates the graph's own container under the held task;
`--root` places that container at project root. `phases:` needs a root graph,
so a phased graph requires both `--root` and a document parent. Root graphs
always require a document parent. Hierarchical/train worker
graphs cannot declare a document parent, and those projects reject phases entirely.
External task-id dependencies, `cross_project`, and authored `parent-child` edges
remain outside scoped worker graph filing; use local `needs` to order its nodes.

Never make a node `need` the planning task you hold, or any task above it, not
even as a barrier that holds children back while you arrange them. The plan
exists before you file, and membership is already the `parent-child` edge. A
gating edge onto the filer or the filing's own parent chain is refused as
`dependency_on_ancestor`, by a graph and by `aq task add-dependency`
alike. Order the new tasks among themselves with local `needs`. A delivered
plan still satisfies anything that depends on it after its branch is cleaned
up, because readiness checks Git containment of its retained source commit, not
the branch or a delivery receipt.

**Checklist ownership.** An unphased planner graph may put `subtasks:` on a
child task. Those are checklist rows owned and settled by the worker that
later holds that child, not work assigned to the planner or separate tasks to
schedule. The planner supplies the initial, concrete checklist; the worker
works the received rows and records progress with the subtask commands.
A phase settles like any container — all children COMPLETED — so a failed
child holds the gate on purpose. A childless phase is never claimable and is
held open (never auto-settled); if it turns out to be unneeded, ask the
supervisor/operator to delete it rather than leaving it empty; that releases the
next phase.

## Dependencies

```bash
aq task add-dependency --task-id <task_id> --depends-on <upstream_id> --dep-type blocks
aq task remove-dependency --task-id <task_id> --depends-on <upstream_id>
```

Dependency types: `blocks`, `parent-child`, `waits-for`,
`conditional-blocks`, `delivery-of`, `discovered-from`, `related`, `duplicates`,
`supersedes`. The first five gate readiness. `delivery-of` requires source
completion and current default-target Git containment; a receipt alone is not
admission authority. `is_blocked` covers graph/gates, while the shared admission
view adds current preparation/delivery evidence. See `docs/concepts/tasks.md`.

## Cooking a formula

A formula is a reusable, parameterised task-graph template in the vault
(`vault/[projects/<pid>/]formulas/<name>.md` — see
`docs/specs/design/formulas.md`). `formula_list` and `formula_show` are
read-only and available to any session; `formula_cook` **creates** the
graph and requires elevated/operator or supervisor authority. Ordinary workers
may use graph creation when their profile grants it, but cannot cook formulas:

```bash
aq formula list --project-id <pid>                # what's available
aq formula show review-and-fix --var branch=feat/x # resolve + validate, no write
aq formula cook review-and-fix -p <pid> \
  --var branch=feat/x --var fixer=coding --dry-run  # then drop --dry-run
```
