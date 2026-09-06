# Task 10c — Root routes, atomic release, and independent cleanup

This is the final independently reviewed Task 10 phase. Begin only after Task 10a and 10b pass
review and Task 9b2 fix 1a/fix 1b are independently approved. It composes the reviewed substrate,
attestation resolver, root finalizer facts, and existing Task 8/9 services into the disabled root
train playbook. It owns no new Git construction or main-promotion algorithm.

## Required incoming state

The implementation must consume these exact reviewed facts:

- Task 9b2 finalization has authenticated exact-main proof, every immutable member receipt, a
  completed root operation, terminal `integration_batches.lifecycle='promoted'`, independent
  `cleanup_state='pending'`, and stable operation-bound `integration.root_delivered`,
  `integration.batch_promoted`, and `integration.cleanup_requested` events with the actual
  `operation_id`;
- Task 10a retains an active schedule request plus at most one first-catch-up tuple and supplies
  bounded service/selectors/outbox delivery;
- Task 10b supplies exact candidate-tree attestation and the mandatory Task 9b2 resolver;
- reviewed Task 8 `TrainService`, Task 9b1 `CandidateService`, Task 7 `RepairService`, and Task 9b2
  `RootPromotionService` remain the sole domain authorities.

If any terminal fact is absent, stop with the concrete missing seam; never emulate finalization,
fabricate receipts, or release a merely `cleanup_pending`/`promoting` batch.

## Outcome and file ownership

Create:

- `src/integration/release.py` — one hierarchy-locked release/catch-up CAS.
- `src/integration/cleanup.py` — normalized cleanup reservation, bounded retry, authenticated
  exact deletion, PR reconciliation, and owned-worktree detach.
- `src/prompts/default_playbooks/root-integration-train.md` — prose-only, disabled root policy.
- `tests/fixtures/playbooks/v2/root-integration-train/source.md`, `artifact.json`,
  `artifact.sha256`, `diagnostics.json`, and `manifest.md` — checked-in reviewed V2 fixture.
- `tests/test_root_integration_playbook.py` — compiler/engine/real-command route acceptance.
- `tests/test_integration_cleanup.py` — cleanup/release/restart/concurrency acceptance.
- One Alembic-generated revision named `normalized integration cleanup`; generate its ID from the
  then-current single head and never rewrite Task 9b2 or 10a migrations.
- `tests/test_migration_integration_cleanup.py` — dual-dialect live-state guards and U-D-U.

Modify only as required:

- `src/database/tables.py` — normalized cleanup table and named constraints/indexes.
- `src/database/queries/integration_reconciliation_queries.py` — implement the 10a pending-cleanup
  page against the normalized table.
- `src/database/queries/integration_schedule_queries.py` — conn-owned release/catch-up CAS helpers.
- `src/database/base.py`, `src/database/adapters/sqlite.py`, and
  `src/database/adapters/postgresql.py` only for new cleanup query methods.
- `src/integration/service.py` — install typed candidate/CI/rebuild/root-intent/release/cleanup
  handlers while preserving the reviewed bounded/overlap/error lifecycle.
- `src/git/github_app.py` and `src/git/manager.py` — isolated App-auth expected-old exact ref delete
  and owned-worktree cleanup only.
- `src/commands/contracts/integration.py` and `src/commands/integration_commands.py` — strict
  subject-only build/CI/rebuild/release command adapters.
- `src/playbooks/definition.py` and exact integration event schema tests only if the reviewed
  Task 9b2 event registration needs the root playbook's additional routing facts.
- `src/vault.py`, `tests/test_shipped_playbook_sources.py`, and
  `tests/test_default_playbook_v2_artifacts.py` — shipped disabled source/inventory/fixture checks.

Do not edit `.github/workflows/tests.yml` or attestation policy in this phase except to fix a direct
regression exposed by the root-route acceptance test and approved as a 10b correction.

## Strict command surfaces

Add and register only these missing design commands, using the existing `CommandResult` envelope,
named outcomes, service authorization, and receipt projections:

```python
class IntegrationBuildCandidateArgs(CommandArgs):
    batch_id: str = Field(min_length=1)

class IntegrationCIEvidenceArgs(CommandArgs):
    batch_id: str = Field(min_length=1)
    revision: int = Field(ge=0)

class IntegrationReleaseArgs(CommandArgs):
    batch_id: str = Field(min_length=1)
```

Commands carry durable subject identity only. The server derives project, repository, operation,
current revision, candidate/default-branch SHA, lease, fence, claim, frozen policy/artifact, and CI
evidence after authorization. Represent rebuild as an `integration_build_candidate` outcome route
that invokes a server-only adapter with the locked batch's current revision: the adapter reads the
authenticated current default-branch head and passes both values internally to the reviewed
`CandidateService.rebuild`. Do not register a second public rebuild command, and no playbook/caller
SHA is authority. Reuse every reviewed domain result/outcome and expose a typed failure for stale/
wait/configuration/human paths; never convert unknown provider/Git results into success.

`IntegrationReleaseResult` is frozen/strict and has outcomes
`released|already_released|empty|wait|stale|invariant_error`, with `project_id`, `batch_id`, the
released `request_id`, optional `catchup_request_id`, and actual `operation_id`. Only `released`,
`already_released`, and `empty` classify as successful.

## Atomic release contract

`IntegrationReleaseService.release(batch_id, now)` performs one `db.immediate()` transaction and
locks in this order: hierarchy project, project, batch, current candidate, project lease, root
operation/current stage, root intent/root-main mutation, complete ordered members/results/
reservations/receipts, schedule, then catch-up state. No provider/Git awaits occur inside.

Before any write, require exact authenticated final-main shipping; all member receipts; completed
operation and committed root intent; terminal `lifecycle='promoted'`; and no unresolved candidate,
repair, handoff, attestation publication, or root-main mutation. Consume the reviewed
Task10b publication reservation rather than inventing another exclusion mechanism.
Then affected-row CAS deletes/releases the exact project
lease, clears the active schedule request only if it matches the batch request, and updates
`last_completed_sweep_at`.

If one catch-up tuple exists, the same transaction increments `request_sequence` once, creates
`integration-sweep:<project_id>:<sequence>`, moves the first catch-up trigger/timestamp into the
new outstanding request, clears catch-up, and enqueues exactly one byte-stable
`integration.sweep_due` event. Concurrent/replayed release returns the canonical same result and
cannot emit twice. No catch-up leaves no new request. Terminal empty batches retain Task 8's
immediate no-lease/no-cleanup release behavior and are canonical replays.

Cleanup never gates lease release. Conversely, release is forbidden until exact main delivery and
all receipts are durable.

## Normalized cleanup contract

Create `integration_cleanup_items`, keyed by immutable `(batch_id, kind, identity)`, with:

- exact `project_id`, canonical `repository_id`, numeric repository identity and `full_name`;
- `kind: source_pr|audit_pr|remote_ref|local_ref|worktree` and immutable target identity;
- exact expected SHA for every ref/worktree and batch/revision/receipt identity for comments;
- `state: pending|retryable|complete|conflict|failed`, nonnegative `attempts`, `next_attempt_at`,
  `last_error`, `created_at`, `updated_at`, and optional terminal timestamp;
- a named all-or-none target check, state/attempt checks, unique domain key, foreign key to the
  original batch, and a partial ordered index on `(next_attempt_at, batch_id, domain_key)` for
  pending/retryable rows.

Materialize the complete cleanup set idempotently from the terminal batch and its immutable
members/publication/receipts. Source PR action adds a batch/receipt/SHA comment then closes the PR;
audit PR action reconciles delivered/closed status; ref action deletes only when an authenticated
read equals `expected_sha`; worktree action detaches/removes only a daemon-owned retained worktree
whose recorded batch/revision/head all match. Use repository-bound App identity and never
checkout-controlled `origin`.

Implement remote deletion through the existing isolated Git App transport using an atomic
expected-old lease and an empty source refspec. An authenticated read followed by GitHub's
unconditional REST delete is insufficient: the ref may move between those calls. Keep authenticated
reads for reconciliation and identity checks, return canonical already-absent when missing, and
refuse a moved ref as `conflict`. Local refs use Git's expected-old update-ref deletion in the
recorded daemon-owned repository; never delete a checked-out or foreign-owned branch.
Include both local and remote integration refs and eligible source refs under frozen retention
policy. Lost delete/PR responses
are reconciled by authenticated read/lookup before retry. Backoff and terminal attempt count come
from the batch's frozen `policy_snapshot.cleanup`, not current project configuration.

Always reject deletion of the designated default branch, regardless of a malformed
stored cleanup/source identity or matching expected SHA. Per-item execution is
exclusively claimed with rowcount-checked CAS; concurrent services cannot both POST
the same PR delivery comment. Use a stable receipt-bound comment marker and
authenticated reconciliation after a lost response, with no blind duplicate POST
while the prior write is unresolved. Reuse the reviewed durable claim patterns;
provider writes never occur under a DB lock.

A moved ref or foreign worktree is a visible terminal conflict, never force-deleted. Retryable
provider failures preserve pending work with bounded backoff. Cleanup failure never changes
promoted lifecycle/final-main/receipts, reruns CI, reopens promotion, reacquires the lease, or blocks
a later train. When every item is terminal, CAS batch `cleanup_state` from `pending` to `complete`
or `conflict` as the aggregate projection.

Migration downgrade must refuse with a clear batch/item diagnostic while any cleanup item or
non-complete terminal batch cleanup state exists. After drain, downgrade drops only 10c objects and
preserves Task 9b2's terminal promoted/receipt/event constraints.

## Disabled root playbook

Ship `root-integration-train.md` with `enabled: false`, declared policies only, and no compiler or
LLM extension. Its explicit routes are:

- due -> `integration_seal`;
- empty -> `integration_release`;
- sealed -> `integration_build_candidate`;
- conflict or red -> existing bounded primary repair;
- current complete green -> `integration_promote_main` through Task 10b attestation;
- base moved -> frozen `on_main_moved` choice: rebuild or wait;
- exhausted primary -> debug, exhausted debug -> human;
- `integration.cleanup_requested` -> idempotently materialize/advance normalized cleanup;
- `integration.batch_promoted` -> `integration_release` independently of cleanup progress, while
  `integration.root_delivered` remains immutable audit/routing evidence.

The playbook cannot take SHA, repository, lease, fence, claim, CI evidence, or cleanup identity
from prose. All active routes use the operation's frozen artifact/configuration after activation is
disabled or replaced. Build the fixture with the existing V2 compiler/tooling, record diagnostics
and digest, and review the artifact against the source. `ensure_default_playbooks` discovers the
source mechanically; update its documentation/tests without adding a second hard-coded inventory.

## TDD slices and acceptance

1. RED/GREEN each strict command through the actual `LiveCommandExecutor` and domain service:
   empty/one/many build, current green CI, conflict/red, rebuild/wait, main promotion, stale/
   configuration/human outcomes, subject-only arguments, and no caller authority fields.
2. RED/GREEN release two-ordering concurrency on SQLite and PostgreSQL: finalizer-first then
   release, release-first waiting/failing closed, exact CAS loss, replay, no catch-up, first
   catch-up only, concurrent tick, and crash before/after event insertion. Assert one released
   lease and one new sweep event/request.
3. RED/GREEN cleanup materialization and each action: source/audit PR replay, exact ref deletion,
   already absent, moved ref, lost response, foreign worktree, owned worktree, retry/backoff/
   exhaustion, restart, and aggregate completion/conflict. Assert zero CI/attestation/main calls.
4. RED/GREEN actual V2 engine event-to-command routes for every bullet above, including
   primary-to-debug-to-human, frozen rebuild versus wait after mutable policy changes, disable/
   activation replacement after sealing, operation-bound event artifact provenance, and outbox
   crash replay.
5. RED/GREEN shipped source and reviewed fixture: prose imports/compiles without runtime-only
   shortcuts, fixture matches byte-for-byte, default installation is disabled/idempotent, and an
   operator-owned existing file is never overwritten.
6. RED/GREEN end-state restart: active ownership, pending root intent, expired repair stage, lost
   notification, terminal promoted batch with failed deletion, later train release, and no
   duplicate delivery/PR/comment/ref/cleanup side effect.
7. Exercise fresh SQLite and a uniquely named disposable PostgreSQL database through upgrade,
   seeded cleanup/catch-up downgrade refusal, drain, downgrade, re-upgrade, and the release versus
   cleanup concurrency orderings. Never touch `postgres`, `integration_test`, the operator DB, or
   worker DB environment variables.

Required final gate:

```bash
aq test tests/test_integration_service.py tests/test_root_integration_playbook.py \
  tests/test_integration_cleanup.py tests/test_integration_candidates.py \
  tests/test_integration_ci.py tests/test_integration_main_promotion.py \
  tests/test_integration_outbox.py tests/test_default_playbook_v2_artifacts.py -x
```

Run changed-Python Ruff, playbook compiler/authority/fixture checks, migration syntax/head checks,
and `git diff --check`. Record exact RED/GREEN/final/migration evidence, files, commits, self-review,
and all Task 10 deliverables reconciliation in `task-10c-report.md`; commit runtime/tests/migration/
fixture first, then report. No whole suite or worker-count increase.

## Binding exclusions

No new candidate builder, CI trust algorithm, root-main promotion algorithm, receipt finalizer,
compiler replacement, LLM policy, polling-only agent, ejection, bisection, speculative next train,
live network/credentials, protection changes, operator activation/config/DB mutation, daemon start,
push, PR, Task 11 controls, or Task 12 E2E. Keep the root playbook and train mode disabled until
the separately authorized cutover.
