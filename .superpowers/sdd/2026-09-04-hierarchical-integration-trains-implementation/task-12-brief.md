# Task 12 requirements

Read `task-12-copy-fixture-preflight.md` in this artifact directory for the exact
current copy-table dependency inventory and the two legal-at-insert receipt fixture
corrections. Refresh the inventory after Task11; the preflight is not the final schema.

E2E safety preflight: main read docs/guides/e2e-swarm.md, e2e-common.sh, relevant
e2e-env.sh, e2e-daemon.sh, and e2e/dbsetup.py. Scripts allow AQ_E2E_HOME/PORT/API_URL,
E2E_PG_HOST/PORT/USER/PASSWORD/DB_NAME overrides. Use a fresh mktemp directory, free
loopback API port and unique database on our disposable PostgreSQL container, provider
fake. Never use/reset default ~/.agent-queue-e2e or shared agent_queue_e2e database.
Initialize the fresh directory without --reset. Do not run aq start or alter protected
AQ_DB_SCOPE/AQ_DATABASE_URL/AGENT_QUEUE_DB. The isolated script launches python -m src.main
with its explicit generated config; confirm configured DB resolves to the scratch DB
and no other inherited override redirects it before launch. For stop, confirm recorded
PID cmdline contains BOTH src.main and this exact generated config (script only checks
src.main). Cleanup only this run's exact validated resources; no Tier2/live LLM sessions.

Update from Task5: full empty SQLite historical replay passed via existing
test_missing_fk_migration.py::test_upgrade_head_is_clean_sqlite (controller inspected
test: nonexistent tmp_path DB then command.upgrade head). Fresh empty PostgreSQL replay
through c7a1e5d92f40 also passed with exact command/setup in task-5-report.md. Current-head
historical proof gap is resolved through c7; do not spend time reconstructing the earlier
unretained failure. Final-head replay remains a relevant final migration check as more
revisions land. Do not describe the old failure's cause as proven.

Migration proof follow-up: Task4 b91 had SQLite+PG current-schema→prior→b91→prior→b91
cycles and append-only guards pass, but full empty-DB historical replay was not proven.
An observed task_dependencies.dep_type ordering failure lacks retained traceback and
baseline-only reproduction, so do NOT claim it pre-existing. Add a focused fresh scratch
replay to prior f02a4a4a3010 and final head, retaining exact failures/successes. No operator
DB access. If prior fails before any feature migration, document exact baseline evidence;
fix feature-induced failures in scope, report unrelated historical defect accurately.

## Global Constraints

- Only one root integration lease is active per project; every batch names one designated target repository.
- Every non-empty sweep uses an ephemeral integration branch, including a single root PR.
- Every eligible root at the snapshot is included; there is no batch cap, ejection, bisection, or speculative next train.
- Children branch from and deliver to their immediate parent. Parents verify the delivered aggregate before completion.
- Batch membership never changes after sealing.
- `main` advances only from the expected base SHA to the exact full-CI-tested candidate SHA.
- No redundant full-CI audit after ordinary promotion to `main`.
- One primary repair stage, one higher-intelligence debug stage, then a human; duration and attempt limits are configurable at both root and parent boundaries.
- Playbooks own policy; deterministic core contracts enforce identity, ownership, idempotency, and delivery evidence.
- Use `aq test` beyond a single test file. Never increase worker counts or run the whole suite during implementation. Ruff only on changed Python files.
- Never migrate the operator database, change worker DB environment variables, or run `aq start` from a worker slot. Generate migrations against scratch infrastructure and exercise them only in test databases.
- Read both repository instructions and this design before executing a task. Changes remain flag-disabled until the operator performs the documented cutover.


## 3. Shared types and protocol

All IDs are strings, timestamps UTC epoch seconds, Git OIDs validated hexadecimal strings, and repository identity is the configured canonical repository ID rather than a checkout path or arbitrary remote alias. Workspace kinds resolve to this ID before authorization. This release integrates one designated repository per project; roots with code in another repository are visibly ineligible with `repository_not_designated`, not silently omitted from an otherwise eligible snapshot.

Define these immutable Pydantic value types in Task 1:

```python
class BranchKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repository_id: str
    branch: str

class Fence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    target: BranchKey
    owner_id: str
    token: int

class PromotionInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation_key: str
    source_task_id: str
    source_head: str
    source_base: str
    expected_target: str
    fence: Fence

class PromotionValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intent_id: str
    receipt_id: str | None = None
    prepared_sha: str | None = None

class RequiredCheckSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    names: tuple[str, ...]
    producer_id: str

class RepairPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    primary_seconds: int = Field(default=1800, gt=0)
    primary_attempts: int = Field(default=3, gt=0)
    debug_seconds: int = Field(default=3600, gt=0)
    debug_attempts: int = Field(default=3, gt=0)
    debug_intelligence_class: str
    debug_profile_id: str | None = None
```

The names above are new implementation interfaces, not claims about existing classes. Import `BaseModel`, `ConfigDict`, and `Field` from Pydantic. Preserve repository ref validation in GitManager. Reject an empty required-check set for code-producing integration. Resolve the configured debug intelligence class before enabling a project; do not assume a profile named Fable exists.

Mutation results use the existing `CommandResult` envelope with typed values and named outcomes from design §10.2. Known validation failures return those outcomes; unexpected I/O errors remain retryable/runtime failures with durable operation identity. Never convert an unknown Git result into successful delivery.


## 16. Task 12 — Cross-boundary failure tests and release evidence

**Files:** Create `tests/test_integration_train_e2e.py`, `tests/test_integration_recovery.py`. Update `docs/guides/hierarchical-integration-trains.md` with verified commands and cutover checklist.

**Interfaces:** Consume the real service/commands and scratch DB/Git repositories. Fake only external agent execution, clock, and forge CI responses; do not fake generation, leases, receipts, hierarchy locks, or Git history.

- [ ] Build a root → child → grandchild fixture. File another child during parent verification, reject stale close, deliver all children, wake/reverify the parent, and complete/review the root.
- [ ] Add a second eligible root and seal both. Inject a conflict followed by aggregate failure. Exhaust primary repair, hand ownership to debug, repair forward, and publish full green evidence. Assert one main fast-forward to the exact candidate SHA, all source receipts, source PR delivery comments, and cleanup completion.
- [ ] Inject process crashes at every intent/event boundary and repeat the operation from a fresh service instance. Assert no duplicate squash, lost repair budget, second project batch, or missing parent wake. Repeat database locking cases against PostgreSQL with its dedicated marker.
- [ ] Run `aq test tests/test_integration_train_e2e.py tests/test_integration_recovery.py`. Run PostgreSQL-marked integration cases explicitly with `aq test -m integration tests/test_integration_state.py tests/test_integration_sealing.py tests/test_integration_recovery.py` against scratch test infrastructure.
- [ ] Perform one final area run using an explicit shell array of the new test files plus the touched hierarchy/workspace/playbook contract files; no whole-repo run. Run `scripts/e2e-env.sh --reset` and `scripts/e2e-smoke.sh` only against their disposable E2E environment as required for hierarchy changes, never against the operator daemon.
- [ ] Record commands and results, run changed-file Ruff and `git diff --check`, reconcile every deliverable below, and commit release evidence. Leave production enablement to the operator; do not close PR #397 automatically as part of code execution.

Final scenario invariants:

```python
assert main_after == fully_tested_candidate_sha
assert root_member_ids == eligible_ids_at_seal
assert debug_dispatch_count == 1
assert max_active_batches_for_project == 1
assert post_promotion_full_ci_runs == 0
assert delivered_children_without_parent_verification == []
assert duplicate_delivery_keys == []
```

## Prescriptive preflight sequence

Begin only after reviewed Tasks9b1, 9b2, 10, and 11. At Task12 start, inventory
the final tables/interfaces rather than freezing names from an earlier head.

Carry Task9b2's nonblocking migration-test finding into the final migration gate:
`task-9b2-fix-1b-review.md` asks for PostgreSQL legacy incompatibility cases beyond
cross-intent and receipt UPDATE/DELETE refusal immediately after downgrade to d4,
on both dialects, before re-upgrade. Reuse the existing parameterized migration
fixture; no new runtime behavior is requested by this finding.

Task10a discovered two failures in tests/test_integration_parent_completion.py
where fixtures directly mutate append-only delivery receipts. Resolve these tests
against the intended invariant (seed invalid data before guards or assert rejection,
as appropriate); do not weaken production immutability. Task10a also observed a
fresh-empty SQLite migration failure and switched its scoped U-D-U fixture to the
established initialized-head pattern. Its cause was not retained/proven baseline;
the final fresh-empty migration gate below must diagnose and resolve it with actual
traceback/baseline evidence. See task-10a-report.md for exact commands.

1. Close SQLite→PostgreSQL cutover first. The canonical adapter is
   `src/database/migrate_sqlite_to_pg.py`; make `scripts/migrate_sqlite_to_pg.py`
   delegate to it or explicitly deprecate the stale duplicate. Add the complete
   dependency-ordered Task7–Task11 table family to `_ORDERED_TABLES`, including
   candidate publications/resolutions/ref mutations, root intents/member receipt
   reservations, schedules/leases/outbox pins, cleanup, controls/cutover/probes.
   Seed and compare one complete live graph and all JSON fields. Use one quiesced
   SQLite snapshot and one PostgreSQL transaction through FK fixups, or explicitly
   fail/document a stopped-daemon plus discard-partial-target contract.
2. Compose `test_integration_train_e2e.py` from real database, hierarchy,
   completion, child promotion, repair, schedule/train, candidate/CI/root
   promotion, outbox/service/commands/ownership, and real temporary bare Git.
   Fake only clock, external agent execution, and authenticated forge adapters.
   Recreate provider/service instances after crashes over a durable fake-forge
   ledger; use real session-instance principals for worker mutations.
3. Three-level scenario: root→child→grandchild, add another child during parent
   verification, prove generation/stale-close refusal/wake/reverification/exact
   receipts/carry-forward provenance/final root review and no unverified or
   duplicate deliveries.
4. Two-root scenario: one immutable sweep; conflict on the later member; repair
   forward; inject aggregate CI failure; exhaust primary and dispatch exactly one
   fresh debug agent; repair/rebuild without membership or budget reset; green;
   exact Task9b2 promotion. Use a remote hook ledger to prove one `main` update to
   the tested OID, no promotion commit, all receipts/comments/cleanup, and zero
   post-main CI observation/publication.
5. `test_integration_recovery.py` is a table-driven fresh-instance crash matrix
   across child promotion, parent resolution, outbox/runtime, schedule/seal,
   candidate/partial/repair/handoff/PR, CI/attestation, root intent/main write/
   receipt finalization, cleanup/release/catch-up. Every retry asserts one squash,
   budget charge, batch, wake, receipt set, and no blind ambiguous remote retry.
6. PostgreSQL tests both orderings for duplicate tick/seal; seal vs hierarchy/
   review; same/different-parent delivery; filing vs verification; green vs
   deadline; CI vs rebuild; candidate mutation vs invalidation; concurrent main
   writers; release vs catch-up. Reuse the established hierarchy→subject→owner
   lock order and claims; assert no deadlocks/raw uniqueness errors.
7. Prove lifecycle compatibility: promoted batch with independent cleanup permits
   lease release and next train; old cleanup retains exact batch refs; moved refs
   conflict visibly; catch-up emits once; disable drains active work; disabled/
   observe preserve legacy behavior and create no managed state.
8. Final migration evidence: empty SQLite and unique PostgreSQL to final head;
   fresh scratch DB to `f02a4a4a3010` then final head with exact retained failures;
   sole head and relevant U-D-U/live-data guards. Do not call a failure pre-existing
   without a baseline reproduction.
9. Run Tier-1 disposable daemon E2E only with unique `AQ_E2E_HOME`, port, and PG
   DB; initialize without `--reset`, resolve config to the exact scratch DB, and
   tighten stop to require `/proc/<pid>/cmdline` contains both `src.main` and that
   exact config. No Tier2/live LLM, credentials, protection, or operator state.

Local required groups are copy/final migrations, the two Task12 tests, deterministic
PostgreSQL lock cases, one explicit affected-area array, and disposable Tier1 smoke.
The full 11,330-test suite, all-marker matrix, 5,000-task wall-clock perf, randomized
crashes, and repeated high-contention stress remain CI-only. Real forge/protection/
probe/cutover remains operator-only.
