# Task change sets

Supervisors stage any change involving multiple tasks as one proposal. This
prevents a scheduler tick from claiming a newly created task before its blockers
have been added. Nothing in a proposal is live until the human decision and
its source application succeed; creates, edits, dependencies, controls and comments
then become visible together.

Preview a YAML or JSON file, then stage the same file:

```bash
aq task batch-propose --project my-project --file changes.yaml --dry-run
aq task batch-propose --project my-project --file changes.yaml
```

`--file -` reads stdin. `--source` overrides provenance; otherwise the file's
`source` or its path is recorded. The preview shows the validated before/after
values, new and removed dependencies, and comments without saving a proposal.

```yaml
source: spec:projects/my-project/specs/api.md
tasks:
  - tempId: prerequisite
    title: Implement shared schema
    description: Add the schema before either API starts.
    intelligence_class: high
    parent_id: existing-container
edits:
  - task_id: existing-api
    title: Implement API using shared schema
    priority: 160
  - task_id: queued-task
    action: pause
edges:
  - from: existing-api
    to: prerequisite
    dep_type: blocks
remove_edges:
  - from: existing-api
    to: old-prerequisite
    dep_type: blocks
comments:
  - task_id: existing-api
    body: Shared schema replaces the old prerequisite.
    kind: note
```

`tasks` is optional for edit-only proposals. New tasks require unique `tempId`,
`title` and `description` (which may be empty). Edges, comments and `parent_id`
can reference temporary ids or existing task ids in the same project. Edits
require an existing `task_id` and accept `title`, `description`, `priority`,
`intelligence_class` and `task_type` hints, `parent_id`, and a lifecycle `action`.
Use `parent_id: null` to detach to the root. The action can be `pause`, `resume`,
`block` or `archive`; `reason` documents an archive. Archiving requires a terminal
subtree, includes descendants, and retains existing integration and live-session
guards. A block is an administrative stop that the scheduler
will not undo. Use an explicit valid `status` transition to reopen it. Execution
assignment and completion belong to the worker/integration protocols.

Dependencies default to `blocks`; removals specify the exact typed edge.
Reversals are validated against the complete resulting graph. `parent-child`
edges use the guarded hierarchy writer. Blocking dependencies cannot target
containers; `parent-child` and valid `waits-for` membership keep their existing
semantics. Routing profiles are chosen by the router; a proposal may carry hints
but cannot choose a profile.

The proposal preview presents the diff and comments. Approve and Discard first
prepare a decision packet for the current proposal digest, project and gate;
the confirmation applies that exact packet. Approval resolves the bound gate
and materializes the proposal in one transaction. A materialization conflict
rolls back the approval too. Decline discards the proposal and cancels its gate,
retaining any approval hold without emitting an approval release.

An authorized supervisor can still replay an already approved materialization:

```bash
aq task batch-commit --proposal-id prop-abc123
```

Before a gate exists, `task_batch_update` replaces the complete payload and
recomputes its diff and expected versions. Once gated, discard and propose a new
revision. A concurrent edit, claim, dependency or relevant hierarchy/integration
change causes `change_set.conflict`; no changes are applied. Review a fresh
proposal against the current graph. A failed commit stays ready and rolls back
all changes; replaying a successful commit returns the original created ids.
The proposal read endpoint retains its receipt, and `task.change_set_committed`
records the actor, source, reviewed diff and receipt in the same transaction.

Human command callers use `ask_prepare_decision` with the ask's
`decision_generation`, `source_fingerprint` and `feedback_version`, then
`ask_decide` with the returned preview id and a stable idempotency key.
`gate_show` exposes the bound ask. An unversioned human `gate_resolve` refuses
with the ask link instead of interpreting an actor label or arbitrary reply.
Generic human gates additionally require producer-owned `approval_hold_v1`
semantics and complete context. Missing context leaves an owned request with
no decision controls. Decline keeps waiters blocked; `gate_reopen` requires the
current version and creates a new decision generation. Technical and routing
gates retain their specialized mechanisms.
