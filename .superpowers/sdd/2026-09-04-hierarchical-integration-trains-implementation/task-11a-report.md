# Task 11a implementation report — config/status/control persistence

Date: 2026-09-06  
Branch: `feat/hierarchical-integration-trains`  
Migration: `a11a5e1e4f04` after reviewed head `a10c5e1e4f03`

## Outcome

Task 11a's rollout foundation is implemented with all effective and desired defaults
disabled.  This slice performs no enablement, provider probe, forge mutation, operator
configuration change, daemon startup, or live GitHub call.  Worker authority remains a
server-observed concern for 11b; there is no YAML confinement assertion.

## Implementation

### Closed restart-required configuration

- Added frozen `ScratchProbeConfig` with exactly the numeric scratch repository ID,
  exact `owner/repository` name, and distinct negative client/App/installation identity
  plus private-key path reference.  `integration.github_app` remains the sole positive
  identity.
- Raw validation runs before environment substitution for both `github_app` and
  `scratch_probe`. Unknown or inline key/PEM/token/body/auth-like fields are rejected
  without reflecting the rejected key or value. Typed validation requires positive IDs,
  non-empty references, one exact repository slash, the positive identity, and distinct
  positive/negative identities and key paths.
- The complete `integration` section is restart-required. Neither `AppConfig.reload()`
  nor `ConfigWatcher.reload()` swaps cached integration state.
- `update_config` command logging now records only safe section/dry-run metadata and a
  redaction marker. Rejected nested configuration and `get_config` responses do not
  serialize sentinels.

### Rollout control persistence

- Projects now carry typed `hierarchical_integration_desired_mode`,
  `hierarchical_integration_draining`, and
  `hierarchical_integration_generation`; existing `integration_mode` and effective
  `hierarchical_integration_mode` remain intact. New projects default to
  disabled/false/zero, and migration backfills desired mode from the existing effective
  mode.
- Generic `update_project` refuses desired/draining/generation updates; later controls
  must use generation CAS. The existing effective field is retained for compatibility,
  while 11c owns its command/runtime guard and actual cutover.
- Added append-only rollout transitions, history waivers, waiver consumptions, and
  legacy-gate applicability evidence, plus reversible per-project legacy suppression.
  Transition rows retain complete old/new legacy policy JSON, operator, reason, exact
  lowercase SHA-256 blocker digest, optional waiver, and generation.
- Waiver applicability never mutates a pinned receipt or historical gate. SQLite and
  PostgreSQL triggers refuse UPDATE and DELETE on all four evidence tables.
- The downgrade checks draining/generation and every evidence/suppression table before
  any destructive DDL. It permits a generation-zero desired value that was solely
  backfilled from effective state, because downgrade retains the effective field and a
  later upgrade deterministically reconstructs desired state.

### Read-only status and explain

- Added `IntegrationStatusService.status(project_id)` and `task_blockers(task_id)`.
  Both resolve relationships server-side and read through one SQLite transaction or one
  PostgreSQL repeatable-read transaction, always rolled back. They perform no writes or
  provider I/O.
- Project status includes effective/desired/draining/generation/repository, schedule,
  active batch/revision/members, parent readiness, branch ownership, project lease,
  repair stages/budgets/deadlines/human state, safe CI identity, promotion and
  reconciliation summary, normalized pending cleanup (including irreversible-prewrite
  marker), latest persisted `integration_release_results`, and legacy suppression.
- Blockers are de-duplicated and sorted by `(code, ref, detail)`. Implemented vocabulary:
  `open_child`, `missing_receipt`, `stale_head`, `stale_generation`, `stale_review`,
  `repository_not_designated`, `active_owner`, `pending_ci`, `budget_exhausted`,
  `human_hold`, `cleanup_conflict`, plus the fail-closed foundation code
  `preflight_evidence_unavailable` until 11b persists transport/protection/probe facts.
- `status.ready` and its explicit alias `status.rollout_ready` mean rollout/preflight
  eligibility, not ordinary task schedulability. Thus a disabled default is inert but
  not rollout-ready without designated repository and preflight evidence.
- Task explain adds integration blockers only when effective or desired integration mode
  is non-disabled. Existing disabled projects retain their ordinary explanation behavior.

## Stable downstream interfaces for 11b/11c

Configuration type:

```python
ScratchProbeConfig(
    repository_id: int,
    repository_full_name: str,
    negative_client_id: str,
    negative_app_id: int,
    negative_installation_id: int,
    negative_private_key_path: str,
)
IntegrationConfig.scratch_probe: ScratchProbeConfig | None
```

Typed project fields:

```python
Project.hierarchical_integration_desired_mode: str = "disabled"
Project.hierarchical_integration_draining: bool = False
Project.hierarchical_integration_generation: int = 0
```

Connection-owned write primitives (all accept an existing `AsyncConnection`):

```python
cas_project_integration_control_on(
    conn, *, project_id, expected_generation, effective_mode, desired_mode, draining
) -> bool
append_integration_rollout_transition_on(
    conn, *, transition_id, project_id, generation,
    old_effective_mode, new_effective_mode, old_desired_mode, new_desired_mode,
    draining, operator_id, reason, blocker_digest,
    old_legacy_policy, new_legacy_policy, waiver_id, now
) -> None
append_integration_history_waiver_on(
    conn, *, waiver_id, project_id, operator_id, reason, blocker_digest, now
) -> None
consume_integration_history_waiver_on(
    conn, *, waiver_id, transition_id, project_id, blocker_digest, consumed_by, now
) -> bool
append_integration_legacy_gate_applicability_on(
    conn, *, project_id, gate_id, waiver_id, transition_id,
    blocker_digest, applicable, now
) -> None
set_integration_legacy_suppression_on(
    conn, *, project_id, generation, merge_sweep_suppressed,
    final_review_route_suppressed, legacy_gate_creation_suppressed,
    policy_snapshot, now
) -> None
```

Convenience reads/writes:

```python
consume_integration_history_waiver(**values) -> bool
get_integration_legacy_suppression(project_id: str) -> dict | None
IntegrationStatusService(db).status(project_id: str) -> dict | None
IntegrationStatusService(db).task_blockers(task_id: str) -> dict | None
```

11c must call the connection-owned methods inside its hierarchy-locked transaction;
11a deliberately does not expose an enablement command or perform an effective-mode
transition.

Schema names added by `a11a5e1e4f04`:

- Project columns: `hierarchical_integration_desired_mode`,
  `hierarchical_integration_draining`, `hierarchical_integration_generation`.
- Tables: `integration_history_waivers`, `integration_rollout_transitions`,
  `integration_history_waiver_consumptions`,
  `integration_legacy_gate_applicability`, `integration_legacy_suppression`.
- Every new PK/FK/UQ/check constraint is explicitly named. The first four tables are
  immutable evidence; only `integration_legacy_suppression` is reversible.

## TDD evidence

### RED

1. Config closure/reload/redaction:

```text
aq test tests/test_config_validation.py::TestScratchProbeConfigValidation tests/test_config_editor.py::TestUpdateConfigCommand::test_rejected_nested_scratch_secret_never_reaches_logs_or_response tests/test_config_watcher.py::TestConfigWatcher::test_integration_credentials_require_restart_and_cached_state_is_not_swapped -x
ERROR collecting tests/test_config_validation.py
ImportError: cannot import name 'ScratchProbeConfig' from 'src.config'
```

2. Persistence/status foundation:

```text
aq test tests/test_integration_controls.py -x
ERROR collecting tests/test_integration_controls.py
ImportError: cannot import name 'integration_history_waivers' from 'src.database.tables'
```

3. Explain integration vocabulary:

```text
aq test tests/test_explain.py::TestExplainCommand::test_integration_reasons_append_without_replacing_ordinary_explanations tests/test_explain.py::TestExplainCommand::test_disabled_project_does_not_add_integration_reasons -x
FAILED ... repository_not_designated absent
```

4. CAS-only control state:

```text
aq test tests/test_integration_controls.py::test_project_control_state_is_typed_and_defaults_disabled -x
FAILED: DID NOT RAISE <class 'ValueError'>
```

5. Fail-closed disabled rollout readiness:

```text
aq test -m "not slow and not tmux" tests/test_integration_controls.py::test_project_control_state_is_typed_and_defaults_disabled -x
FAILED: assert status["ready"] is False (was True)
```

6. Exact blocker digest:

```text
aq test -m "not slow and not tmux" tests/test_integration_controls.py::test_waiver_consumption_and_gate_applicability_are_append_only -x
FAILED: DID NOT RAISE <class 'ValueError'>
```

7. Latest release after terminal batch:

```text
aq test -m "not slow and not tmux" tests/test_integration_controls.py::test_status_is_read_only_sorted_and_absent_preflight_is_blocking -x
FAILED: TypeError: status["release"] is None
```

Migration iteration also exposed two fixture/expectation defects before exercising the
new revision: setup initially ran an older hierarchy migration without initialized
prerequisites on each backend, and PostgreSQL trigger failures surface as the broader
`SQLAlchemyError` rather than SQLite's `IntegrityError`. The fixtures were corrected to
initialize adapter-owned schemas first and assert the portable exception base.

### GREEN

```text
aq test tests/test_config_validation.py::TestScratchProbeConfigValidation tests/test_config_editor.py::TestUpdateConfigCommand::test_rejected_nested_scratch_secret_never_reaches_logs_or_response tests/test_config_editor.py::TestGetConfigSchemaCommand::test_returns_schema tests/test_config_watcher.py::TestConfigWatcher::test_integration_credentials_require_restart_and_cached_state_is_not_swapped -x
16 passed
```

```text
aq test -m "not slow and not tmux" tests/test_integration_controls.py tests/test_explain.py::TestExplainCommand::test_integration_reasons_append_without_replacing_ordinary_explanations tests/test_explain.py::TestExplainCommand::test_disabled_project_does_not_add_integration_reasons -x
7 passed
```

```text
POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/integration_test aq test -m migration tests/test_migration_integration_controls.py -x
2 passed in 4.63s
```

The migration test creates a unique helper-owned PostgreSQL scratch database, exercises
upgrade/backfill/downgrade/re-upgrade, all named constraints, UPDATE and DELETE refusal
for every immutable table, and the pre-DDL live-evidence downgrade guard, then disposes
connections and drops that scratch database. SQLite executes the same assertions on a
temporary test-owned file.

Final explicit affected-area gate (the only broad gate for this slice):

```text
POSTGRES_TEST_DSN=postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/integration_test aq test -m "not slow and not tmux" tests/test_config_validation.py tests/test_config_editor.py tests/test_config_watcher.py tests/test_integration_controls.py tests/test_explain.py tests/test_migration_integration_controls.py -x
296 passed, 11 warnings in 6.99s
PostgreSQL migration node: 2.64s; SQLite migration node: 0.38s
```

Hygiene:

```text
ruff check <all changed Python paths>
All checks passed!
git diff --check
(no output)
python3 -m alembic heads
a11a5e1e4f04 (head)
```

The installed `alembic` console script has an invalid interpreter in this workspace
(`/home/jkern/.local/bin/alembic: cannot execute: required file not found`), so the
equivalent repository interpreter invocation `python3 -m alembic heads` was used.

## Changed paths

- `.superpowers/sdd/2026-09-04-hierarchical-integration-trains-implementation/task-11a-report.md`
- `migrations/versions/a11a5e1e4f04_integration_rollout_controls.py`
- `src/commands/handler.py`
- `src/commands/system_commands.py`
- `src/commands/task_commands.py`
- `src/config.py`
- `src/database/adapters/postgresql.py`
- `src/database/adapters/sqlite.py`
- `src/database/queries/integration_control_queries.py`
- `src/database/queries/project_queries.py`
- `src/database/tables.py`
- `src/explain.py`
- `src/integration/status.py`
- `src/models.py`
- `tests/test_config_editor.py`
- `tests/test_config_validation.py`
- `tests/test_config_watcher.py`
- `tests/test_explain.py`
- `tests/test_integration_controls.py`
- `tests/test_migration_integration_controls.py`

## Deliverable reconciliation and self-review

- [x] Existing project integration fields preserved; separate typed desired/drain/CAS
  generation added with disabled defaults and migration backfill.
- [x] Conn-owned CAS/audit/waiver/consumption/applicability/suppression primitives work
  on both adapters and are ready for one later hierarchy-locked transaction.
- [x] History is immutable on SQLite and PostgreSQL; downgrade checks all live evidence
  before destructive DDL; constraints are named.
- [x] Scratch config is closed and non-secret, positive/negative identities are distinct,
  integration is restart-required, and logging/editor/get-config leak tests pass.
- [x] Status is one-snapshot/read-only, fail-closed, normalized over reviewed cleanup and
  release persistence, and exposes sorted stable blockers without raw argv/credentials.
- [x] Explain remains additive and preserves disabled-project ordinary behavior.
- [x] No probe schema, transport/protection architecture, enablement/control calls,
  operator state, daemon startup, external mutation, or worker-confinement claim added.
- [x] Focused RED/GREEN evidence, dual-dialect migrations, one final area gate, changed-file
  Ruff, diff hygiene, and migration-head check completed.

Self-review specifically checked project hydration/default compatibility, adapter parity,
caller-owned transaction boundaries, CAS generation checks, exact waiver binding and
single consumption, immutable triggers, guard-before-DDL ordering, secret-safe error and
log serialization, status snapshot rollback, server-side task/project resolution, stable
sorting, disabled/task-readiness semantics, latest release visibility, and preservation of
cleanup irreversible markers.

## Concerns / downstream notes

- `preflight_evidence_unavailable` intentionally keeps rollout readiness false until 11b
  defines and persists the reviewed transport/protection/probe evidence. 11a does not
  speculate about that schema or mark the environment safe.
- 11c must compose hierarchy locking, preflight revalidation, CAS, transition append,
  waiver consumption/applicability, schedule changes, and suppression in one transaction;
  the primitives intentionally do not create an operator command or independently change
  effective mode.
- Existing generic updates of the pre-existing effective mode remain source-compatible in
  this foundation.  The command/cutover authorization and legacy runtime guard belong to
  11c and must not treat that compatibility path as an enablement API.

## Review fix round 1 — 2026-09-06

All five Important findings in `task-11a-review.md` were addressed without adding any
11b transport/probe implementation or 11c operator control.

### 1. Allowed-field credential value closure

- Known fields now have value-level contracts, not merely an allowlist of field names.
  GitHub App client IDs use the exact `Iv1.<identifier>` shape; numeric identities are
  positive non-boolean integers; key references are normalized absolute POSIX path
  shapes with no empty, dot, or dot-dot components; and scratch repositories use bounded
  GitHub owner/repository components with no whitespace, query string, or newline.
- Raw validation permits a single `${ENV_NAME}` reference, validates once before
  substitution, then validates the expanded value again. Errors remain generic and do
  not reflect rejected values.
- Dataclass metadata supplies the same minimum/pattern/`anyOf` constraints to the
  generated closed schema. `get_config`, update validation, and watcher reload reject
  an inline PEM placed in an otherwise allowed path field without serializing it.

RED:

```text
aq test <7 focused config validation/editor/watcher nodes> -x
3 failed: unsafe client/path accepted; allowed scratch PEM returned by get_config;
environment-expanded PEM accepted by watcher reload.

pytest -q tests/test_config_validation.py::TestGitHubAppConfigValidation::test_rejects_unsafe_allowed_identity_and_reference_values -x
1 failed, 2 passed: arbitrary inline-secret-token still matched generic identity syntax.
```

GREEN:

```text
aq test <the same 7 focused config validation/editor/watcher nodes> -x
23 passed, 9 inherited warnings in 2.23s

pytest -q tests/test_config_validation.py::TestGitHubAppConfigValidation::test_rejects_unsafe_allowed_identity_and_reference_values -x
6 passed, 2 inherited warnings in 0.21s
```

Self-review mutation checks: removing either pre- or post-substitution validation makes
the environment PEM cases fail; widening client ID syntax makes the token-shaped case
fail; weakening path/repository syntax fails literal PEM, relative/traversal path,
space/query/newline repository cases; removing schema metadata or raw get validation
fails editor boundary assertions.

### 2. Real SQLite status snapshot

`IntegrationStatusService` now puts SQLite connections in AUTOCOMMIT driver mode and
issues explicit `BEGIN`/`ROLLBACK`. This compensates for sqlite3 legacy transaction
behavior, where SQLAlchemy's logical `begin()` did not start a transaction for SELECTs.
PostgreSQL retains its repeatable-read transaction.

RED/GREEN:

```text
aq test -m "not slow and not tmux" tests/test_integration_controls.py::test_sqlite_status_snapshot_excludes_an_intervening_writer tests/test_integration_controls.py::test_status_is_read_only_sorted_and_absent_preflight_is_blocking -x
RED: 1 failed — the status projection observed a schedule committed after its project read.
GREEN: 2 passed, 8 inherited warnings in 1.77s — status excluded the intervening row,
while a fresh connection proved the writer's schedule was durably committed.
```

Self-review verified the test uses the real file-backed NullPool/WAL engine and a separate
`immediate()` writer, so it proves a database snapshot rather than an in-memory cache.

### 3. Shared parent receipt applicability

Status removed its source/target existence query and directly calls the established
connection-owned `ParentCompletion.readiness_on(...)` inside the same status snapshot.
Only the established reasons are translated to stable status codes:
`receipt_missing`/`failed_child` to `missing_receipt`, `receipt_chain` to `stale_head`,
`origin_mismatch` to `repository_not_designated`, and non-terminal children to
`open_child`. The full established selector continues to own multiple receipt history,
current head/repository/branch/operation/episode binding, disposition revision/evidence,
carry-forward ancestry, and FAILED-child policy.

RED/GREEN:

```text
aq test tests/test_integration_parent_completion.py::test_status_uses_current_receipt_among_multiple_historical_deliveries tests/test_integration_parent_completion.py::test_status_rejects_receipt_not_applicable_to_current_parent_context tests/test_integration_parent_completion.py::test_status_blocks_failed_child_without_current_disposition_receipt -x
RED: MultipleResultsFound on legitimate historic + current receipt rows.
GREEN: 6 passed, 8 inherited warnings in 3.19s.

aq test tests/test_integration_parent_completion.py::test_disposition_revision_supersedes_only_changed_child tests/test_integration_parent_completion.py::test_parent_completion_pins_exact_verification_for_rollover -x
2 passed, 8 inherited warnings in 2.68s.
```

Self-review checked that no receipt or gate mutability changed and no second applicability
implementation remains in status. The focused cases cover multiple receipts, wrong head,
repository, branch and current episode binding, FAILED children, revised disposition,
and accepted carry-forward.

### 4. Typed current-stage repair budgets and deadlines

Status accepts an injectable read clock and validates each current stage's persisted
policy with `RepairPolicy`. Ordinal zero uses `primary_attempts`; ordinal one uses
`debug_attempts`. Only the operation's current active/awaiting-completion stage is
evaluated. Attempt exhaustion applies while the stage is active. Deadline exhaustion
applies to active stages and to parent final verification while awaiting completion.
A root exact-green stage awaiting deterministic promotion is not reported expired merely
because wall clock passed; if a candidate rebuild returns it to active, the original
deadline immediately blocks, matching the existing RepairService ruling.

RED/GREEN:

```text
aq test -m "not slow and not tmux" tests/test_integration_parent_completion.py::test_status_uses_typed_limit_for_current_repair_stage tests/test_integration_parent_completion.py::test_parent_current_stage_remains_deadline_bound_while_awaiting_completion tests/test_integration_controls.py::test_root_current_stage_deadline_preserves_green_awaiting_promotion -x
RED: 2 failed — IntegrationStatusService had no clock/current typed policy behavior.
GREEN: 5 passed, 8 inherited warnings in 2.85s.
```

Self-review checked primary/debug limits independently, current-stage selection, parent
awaiting-completion deadline binding, root active overdue behavior, and the root
awaiting-promotion exception.

### 5. Meaningful no-mutation evidence

The ineffective cross-connection SQLite `total_changes()` comparison was removed. The
status test now installs a real engine statement observer that raises immediately on
`INSERT`, `UPDATE`, `DELETE`, or `REPLACE`, proves SELECTs were issued, and exercises the
full service. The separate intervening-writer test also proves status itself did not
produce the durable schedule row.

Self-review mutation check: adding any DML to the service makes the observer raise rather
than comparing two fresh per-connection counters that always start at zero.

### Amended-scope gate and warning disposition

No migration test or prior 296-test area gate was repeated. The single review-fix gate
covered only changed behavior and the narrow shared parent-readiness consumers:

```text
aq test -m "not slow and not tmux" \
  tests/test_config_validation.py::TestGitHubAppConfigValidation \
  tests/test_config_validation.py::TestScratchProbeConfigValidation \
  tests/test_config_editor.py::TestGetConfigCommand \
  tests/test_config_editor.py::TestGetConfigSchemaCommand::test_returns_schema \
  tests/test_config_editor.py::TestUpdateConfigCommand::test_rejected_nested_scratch_secret_never_reaches_logs_or_response \
  tests/test_config_watcher.py::TestConfigWatcher::test_integration_credentials_require_restart_and_cached_state_is_not_swapped \
  tests/test_config_watcher.py::TestConfigWatcher::test_environment_expanded_inline_key_is_rejected_without_leak_or_swap \
  tests/test_integration_controls.py \
  tests/test_explain.py::TestExplainCommand::test_integration_reasons_append_without_replacing_ordinary_explanations \
  tests/test_explain.py::TestExplainCommand::test_disabled_project_does_not_add_integration_reasons \
  <7 named status/parent applicability, budget, disposition, and rollover nodes> -x
66 passed, 11 warnings in 5.69s
```

All warnings are inherited dependency deprecations, not introduced warnings:

- `src/_compat.py` imports the deprecated `pkg_resources` API (reported once per xdist
  process).
- system setuptools reports deprecated `pkg_resources.declare_namespace('zope')`
  (reported once per xdist process).
- Discord's installed `player.py` imports Python's deprecated `audioop` module (reported
  once per xdist process for command-handler tests).

Review-fix changed paths: `src/config.py`, `src/config_editor.py`,
`src/integration/status.py`, `tests/test_config_validation.py`,
`tests/test_config_editor.py`, `tests/test_config_watcher.py`,
`tests/test_integration_controls.py`, `tests/test_integration_parent_completion.py`, and
this report. No schema/migration/query-control path changed.
