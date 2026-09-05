# Hierarchical Delivery and Integration Trains Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver task trees recursively into parent branches and periodically promote every eligible root through one sealed, fully tested integration per project.

**Architecture:** Add durable integration records and fenced commands beneath two project-scoped playbooks. Git mutation uses prepared intents and exact expected-target pushes; asynchronous progress uses a transactional outbox, persisted deadlines, and reconciled event re-entry. Existing task status remains separate from proof of Git delivery.

**Tech Stack:** Python 3.12+, async SQLAlchemy Core, Alembic, SQLite/PostgreSQL, Pydantic command contracts, async GitManager, Playbook V2 prose sources and compiled artifacts, existing CLI command execution transport.

**Spec:** [Hierarchical Delivery and Integration Trains](../specs/2026-09-04-hierarchical-integration-trains-design.md), including follow-up solutions in §19.

**Status:** Implementation specification; no runtime changes are included in this document. Numerical repair defaults below are recommended initial settings, configurable by project playbook. The design's 300-second schedule is retained.

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

## 1. Delivery packages and dependency order

Implement four reviewable packages in one coordinated plan. Keeping the interfaces here avoids independently designed Git, hierarchy, and scheduling protocols.

| Package | Tasks | Independently verifiable result |
|---|---|---|
| Durable mechanisms | 1–4 | Schema, replayable events, exclusive ownership, recoverable Git promotion; no automatic shipping |
| Recursive delivery | 5–7 | Isolated children, parent verification, bounded repair, disabled hierarchy playbook |
| Root train | 8–10 | Durable schedules, sealed candidates, exact-CI promotion, disabled train playbook |
| Operation and rollout | 11–12 | Explain/control surfaces, migration checks, failure/restart integration coverage |

Dependencies: `1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12` is a safe execution order. Some test authoring may overlap, but do not implement competing ownership or persistence protocols in separate tasks. Each task ends with focused checks and a scoped commit. Do not merge a package with its project flag enabled by default.

## 2. Repository map and ownership

These existing entry points were inspected when writing this plan:

- `src/database/tables.py`, `src/database/base.py`, and both database adapters: schema and query composition.
- `src/database/queries/hierarchy_queries.py`: `set_parent`, batch hierarchy changes, and `settle_containers` currently infer container completion from child task statuses.
- `src/orchestrator/workspace.py`: child branch selection and `_resume_branch_for` currently permit plan-subtask parent branch reuse.
- `src/git/manager.py`: `apush_validated_delivery` preserves reserved-path checks and pushes an immutable OID, but its boolean force-with-lease does not express the required expected old OID.
- `src/commands/handler.py`, `src/commands/contracts/builtin.py`, `models.py`, and `registry.py`: handler mixins and typed callable contracts.
- `src/playbooks/services.py`, `engine.py`, `activation.py`, and `src/database/queries/playbook_run_queries.py`: activation and durable run/event storage. Do not assume a `src/playbooks/store.py` exists.
- `src/orchestrator/core.py`: initialization, periodic cycle, and shutdown integration seams.
- `src/event_bus.py`, `src/event_schemas.py`: in-process notification and schema validation; an emitted callback is not durable acknowledgment.
- `src/prompts/default_playbooks/`, `src/vault.py`: shipped prose playbook installation. Machine graph JSON does not belong in installed Markdown sources.
- `.github/workflows/tests.yml`: currently runs the full matrix on both pushes and pull requests.

New runtime modules live in `src/integration/`: `models.py` (typed values), `ownership.py`, `promotion.py`, `hierarchy.py`, `repair.py`, `scheduler.py`, `candidates.py`, `ci.py`, `outbox.py`, `service.py`, and `status.py`. Do not create a second orchestrator: `service.py` only reconciles durable work and invokes existing command handling. Keep DB statements in focused query mixins, not in playbook prompts.

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

## 4. Durable schema contract

Use text primary IDs, integer fencing/generation counters, JSON only for immutable evidence/policy payloads, and named check constraints. Preserve records after task archival; do not cascade-delete audit records when a task is deleted.

| Table | Key and essential columns | Constraints |
|---|---|---|
| `task_integration_checkpoints` | task ID; generation; checkpoint SHA; verified SHA/generation; state; version | Nonnegative generation; conditional version updates |
| `task_branch_origins` | origin ID; task; repository; parent task/ref; base SHA; creation generation; reserved/materialized/retired | One live origin per task/repository; materialized origin immutable |
| `integration_branch_owners` | repository/ref; owner ID/role; fence; handoff state; session/workspace references | Unique physical branch; monotonic token; expired owner still reserves branch |
| `integration_promotion_intents` | intent ID; domain key; reserved receipt ID; source/base; old/new target SHA; fence; recovery ref; state | Unique domain key; prepared identity immutable; no committed receipt before remote evidence |
| `task_delivery_receipts` | receipt ID; domain key; source/target task; repository/ref; before/after; source/review/tree; resolution evidence; batch/member/revision | Unique domain key; append-only; explicit no-op/disposition evidence separate from code receipt |
| `integration_batches` | batch ID; project/repository; source manifest digest; lifecycle; current revision; policy/artifact snapshot; cleanup state | One active batch per project, including human-blocked; terminal audit retained |
| `integration_batch_members` | batch ID/ordinal; task; PR; repository; source base/head/tree; review evidence | Unique batch/task; no post-seal insert/update/delete |
| `integration_candidate_revisions` | batch/revision; construction base; ordered application progress; repair lineage; head; evidence; state | Monotonic revision; only current revision promotable |
| `integration_repair_operations` | operation ID; kind; batch or parent ID; episode ID; active stage; state | Exactly one target kind; one active operation per target |
| `integration_repair_stages` | operation/stage; policy; task history; starting SHA; started/deadline; attempts; dossier; state | Unique operation/stage; counters cannot decrease; shipped ordinals 0 and 1 |
| `integration_check_evidence` | evidence ID; operation/revision or parent generation/head; workflow/run/attempt; checks; conclusion; classification; observed time | Unique producer/run/attempt/check-set identity; obsolete evidence never advances state |
| `project_integration_schedules` | project; enabled; interval; next due; request sequence; outstanding request; last completed sweep | At most one outstanding request; interval positive |
| `project_integration_leases` | project; repository; batch; owner; fence; heartbeat/expiry | Project primary key; expiry grants reconciliation only |
| `integration_outbox` | event ID; domain dedup key; project; type/payload; available time; delivered time | Unique dedup key; bounded polling index on pending/available time |

Delivery and event acknowledgment finalize in one database transaction. No database transaction is held open across Git, forge HTTP, agent termination, or test execution. Use the existing project hierarchy lock for hierarchy mutation and root sealing; reserve ownership and operation state transactionally, perform I/O, then conditionally finalize against the fence/version.

Build partial unique indexes for SQLite and PostgreSQL with equivalent predicates. Store active membership lock state in authoritative rows, not JSON arrays. Future migrations must not edit historical source manifests. A candidate's application progress can be normalized further if needed, but must retain one result per member/revision and the exact ordering.

## 5. Task 1 — Persist integration state and validated policy

**Files:** Create `src/integration/__init__.py`, `src/integration/models.py`, `src/database/queries/integration_state_queries.py`, `tests/test_integration_state.py`. Modify `src/database/tables.py`, `src/database/base.py`, `src/database/adapters/sqlite.py`, `src/database/adapters/postgresql.py`. Generate an Alembic revision in `migrations/versions/` using the repository tool's generated revision ID, with message `hierarchical integration state`.

**Interfaces:** Produce the §3 value types and query mixin `IntegrationStateQueriesMixin`. Expose `get_integration_checkpoint(task_id: str) -> dict | None`, `get_integration_batch(batch_id: str) -> dict | None`, and `get_integration_operation(operation_id: str) -> dict | None`. Mutating query functions take an explicit `conn` from their owning transaction.

- [ ] Add scratch-DB constraint tests proving duplicate active batches, negative generations, and duplicate intent keys fail. Include both database backends using the repository's existing backend fixture pattern.
- [ ] Run `aq test tests/test_integration_state.py -x`; new tables/queries should be absent before implementation.
- [ ] Implement §3–4 schema, adapter composition, and read projections. Freeze the resolved playbook artifact hash, required-check version, and repair policy at operation creation so later playbook edits affect new operations only.
- [ ] Exercise upgrade from the prior schema and round-trip all records in disposable databases. Include archive/delete tests demonstrating receipts survive task removal. Run `aq test tests/test_integration_state.py` and changed-file Ruff; commit the schema, migration, types, and tests together.

Representative constraint test body (use SQLAlchemy `insert`, `IntegrityError`, and the scratch connection fixture):

```python
async def test_active_batch_is_unique_per_project(conn):
    values = dict(project_id="p", repository_id="r", lifecycle="sealed")
    await conn.execute(insert(integration_batches).values(id="b1", **values))
    with pytest.raises(IntegrityError):
        await conn.execute(insert(integration_batches).values(id="b2", **values))
```

Supply the schema's required manifest/policy values through fixture defaults. Run each expected constraint violation inside a savepoint so the PostgreSQL transaction remains usable.

## 6. Task 2 — Durable outbox and command registration

**Files:** Create `src/integration/outbox.py`, `src/commands/integration_commands.py`, `src/commands/contracts/integration.py`, `tests/test_integration_outbox.py`, `tests/test_integration_contracts.py`. Modify `src/commands/handler.py`, `src/commands/contracts/builtin.py`, `src/commands/contracts/models.py`, `src/event_schemas.py`, `src/database/queries/playbook_run_queries.py`.

**Interfaces:** Produce `enqueue_integration_event(conn, *, event_id: str, dedup_key: str, project_id: str, event_type: str, payload: dict, available_at: float) -> None` and `IntegrationOutbox.dispatch_due(now: float) -> int`. Add `IntegrationCommandsMixin` and `register_integration_contracts(registry)`, called by builtin registration. Register implemented handlers as their tasks land; reject unavailable operations explicitly rather than shipping success stubs.

- [ ] Test a committed event surviving dispatcher restart and a consumer crash after durable acceptance but before outbox acknowledgment.
- [ ] Run `aq test tests/test_integration_outbox.py tests/test_integration_contracts.py -x`.
- [ ] Implement transactional enqueue and acknowledgment only after the event is durably accepted by playbook activation/pending-event storage. Carry `event_id` through activation deduplication; callback return from `EventBus.emit` alone is insufficient. Retry with bounded polling pages and exponential delay; never drop integration events under generic pending-event overflow policy.
- [ ] Extend effect subjects for integration operation, branch ownership, and delivery evidence, and declare command-specific authority and redaction. Register all design event schemas with project and operation identity. Run the two focused files and commit.

Required replay assertion:

```python
await outbox.dispatch_due(now)
await outbox.dispatch_due(now)
assert await accepted_activation_count(event_id) == 1
```

`outbox`, `now`, and `accepted_activation_count` are test helpers backed by a scratch DB and the real pending-event repository, not in-memory deduplication mocks.

## 7. Task 3 — Branch ownership and exact expected-target Git pushes

**Files:** Create `src/integration/ownership.py`, `tests/test_integration_ownership.py`. Modify `src/git/manager.py`, `src/orchestrator/workspace.py`, `src/orchestrator/workspace_attachments.py`, `tests/test_git_manager_async.py`.

**Interfaces:** `BranchOwnership.acquire(target: BranchKey, owner_id: str, role: str) -> Fence`; `BranchOwnership.transfer(fence: Fence, next_owner_id: str, next_role: str) -> Fence`; `BranchOwnership.assert_current(fence: Fence) -> None`. Add `GitManager.apush_expected_delivery(checkout_path: str, base_oid: str, tip_oid: str, branch: str, expected_old_oid: str) -> str`.

- [ ] Write tests for collector acquisition during active parent work, expired-but-attached ownership, stale owner writes, and remote movement after validation. Use a temporary local bare remote for Git tests.
- [ ] Run `aq test tests/test_integration_ownership.py tests/test_git_manager_async.py -x`.
- [ ] Implement an ownership state machine: `owned → stopping → detached → transferred`. Confirm session termination and workspace release before increasing the token. Never treat timeout as proof of detachment.
- [ ] Preserve reserved-path validation, resolve and validate OIDs once, verify ancestry, then push an explicit refspec with an explicit lease:

```python
args = ["push", "origin",
        f"--force-with-lease=refs/heads/{branch}:{expected_old_oid}",
        f"{tip_oid}:refs/heads/{branch}"]
```

The integration service rejects non-fast-forward target updates before invoking this helper. Candidate rebuilding replaces the ephemeral branch only through a separate revision operation; it must not weaken parent or `main` ancestry checks. No implicit tracking-ref lease and no `+` refspec.

- [ ] Confirm the source branch is unchanged and a competing remote update is preserved. Run focused checks and commit.

## 8. Task 4 — Prepared promotion intents and receipts

**Files:** Create `src/integration/promotion.py`, `src/database/queries/integration_delivery_queries.py`, `tests/test_integration_promotion.py`. Modify `src/commands/integration_commands.py` and `src/commands/contracts/integration.py`.

**Interfaces:** `PromotionService.prepare(request: PromotionInput) -> PromotionValue`; `push(intent_id: str, fence: Fence) -> PromotionValue`; `reconcile(intent_id: str) -> PromotionValue`. Handler outcomes are `promoted`, `already_promoted`, `conflict`, `source_moved`, `target_moved`, and invariant/error outcomes in the design contracts.

- [ ] Write parameterized crash tests at prepare, before push, after push, and before outbox acknowledgment. Assert one target squash and one receipt after retry.
- [ ] Run `aq test tests/test_integration_promotion.py -x`.
- [ ] Implement clean three-way application using the child's immutable base, pinned reviewed head, and expected target. Persist source/review evidence and a deterministic prepared result; reserve the receipt ID before constructing the commit message. Store all distinct authors/co-authors and task/session provenance.
- [ ] Pin prepared objects in a recovery ref under `refs/aq/integration-intents/<intent-id>` in a retained repository, not only in a disposable slot. Persist intent before target push. Conflict handling records base/source/target inputs and returns `conflict` without inventing a receipt; the repair operation later prepares an explicit resolution result.
- [ ] Reconcile remote tip/ancestry with the prepared OID. Equal old target permits retry; prepared OID reachable from current target proves application; divergent target blocks. Finalize receipt and outbox in one transaction, then enqueue cleanup. Run focused tests and commit.

Critical behavior test:

```python
prepared = await promotion.prepare(request)
await promotion.push(prepared.intent_id, request.fence)
recovered = await promotion.reconcile(prepared.intent_id)
again = await promotion.reconcile(prepared.intent_id)
assert recovered.receipt_id == again.receipt_id
assert recovered.prepared_sha == prepared.prepared_sha
```

Construct `promotion` against the scratch database and local bare Git remote; inject a crash between remote push and receipt transaction as a separate case. Identical code assertions without that failure injection do not prove recovery.

## 9. Task 5 — Isolated child origins and hierarchy guards

**Files:** Create `src/integration/hierarchy.py`, `tests/test_integration_hierarchy.py`. Modify `src/database/queries/hierarchy_queries.py`, `src/commands/task_commands.py`, `src/commands/proposal_commands.py` (`_cmd_task_batch_commit`), `src/orchestrator/workspace.py`, `src/commands/contracts/integration.py`.

**Interfaces:** `HierarchyIntegration.file_children(parent_id: str, children: list[dict], expected_generation: int) -> dict`; `checkpoint_parent(task_id: str, head_sha: str, generation: int) -> dict`; `mutate_hierarchy(task_id: str, mutation: str, arguments: dict) -> dict`. Implement design contracts `integration_file_children`, `integration_checkpoint_parent`, and `integration_mutate_hierarchy`.

- [ ] Add tests for a branchless three-level tree, concurrent batch child filing, and a new child after an earlier child's base was recorded. Assert materialized ancestor branches are distinct and no child targets `main`.
- [ ] Run `aq test tests/test_integration_hierarchy.py -x`.
- [ ] Under the project hierarchy lock, reserve parent/child origins top-down, advance the parent's generation once per filing transaction, invalidate verification, and enqueue branch materialization. Publish runnable children only after exact refs are confirmed. Route ordinary task creation and `task_batch_commit` through this path under the project flag.
- [ ] Retire plan-subtask branch sharing only for enabled projects. Source PR targets are immediate-parent refs. Reparent only an unstarted, unmaterialized child; retire its old reservation. Guard reopen/delete/archive/disposition paths as well as `set_parent`, including bulk operations. A delivered or sealed subtree cannot be mutated to evade verification; subsequent work is a follow-up task.
- [ ] Prove stale generation closes fail and both parents are invalidated on reparenting. Run the new test file plus `tests/test_hierarchy_queries.py` and `tests/test_branch_isolated_workspace.py`; commit.

Generation example to encode in the fixture-backed tests:

```python
first = await hierarchy.file_children("parent", [{"title": "A"}], 0)
second = await hierarchy.file_children("parent", [{"title": "B"}], 1)
assert first["generation"] == 1
assert second["generation"] == 2
assert first["origins"][0]["base_sha"] == original_parent_sha
```

## 10. Task 6 — Receipt-driven collection and guarded parent completion

**Files:** Extend `src/integration/hierarchy.py`. Create `tests/test_integration_parent_completion.py`. Modify `src/database/queries/hierarchy_queries.py`, `src/commands/task_commands.py`, `src/commands/integration_commands.py`, `src/commands/surface_commands.py`, and `src/prime/sections.py` for the resumed parent's delivery summary.

**Interfaces:** `readiness(task_id: str) -> dict`; `verify_parent(task_id: str, generation: int, head_sha: str, evidence_ids: list[str]) -> dict`; `complete_parent(task_id: str, generation: int, head_sha: str) -> dict`. Produce `integration_delivery_readiness`, `integration_parent_verify`, `integration_complete_parent`, and `delivery_receipts` contracts.

- [ ] Test terminal children without receipts, partial generations, verified no-ops, accepted abandonment, and a child added while the parent verifies.
- [ ] Run `aq test tests/test_integration_parent_completion.py tests/test_hierarchy_settlement.py -x`.
- [ ] Suppress legacy container auto-completion for integration-managed parents. Settlement emits a deduplicated readiness event only when all required children across generations are delivered/disposed. Collect under the ownership handoff from Task 3; integrate siblings serially without full CI after each sibling.
- [ ] Resume the parent on the delivered head with receipt summary in prime. Record verification against generation/head and required check evidence. Guard close under hierarchy lock and ownership/version checks; separate normal review from completion readiness. Leaf reviews pin their head; parent review pins the final verified aggregate. Never reuse an older review after parent fixes.
- [ ] Record no-op/disposition evidence with the same invalidation rules as code delivery. Use a delegated verifier for initially branchless parents. Run focused tests and commit.

Expected race outcome:

```python
await hierarchy.verify_parent("parent", 2, merged_sha, evidence_ids)
await hierarchy.file_children("parent", [{"title": "new defect"}], 2)
result = await hierarchy.complete_parent("parent", 2, merged_sha)
assert result["outcome"] in {"waiting", "stale_verification"}
```

## 11. Task 7 — Shared bounded repair and hierarchy playbook

**Files:** Create `src/integration/repair.py`, `tests/test_integration_repair.py`, `src/prompts/default_playbooks/hierarchical-delivery.md`, `tests/test_hierarchical_delivery_playbook.py`. Modify `src/vault.py`, `src/commands/contracts/integration.py`, `src/commands/integration_commands.py`.

**Interfaces:** `RepairService.start(kind: str, target_id: str, policy: RepairPolicy, now: float) -> str`; `record_result(operation_id: str, evidence_id: str, now: float) -> dict`; `expire(operation_id: str, stage: int, now: float) -> dict`. Persist event IDs and conditional stage transitions, and expose `integration_record_repair`/`integration_repair_timeout`.

- [ ] Test primary timeout with no CI event, debug timeout, simultaneous green/timeout events, repeated infrastructure failures, stale stage events, and a parent adding children during repair.
- [ ] Run `aq test tests/test_integration_repair.py tests/test_hierarchical_delivery_playbook.py -x`.
- [ ] Implement two stages with persisted absolute deadlines. Snapshot separate parent/root policies. Count required aggregate check attempts once per conclusive run attempt; exclude focused diagnostics and classified infrastructure failures. Queue time and CI waiting count toward duration. Debug handoff is blocked until the old writer is confirmed detached.
- [ ] Associate repair/verifier tasks with the operation as execution delegates, never as structural children waiting to deliver to themselves. Dossiers include pinned inputs, receipts/manifest, branch head, failures, logs, hypotheses, commands, budgets, and earlier repair commits. Parent final verification remains inside the active stage budget.
- [ ] Write the disabled prose playbook with explicit named routes: child/receipt events → readiness; ready and parent suspended → collect; conflict → repair; generation delivered → wake parent; verified → guarded close; failed child → configured block/human disposition. Pin compiled artifacts in operation provenance and validate authority through the existing compiler tests.
- [ ] Verify recursive primary/debug exhaustion blocks only that parent subtree, while root exhaustion retains the project lease. Run focused tests and commit.

Timeout test core:

```python
op = await repair.start("parent", "parent", policy, now=100)
result = await repair.expire(op, stage=0, now=100 + policy.primary_seconds)
assert result["outcome"] == "escalate"
repeat = await repair.expire(op, stage=0, now=100 + policy.primary_seconds + 1)
assert repeat["outcome"] == "already_terminal"
```

The test fixture confirms the primary writer is detached. A second test keeps it attached and asserts no debug agent is dispatched.

## 12. Task 8 — Project schedule and sealed root frontier

**Files:** Create `src/integration/scheduler.py`, `src/database/queries/integration_train_queries.py`, `tests/test_integration_schedule.py`, `tests/test_integration_sealing.py`. Extend integration contracts/handlers.

**Interfaces:** Define `IntegrationScheduler` and `TrainService` in `src/integration/scheduler.py`. Expose `IntegrationScheduler.mark_due(project_id: str, now: float, trigger: str) -> dict`; `TrainService.seal(project_id: str, request_id: str, now: float) -> dict`. `seal` returns `sealed` with `batch_id`, `empty`, or `busy`. Use stable order `(task_id, reviewed_head_sha)`; pagination must collect the whole snapshot, not impose an implicit cap.

- [ ] Test twelve missed windows and repeated manual flushes while a batch is active. Assert one outstanding request and one new sweep after release.
- [ ] Run `aq test tests/test_integration_schedule.py tests/test_integration_sealing.py -x`.
- [ ] Persist a request only when none is outstanding. Advance `next_due` to the first future interval boundary without emitting each missed interval. Interval edits use `now + new_interval` while preserving an already outstanding request. Disabled schedules retain active batches for reconciliation but create no periodic requests.
- [ ] Atomically consume request, acquire project lease, lock hierarchy, and snapshot all eligible roots in the designated repository. Eligibility requires current reviewed source, recursive receipts/verification, no hold, no prior root receipt, and no structural parent. Zero roots finalize a no-op sweep and release ownership in the same transaction.
- [ ] Seal sources and review evidence immutably. Write uniqueness/immutability tests for SQLite and PostgreSQL. Lease expiry resumes the same batch, never creates another one. Run focused tests and commit.

Coalescing assertion:

```python
requests = [await scheduler.mark_due("p", tick, "periodic")
            for tick in range(300, 3900, 300)]
assert len({item["request_id"] for item in requests}) == 1
```

Keep an active batch fixture throughout this test. Test request consumption and a genuinely later tick separately.

## 13. Task 9 — Candidate construction, CI evidence, and exact promotion

**Files:** Create `src/integration/candidates.py`, `src/integration/ci.py`, `tests/test_integration_candidates.py`, `tests/test_integration_ci.py`, `tests/test_integration_main_promotion.py`. Modify `src/git/manager.py`, integration handlers/contracts.

**Interfaces:** `CandidateService.build(batch_id: str) -> dict`; `rebuild(batch_id: str, expected_revision: int, new_base_sha: str) -> dict`; `CIService.observe(operation_id: str, evidence: dict) -> dict`; `CandidateService.promote(batch_id: str, revision: int) -> dict`.

- [ ] Test zero/one/many source construction, conflict after an earlier member was applied, restart mid-construction, and main movement after green CI. Tests use actual temporary Git repositories.
- [ ] Run `aq test tests/test_integration_candidates.py tests/test_integration_ci.py tests/test_integration_main_promotion.py -x`.
- [ ] Create `integration/<batch-id>` from the locked base and apply ordered member deltas as squashes, preserving source refs. Track application progress and repair commits in the candidate revision. A conflict pauses at the member ordinal, invokes bounded repair, then continues through every remaining member; it cannot seal a partial candidate as complete.
- [ ] Start the root primary deadline before construction. Open the integration PR as the review/audit surface. Push exact candidate commits through existing authority/reserved-path guards. Full CI runs on that head, not a synthetic merge commit. Ignore results for other revisions, check-set versions, producers, or incomplete matrices.
- [ ] Require every named check to be successful from the trusted producer; skipped/cancelled/missing required checks are not green. Store conclusive failed attempts too. Parent local check evidence enters the same accounting service through authenticated command execution records bound to generation/head.
- [ ] Prepare main-promotion intent only when candidate construction is complete and exact required evidence is green. Compare expected main, verify ancestry, push the identical tested OID, then finalize root receipts and delivery events. The service never invokes forge squash/rebase/merge APIs for this promotion.
- [ ] On main movement, the default creates revision N+1 in the same batch, reapplies sealed sources and accepted repairs, preserves budgets, and retests. `wait` creates a human gate. Pin each revision in recovery storage before replacing the ephemeral ref. Run focused tests and commit.

Exact promotion test core:

```python
built = await candidates.build(batch_id)
await ci.observe(operation_id, green_evidence_for(built["head_sha"]))
promoted = await candidates.promote(batch_id, built["revision"])
assert promoted["outcome"] == "promoted"
assert await remote_main_sha() == built["head_sha"]
```

`green_evidence_for` constructs all configured required checks from the trusted producer; add separate wrong-SHA, partial-matrix, stale-revision, and moved-main cases. Mocking only a boolean `ci_passed` is insufficient.

## 14. Task 10 — Train playbook, daemon reconciliation, CI workflow, cleanup

**Files:** Create `src/integration/service.py`, `src/prompts/default_playbooks/root-integration-train.md`, `tests/test_integration_service.py`, `tests/test_root_integration_playbook.py`, `tests/test_integration_cleanup.py`, `scripts/check-integration-attestation.py`. Modify `src/orchestrator/core.py`, `src/vault.py`, `.github/workflows/tests.yml`.

**Interfaces:** `IntegrationService.tick(now: float) -> None`, `start() -> None`, `stop() -> None`; handlers for schedule, CI completion, deadline, repair, resume, and cleanup events. `integration_release` reconciles terminal shipping and enqueues outstanding catch-up work once.

- [ ] Test restart with active ownership, pending intent, expired stage, lost notification, and completed promotion with failed branch deletion. Run `aq test tests/test_integration_service.py tests/test_root_integration_playbook.py tests/test_integration_cleanup.py -x`.
- [ ] Implement bounded per-cycle polling of durable schedules, outbox, CI observations, deadlines, and unresolved intents. Run through daemon initialization/shutdown without global timer semantics. Each activation performs a bounded transition and returns; no agent remains attached solely to poll CI.
- [ ] Ship disabled prose with explicit routes: due → seal; empty → release; sealed → build; conflict/red → repair; current complete green → promote; base moved → configured rebuild/wait; exhausted → debug/human; promoted → receipts/cleanup/release. All policy inputs are declared; active operations use their frozen artifact/configuration.
- [ ] Authenticate the CI attestation as a check created by the integration identity's trusted app, tied to repository, candidate SHA, required-check version, run IDs, and successful conclusion. Publish it before the main push. Workflow reads must verify producer identity, not accept a PR label, commit message, or arbitrary status context. A failed lookup fails closed to full CI.
- [ ] Enabling train mode requires a configured trusted integration app identity and a successful scratch-repository probe of check publication and exact-OID main promotion. Expose missing credentials or incompatible branch protection as enablement blockers. The daemon reads existing secret configuration; no credentials are embedded in playbooks, receipts, logs, or this spec. The workflow authenticates the attestation through forge app identity, so no new custom signature/key-distribution scheme is needed.
- [ ] Modify the main push workflow to run the lightweight attestation check first and skip full jobs only for a valid attestation. Keep full CI for unattested pushes. Route task PRs to focused declared checks and integration branch pushes to full CI; suppress duplicate integration-PR full runs. Preserve tests for both SQLite migrations and PostgreSQL in the integration matrix. Verify the checkout head equals the attested candidate head.
- [ ] Close source PRs with batch/receipt/SHA comments; reconcile integration PR delivered status. Delete refs only if they still point to the recorded expected SHA, and detach only owned worktrees. Changed refs become visible cleanup conflicts. A failed cleanup records pending work without rerunning CI. Finalized shipping may release the project lease while independent cleanup retries retain the old batch ID; no second integration starts before main delivery receipts are durable.
- [ ] Run focused tests, compile/authority-check both prose playbooks, inspect workflow gating fixtures, and commit.

Attestation decision implemented by the script must reduce to:

```python
skip_full_ci = (
    event_name == "push" and ref == "refs/heads/main"
    and attestation.producer_id == configured_integration_app_id
    and attestation.repository_id == repository_id
    and attestation.head_sha == checkout_sha
    and attestation.required_check_version == required_check_version
    and attestation.conclusion == "success"
)
```

The workflow selects the trusted check record before constructing `attestation`. Missing/invalid fields return false. Credentials with authority to write this check must not be present in ordinary worker sessions. Use authenticated forge reads; no access from hosted CI to the operator's database is required.

## 15. Task 11 — Operator status, controls, and project cutover

**Files:** Create `src/integration/status.py`, `tests/test_integration_controls.py`, `docs/guides/hierarchical-integration-trains.md`. Extend integration handler/contracts and the existing project configuration/CLI command transport. Add `src/cli/integration.py` and register it in `src/cli/app.py` after reading `src/cli/CLAUDE.md`.

**Interfaces:** `integration_status(project_id)` returns schedule, batch/revision, members, parent blockers, ownership, budgets, evidence, and cleanup; `integration_flush(project_id)`, `integration_resume(operation_id)`, `integration_abort(operation_id, reason)`, `integration_retry_cleanup(batch_id)`, and `integration_enable(project_id, mode)` use the existing authenticated command envelope. Modes are `disabled`, `observe`, `hierarchy`, `train`.

- [ ] Test worker/reviewer/integrator/human capabilities and cross-project argument rejection. Assert only a human can abort/resume a human-blocked operation and abort never rewrites main.
- [ ] Run `aq test tests/test_integration_controls.py -x`.
- [ ] Implement read projections with stable blocker codes: open child, missing receipt, stale head/generation/review, wrong repository, active owner, pending CI, budget exhausted, human hold, cleanup conflict. `aq integration status --project-id <id>` and `aq task explain` expose these reasons without requiring log inspection.
- [ ] Implement observe mode as read-only eligibility/reporting; it does not reserve refs, write receipts, or start repair agents. Enable preflight verifies designated repository, origin retention, required checks, debug routing, integrator authority, and branch protection compatibility.
- [ ] Require explicit historical gate waiver with recorded operator identity/reason; do not fabricate pinned-review receipts. Disable the migrated project's legacy merge sweep and child merge gates as part of the atomic configuration transition. Existing unreviewed/unpinned work stays held until reviewed or waived through the documented migration procedure.
- [ ] Document inspection, flush, human repair/resume, abort, cleanup retry, and rollback. Disabling future schedules never abandons an active train. Rollback waits for active operations to finish/abort and restores legacy policy explicitly, without deleting audit records or downgrading the DB.
- [ ] Run focused controls tests and commit. If implementation adds public API DTOs or a codegen router rather than using existing generic execution, regenerate both API clients with the repository scripts and run `tests/test_api_client_contract.py`; never hand-edit generated clients.

Example status payload contract:

```json
{"project_id":"p","mode":"train","batch_id":"b1","revision":2,
 "state":"awaiting_ci","pending_sweep":true,
 "repair":{"stage":0,"attempts":1,"deadline_at":1800},
 "blockers":[{"code":"pending_ci","head_sha":"recorded-candidate-oid"}],
 "cleanup_pending":[]}
```

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

## 17. Required command coverage and ownership

The implementation must register every design contract, not only the commands used in the happy path:

| Task | Contracts |
|---|---|
| 3 | `integration_transfer_owner` |
| 4 | `delivery_promote`, `integration_reconcile_promotion` |
| 5 | `integration_file_children`, `integration_checkpoint_parent`, `integration_mutate_hierarchy` |
| 6 | `integration_delivery_readiness`, `integration_parent_verify`, `integration_complete_parent`, `delivery_receipts` |
| 7 | `integration_record_repair`, `integration_repair_timeout` |
| 8 | `integration_schedule_due`, `integration_seal` |
| 9 | `integration_build_candidate`, `integration_ci_evidence`, `integration_promote_main` |
| 10 | `integration_release` |
| 11 | Status, enablement, flush, resume, abort, cleanup retry |

Worker authority permits its own checkpoint, child filing, evidence submission, and guarded close. Review authority pins source evidence. Integrator authority consumes approved sources and controls only the assigned branch/operation. Human authority controls exceptional disposition, abort, and resume. All authority checks resolve current project/task ownership on the server; an argument claiming an owner ID is not authentication.

## 18. Completion and design coverage

- [ ] Design §5 invariants: schema constraints, hierarchy guards, ownership, promotion, and sealing tasks 1–9.
- [ ] Design §6 recursive lifecycle and branchless parents: tasks 3–7.
- [ ] Design §7 periodic all-root train and revision rebuilding: tasks 8–10.
- [ ] Design §8 exact CI and no redundant main audit: tasks 9–10.
- [ ] Design §9 bounded recursive repair/debug/human escalation: tasks 7, 9, 12.
- [ ] Design §10 playbook/core separation and typed contracts: tasks 2, 7, 10, 17's coverage table.
- [ ] Design §11 durable state, intents, ownership, outbox: tasks 1–4 and 8.
- [ ] Design §12 operator controls and §14 authority: tasks 2, 3, 9–11.
- [ ] Design §13 failure/restart behavior: tasks 4, 7–10, 12.
- [ ] Design §15 rollout: task 11; §16 tests and §17 acceptance: task 12 plus this checklist.
- [ ] All eight follow-up findings in design §19 have concrete schema, behavior, and regression tests in this plan.

The resulting implementation is ready for an operator-controlled pilot when these deliverables pass with both playbooks still disabled by default. Begin with hierarchy delivery on a test project, then observation, one-root trains, and finally multi-root trains through the same candidate path.

