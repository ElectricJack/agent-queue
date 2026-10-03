# Parent episode subject adapters

Task `amber-meadow-15.1`, rev-agile-ridge revision 2 §§3.2, 3.4 and phase 2.
Prerequisite: subject and read-only observer contracts published at `645a693d2`.

The additive bridge pins `integration_subjects.parent_episode_id` to the existing
parent episode. A nullable column permits pre-cutover rows; a RESTRICT foreign
key, unique episode index and immutable binding trigger prevent reassignment.
The episode's original collection generation supplies the natural subject key.
The subject's current generation and exact head can advance independently.
Reopening verification increments the checkpoint generation while retaining the
episode, as implemented by keen-stone-14. Receipt episode IDs and heads never
change as a consequence of that increment.

`ParentSubjectAdapter.ensure_on(conn, task_id, policy, max_wait_seconds)` is an
idempotent transaction-owned bridge for command owners. It validates parent,
repository, branch, episode and operation identity. A repeated call returns the
original subject and its policy, schedule and engine. It defaults to `legacy`;
creation does not transfer authority, reopen work or dispatch a verifier.

`ParentDatabaseObservationReader` extends the shared snapshot inside its
read-only repeatable-read transaction. It reads receipts, dispositions,
acceptances, verification identities and evidence, completion records,
recovery audits, archived verifiers and gates. Eligibility is supplied by the
existing `ParentCompletion.readiness_on`, including source-head equality,
rework cutoffs, disposition revision, contiguous trusted receipt chain and
explicit carry-forward proof. An acceptance row alone is insufficient.

`ParentIntegrationObserver` reads that snapshot once and returns
`ParentSubjectFacts`, an extension of `SubjectFacts`. The normal primitive port
and JSON decision binding remain compatible. Parent fields include:

- Exact episode/operation binding and current checkpoint head/generation/state.
- Child task status, pending collection, dispositions and readiness blockers.
- Code/noop/skipped receipt lineage, selection and explicit acceptance details.
- Current verification and history, exact completion/evidence bindings, failed
  aggregate marker and reopened collection state.

A moved checkpoint produces `parent_checkpoint_moved`; a newer episode produces
`parent_episode_superseded`. A missing binding remains unknown. Historical
receipts retain their original episode, source head and target head. Failed
completions remain observable after recovery clears the current verifier.
Human gates and manual pauses remain binding holds. Observation never performs
recovery or records a successful attempt. Events accelerate visits; a due visit
can discover completed children and verifier failure without an event.

A committed child receipt may advance the trusted aggregate before the legacy
checkpoint is updated. The observer exposes the aggregate and checkpoint heads
separately and reports `parent_collection_head_moved`; the visit owner refreshes
the subject with its version check before verifying the new head. A generation
increment is reported separately from an actual failed-verification reopen.

For records/ejection primitives, the existing `integration_child_dispositions`
and `task_delivery_receipts` tables remain authoritative. Resolve the operation
from `(subject.task_id, subject.parent_episode_id)` while holding the subject's
version lock; disposition and audit writes must share that transaction.

Operator handoff: migration `a00000000058` is additive and idempotent; only the
operator applies it to the daemon database. Parent reconciler wiring should use
the bridge and observer above. No production cutover, legacy deletion, playbook
activation or daemon restart is performed by this task. Feature-off rollback
continues to use the legacy collection and keen-stone-14 recovery unchanged.
