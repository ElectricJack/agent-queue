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

Recovery may instead name the current published aggregate H' after a completed
repair proved H. The stage and latest completion must still prove H with the
exact fenced close audit; a later collection never substitutes for that audit.
The stage generation may precede the current generation in the same episode.
Readiness must fold the repair edge and every current child receipt to H', and
Git must prove H is an ancestor of H' and the complete ordered range H..H' is
covered by those receipt edges. Missing, foreign, ambiguous or incomplete
receipt chains and unpublished/unrelated heads refuse recovery. A detached
collector reservation may retain its confirmed workspace: prove that workspace's
canonical ref is published ancestry and that its parent checkouts are clean and
have no retained writers. Recheck this local proof before apply. An existing
verifier must be an idle matching task, never a completed task silently re-armed.
The preview reports both the repair head and the receipts covering its advancement.
Apply records
the original repair edge, preserves all receipt bytes and repair budgets,
settles the closed repair stage, returns the operation to active verification,
and enqueues a fresh exact-head verifier handoff. It does not complete the
operation or parent until fresh trusted verification passes. A replay retains
that new verification and enqueues no duplicate handoff. The advanced-head path
requires an aggregate verifier even when the parent already has a past session.

## Operator acceptance for vivid-quest-44

After review, delivery and daemon deployment, preview the current aggregate:

```bash
aq integration recover-parent-head bdbdd609-7f16-42fc-942d-3dedc1e2adeb \
  --head 0c61aada8cb782c3a6a0d4150c62ff55feb47869 --dry-run --json \
  > /tmp/vivid-quest-44-recovery.json
```

Expect `would_recover`, stage `1`, repair head
`4f458b9c6708725ca9d84d4e164de3c8fdf14cce`, and the current collection
receipt IDs covering advancement to the supplied aggregate. Inspect the episode,
generation, fence, attempts, deadline and completion identity. If the branch has
advanced again, repeat the preview with that fully published head; never replace
the original stage/close proof. A missing receipt, live holder or dirty/divergent
confirmed workspace is a remaining prerequisite, not permission to bypass proof.

After reviewing the preview, run its exact `apply_command`, or equivalently:

```bash
aq integration recover-parent-head bdbdd609-7f16-42fc-942d-3dedc1e2adeb \
  --head 0c61aada8cb782c3a6a0d4150c62ff55feb47869 --apply \
  --episode "$(jq -er '.data.episode_id' /tmp/vivid-quest-44-recovery.json)" \
  --generation "$(jq -er '.data.generation' /tmp/vivid-quest-44-recovery.json)" \
  --stage "$(jq -er '.data.stage' /tmp/vivid-quest-44-recovery.json)" \
  --fence "$(jq -er '.data.fence_token' /tmp/vivid-quest-44-recovery.json)" \
  --reason 'Reconcile the published authorized parent repair'
```

Expect `recovered`: the closed stage is passed, the operation is active, and the
normal verifier handoff names H'. The checkpoint retains its episode/generation
and clears old verification. Receipt bytes, original repair identity, deadlines
and attempt counts remain unchanged. Fresh trusted H' verification and the
standard completion gates must still pass. Replay with the current fence returns
`already_recovered` without clearing that verification or queuing another handoff.
The implementing worker does not run these commands against the operator database.
No knowledge migration is delivered or renumbered by this recovery.

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

## Finished collection stages with historical receipt gaps

The same recovery command may reconcile an active/escalated collection whose
last conflict-resolution delegate completed but whose stage remains active.
The final head can already be covered by a trusted conflict-resolution receipt.
When its delegate-close outbox audit is absent, require the exact committed
promotion intent and trusted receipt to agree on operation, episode, stage,
delegate, repository, branch, source identity, resolution tree, commit range,
session, instance, workspace, fence and observed push. An empty completion still
requires its accepted-close identity; a contradictory audit or completion refuses.
This proof settles resolution bookkeeping and never supplies green check evidence.

Missing edges between immutable child receipts may be recovered only from a
completed stage bound to the exact gap head. Each gap independently requires the
original fenced close audit, latest passing completion (or its exact accepted
empty close), recorded commit range and Git ancestry to the current published
parent head. Unattributed commits and summary text are insufficient. Preview
reports every gap or refuses. Apply rechecks all stage/delegate/owner facts and
remote ancestry, appends each edge to its originating stage, and projects ordinary
verifier readiness in one transaction. Both completed collection recovery and
receipt-advanced repair recovery require a dedicated matching idle verifier,
including parents with prior sessions, and a fresh exact-head handoff event. The
confirmed former workspace is checked before preview and again under apply.
No episode, generation, attempt budget,
deadline, receipt, completion or human gate is replaced. Replay is idempotent.
Fresh exact-head verification and the configured parent review path still own
completion and delivery. Live apply follows supervisor review and deployment.

## Resume collection after a no-progress escalation

An escalated collection may own a later conflicted child intent while its current
stage ended without progress. `reopen-collection` also accepts this narrowly
proved state: the current failed/expired stage has its exact supervisor-recovery
incident, every old stage is terminal, every old delegate is settled and detached,
and the operation's detached collector reservation has the conflict's exact fence.
Exactly one current conflict must name a completed child's current head. No old
stage may already be bound to this conflict; retrying an exhausted conflict needs
its existing recovery path, not another attempt budget.

The remote tip must equal the conflict's expected target and retain the episode
base and every collection receipt. A confirmed former parent checkout must have
no unpublished ref, dirty canonical checkout or retained writer. Open human gates,
manual holds, ambiguous writes, verifier work and stale episode/route evidence
refuse this additional path. Apply repeats the database and Git proofs before
transferring the collector fence and opening one fresh, writerless stage under the
operation's frozen debug policy. Existing stage budgets, incidents and receipts
remain byte-identical. Normal dispatch files a new delegate to resolve and publish
the conflict; this command accepts no delivery and changes no Git ref. A retry
cannot allocate another stage once the new stage is live or bound to that conflict.

For quick-glacier-88 / operation c6ee6a67-66ac-424c-bbb5-bbf532f18348:

```bash
aq integration reopen-collection quick-glacier-88 --json
aq integration reopen-collection quick-glacier-88 --apply \
  --head 47760887c51dd4d2f2b08ce05ff85e5c94536056 \
  --reason 'Resume the later child conflict after the detached no-progress stage'
```

Expect `would_reopen` with that operation, the current episode, the sole current
child conflict and proposed frozen stage budget. Use the full head the preview
reports if the branch has changed. Apply returns `reopened`, advances the collector
fence and creates the next stage bound to that intent. Normal dispatch owns the
conflict repair. Collection receipts for prerequisites are produced only by the
ordinary fenced resolution/delivery path; a missing receipt is never fabricated.
Any failed proof is a named refusal requiring inspection before a live apply.
