# Task 7c implementation report

Status: implemented and verified on 2026-09-05

Base: `6d8f86f4`

## Outcome

Task 7c now binds immutable current-invocation provenance only around actual
`LiveCommandExecutor` dispatch, rejects managed promotion provenance whose current
artifact differs from the operation's frozen ArtifactRef, admits new integration
events through the operation's stable owner route while preserving current
enabled/ready/scope authorization and sibling fanout, and keeps normalized operation
artifact pins visible to garbage collection.

The disabled `hierarchical-delivery` policy ships through the existing offline
reviewed-fixture path. Its fourteen rules route terminal-child and receipt readiness,
child filing, parent checkpointing, promotion/conflict repair, bounded repair evidence
and deadlines, ready verifier handoff, aggregate verification, guarded completion,
and exact resolution reconciliation. A repair-delegate close is deliberately only a
lifecycle terminal. No source seeding/import creates an activation.

## Slice 1 — live invocation and provenance

RED:

- `pytest -q tests/test_command_executor.py -k LiveInvocationContext`
  - `4 failed`; the invocation module/accessor did not exist.
- The promotion-provenance case then failed because the persisted fields remained
  `None` rather than the current `promotion-run` / `promotion-step` / attempt 7.

GREEN:

- `pytest -q tests/test_command_executor.py`
  - `32 passed`.
- `pytest -q tests/test_command_executor.py -k 'promotion_provenance or promotion_rejects'`
  - `2 passed` after adding the operation-artifact mismatch invariant.

Implementation evidence:

- `PlaybookInvocation` is immutable and contains current run, dispatch, ArtifactRef,
  rule, step, and attempt. The context manager resets its token in `finally`, so
  nesting, adapter exceptions, and cancellation restore the outer value or `None`.
- Only live adapter invocation is wrapped; preview and shadow never acquire context.
- Promotion provenance reads the current invocation, never delegation ancestry. For a
  managed operation the invocation playbook/artifact identity must equal the frozen
  operation binding. This provenance is not an authority grant. LOCAL/SERVICE creation
  remains possible only through the already-required explicit configured route and
  does not fabricate a current run.

Commit: `3f5139cd` (`feat(playbooks): bind live invocation provenance`).

## Slice 2 — frozen route admission and event contracts

RED:

- `pytest -q tests/test_integration_outbox.py -k 'new_operation_event_uses_frozen_owner or disabled_frozen_owner or operation_pin'`
  - The owner reactivation cases initially produced two failures: a later operation
    event selected the new owner artifact, and disabling the owner still allowed a
    sibling consumer to acknowledge the event.
- The strict GC recheck characterization failed with `set()` after the artifact row
  was removed under SQLite foreign-key-off simulation; the operation pin had not been
  consulted by the file-side recheck.
- Exact schema inventory assertions initially failed because the integration events
  exposed only project/operation identity.

GREEN:

- `aq test tests/test_integration_outbox.py tests/test_integration_contracts.py tests/test_event_schema_registry_validation.py tests/test_command_executor.py`
  - `482 passed, 11 warnings in 19.4s`.

Implementation evidence:

- The operation lookup returns the stable `(playbook_id, scope, scope_identifier)`
  owner route, frozen ArtifactRef/hash, and audit-only construction activation ID.
- Only a manifest-less new event performs this admission. It still iterates current
  enabled, ready, scope-authorized destinations; the stable owner route substitutes
  the frozen artifact bytes and records the current authorizing activation ID.
- A missing/disabled/unready/out-of-scope owner or unavailable/nonmatching frozen
  artifact keeps the event pending even when a sibling matches. Unrelated consumers
  retain current-artifact fanout. Existing frozen-manifest replay is unchanged.
- Operation pins are included in both database collection and file rechecks.
- Event schemas expose exact typed command inputs, including the required
  `integration.resolution_push_observed` triple
  `(project_id, operation_id, promotion_intent_id)`. That event binds
  `promotion_intent_id` to reconcile `intent_id`; it is never delivery/check proof.

Commit: `15795310` (`feat(playbooks): pin hierarchy event owner routes`).

## Slice 3 — disabled reviewed policy and real flow

RED:

- `pytest -q tests/test_integration_parent_completion.py -k failed_child_readiness_exposes_frozen_disposition_policy`
  - `2 failed`; `on_failed_child` was absent from readiness.
- `pytest -q tests/test_integration_parent_completion.py -k branchless_parent_creates_exact_routed_verifier_delegate_before_handoff`
  - `1 failed`; the ready fact lacked exact target/fence/successor inputs.
- `pytest -q tests/test_integration_parent_completion.py -k transfer_owner_replay_after_crash_still_wakes_verifier`
  - `1 failed`; transfer replay returned success while the verifier remained PAUSED.
- `pytest -q tests/test_integration_promotion.py -k clean_promotion_is_retained_attributed_pushed_and_reconciled`
  - `1 failed`; `delivery.applied` lacked receipt/source/target/repository/branch route
    data needed by the reviewed rule.
- `pytest -q tests/test_hierarchical_delivery_playbook.py -k reviewed_hierarchy_routes_lifecycle_without_invented_success`
  - `1 failed`; the initial reviewed artifact lacked explicit terminal child readiness
    routes. The final artifact adds separate guarded `task.completed` and `task.failed`
    rules using hydrated immediate-parent identity.

GREEN:

- `pytest -q tests/test_hierarchical_delivery_playbook.py -x`
  - `4 passed`; the real engine and real `CommandHandler` run readiness, the frozen
    failed-child DecisionStep, and `gate_create` for both `block` and `ask` policies.
    Two distinct failed delivery events yield one open human gate through stable
    failed-parent `await_id`; block leaves the parent PAUSED and the run failed.
- `pytest -q tests/test_event_schema_registry_validation.py -x`
  - `416 passed`.
- `pytest -q tests/test_playbook_v2_import.py -x`
  - `18 passed, 16 skipped`; every reviewed fixture imported without activation.
- `aq test tests/test_integration_repair.py tests/test_hierarchical_delivery_playbook.py -x`
  - Required Task 7 gate: `38 passed, 11 warnings in 16.03s`.

The ready fact now freezes exact target/current token/next verifier owner and is
emitted only on the operation/generation state projection. Repeating readiness after
fence movement reuses the byte-identical durable event. Transfer replay after a crash
between ownership mutation and wake runs `wake_verifier` before returning success.

The fixture bundle contains exact source/artifact/hash/empty diagnostics/review
manifest. Static shipped-source and import inventories include it; live-source digest,
canonical bytes/hash, strict model, event refs, exact command fingerprints/outcomes,
profiles, and non-wildcard capability review all validate offline. Vault installation
is write-if-absent and the source declares `enabled: false`; neither seeding nor import
creates an activation. Task 11 remains the only operator activation path.

Commits:

- `299b11d3` (`feat(playbooks): ship disabled hierarchy delivery policy`)
- `1cd1ae7f` (`fix(playbooks): route terminal child readiness`)

## Final affected-area verification

- `aq test tests/test_hierarchical_delivery_playbook.py tests/test_integration_repair.py tests/test_integration_outbox.py tests/test_command_executor.py tests/test_integration_parent_completion.py tests/test_integration_promotion.py tests/test_integration_contracts.py tests/test_event_schema_registry_validation.py tests/test_default_playbook_v2_artifacts.py tests/test_playbook_v2_import.py -x`
  - Final rerun after all fixture/schema changes: `687 passed, 16 skipped, 11 warnings
    in 33.14s`.
- `task7c_py=$(git diff --name-only 6d8f86f4..HEAD -- '*.py'); ruff check $task7c_py && git diff --check 6d8f86f4..HEAD`
  - `All checks passed!`; whitespace check exited 0.

One intermediate two-file focused invocation was accidentally run as bare pytest
(`79 passed`) instead of through `aq test`. It was bounded to the hierarchy fixture
and reviewed-artifact files; the complete 703-item affected-area selection was then
rerun through `aq test` as required, producing the final result above.

Warnings and skips are existing environment/dependency behavior: `pkg_resources`,
legacy `zope` namespace declarations, Python `audioop` through Discord, and the
existing unavailable PostgreSQL parameterizations. No Task 7c behavior was skipped.

## Overall Task 7 deliverables reconciliation

- Primary timeout with no CI event, debug timeout, simultaneous green/timeout,
  repeated infrastructure failures, stale stages, and children added during repair:
  supplied and focused-tested by Task 7a's durable repair service.
- Two stages and budgets: Task 7a persists absolute primary/debug deadlines, counts
  conclusive aggregate attempts once, excludes infrastructure/focused diagnostics,
  never resets clocks/attempts, requires explicit configured higher debug routing,
  and blocks debug handoff until old-writer stop is proven.
- Execution delegates and dossiers: Task 7a binds the exact stage repair/verifier
  task without a structural child edge, persists pinned subject/receipts/manifest,
  failures/logs/hypotheses/commands/budgets/repair commits, and retains the exact
  workspace through single-writer handoff. Task 7b/its reviewed fix preserves the
  server-minted session-instance principal; Task 7c does not synthesize claims.
- Parent conflict resolution: Task 7b reserves immutable exact resolution identity,
  checks session/stage/workspace/fence and linear Git proof, pushes with exact lease,
  reconciles remote proof into the original receipt, and supports crash/replay and
  later-episode carry-forward. The Task 7c rule consumes only its lifecycle push fact
  to invoke trusted reconcile; no OID or success proof is invented.
- Hierarchy policy routes: the reviewed artifact explicitly maps terminal child and
  receipt events to readiness; ready suspended parents to exact verifier transfer and
  wake; delivery conflict to primary repair; primary exhaustion to debug dispatch;
  deadlines to the public timeout outcomes with typed action separate; successful
  aggregate CI to exact parent verification; verified generation/head to guarded
  completion; failed child to frozen `block` terminal or deduplicated human gate.
- Recursive exhaustion: Task 7a blocks only a failed parent subtree and retains the
  project lease on root exhaustion; it does not add bisection, ejection, sibling full
  CI, a third automated stage, or a redundant post-main audit.
- Artifact/provenance: Task 6/7a freezes a full ArtifactRef and normalized operation
  pin. Task 7c enforces current-invocation equality for managed provenance, stable
  owner-route admission for each later event, current authorization, immutable
  accepted-manifest replay, sibling fanout, and GC retention.
- Aggregate completion: Task 7a/7b require immediate-parent delivery receipts,
  contiguous exact heads, configured aggregate checks, current generation/head
  verification, and guarded completion. Repair close remains lifecycle-only.
- Required explicit Task 7 test command and the larger final affected-area gate both
  pass. The source/fixture remains disabled and no operator cutover was performed.

No overall Task 7 checklist item is intentionally unshipped in Tasks 7a, 7b, and 7c.
Overall completion still depends on the controller's independent phase review, as the
brief requires.

## Files and self-review

- Invocation/provenance: `src/playbooks/invocation.py`,
  `src/playbooks/executors/command.py`, `src/integration/promotion.py`.
- Frozen admission/retention: `src/playbooks/runtime.py`,
  `src/database/queries/integration_state_queries.py`,
  `src/database/queries/playbook_artifact_queries.py`.
- Policy/runtime facts: `src/integration/parent_completion.py`,
  `src/database/queries/integration_delivery_queries.py`,
  `src/commands/contracts/integration.py`, `src/commands/integration_commands.py`,
  `src/event_schemas.py`, `src/vault.py`.
- Shipped policy: `src/prompts/default_playbooks/hierarchical-delivery.md` and
  `tests/fixtures/playbooks/v2/hierarchical-delivery/`.
- Focused tests: command executor, outbox, contracts/schema registry, promotion,
  parent completion, import/reviewed fixture, and hierarchy playbook suites.

Self-review verified that current invocation differs from parent ancestry; invocation
context never grants authority; stable owner route does not fall back to a reactivated
artifact; accepted manifests remain replayable; block/ask policy remains authored;
human gate resolution cannot fabricate delivery; ready-event identity survives fence
movement; repair close invokes no success-bearing command; and no activation, LLM
compiler call, success stub, duplicate engine, external push/PR, daemon start, operator
database change, protected environment change, or Task 6 cleanup was introduced.

## Concerns

No known implementation blocker or correctness concern. Deferred dependency warnings
and Task 6 module-size triage remain outside this task's scope.
