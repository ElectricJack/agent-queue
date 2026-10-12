# Task change sets

Supervisors stage any change involving multiple tasks as one proposal. This
prevents a scheduler tick from claiming a newly created task before its blockers
have been added. Nothing in a proposal is live until the human decision and
`task_batch_commit` succeed; creates, edits, dependencies, controls and comments
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

The existing proposal preview presents the diff and comments. The pipeline's
human gate must await the exact proposal id and resolve `approve` or `approved`.
Then the pipeline commits it, or an authorized supervisor runs:

```bash
aq task batch-commit --proposal-id prop-abc123
```

Before a gate exists, `task_batch_update` replaces the complete payload and
recomputes its diff. Once gated, its payload and approval are retained: commit
the approved batch, then put subsequent changes in a subsequent batch. Concurrent
edits, pause/resume, claim churn and changed metadata do not invalidate approval
or require a new decision. Approved fields overwrite their current values;
fields outside the proposal keep their current values. Gated proposals cannot
be discarded. Removing an already-removed edge and resuming an already-resumed
task satisfy the approved intent without failing.

If a referenced task has disappeared, commit applies the remaining operations
and reports `missing_task_ids` and `skipped_operations`. Skipped entries identify
the operation list and its zero-based index in the approved payload. Creates
whose parent disappeared, their new descendants, and references to those skipped
creates are skipped too; children never silently become roots. An entirely
skipped proposal commits with an empty receipt. Inspect omissions before filing
follow-up work. Existing lifecycle and graph-integrity guards still apply; a
transaction failure stays ready with its approval and rolls back all changes.
Replaying a successful commit returns the original created ids and omissions.
The proposal read endpoint retains its receipt, and `task.change_set_committed`
records the actor, source, reviewed diff and receipt in the same transaction.
