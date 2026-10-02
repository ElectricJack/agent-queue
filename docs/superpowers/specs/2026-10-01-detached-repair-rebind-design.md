# Rebind a detached parent repair stage frozen on an unpublished head

Task `eager-orbit`, 2026-10-01. Operational recovery for one failure mode of the
hierarchical parent repair loop. It adds an operator control; it changes no
automatic policy, budget rule or human gate.

## Failure mode

A continuous-policy parent repair escalates through debug stages. When a stopped
writer's workspace is retained for the next stage, `_retained_debug_handoff`
binds the stage subject to that workspace's local `HEAD`. If the writer never
published it, every later stage copies the same head into `starting_sha`
(`_activate_debug_on`). Admission then refuses each delegate before its claim:
`_hierarchy_repair_start` requires the published parent branch to descend from
the frozen starting commit, so the claim fails `slot_reset_failed` ("repair
branch no longer descends from its frozen starting commit") and the delegate
ends `BLOCKED` for manual retry.

Debug stages also carry the trigger `stage-exhausted:<operation>:<ordinal>`.
(Since task `sound-forge`, a debug stage starting at the open conflict's
`expected_target` carries that intent instead; a frozen unpublished head never
does.) The conflict that started the repair is still an open promotion intent, but
`integration-resolve-conflict`, `push-conflict-resolution` and
`aq integration rebind-repair` all require the stage trigger to name that
intent, and `rebind-repair` also requires an attached writer. No supported
control could reconnect the stage to its conflict.

Live incident: operation `bcab5af6-bf48-49d8-9491-c003e927e3ac` (parent
`azure-vault-92`). Stage 1's retained handoff bound `93da7393`, the reviewed tip
of child `azure-vault-92.3`, published only on `aq/azure-vault-92.3`. The parent
branch is still `2590e79f`, the after-SHA of its only receipt and the
`expected_target` of the open conflict intent `intent-19b44073-…` for that same
child. Stages 2 to 7 never admitted a writer.

## Control

```bash
aq integration rebind-detached-repair OPERATION_ID                  # dry run
aq integration rebind-detached-repair OPERATION_ID --apply \
    --stage N --remote-head SHA --reason "..."
```

Command `integration_rebind_detached_repair`. Only a local operator or the
project's live named supervisor may run it (`integration_operator`). The dry run
reports the proof. Apply requires the stage ordinal and published head that the
dry run reported, plus a reason. Apply re-proves everything under locks and
reports `changed` if any identity moved.

### Durable proofs (under the project lock, operation, stage, parent, intent, owner and delegate row locks)

- The operation is a `parent` operation in state `active` or `escalated`.
  `human_required`, `completed` and `cancelled` are refused: human decisions
  stay with `integration resume` / `abort`.
- The active stage is `active`, is a debug stage (`ordinal > 0`) whose trigger is
  exactly `stage-exhausted:<operation>:<ordinal-1>`, and is bound to a
  `repair_delegate`. It has no retained workspace, no preserved progress and no
  `supervisor_recovery` incident. Its subject is exactly its frozen starting
  commit (the writer made no progress).
- Budget: `now < deadline_at` and `attempts` below the frozen policy's limit.
  The control never renews a deadline, attempt count or deadline event and never
  creates a stage.
- The parent is `PAUSED` with its checkpoint `awaiting_children` in the
  operation's episode. The project is in `hierarchy` or `train` mode.
- Exactly one open intent of this operation targets the parent branch. It is in
  state `conflict`, is fenced by the operation's collector, has no resolution
  and is not superseded. Its source is a `COMPLETED` child of the parent.
  `RepairService._start_context_on` accepts it as the trigger at its
  `expected_target`, so the frozen policy, route and artifact identity still match.
- The branch owner is the stage's delegate in role `repair`, `reserved`, with no
  session or workspace. The delegate is the operation's generated repair task
  for this branch and is `PAUSED`, `READY` or `BLOCKED` with no assigned agent.
  If it is `BLOCKED`, its `needs_attention` is `slot_reset_failed`. No session
  for it is live or holds a claim phase. No workspace is locked by it. Neither
  it nor the parent has an open gate.
- `IntegrationRecoveryControls._ambiguous_writes_on(...,
  allow_reserved_delegate=True)` finds no ref mutation, candidate resolution,
  attestation, non-terminal promotion or other unreleased writer for the
  operation.

### Git proofs (retained promotion store, after fetching every remote head)

- The published parent head `R` is present and equals the conflict intent's
  `expected_target`.
- The frozen head `S` is a commit reachable from a fetched remote ref. `R` is a
  proper ancestor of `S`, and `S` is not reachable from `R`, so `S` was never
  published on the parent branch. The remote refs that contain `S` are reported.
- Every current receipt's `after_sha` is an ancestor of, or equal to, `R`, so
  published deliveries stay in the parent's history.

Apply repeats `ls-remote` inside the locked transaction and refuses if `R` moved.

### Effect

One compare-and-swap on the stage row: `starting_sha = R`, trigger = the
conflict intent, `current_subject = {parent, generation, R}`, success fields
cleared. `started_at`, `deadline_at`, `deadline_event_id`, `attempts`, the
operation state and the branch owner (fence included) are unchanged. The
dossier's `starting_sha`, `branch_sha`, `trigger_id`, `current_conflict` and
`receipts` name the new subject. A `continuations` entry for the intent is appended,
the same marker a conflict continuation writes, so the parent playbook's
operation-key `start` replay reports `already_started` and its stage-zero
dispatch alias selects this stage. The unpublished `repair_commits` move into an
appended `detached_rebinds` record. That record keeps the previous starting
SHA, trigger and subject, the remote refs containing `S`, the receipt IDs, the
owner row and fence, the delegate's prior status, the unchanged budget, the
principal and the reason.

The delegate is forced to `PAUSED` with its description re-rendered. After
commit, the ordinary `RepairService.dispatch` re-checks the fence and makes it
`READY`. The orchestrator's continuation replay does the same if that call
fails. The next claim prepares at `R`. The writer's prime shows the
"Current parent conflict repair" intent and fence. It resolves and publishes
through `integration-resolve-conflict` and `push-conflict-resolution`, which
fast-forward the parent branch from `R` under the delegate's fence.

Nothing is pushed, reset, reserved or received by this control. No delivery
receipt, intent state, gate or ownership changes. A replay after apply reports
`already_rebound`.

## Interaction with `keen-bridge-30`

That change stops a debug stage from continuing when its subject matches the
preceding stage's subject. A rebound stage's subject differs from its
predecessor's. If it exhausts without progress, one successor can therefore be
allocated, and that successor then stops on the unchanged head. If an
unchanged-head stage has already ended under `supervisor_recovery`, this control
refuses it as an exhausted budget. Continuing from that point needs a human
decision.

## Not in scope

- Preventing the failure. A retained parent handoff should never bind an
  unpublished head, and debug stages should keep the open conflict intent as
  their trigger. Both are filed as follow-up work. Task `sound-forge` did the
  second: `RepairService._activate_debug_on` binds the sole unresolved
  conflict whose `expected_target` is the exhausted stage's subject.
- Batch (root) operations. They already preserve unpublished progress on
  `aq/preserved/*` refs.
