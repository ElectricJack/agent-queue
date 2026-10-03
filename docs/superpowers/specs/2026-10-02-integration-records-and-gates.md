# Integration records and gates: phase 2 primitives 14–19

Task `amber-meadow-15.3` (`p2_gates`), implementing `rev-agile-ridge`
revision 2, §3.2–§3.7. Builds on the published `vivid-willow.1`
foundation (`9ad7f4ead`). This task preserves that prerequisite commit;
its modules cannot compile on the assigned source base without it.

## Records

`RecordsPrimitives` binds `record_receipt`, `record_attempt` and
`record_decision` in `PrimitivePorts`. Each also has a caller-transaction
`*_on` form. The existing append-only `integration_subject_journal` is
authoritative; no new schema is needed.

Receipts bind the exact source task/head to the subject's exact target
repository/ref/head/generation. They require a server-owned `delivery_reader`
returning `DeliveryProof`, never playbook-supplied proof. The reader establishes
ancestry or content equivalence, or explicit policy authorization for a skip.
Skipped receipts require an active same-project repair task and
`skip_authorized: true`. `ReceiptProjection` carries the existing parent
operation/episode or batch/member/revision bindings from the delivery adapter.
The task-side `task_delivery_receipts` projection and journal commit together.
Replays return the original receipt; a later generation never rewrites it.

Attempts require a server-owned `attempt_reader` returning a durable
`AttemptObservation`: trusted producer, exact repository/ref/head/generation,
conclusive green/red, current writer identity, and proof that this writer
published the head during the current budget. Missing proofs, infrastructure,
cancelled/superseded observations, future timestamps and pending outcomes
consume nothing. Count once per exact head and budget ordinal, including
across process restarts and repeated CI runs. Counting and journaling commit
together. Policy, not this primitive, decides exhaustion and the successor.

Decisions retain the pinned policy artifact, observed subject version, rule,
facts digest, primitive, and shadow/active mode. Their domain identity excludes
retry clocks so replay does not append another decision.

## Gates and waits

`GatePrimitives` binds `wait`, `gate` and `eject`. Waits clamp to the subject's
`max_wait_seconds` and preserve a current gate. Neither waiting nor answering
a gate clears a product pause.

Gate definitions and answers are immutable action journal entries. The existing
`gates` row is a dashboard projection. Legacy generic resolution or expiry of
that projection cannot approve or release a subject. Each gate requires either
a policy-selected default and timeout or an explicit `no_default` annotation.
A retry reuses its definition without extending its deadline. Changing the
question/choices while a gate holds refuses with `existing_human_gate`.

Only a CommandHandler adapter with verified human evidence may call
`answer`/`answer_on`; they are not playbook primitive ports. A human answer
cannot arrive at or after the timeout, cannot change an existing answer, and
cannot authorize a different head/generation/artifact. Consuming a human
approval after its timeout also refuses. Policy defaults apply only to an
unanswered timed gate; a `no_default` gate has no automatic answer.
Answering wakes the subject; a subsequent `gate` visit checks product holds
before returning the immutable choice for policy to apply.

## Ejection and kind adapters

`EjectionPlan` records exact member head, pinned policy, disposition, explicit
permission to skip required work when applicable, a live repair task, and the
evidence establishing membership and policy authorization. Ejection refuses a
held gate, manual pause, inactive project, unstopped writer, or publication
already started. It retains the previous exact subject head and audited reason,
applies the member/disposition projection, and returns the subject to `building`
with a fresh generation and immediate due time in one transaction.

The parent/root owners provide these server-owned callbacks:

- `ejection_reader(conn, subject, EjectArgs) -> EjectionPlan | None`: read
  authoritative membership under the caller's locks, return `None` for an
  absent member, and derive authorization from the pinned policy. It makes
  no mutations.
- `apply_ejection_on(conn, subject, EjectArgs, EjectionPlan) -> None`: remove
  membership and apply the chosen task disposition, using this connection.
  Preserve required work and its repair relationship. It must neither commit
  nor open a separate transaction. Exceptions roll back audit and projection.

These callbacks preserve the existing schema and leave kind-specific membership
in `p2_parents`, as ownership requires. Missing adapters refuse, rather than
pretending a member was removed. The legacy `IntegrationControlService.eject`
opens a separate transaction and must not be nested as this callback.
No generic task completion, abandonment or hierarchy mutation occurs here.

## Wiring and operator handoff

This phase ships primitives and adapter contracts. They are inert until the
separate `p2_wiring` task binds them behind CommandHandler; it supplies the
trusted git/CI readers, parent/root membership adapters and verified human
principal evidence. Never take `verified_human`, `trusted`, `allowed`, or
proof objects from playbook arguments. Interpret the returned gate choice
through the pinned policy, including any explicit human hold/rejection.

Mutating ports lock the subject's observed version. Attempt counts, wait,
gate scheduling and ejection bump it. Wiring must reload before its final
schedule CAS, or compose the `*_on` methods in its visit transaction and use
the returned subject version. Decision/receipt appends only advance the journal
pointer. Human answers wake without invalidating a visit.

No feature flag, production cutover, migration, command registration or API
change is introduced by this task. Keep the existing event rules and
feature-off rollback until the phase's production evidence and approval gates
are met. The already-delivered `keen-stone-14` recovery remains the recovery
implementation.

## Verification

`tests/test_integration_records_and_gates.py` covers exact receipt identity,
proof refusals, replay/restart, concurrent attempt counting, excluded CI outcomes,
immutable and stale gate answers, timeout/default behavior, generic-resolution
bypass, bounded waits, product holds, policy authorization, required-work skips,
repair provenance, and rollback when a disposition projection fails.

Use the disposable PostgreSQL test service, preserving the worker database
refusal variables. Run the focused file, then the affected subject/ejection/
repair and selection checks under `aq test`. Regenerate the selection catalogue
for the new test module. Production adapter and cutover evidence belongs to
`p2_wiring`, which depends on this task.

Final check: `aq test tests/test_integration_records_and_gates.py
tests/test_integration_subjects.py tests/test_integration_eject.py
tests/test_integration_repair.py tests/test_selection_catalogue.py --tb=short`
passed 287 tests, including all 36 final primitives tests. Changed Python paths
passed Ruff; `python scripts/generate-selection-catalogue.py --check` passed.
The earlier focused run passed 31 tests before the final five regressions were
added. All database checks used the disposable service on port 5534.
