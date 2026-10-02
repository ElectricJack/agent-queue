# Recover a preserved parent conflict resolution

Task `calm-journey-95`. `resume` requires a human-required operation; detached
rebind requires an active stage without preserved progress. Neither can consume
a completed candidate after a debug stage expired under `supervisor_recovery`.

`aq integration recover-preserved-repair OPERATION --intent ID --candidate SHA`
is an operator-only preview. Apply additionally requires `--apply --stage N
--released-fence N --reason TEXT`. The candidate and intent are exact identities,
not branch names. The preview reports the target, source, tree, parents,
preserved ref, release audit, remaining attempts and expired deadline.

This is completion reconciliation, not another repair attempt. It publishes only
the already completed two-parent merge preserved by supported owner recovery.
An expired clock stays expired; attempts, deadline, deadline event, stage state
and operation state are never reset, nor is any stage allocated. Exhausted attempt
budgets, human-required operations, open parent/source/delegate gates and manual
pauses refuse recovery with an actionable reason. A further conflict or repair
requires its own supported decision; this command cannot authorize more work.

Under project and row locks, prove the current parent episode and generation,
escalated operation, expired debug stage and exact no-progress incident, sole
unsuperseded conflict intent, completed/current source and applicable review,
frozen policy/route, stopped and detached delegate, and the owner recovery audit
that preserved the supplied SHA. The released branch fence must still match that
audit. Refuse any unresolved external write, reused writer or moved subject.
Fetch the retained repository and prove origin's preserved ref equals the audited
SHA, the parent still equals the intent's expected target, the source branch is
still the frozen source, and the candidate has exactly target/source parents and
its reported tree. Reuse the normal resolution lineage proof and retain receipts.

Apply repeats the proof, takes a fresh collector fence from the released owner,
and reserves the exact resolution with its original authoring session/workspace
from the release audit. The stage dossier records the operator, reason, release
proof, original budget and collector fence. A committed pre-push marker precedes
the single expected-old Git push. All gates, subject and ownership are rechecked
under the same locks before that push. A retry with a marker and old target
refuses as ambiguous; it never blindly retries Git. An exact candidate already
on the target can be reconciled without another push. Finalization uses the
ordinary receipt/outbox path. The collector retains ownership for normal child
collection and parent verification; recovery supplies no check or approval.
The exhausted stage and blocked former delegate remain as audit history.

Regression coverage uses PostgreSQL and real local Git: preview/apply/replay,
unchanged budgets and no extra stages, exact parents/tree, stale candidate/intent/
fence/subject, moved target/source/preserved ref, gates, exhausted attempts,
live or reused writer, ambiguous intents/writes, and interrupted publication.

## Deployment and acceptance for the reported incident

Publish this change through normal AQ integration. The operator must then update
the daemon's running checkout and restart with `aq restart --no-dashboard`.
There is no database migration. The supervisor's additive capability sync loads
`integration_recover_preserved_repair` on startup/profile reload. An old daemon
cannot execute the new command, even when the CLI already has its help entry.

Preview the preserved stage-9 candidate (promotion intent IDs have `intent-` prefix):

```bash
aq integration recover-preserved-repair bcab5af6-bf48-49d8-9491-c003e927e3ac \
  --intent intent-67f48f4b-ed47-5af3-96ed-1cebb5909354 \
  --candidate 91e8d994009fed7dc214b50ce833213473bbba0d --json
```

Require `would_recover` and inspect the reported release audit and fence. Expected
parents are `e2aea1d98e00486460575d30f74d69a0b1cf6f3d` and
`29aaea269ad6338b5cf3ba44b0daf0e725ea73cf`; expected tree is
`4b6a136007ad9a01be1969c05732e7efba0bf796`. The preserved ref must be
`aq/preserved/54018a31-a6db-4918-be01-91b5148eea36`. Run the preview's exact
`apply_command`; it binds the observed stage and released fence instead of
assuming that the historical fence number is still current.

After `recovered`, use these read-only acceptance commands:

```bash
aq system integration-delivery-readiness --task-id azure-vault-92 --json
aq task explain --task-id azure-vault-92.6 --json
aq task show azure-vault-92.6 --json
```

The readiness response must include the `azure-vault-92.5` receipt with the
frozen reviewed source `29aaea269ad6338b5cf3ba44b0daf0e725ea73cf`, before-SHA
`e2aea1d98e00486460575d30f74d69a0b1cf6f3d`, after-SHA
`91e8d994009fed7dc214b50ce833213473bbba0d`, and the same parent operation.
The parent can still wait for other children; this is not an aggregate-check
pass. After the normal scheduler sweep, `.6` must have no remaining delivery
blocker and be claimable, or already have been claimed by its own worker.
If another gate or dependency remains, report its exact reason rather than
closing or bypassing it. These live acceptance checks belong to the operator;
the implementing worker must not manipulate or claim these other tasks.

The failure had two parts: rollover substituted a `stage-exhausted` trigger for
the unique conflict, then expiry made normal writer publication impossible.
The rollover regression (`test_debug_stage_at_conflict_target_resolves_under_its_own_fence`)
already covers the prevention shipped by `sound-forge`. Recovery regressions
cover the remaining lifecycle: supported owner release clears a stopped pool
claim and checkout lock, its audit binds the preserved candidate, and adoption
returns collection to a fresh collector without opening a new stage. A passing
merge is therefore only code delivery; the live receipt and `.6` readiness above
are the operational acceptance.
