# Parent repair heads after child collection

Child delivery receipts remain immutable. An authorized parent repair can extend
their aggregate with an append-only `parent_head_extensions` proof in its stage
dossier. Each edge binds the operation, episode, generation, repository, branch,
stage, original writer identity, before/after SHA and exact ordered commit range.
Readiness folds these edges alongside receipt edges. A bare stage subject, branch
tip or successful check never establishes an extension.
An edge retains its original generation when more children are filed in the same
episode; a future-generation edge or an edge from another episode is refused.

Normal publication-backed repair subject binding records an edge under the current
attached writer fence. Advancing the parent checkpoint clears its current aggregate
verification. Stage budgets, deadlines, policy, state and original receipts retain
their existing lifecycle. Verification and completion continue through the existing
exact-head checks and ownership controls.

`aq integration recover-parent-head OPERATION --head SHA` previews recovery of a
completed authorized repair whose older close recorded its commit range but omitted
the extension. Apply additionally requires the previewed episode, generation, stage,
current fence and an operator reason. Active or archived completed delegates are
eligible only with their latest passing completion containing that exact head,
matching canonical branch and repair origin, and the stage's exact subject and
recorded lineage. An older active delegate's empty commit list is eligible only
when its accepted-close metadata names that exact completion, audited session and
current claim epoch. A contradictory nonempty list or missing accepted-close
identity refuses recovery. The immutable completion record remains unchanged;
the stage's complete recorded range and published Git head still prove the repair.
Future repair closes pass their already-proven writer head directly to completion
recording, including when the base checkout lacks the canonical parent ref.
The original fenced `integration.repair_delegate_closed` outbox
audit must identify that stage and delegate; the current owner must hold a later
fence. Fetch and observe the actual origin ref, prove ancestry and the
complete recorded range, and require every receipt tip to remain an ancestor.

Recheck the database and remote proof under the project/operation/owner locks before
appending the edge and advancing the checkpoint. No Git push, new writer, episode,
generation or budget is allocated. Refuse active writers, pending external writes,
manual pauses, open gates, paused/draining projects and human-required operations.
Replay requires the same proof and current fence and leaves verification intact.
Recovery clears old verification on its first application; fresh exact-head
verification is required before completion. If the aggregate has no verifier yet,
the same transaction uses the ordinary readiness projection to file its verifier
and enqueue the fenced handoff. It preserves repair state and budgets and refuses
if readiness cannot be projected; replay creates no duplicate verifier or event.
Live recovery and daemon deployment
belong to the supervisor/operator, not the implementing worker.

## Operator acceptance for azure-vault-92

Deliver the code through normal integration and update/restart the running daemon
with the operator's supported procedure. No migration is required. The supervisor
capability is merged additively on startup/profile reload. Before recovery, inspect
the stage-12 close audit, latest passing completion (active or archived), current
branch owner, and the verifier's session/workspace. A live verifier must be handled
through the supported owner controls; this command refuses to detach it.

```bash
aq integration recover-parent-head bcab5af6-bf48-49d8-9491-c003e927e3ac \
  --head 9928015dd5e269be00ad1fd03366ca2adb5cb237 --json
```

Require `would_recover`, episode `cfb14ee1-6cc1-4b2a-a362-16a4c1581946`,
generation `2`, stage `12`, and receipt head
`55d4a049fafc94bdc3ad9bbf05ecbc597c83129f`. Inspect the reported fence,
deadline, attempts and completed repair identity. Run the preview's exact
`apply_command`. A refusal names the unresolved prerequisite; do not substitute
database edits, a reset or an exemption.

After `recovered`, delivery readiness must report the supplied repair head while
all nine original receipts retain their exact bytes. The checkpoint must retain
its episode and generation, with no current aggregate verification. Resume the
existing normal verifier flow using fresh exact-head evidence and close through
the standard completion gates. These live acceptance steps are supervisor/operator
work; the implementing worker's checks use disposable PostgreSQL and local Git.

## Integration with parent subject ownership

The operator recovery path holds the shared parent-engine exclusion across proof,
Git read-back and application. A parent assigned to the reconciler refuses legacy
recovery, including previews, without changing checkpoint or repair-stage state.
This uses the existing operation-scoped parent engine guard and preserves the
exclusive cutover protocol; feature flags alone are not ownership evidence.
