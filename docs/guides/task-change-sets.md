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


## Scoped edits to held ingestion batches

The assigned live fidelity reviewer, or a project supervisor, can update a held
batch with `task_batch_update`. Supply `proposal_id`, `expected_revision` and
`operations` instead of the ordinary proposal `payload`. Every task reference
must name an active batch member (its stable member key or task id), or a new
leaf's `tempId` in the same update. No member may have an active claim.

```json
{
  "proposal_id": "prop-example",
  "expected_revision": 1,
  "operations": [
    {"kind": "rewrite", "task_id": "api", "changes": {"acceptance": ["Verify the approved API contract"]}},
    {"kind": "reparent", "task_id": "api", "parent_id": "core"},
    {"kind": "rewire", "edges": [{"from": "docs", "to": "api", "reason": "Documents the API output"}]}
  ]
}
```

Supported kinds are `rewrite` (`changes`), `reparent` (`parent_id`), `rewire`
(`edges` / `remove_edges`), `add_leaf` (`task`), `split` (`task_id`, `tasks`,
`reason`), `combine` (`task_ids`, `task`, `reason`), `move_to_root`
(`delivery_targets`), and `retire` (`task_id`, `reason`, optional
`replacement_task_id`, `satisfied_by`, `rewire`). New leaves carry the same
source mapping, outcome, acceptance and constraints as ingestion tasks and
require a structural parent or explicit operational placement. Retirement
preserves history and rewires typed dependencies through the obsolete lifecycle;
it records no successful implementation. References outside the batch and
profile/provider/model pins are refused.

The final graph and automatic checks run in the same transaction as every write.
A hard finding rolls everything back with `validation_failed`. Structural edits
bump the revision, recompute the canonical digest and supersede earlier review
receipts. Title, priority, tags and whitespace-only description edits preserve
the revision and usable receipts. The response includes the current revision,
digest, membership and before/after diff; `check_results.last_diff` retains that
diff. An applied scope answer permits editing a `needs_scope` batch again;
the update keeps the graph held for review and release.
