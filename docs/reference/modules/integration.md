# Module catalog — integration

Development integration and the optional strict modes: the 29 production
modules that decide whether a finished task branch reaches your default branch,
and what is recorded about it.

Prose for everything here lives on four pages:

* [Integration](../../concepts/integration.md) — the mechanism, the modes and
  the guarantees.
* [Development integration](../../guides/development-integration.md) — running
  the configured path.
* [Integration troubleshooting](../../guides/integration-troubleshooting.md) —
  symptom to command.
* [Switching integration modes](../../guides/integration-migration.md) —
  transitions, preflight and waivers.

Modes are labelled throughout: **development** is the mode this repository
configures; **strict** marks a module that only runs in the optional
`hierarchy` / `train` compatibility modes; **any** marks shared machinery.

## Package entry points

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/integration/__init__.py`](../../../src/integration/__init__.py) | Re-exports the frozen value objects (`BranchKey`, `Fence`, `PromotionInput`, `PromotionValue`, `RepairPolicy`, `RequiredCheckSet`) that commands and playbooks pass across the boundary. | [concepts/integration.md](../../concepts/integration.md) | Any mode. Import the models from here, not from submodules. |
| [`src/integration/models.py`](../../../src/integration/models.py) | The frozen Pydantic value objects: branch keys and fences, promotion inputs, required-check sets, repair and cleanup policy, the validated `HierarchicalIntegrationPolicy`, and the owner-role sets that decide which reservations may be retried or re-queued. | [concepts/integration.md](../../concepts/integration.md) | Any mode. `extra="forbid"` everywhere: an unknown field is a bug, not a passthrough. `tests/test_integration_hierarchy.py` |
| [`src/integration/service.py`](../../../src/integration/service.py) | The single bounded reconciliation loop: one tick drives development sweeps, materialization, schedules, collection, candidate CI, repair deadlines, parent CI, intents, cleanup, drains, discards and the outbox, each paged by its own cursor. | [concepts/integration.md](../../concepts/integration.md) | Any mode. A handler that raises is logged and its row stays retryable, so one bad project cannot stall the rest. `tests/test_integration_service.py` |
| [`src/integrations/__init__.py`](../../../src/integrations/__init__.py) | Marker package for glue that wires core services into the orchestrator lifecycle without importing framework concerns into the core modules. | [concepts/integration.md](../../concepts/integration.md) | Any mode. Docstring-only package; the wiring itself lives in [`src/orchestrator/core.py`](../../../src/orchestrator/core.py). |

## Development delivery

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/integration/development.py`](../../../src/integration/development.py) | The whole development path: publisher exclusion, the retained clone, dependency-ordered batch assembly, local validation, ancestry-preserving publication under a lease, crash reconciliation, parked-row disposition, repair filing, operator adoption, preserving stopped writers and mode configuration. | [guides/development-integration.md](../../guides/development-integration.md) | **Development.** Writes no synthetic review or CI receipts; legacy episodes stay as audit history. `tests/test_development_integration.py` |
| [`src/integration/status.py`](../../../src/integration/status.py) | Read-only, snapshot-consistent projections for `aq integration status` and per-task blockers, with a separate branch for development-mode projects (journal, ownership classification, pending publications, parked rows). | [guides/development-integration.md](../../guides/development-integration.md) | Any mode. Does no provider I/O and never writes. `tests/test_integration_controls.py`, `tests/test_development_integration.py` |
| [`src/integration/controls.py`](../../../src/integration/controls.py) | Authenticated rollout and recovery controls: functional preflight, generation-CAS mode changes, drain reconciliation, history waivers, manual flush, legacy suppression and the rollout audit trail. | [guides/integration-migration.md](../../guides/integration-migration.md) | Any mode. Provider and repository checks run *before* the hierarchy lock; every durable write commits behind the generation CAS. `tests/test_integration_operational_controls.py` |
| [`src/integration/preflight.py`](../../../src/integration/preflight.py) | Read-only functional dependency check run before a strict mode may be enabled: the runtime dependencies and repository configuration the mode will actually use. | [guides/integration-migration.md](../../guides/integration-migration.md) | Strict (gate). Read-only by construction; produces blocker codes, not fixes. `tests/test_integration_operational_controls.py` |
| [`src/integration/recovery_controls.py`](../../../src/integration/recovery_controls.py) | `resume`, `abort` and `retry-cleanup` over already-frozen work, admitting a change only when no external mutation is left ambiguous. | [guides/integration-troubleshooting.md](../../guides/integration-troubleshooting.md) | Strict. An ambiguous external write is reported, never assumed resolved. `tests/test_integration_operational_controls.py`, `tests/test_integration_candidates.py` |

## Branches and ownership

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/integration/ownership.py`](../../../src/integration/ownership.py) | Durable, fenced ownership of repository-qualified branches: acquire, attach, transfer with proof, assert-current, and the bounded `mutation_exclusion` held across one external Git write. | [concepts/integration.md](../../concepts/integration.md) | Any mode. A monotonic fence token is what serialises writers; expiry never overrides an attached owner. `tests/test_integration_ownership.py` |
| [`src/integration/branch_materialization.py`](../../../src/integration/branch_materialization.py) | Cuts the child branches a hierarchical project reserved but never created, draining the reservations from the integration loop rather than through an outbox event nothing consumes. | [guides/integration-troubleshooting.md](../../guides/integration-troubleshooting.md) | Strict. Exists because claiming is gated on `materialized`; unmaterialized reservations made tasks permanently unclaimable. `tests/test_integration_hierarchy.py`, `tests/test_integration_service.py` |
| [`src/integration/branch_discard.py`](../../../src/integration/branch_discard.py) | Removes task branches an operator explicitly asked to discard when deleting a task, compare-and-swapping on the observed head and refusing a live owner or the default branch. | [guides/integration-troubleshooting.md](../../guides/integration-troubleshooting.md) | Any mode. Transport failures back off toward an hour over at most 8 attempts; a conflict never retries. Parked rows surface in `aq doctor --check integration.branch_discards`. `tests/test_branch_discard.py` |
| [`src/integration/hierarchy.py`](../../../src/integration/hierarchy.py) | The project-locked writer for hierarchical delivery: atomic child filing, branch-origin reservation and materialization, parent checkpoints, workspace-checkpoint and repair-commit proofs, and mutation fencing. | [concepts/integration.md](../../concepts/integration.md) | Strict. `hierarchy_mode_enabled` is the single predicate that decides whether a project takes this path. `tests/test_integration_hierarchy.py` |
| [`src/integration/completion_recovery.py`](../../../src/integration/completion_recovery.py) | Repairs delivery links a close path lost — ended attachments before a reopened task retries, stopped writers whose close already freed the slot, interrupted pool-claim cleanup and missing root PR links. | [guides/integration-troubleshooting.md](../../guides/integration-troubleshooting.md) | Any mode. Recovers links without manufacturing review evidence; network probes stay off the scheduler. `tests/test_integration_completion_recovery.py` |

## Collection and promotion

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/integration/collection.py`](../../../src/integration/collection.py) | Reconciles approved children into the durable parent promotion policy, submitting one child per parent and returning a repaired branch to the queue. | [concepts/integration.md](../../concepts/integration.md) | Strict. Submits work; promotion remains the only mutation authority. `tests/test_integration_hierarchy.py` |
| [`src/integration/parent_completion.py`](../../../src/integration/parent_completion.py) | Connection-owned primitives for one parent's collection episode: reserve the episode, compute and mark readiness, record child dispositions, wake the verifier and complete the parent. | [concepts/integration.md](../../concepts/integration.md) | Strict. Receipt-owned: the episode's receipt, not a caller's assertion, is what closes it. `tests/test_integration_promotion.py`, `tests/test_integration_parent_completion.py` |
| [`src/integration/promotion.py`](../../../src/integration/promotion.py) | Crash-recoverable child-to-parent merge promotion: prepare once, push under an exact lease, recover receipts by ancestry, and reserve and push conflict resolutions. | [concepts/integration.md](../../concepts/integration.md) | Strict. Its typed failures (`PromotionConflict`, `PromotionSourceMoved`, `PromotionTargetMoved`, `PromotionNotApplied`) each map to one deterministic command outcome. `tests/test_integration_promotion.py` |
| [`src/integration/main_promotion.py`](../../../src/integration/main_promotion.py) | Durable root-to-`main` intent preparation and the one fenced mutation that promotes an exactly tested SHA, plus reconciliation of an intent whose outcome is unknown. | [concepts/integration.md](../../concepts/integration.md) | Strict. Requires an attestation subject that the daemon derived, never one a caller supplied. `tests/test_integration_main_promotion.py` |
| [`src/integration/candidates.py`](../../../src/integration/candidates.py) | Ordered, restartable construction of a sealed root candidate revision from immutable members, plus the frozen candidate-member repair lifecycle (reserve, accept, recover, push) and optional audit pull requests. | [concepts/integration.md](../../concepts/integration.md) | Strict. Caller-supplied data may never stand in for durable authority — that is what `CandidateAuthorizationError` and `CandidateStaleAuthority` exist to refuse. `tests/test_integration_candidates.py`, `tests/test_batch_ci_repair_rebuild.py` |
| [`src/integration/scheduler.py`](../../../src/integration/scheduler.py) | Coalesces periodic and manual triggers into one durable per-project sweep request (`IntegrationScheduler`), and seals a request's complete reviewed root frontier into a batch without Git I/O (`TrainService`). | [concepts/integration.md](../../concepts/integration.md) | Strict (`train`). Sealing is pure database work; nothing touches a remote. `tests/test_integration_schedule.py`, `tests/test_integration_sealing.py` |
| [`src/integration/release.py`](../../../src/integration/release.py) | Releases one terminal, shipped root train under the hierarchy fence, without coupling the release to cleanup progress. | [concepts/integration.md](../../concepts/integration.md) | Strict. A lost CAS is an ordinary outcome, not an error. `tests/test_integration_cleanup.py`, `tests/test_root_integration_playbook.py` |
| [`src/integration/cleanup.py`](../../../src/integration/cleanup.py) | Materialises immutable post-promotion cleanup work and executes one claimed item at a time: source-ref retention, failed-work retention windows and bounded retries. | [guides/integration-troubleshooting.md](../../guides/integration-troubleshooting.md) | Strict. Independent and restartable, so a stalled cleanup never blocks a promotion. `aq integration retry-cleanup` requeues exactly the safe items. `tests/test_integration_cleanup.py` |

## Evidence: review, CI and attestation

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/integration/review_evidence.py`](../../../src/integration/review_evidence.py) | Pins a trusted reviewer verdict to an exact source snapshot, and reopens the task on rejection. | [concepts/integration.md](../../concepts/integration.md) | Strict. The Git verdict is server-observed; a reviewer's claim about a SHA is not taken on trust. `tests/test_integration_review_evidence.py` |
| [`src/integration/ci.py`](../../../src/integration/ci.py) | The trusted-CI contract: the required-checks manifest and producer identity, canonical attestation and receipt payloads, typed parent/candidate subjects, the append-only evidence writer, and the authenticated GitHub observer. | [concepts/integration.md](../../concepts/integration.md) | Strict. Evidence is built only from authenticated API reads; `TrustedFixtureObserver` is an explicit test-only seam. `tests/test_integration_ci.py` |
| [`src/integration/attestation.py`](../../../src/integration/attestation.py) | Resolves an exact candidate tree's live CI into a durable receipt and, where an App is configured, publishes the attestation check; also answers the enablement blockers that gate a strict rollout. | [concepts/integration.md](../../concepts/integration.md) | Strict. Publication is leased; trust input is size-capped. `tests/test_integration_attestation.py` |
| [`src/integration/candidate_ci.py`](../../../src/integration/candidate_ci.py) | Resumes durable candidate publication before asking for exact CI evidence, so a restart cannot lose the ordering between the two. | [concepts/integration.md](../../concepts/integration.md) | Strict. Thin handler; the work belongs to `candidates.py` and `attestation.py`. `tests/test_integration_candidates.py` |
| [`src/integration/parent_ci.py`](../../../src/integration/parent_ci.py) | Publishes immutable per-parent CI snapshot refs so a frozen delivery branch is never mutated to get a CI run, and never overwrites a conflicting remote ref. | [concepts/integration.md](../../concepts/integration.md) | Strict. `tests/test_parent_ci_publication.py` |

## Repair and eventing

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/integration/repair.py`](../../../src/integration/repair.py) | The strict modes' bounded repair machine: atomic stage clocks, evidence-accounting transitions, delegate dispatch and completion, deadline expiry, subject rebinding after a rebuild, and root-collection resumption. | [guides/integration-troubleshooting.md](../../guides/integration-troubleshooting.md) | Strict. Wall-clock stage deadlines live here; development-mode repair is an ordinary queue task with no ladder. `tests/test_integration_repair.py`, `tests/test_batch_ci_repair_adoption.py` |
| [`src/integration/outbox.py`](../../../src/integration/outbox.py) | Transactional outbox for correctness-critical integration events: enqueue on the caller's transaction, freeze the destination manifest, deliver one bounded page and advance only on durable consumer acceptance. | [concepts/integration.md](../../concepts/integration.md) | Any mode. Delivers only to playbooks (`docs/concepts/playbooks.md` is planned), which is why branch-level work that no playbook consumes is drained directly instead. `tests/test_integration_outbox.py` |

## Adjacent modules other shards own

These are not integration modules, but you will land in them from here.

| Module | Owning shard | Why you would open it |
|---|---|---|
| [`src/commands/integration_commands.py`](../../../src/commands/integration_commands.py) | `cli` | The command handlers behind every `aq integration …` call, including the authority checks. |
| [`src/cli/integration.py`](../../../src/cli/integration.py) | `cli` | Flags, defaults and choices for the CLI group. |
| [`src/doctor/integration_checks.py`](../../../src/doctor/integration_checks.py) | `operations` | `integration.operational`, `integration.stranded_fences`, `integration.branch_discards`, `integration.unreviewed_prs`. |
| [`src/database/tables.py`](../../../src/database/tables.py) | `database` | Every `integration_*` table plus `development_deliveries` and `task_branch_origins`. |
| [`src/git/manager.py`](../../../src/git/manager.py) | `workspaces` | The async Git API every module here uses. |

## Documentation in this shard

| Page | Owner |
|---|---|
| [`docs/guides/development-integration.md`](../../guides/development-integration.md) | This ticket; assigned to the `integration` shard by the coverage manifest. |

## Coverage

The manifest assigns 29 production modules to this shard —
`src/integration/*.py` plus `src/integrations/__init__.py` — and every one has
a row above. Verify with:

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check
```

> **Note for the assembly ticket.** This shard's home
> (`docs/concepts/integration.md`) and catalog
> (`docs/reference/modules/integration.md`) are derived automatically, and
> `docs/guides/development-integration.md` is already named in `OWNED_PAGES`.
> The two remaining pages —
> `docs/guides/integration-troubleshooting.md` and
> `docs/guides/integration-migration.md` — are new paths that
> [`refresh_inventory.py`](../../plans/documentation-overhaul/refresh_inventory.py)
> does not name, so they fall through to the generic `docs/**` rule and would
> be attributed to `legacy` on the next regeneration. Add them to `OWNED_PAGES` under the `integration` shard when
> the manifest is next rewritten. This ticket deliberately does not rewrite
> the manifest itself: it is a shared file every sibling would then conflict
> on, which is exactly the conflict recorded in the delivery journal for
> `solid-grove.21`.
