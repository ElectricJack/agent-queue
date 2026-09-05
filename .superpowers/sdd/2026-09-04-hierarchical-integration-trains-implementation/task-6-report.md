# Task 6 report — receipt-driven parent completion and trusted review evidence

## Outcome

Implemented all five Task 6 milestones behind `hierarchical_integration_mode` values
`hierarchy|train`. Projects in `disabled` or `observe` mode keep the legacy lifecycle;
no project was enabled and no operator database was touched.

The implementation reserves a durable parent collection episode and one frozen
operation, derives parent readiness only from current immutable receipts/dispositions,
consumes stored trusted check evidence for exact aggregate verification, and permits
parent completion only through a private conn-owned guard. It also produces real
review approval/rejection evidence from a live reviewer attempt and an exact retained
remote Git snapshot, atomically with the corresponding task transition.

## Milestone 1 — schema, frozen policy, episode, and evidence

- Added revision `e4c6a8b20d31` over `c7a1e5d92f40`.
- Added nullable `projects.hierarchical_integration_policy`; checkpoint episode/current
  verification references; receipt disposition revision; operation verifier/route
  identity; parent episodes, normalized child dispositions, immutable parent
  verifications and exact verification-to-check links; and normalized operation to
  artifact `RESTRICT` pins.
- Added append-only guards for aggregate check evidence, episodes, verifications, and
  verification links. Task 4's review-evidence append-only guard is reused.
- Added full frozen ArtifactRef wire snapshots, stable playbook routes, required checks,
  repair policy, and optional nonempty primary/verifier profile and intelligence routes.
  No profile/model/check producer/default is inferred.
- Operation reservation validates the stored compiled artifact identity, snapshots the
  policy/route, and pins the artifact in the same transaction. Artifact GC excludes and
  rechecks operation pins.

## Milestone 2 — readiness, dispositions, verification, and completion guards

- Readiness reads the current direct-child set under the project hierarchy lock. Each
  child must have an exact live origin, terminal state, exact finished checkpoint head,
  and either one code receipt or one current normalized disposition receipt. Code
  receipts must form a contiguous chain from the immutable episode pre-collection head.
- Disposition changes increment only that child's revision and invalidate the parent
  generation/current verification. Old receipts remain immutable audit records.
- Aggregate verification accepts only stored evidence IDs matching operation, parent,
  generation, head, configured check-set version/producer, successful conclusion, and
  non-infrastructure classification. The exact evidence links and current verification
  reference are immutable.
- Generic/admin completion, force completion, legacy container settlement, generic
  resume, and orphan recovery cannot bypass an active parent episode. Only the private
  verified completion and verifier-wake tokens pass the canonical transition seam.
- The sealed-batch mutation guard is ancestor-aware: a descendant cannot reopen,
  reparent, change disposition, or otherwise mutate beneath a sealed member.
- `delivery.applied` collection events carry the persisted parent operation separately
  from the promotion intent identity.

## Milestone 3 — suspension, ownership handoff, verifier launch, and prime

- A managed parent worker close runs source-branch verification, records its actual
  clean pushed head, reserves/reuses its episode, and enters controlled `PAUSED` without
  a manual-pause snapshot or timer. It never invokes the legacy direct/main integration
  pipeline. The exact attachment is stopped/detached and returned to a reserved fence.
- A leaf close similarly skips legacy integration but advances its live completion
  checkpoint to the actual finished head; immutable `origin.base_sha` is unchanged.
- Collector-to-verifier handoff is readiness-gated and the guarded wake revalidates the
  exact current fence, verifier role/state, operation relationship, and manual holds.
  The live parent checkpoint advances to the collected head before launch.
- Session/workspace preparation and attachment propagate the server-resolved owner role.
  A real regression covers collector transfer, verifier wake, parent session attachment,
  stored aggregate verification, and guarded parent close. Neither parent leg calls the
  legacy integration pipeline.
- Initially branchless parents receive an originless, nonstructural persisted verifier
  delegate bound to the active operation and parent branch. Creation requires the frozen
  verifier route; missing routing emits a visible configuration blocker.
- Prime renders the exact receipt/readiness projection, pre-collection and aggregate
  heads, generation, dispositions/squashes, blockers, and configured required checks.
- The carried `ws_row` launch bug is fixed by initializing the row before the guarded
  lookup; a throwing workspace lookup now pauses cleanly instead of dereferencing an
  unbound local.
- Task 4's actual remote push is inside the existing ownership row exclusion and requires
  the current `collector` role. The command also validates the server-resolved persisted
  operation/batch collector. Terminal/reconciliation replay remains read-only and works
  after obsolete source/fence cleanup.

## Milestone 4 — trusted reviewer producer

- `ReviewEvidenceProducer` derives reviewer/final-reviewer subjects only from typed graph
  edges. Enabled final-review subjects must reduce to exactly one integration task.
- The pre-transaction snapshot requires the newest live reviewer session attempt and
  matching task/profile/agent/project identity, immutable source origin base, current
  source generation, and exact leaf completion head or current verified parent aggregate.
- Git facts come from Task 4's retained-repository resolver/fetch path. The configured
  remote branch must equal the pinned head and the producer records its tree OID.
- Approval/rejection revalidates attempt, graph, origin, generation, head, and parent
  verification while holding the hierarchy lock, then appends evidence and completes or
  reopens in the same transaction. Deterministic IDs make pre-commit crash retries safe.
- A rejection by an attempt suppresses approval when that same review task subsequently
  closes successfully. A later rejection sorts after and therefore supersedes an older
  approval for the same exact tuple.
- Disabled projects return before integration graph/Git observation and retain ordinary
  legacy close/reopen behavior. Administrative close cannot mint a managed approval.

The ordinary leaf-to-delivery proof is deliberately split by seam, rather than using a
parent fixture as a substitute: the real session close regression proves the leaf's
finished pushed checkpoint replaces the initial checkpoint while its origin base stays
immutable; `test_leaf_real_reviewer_approval_is_atomic_and_drives_promotion` then uses a
live reviewer attempt and real bare remote, atomically records the exact approval, and
feeds that stored evidence into Task 4 `PromotionService.prepare/push` to obtain the
delivery receipt.

## TDD RED evidence

Focused tests were written at each seam before its implementation. The material RED
observations were:

- `pytest -q tests/test_integration_parent_completion.py` initially could not import the
  new frozen policy/episode interfaces, then exposed missing receipt-chain,
  disposition-supersession, aggregate-evidence, and generic-completion guards.
- The focused PromotionService race test initially allowed an ownership transfer to
  complete between initial fence validation and the remote push.
- The session lifecycle regressions initially retained the leaf's filing checkpoint and
  routed a managed parent close through the legacy integration pipeline.
- The reviewer producer tests initially had no producer and Task 4 correctly failed
  closed with no applicable stored approval.
- While tightening the new session regressions, the test harness first lacked its Git
  facade (`AttributeError: '_Orch' object has no attribute 'git'`) and the parent fixture
  initially duplicated its origin. Those were fixture defects; after correction the
  product regressions remained green.

## GREEN verification

- Required parent run:
  `aq test tests/test_integration_parent_completion.py tests/test_hierarchy_settlement.py -x`
  — **31 passed**.
- Runtime/reviewer/promotion run:
  `aq test tests/test_session_commands.py tests/test_integration_review_evidence.py tests/test_integration_promotion.py -x`
  — **119 passed**.
- Hierarchy/contracts/ownership/GC/reopen compatibility run:
  `aq test tests/test_integration_contracts.py tests/test_integration_hierarchy.py tests/test_integration_ownership.py tests/test_playbook_artifact_store.py tests/test_reopen_with_feedback.py tests/test_review_reopen_cascade.py -x`
  — **81 passed**.
- Final affected-area run across the eleven files above:
  `aq test tests/test_integration_parent_completion.py tests/test_hierarchy_settlement.py tests/test_session_commands.py tests/test_integration_review_evidence.py tests/test_integration_promotion.py tests/test_integration_contracts.py tests/test_integration_hierarchy.py tests/test_integration_ownership.py tests/test_playbook_artifact_store.py tests/test_reopen_with_feedback.py tests/test_review_reopen_cascade.py -x`
  — **232 passed**.
- Post-sweep reviewer crash/retry additions:
  `pytest -q tests/test_integration_review_evidence.py` — **5 passed**.
- `POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres pytest -q tests/test_migration_parent_collection.py -m migration`
  — **2 passed** (SQLite and PostgreSQL).
- `ruff check <all changed Python files>` — **All checks passed**.
- `git diff --check` — clean.
- `/usr/bin/python3.12 -m alembic heads` — `e4c6a8b20d31 (head)`.

An attempted `aq test --aq-all-markers tests/test_migration_parent_collection.py -x`
collected zero tests under this wrapper's marker handling and explicitly reported that
nothing was verified. The single migration file was therefore run directly as permitted
by the repository testing rules, producing the two passing dialect arms above.

Process deviation retained for accuracy: during iteration, one two-file reopen command
was accidentally run with bare `pytest`; it was rerun via `aq test` and passed as part of
both the 81-test and 232-test sweeps.

An exploratory `tests/test_review_pipeline_e2e.py` collection failed before Task 6 code
ran because the existing fixture imports absent `src.playbooks.pipeline_compiler`. No
unrelated playbook compiler change was made; the real producer and command-adjacent
review seams are covered by the focused tests above.

## Exact migration evidence

Revision cycle tested on both dialects was:

`e4c6a8b20d31 (initialized head) -> c7a1e5d92f40 -> e4c6a8b20d31 -> c7a1e5d92f40 -> e4c6a8b20d31`

The SQLite arm used a new pytest `tmp_path/parent-collection-migration.db`. The
PostgreSQL arm used the supplied container on port `16833` and helper-created disposable
database:

`postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres_master_task6_parent_collection`

Inspection after each final upgrade confirmed all five new tables, nullable checkpoint
episode/current-verification columns, non-null operation episode identity, disposition
revision, verifier/route columns, and the unique `(parent_task_id, episode_id)` index.
Both dialect arms inserted an episode row and proved update/delete fails with the
append-only trigger. The PostgreSQL scratch database was dropped in `finally`; no
operator URL, protected database, or persistent interim stamped database was used.

## Files

- Schema/models: `migrations/versions/e4c6a8b20d31_parent_collection_and_review_evidence.py`,
  `src/database/tables.py`, `src/models.py`, `src/integration/models.py`, project and
  artifact query modules.
- Parent lifecycle: `src/integration/parent_completion.py`, `src/integration/hierarchy.py`,
  hierarchy/task/delivery query modules, integration contracts/commands, settlement,
  prime renderer/sections.
- Ownership/runtime: `src/integration/ownership.py`, `src/integration/promotion.py`,
  claim/session/task commands, orchestrator execution/workspace.
- Reviewer producer: `src/integration/review_evidence.py` plus the session-close and
  reopen-with-feedback hooks.
- Focused coverage: parent completion, review evidence, migration, promotion,
  hierarchy, settlement, contracts, and session tests listed by Git status.

## Self-review and concerns

- Aggregate check observation is intentionally absent and owned by Task 9. The consumer
  remains fail-closed until exact trusted evidence rows exist; tests seed those rows only
  to exercise validation.
- Task 7 repair stages, budgets/deadlines, repair delegates, and playbook routing are not
  implemented. Task 6 only freezes the route inputs and clean parent operation.
- Root train construction/promotion and aggregate forge observation were not added.
- The broad legacy-disabled proof is the affected-area suite plus the explicit disabled
  reviewer no-Git test. All new runtime branches require `hierarchy|train` and an exact
  checkpoint/operation relationship.
- No unresolved Task 6 product failure remains. The pre-existing review-pipeline import
  issue above is outside the authorized modules and did not affect focused verification.

The superpowers TDD and verification skills drove the RED-first seam tests and the final
evidence-before-completion checks; the written-plan skill kept implementation ordered by
the five prescribed milestones without expanding into Task 7, Task 9, or root trains.

## Fix round 1/5 — review findings at `fe55b81e`

All eight Important findings in `task-6-review.md` were addressed without changing Task 7:

1. Legacy settlement now suppresses auto-completion only for `hierarchy|train` projects
   with a non-null active episode. Disabled/observe projects keep legacy settlement even
   when a checkpoint row exists.
2. A guarded rejection/reopen of a successfully completed parent now advances generation,
   clears only the live episode/current verification projection, and preserves immutable
   episode/operation/verification audit. The next suspension reserves a new operation.
   Already-delivered unchanged children are retained only by immutable
   `integration_episode_receipt_acceptances` rows backed by the previous completed
   operation/verification and trusted Git ancestry. Carried code receipts are not replayed
   in the new squash chain. Unbound and unrelated historical receipts fail closed.
3. Producer checkpoint, episode reservation, and PAUSED transition now commit in one
   hierarchy-locked transaction. A stale claim rolls the whole transaction back, leaving
   neither a checkpoint episode nor an operation.
4. New code and disposition receipts persist `parent_operation_id` and
   `parent_episode_id`; the consumer requires the current binding or a validated immutable
   carry-forward link. Promotion finalization refuses a hierarchical parent with no active
   episode or a collector fence that is not the active parent operation.
5. Review snapshots record the latest exact-tuple evidence cursor. Commit revalidates that
   cursor plus the source's still-COMPLETED state under the project lock, so a competing
   rejection/reopen makes an older approval stale. A reviewer attempt that already wrote
   its own rejection may still close without minting an approval.
6. The unpublished `e4c6a8b20d31` revision now adds named RESTRICT foreign keys for
   checkpoint episode/current verification, parent episode task/repository, operation
   episode/verifier delegate, parent verification identities, receipt bindings, and
   carry-forward provenance. Metadata and migration definitions match.
7. `task_show` now exposes `integration_delivery`; a reopened parent with no active episode
   reports its current working generation/head, while an active episode exposes the full
   receipt readiness projection.
8. The real command-level regression now performs an actual clean pushed leaf
   `task_close`, an actual reviewer `task_close` evidence hook, and the real
   `delivery_promote` command. It verifies immutable leaf origin, updated completion head,
   command authorization/collector validation, and receipt operation/episode binding.

### Fix-round RED/GREEN evidence

- RED: `pytest -q tests/test_hierarchy_settlement.py::TestSettlement::test_disabled_checkpoint_without_episode_retains_legacy_settlement`
  — failed because the parent remained `IN_PROGRESS`; GREEN — **1 passed**.
- RED: `pytest -q tests/test_integration_review_evidence.py::test_approval_snapshot_is_stale_after_another_reviewer_rejects`
  — failed with `DID NOT RAISE`; GREEN — **1 passed** after tuple-cursor/source-state
  revalidation.
- RED: `pytest -q tests/test_integration_parent_completion.py::test_unbound_historic_receipts_do_not_satisfy_current_parent_episode`
  — returned `ready` instead of `waiting`; GREEN — **1 passed** after binding enforcement.
- RED during rollover: the extended guarded-completion regression returned `waiting`
  because a carried receipt was incorrectly replayed against the fresh pre-collection
  head; GREEN — **1 passed** after separating carried satisfaction from the new code chain.
- Atomic suspension/FK regressions:
  `pytest -q tests/test_integration_parent_completion.py::test_first_parent_checkpoint_reserves_one_frozen_episode_operation`
  and `::test_parent_identity_foreign_keys_reject_mismatched_episode_links` — **passed**;
  the stale-claim arm proves reservation/checkpoint rollback.
- Real command chain:
  `pytest -q tests/test_integration_review_evidence.py::test_leaf_close_review_hook_and_delivery_promote_command_end_to_end`
  — **1 passed**. Intermediate failures were test-fixture corrections (Workspace field and
  current session-attempt identity), not product-success substitutions.
- Completed-parent review rollover:
  `pytest -q tests/test_integration_review_evidence.py::test_parent_review_rejection_rolls_completed_episode_for_next_collection`
  — **1 passed**, including an unrelated historic rejection.
- Historical receipt rejection:
  `pytest -q tests/test_integration_parent_completion.py::test_receipt_bound_to_unrelated_historic_episode_is_rejected`
  — **1 passed**.
- Surface projection:
  `pytest -q tests/test_surface_commands.py::TestTaskShow::test_exposes_resumed_parent_delivery_projection`
  — **1 passed**.
- Promotion compatibility:
  `aq test tests/test_integration_promotion.py -x` — **28 passed**.
- Parent suspension/verifier lifecycle:
  the two focused `TestEndToEndOnFakeProvider` parent tests — **2 passed**.
- Fix-round final affected-area gate:
  `aq test tests/test_integration_parent_completion.py tests/test_integration_review_evidence.py tests/test_integration_promotion.py tests/test_hierarchy_settlement.py tests/test_session_commands.py tests/test_integration_hierarchy.py tests/test_surface_commands.py`
  — **214 passed**. The subsequent narrow self-review changes (missing-active-episode
  producer refusal plus stronger carry provenance filtering) were verified by **4 focused
  passing tests**; schema did not change after the dialect cycles below.

### Updated migration evidence

- SQLite:
  `pytest -q tests/test_migration_parent_collection.py::test_sqlite_parent_collection_upgrade_downgrade_upgrade -m migration`
  — **1 passed**.
- PostgreSQL:
  `POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/postgres pytest -q tests/test_migration_parent_collection.py::test_postgres_parent_collection_upgrade_downgrade_upgrade -m migration`
  — **1 passed**.

Both arms exercised `e4c6a8b20d31 -> c7a1e5d92f40 -> e4c6a8b20d31`, inspected the new
acceptance table, receipt columns, and named logical-identity foreign keys, and retained
append-only episode evidence. PostgreSQL used only the helper-created disposable
`postgres_master_task6_parent_collection` database, which the passing test dropped in
`finally`. No operator/protected database was read, stamped, or mutated.

### Fix-round self-review

- Aggregate forge observation remains deliberately absent (Task 9); readiness still
  fails closed without stored trusted evidence.
- Carry-forward is possible only from the latest successfully completed operation with
  an exact immutable verification and a positive server Git ancestry result. Consumption
  rechecks parent/repository/branch, child origin/head, current disposition revision, and
  the complete prior provenance tuple.
- Generic managed-parent COMPLETED guards, delivered-child immutability, sealed ancestor
  guards, and operator manual holds remain in force. Parent producer suspension touches
  only its owned source branch and does not invoke the legacy main/direct integration path.
- The reviewer-requested broad `ParentCompletion` decomposition remains deferred as
  directed for final review; this fix pass made only invariant-focused edits.
- No known Task 6 product failure or architectural conflict remains.
