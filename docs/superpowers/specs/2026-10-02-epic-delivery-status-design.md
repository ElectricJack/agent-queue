# Epic implementation progress vs delivery status

Task noble-nexus-42 (user-approved dashboard change).

## Problem

An epic card read `5/5 done` and `PAUSED` while its delivery was blocked. Both
signals were true and both were misleading. The count measured implementation,
not delivery. `PAUSED` was the integration-owned checkpoint hold of a managed
parent, not a person's decision. Two live examples:

* `calm-grove-25`: 5/5 children are complete, the checkpoint is `verifying` and
  readiness reports `receipt_missing` for `calm-grove-25.5`. That fix child
  completed after the aggregate froze for verification, so nothing collects it.
* `azure-vault-92`: 9/9 children are complete and readiness is `ready`. The
  verifier is `READY` but the claim frontier excludes it
  (`origin_not_materialized`) because repair stage 13, a finished task, still
  owns the branch reservation.

## Decision

Add one read-only display projection, `src/integration/epic_delivery.py`. It is
not a state machine. It persists nothing, never changes a task's stored
lifecycle, and authorizes nothing. It classifies facts that existing reads
already establish:

| Fact | Existing source |
|---|---|
| Collection readiness, receipt blockers | `ParentCompletion.readiness_on` |
| Claim eligibility of a READY verifier or repair writer | `claim_frontier_predicates` / `FRONTIER_PREDICATE_DETAILS` |
| Live operation, active stage, verifier binding | `integration_repair_operations`, `integration_repair_stages` (`ACTIVE_OPERATION_STATES`) |
| Branch reservation | `integration_branch_owners` on the checkpoint's `(repository, branch)` |
| Root delivery | `task_delivery_receipts` under `_root_delivery_receipt_conditions` |
| Train batch | `integration_batch_members` + `integration_batches` (`_ACTIVE_BATCH_LIFECYCLES`) |
| Operator hold | `manual_pause` metadata, `hold:*` labels |
| Human approval | open `human` gates on the epic, operation `human_required`, batch `human_blocked` |
| Active work | a session bound to the worker task, not stopped, with activity inside `sessions.lease_ttl_seconds` |

The classifier is a pure function from those facts to a result, so every state
is tested from fixtures without a daemon.

## Result shape

`EpicDeliveryStatus` (API model in `src/api/models/task.py`):

* `state`: one of `implementing`, `queued`, `integrating`, `verifying`,
  `blocked`, `awaiting_approval`, `paused`, `delivered`, `unknown`,
  `not_tracked`.
* `label`: the sentence a card shows, for example `Integration blocked - final
  fix not collected` or `Verification blocked - branch handoff required`.
* `display_status`: the short word that replaces the stored status on an epic
  card. `Paused` appears only for an operator hold.
* `hold`: what a stored `PAUSED` means: `operator`, `integration`, `backoff`,
  or none.
* `reason`, `remedy`: a concise blocker reason and, where one exists, the
  guarded dry-run command that diagnoses it.
* `responsible`: the agent, task or system that has to act next
  (`kind` is `session`, `task`, `system`, `operator` or `supervisor`).
* `since`: the timestamp of the last meaningful progress the evidence shows.
* `links`: the task, operation or batch the evidence points at.
* `evidence`: `current`, `stale`, `unavailable` or `untracked`.
* `implementation_completed` / `implementation_total`: direct children.

## Classification order

The first rule that matches wins.

1. **Delivered**: a code receipt binds the epic's current head. For a root
   epic, the receipt is on the default branch; for a nested epic, it is in its
   parent's branch. A receipt for another head is `unknown`/`stale`, never
   delivered.
2. **Paused**: `manual_pause` or a `hold:*` label.
3. **Awaiting approval**: an open human gate, an operation in `human_required`,
   or a batch in `human_blocked`.
4. **Implementing**: some direct child is not terminal.
5. **Not tracked**: the project's integration mode is neither `hierarchy` nor
   `train`. Implementation progress still shows. A `PAUSED` epic without an
   operator hold reads as waiting, not paused.
6. **Train/hierarchy delivery**:
   * A live writer session on the active stage's repair task is
     `integrating`. A live verifier session is `verifying`. A live session with
     no activity inside the lease TTL keeps its state with `evidence: stale`.
   * A failed child is `blocked`.
   * In a frozen (`verifying`) checkpoint, a completed child without a receipt
     is `blocked` (`final fix not collected`). The remedy is
     `aq integration reopen-collection <epic>`. While collection is open, the
     same fact is `queued` for the collector.
   * Other readiness blockers (`receipt_chain`, `origin_mismatch`,
     `failed_aggregate_head_unchanged`) are `blocked`.
   * A READY verifier that the frontier admits is `queued`. One the frontier
     excludes while another owner holds the branch is `blocked` (`branch
     handoff required`). Any other exclusion is `blocked` with the predicate's
     own detail. A `BLOCKED` or `FAILED` verifier is `blocked`.
   * A READY repair writer is `queued`. An expired or failed stage with nothing
     live is `blocked`.
   * A completed, verified root waits for the train (`queued`). In an active
     batch, `building`, `testing` and `promoting` are `integrating`, because
     the train is running; `sealing` and `sealed` are `queued`.
   * An epic that completed outside the train is `not_tracked` (`Completed
     outside the train`). That covers no checkpoint, a cancelled collection,
     a root never train-verified, or a parent that finished without
     collecting it. The train never owed such an epic a receipt, so its
     silence is not a blocker.
   * Missing evidence for an epic still in flight (no checkpoint, an episode
     with no live operation, an archived verifier, a readiness error) is
     `unknown` with `evidence: unavailable`.

Readiness is read only for epics whose children are all terminal and that hold
a live operation, at most 32 per read, so the statements per graph response
stay bounded by the visible epics that can be in delivery. Children, operator
holds and approval gates come back as aggregates of one statement. An
untracked project therefore pays one extra round trip per tiles request
(`tests/perf/test_layout_api_statements.py`). The train evidence for
`hierarchy`/`train` projects is a constant number of batched statements in the
same transaction. A failure is reported per epic as `unknown` with
`evidence: unavailable` and never fails a graph or task read.

## Surfaces

* Graph layout (`tiles`, `list`, `node`): `LayoutNode.delivery` for every
  visible node with children, next to `phase_hold`.
* `get_task`: `delivery_status` for a task with children. The command contract
  (`GetTaskValue`) is unchanged, so no reviewed playbook bundle is stale.
* Dashboard: container headers and collapsed epic cards show
  `N/M tasks complete` beside an icon-and-text delivery badge. The task detail
  pane and page show an *Implementation & delivery* section with the reason,
  the responsible party, the time since progress, the remedy and links. Task
  controls (pause, resume, stop) are unchanged and still act on the stored
  status.

## Coordination

The projection reads train tables only through the helpers named above, so a
train refactor that changes those helpers changes this view with them. If a
refactor adds a state, extend the classifier and its fixtures, not a stored
column.
