# Task 5 report — isolated child origins and hierarchy guards

## Outcome

Implemented the Task 5 hierarchy foundation behind the project-scoped durable mode
`disabled|observe|hierarchy|train` and a separately designated integration repository.
`disabled` and `observe` preserve legacy routing and launch behavior; hierarchy behavior
is active only for `hierarchy` and `train` projects.

The implementation uses the existing durable branch origins and integration
checkpoints, canonical top-down branch naming and atomic child filing, strict absent-or-exact branch
materialization, hierarchy mutation guards, claim/readiness exclusion for pending
origins, and fenced exact-origin preparation for direct and pooled launches. No
Task 6 review completion/parent verification, repair loop, or train scheduler was
implemented. No operator database was migrated and no project mode was enabled.

## Implementation

- Added migration `c7a1e5d92f40` (down revision `b91e4d7a2c10`) for only the project
  rollout mode and designated-repository columns plus the mode constraint. The
  `task_branch_origins` and `task_integration_checkpoints` tables were introduced by
  Task 1 and are consumed here, not created by this migration.
- Added `HierarchyIntegration` operations for atomic child filing, checkpoints,
  guarded hierarchy mutations, origin materialization, and pending-origin outbox work.
- Registered `integration.branch_materialization_pending`; pending origin rows remain
  the durable scanner source and carry `project_id` plus `operation_id`.
- Enforced immutable first-child bases across later sibling filing. Branchless ancestor
  origins are reserved top-down from the designated base without requiring workers.
- Materialization creates only an absent remote ref at the exact expected SHA or accepts
  an already-exact ref. An unexpected existing ref is rejected; the Task 3 push helper
  was not weakened.
- Production checkpointing verifies the owned workspace's clean status, canonical
  branch, actual local HEAD, and exact server-side remote ref rather than trusting a
  caller SHA or remote HEAD alone.
- Routed task creation and proposal batches through the canonical hierarchy lock and
  transaction. Added delete/archive/reparent/reopen/disposition guards and checkpoint
  invalidation/generation bumps at canonical mutation seams.
- Added the enabled-project materialized-origin predicate to canonical readiness and
  claim queries so pending origins cannot be claimed through direct paths.
- Added owner-fenced workspace preparation, attachment CAS, and session startup. A
  durable `starting` session and ownership attachment commit before provider start;
  the same ownership exclusion spans Git preparation/provider start and the running
  transition. Failed starts retain a stopped session proof; cancellation after provider
  start leaves recoverable `starting` state.
- Pooled claims resolve the exact materialized origin, reset to its pinned base, attach
  the existing session/workspace, and keep reset/claim-file/activation inside the same
  owner exclusion. Collector-owned branches cannot be reset or activated.
- Startup reconciliation uses the same owner fence. A stale scan rechecks after taking
  the fence, so it cannot stop/release a workspace while a fenced provider start is in
  progress.
- Enabled-task context uses the immediate parent origin ref as the PR base.

## Test and check evidence

TDD and focused seam runs:

- `aq test tests/test_integration_hierarchy.py tests/test_hierarchy_queries.py tests/test_branch_isolated_workspace.py -x`
  — **87 passed, 5 skipped**.
- `aq test tests/test_integration_hierarchy.py tests/test_integration_ownership.py tests/test_integration_contracts.py tests/test_workspace_branch_wait.py tests/test_claim_commands.py tests/test_session_commands.py tests/test_session_queries.py tests/test_session_reconciler.py -x`
  — **305 passed**.
- `pytest -q tests/test_integration_ownership.py -x` — **11 passed**.
- `pytest -q tests/test_workspace_branch_wait.py -x` — **7 passed**.
- `pytest -q tests/test_session_queries.py -x` — **25 passed**.
- Focused direct launch race tests — hierarchy starting persistence **1 passed**;
  transfer/failure **3 passed**; cancellation after provider start **1 passed**;
  startup-vs-reconciler **1 passed**.
- `pytest -q tests/test_claim_commands.py -x -k 'hierarchy_pool_claim or collector_owned_hierarchy'`
  — **2 passed**.
- Focused real-Git checkpoint/materialization tests — **2 passed**, covering dirty and
  unpushed refusal plus absent/exact/unexpected remote-ref behavior.
- `pytest -q tests/test_migration_single_head.py tests/test_database.py -x -k 'single_headed or alembic_revision_is_at_head'`
  — **2 passed, 71 deselected**.
- `pytest -q tests/test_missing_fk_migration.py -x -k upgrade_head_is_clean_sqlite -m migration`
  — **1 passed, 4 deselected**.
- `python3.12 -m alembic heads` — `c7a1e5d92f40 (head)`.
- The broader archive/proposal/event run initially reached **471 passed, 18 skipped**
  before exposing one canonical-event fixture failure listing 15 new integration event
  schemas. The minimal fixture was completed for all 15. Re-running
  `pytest -q tests/test_event_schema_registry_validation.py -x -k 'coverage_every_registered_schema_has_canonical_payload or canonical_payloads_validate'`
  produced **1 passed, 409 deselected**.
- `ruff check <all changed Python files>` — **All checks passed**.
- `git diff --check` — clean.

Process deviation: the two-file migration command above used bare `pytest` rather than
the required `aq test` wrapper. This report preserves the command and result as they
actually occurred; it was not rerun merely to rewrite the history.

The focused 305-test runtime set exercises both enabled and unmanaged/legacy paths and
is the unchanged-legacy proof. Mode checks are project-local and do not alter routing,
claim, workspace, or pool behavior for `disabled`/`observe` projects.

## Migration evidence

The SQLite replay started from a nonexistent pytest `tmp_path/clean.db` and called
`command.upgrade(cfg, "head")` with
`sqlite+aiosqlite:///<tmp_path>/clean.db`; it completed from an empty database at head.

The PostgreSQL replay started from a newly created, empty scratch database (there was
no `alembic_version` table before the run):

`postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/task5_c7a1e5d92f40_20260905`

The historical upgrade was invoked from the repository root with the exact
programmatic operation below; `AGENT_QUEUE_DB_URL` was removed so the explicit Config
URL could not be overridden:

```bash
env -u AGENT_QUEUE_DB_URL /usr/bin/python3.12 - <<'PY'
from alembic import command
from alembic.config import Config

cfg = Config("alembic.ini")
cfg.set_main_option(
    "sqlalchemy.url",
    "postgresql+asyncpg://integration_test:integration_test@127.0.0.1:16833/task5_c7a1e5d92f40_20260905",
)
command.upgrade(cfg, "head")
PY
```

The command completed at `c7a1e5d92f40`. Inspection confirmed:

- `projects.hierarchical_integration_mode`: non-null, default `'disabled'`
- `projects.integration_repository_id`: nullable
- `ck_projects_hierarchical_integration_mode` exists

The scratch database was dropped afterward. Worker AQ database environment variables
were not changed.

## Task 6 interfaces and boundaries

Task 6 should consume `HierarchyIntegration.file_children`, `checkpoint_parent`,
`mutate_hierarchy`, and `materialize_origin`, along with `task_branch_origins`,
`task_integration_checkpoints`, and the existing branch-ownership fence. Checkpoints expose
`generation`, `checkpoint_sha`, branch/ref identity, and verification state; child
origins pin the immediate parent's canonical ref and base SHA.

Collectors must continue using the same ownership row/fence rather than adding a
second lease. Review completion, parent verification, post-review delivery, repair,
and train scheduling remain deliberately unimplemented for Task 6 and later tasks.

## Review base and preserved external work

External mainline merge `de38e2c6` was present before the Task 5 commit and was
preserved unchanged. The scoped review base for Task 5 is therefore `de38e2c6` (not
the earlier pre-merge planning base `0f4f0270`).

## Review concerns

- A low-level service constructed without a workspace checkpoint verifier can still
  perform filing/materialization operations, but checkpointing now fails closed.
- Unknown provider-stop or Git-detach state deliberately retains the workspace lock and
  `handoff_pending` owner. A later transfer retry repeats exact proof and can recover.

## Review fix round 1 (base `131e56bd`)

All five Important findings from `task-5-review.md` were addressed without adding Task 6
delivery/review behavior:

1. Enabled slots and non-slot CLONE/LINK workspaces now use one exact-origin preparation
   helper. It holds the owner fence across clone/fetch/clean/checkout or slot reset,
   creates the canonical `aq/<task_id>` branch at the pinned `base_sha`, verifies HEAD,
   and skips legacy resume/default-branch cleanup. Transfer-before-preparation performs
   no Git mutation.
2. Failed startup and stale-STARTING recovery route through the existing transfer and
   server-side handoff confirmer. Stop, exact clean/pushed checkout proof, detach, owner
   release, and workspace unlock precede a fresh same-task reservation. Unknown stop or
   detach keeps both lock and fence; a later transfer retry is recoverable. Non-slot
   checkouts now receive the same exact detach proof as slots.
3. The materialized-origin predicate is shared by pooled claims, the push scheduler
   snapshot, pre-assignment execution, and the atomic assignment update. Pending tasks
   cannot become ASSIGNED/IN_PROGRESS or emit `task.started`.
4. `checkpoint_parent` refuses calls without a verifier; test fixtures that checkpoint
   now inject an explicit verifier.
5. `set_parent_bulk` rejects hierarchy/train projects, and `task_graph.creator.write_plan`
   performs that guard before task insertion or ordinal mutation. Disabled behavior is
   preserved.

Focused RED evidence:

- The first four-item review run failed exactly on missing scheduler eligibility,
  enabled bulk-parent acceptance, and missing-verifier checkpoint acceptance.
- The push-path regression then observed a pending-origin task transition to PAUSED via
  workspace preparation rather than remain READY.
- Non-slot preparation initially failed because the shared exact-origin helper did not
  exist.
- Startup/reconciler recovery initially left ownership attached or `handoff_pending`
  without the new proof-and-release path.
- The non-slot detached-handoff regression initially remained locked because only slots
  had exact Git detach proof.

Green verification:

- `aq test tests/test_integration_hierarchy.py tests/test_scheduler.py tests/test_hierarchy_queries.py tests/test_hierarchy_graph_creator.py -x -k 'without_verifier or pending_origin_is_not_push or enabled_project_rejects'`
  — **4 passed**.
- `aq test tests/test_scheduler.py tests/test_session_commands.py -x -k 'hierarchy_pending_origin'`
  — **2 passed**.
- `aq test tests/test_workspace_branch_wait.py -x -k 'hierarchy_non_slot or hierarchy_slot_prep or hierarchy_transfer_winning'`
  — **4 passed**.
- `aq test tests/test_integration_workspace_handoff.py tests/test_session_commands.py tests/test_session_reconciler.py -x -k 'detached_non_slot_releases or failed_start_releases_with_fence or failed_start_unknown_stop or stale_hierarchy_starting_release'`
  — **4 passed**.
- `aq test tests/test_integration_workspace_handoff.py -x` — **10 passed**.
- `aq test tests/test_integration_hierarchy.py tests/test_hierarchy_queries.py tests/test_hierarchy_graph_creator.py tests/test_scheduler.py tests/test_workspace_branch_wait.py tests/test_branch_isolated_workspace.py tests/test_session_commands.py tests/test_session_reconciler.py -x`
  — **336 passed, 5 skipped**.
- `aq test tests/test_claim_commands.py -x` — **57 passed**.
- Final combined verification:
  `aq test tests/test_integration_hierarchy.py tests/test_hierarchy_queries.py tests/test_hierarchy_graph_creator.py tests/test_scheduler.py tests/test_workspace_branch_wait.py tests/test_branch_isolated_workspace.py tests/test_integration_workspace_handoff.py tests/test_session_commands.py tests/test_session_reconciler.py tests/test_claim_commands.py -x`
  — **403 passed, 5 skipped**.
- `ruff check <all changed Python files>` — **All checks passed**.
- `git diff --check` — clean.

One attempted combined run also named `tests/test_claim_queries.py`, but collection stopped
before tests because that pre-existing file imports removed `src.models.PlaybookRun`.
The exact error was `ImportError: cannot import name 'PlaybookRun' from 'src.models'`.
No unrelated playbook/model change was made; claim command and hierarchy claim coverage
above remained green.
