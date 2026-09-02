# Pools Exit Gate A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adversarially verify the combined pool launch, PostgreSQL claim-locking, and pool-hardening candidate, then demonstrate one real Codex pool loop and one real Claude pool loop on an isolated PostgreSQL-backed daemon.

**Architecture:** Review and integrate `origin/aq/smart-orbit.1`, the combined prerequisite head containing bright-stone and agile-ridge. Exercise database behavior with the repository's dedicated PostgreSQL test database, then run the candidate worktree as a separate local daemon with its own API port, vault copy, data directory, and PostgreSQL database so the production queue and daemon remain untouched.

**Tech Stack:** Python 3.12, pytest/pytest-asyncio, SQLAlchemy/asyncpg, PostgreSQL, tmux, Codex CLI, Claude CLI, aq CLI.

**Spec:** `docs/superpowers/specs/2026-08-28-swarm-work-model-design.md` §§9–11 and `docs/specs/config.md` §4.11; task `smart-orbit.3` is the acceptance specification.

## Global Constraints

- PostgreSQL is required for every locking-sensitive assertion; SQLite-only success is insufficient.
- `swarm.enabled` and `sessions.enabled` must both be true for live pool launches.
- Pool size is `busy + ready`, clamped by `min_active`, `max_active`, and project `max_concurrent_agents`.
- The live exercise must use `lifecycle: pool`, `max_active: 2`, and restore both tested profiles to `lifecycle: task` afterward.
- No second orchestrator may share the production database; the candidate daemon must have its own PostgreSQL database and API port.
- Run focused tests while investigating and one broader pool/claim area sweep at the end, per the supervisor comment on `smart-orbit.3`.

## Gate Outcome

**NEEDS_CONTEXT.** The static review, focused PostgreSQL tests, Codex live loop,
profile restoration, doctor checks, and broader 349-test PostgreSQL sweep all
passed. The Claude live loop reproduced a substantive close/release race twice:
the task became terminal while the pool session still held its task id, allowing
`SessionReconciler._step_orphans` to terminate the worker as `orphaned` before
claim-next/no-ready-work and graceful scale-down. The deterministic fix is filed
as `smart-orbit.8`. A separate late trust-dialog readiness race found during
startup is filed as `smart-orbit.7`.

The live run used the repository's established Tier 2 e2e environment
(`agent_queue_e2e_live`, API port 8098) rather than creating another temporary
root. The candidate daemon and its tmux server were stopped; production stayed
healthy and its database was not used for live smoke tasks.

---

### Task 1: Integrate and review the candidate bundle

**Files:**
- Review: `src/orchestrator/pools.py`
- Review: `src/database/queries/claim_queries.py`
- Review: `src/commands/claim_commands.py`
- Review: `src/sessions/spec.py`
- Review: `src/sessions/reconciler.py`
- Review: `src/doctor/pool_checks.py`
- Review: `tests/test_pool_lifecycle_integration.py`

**Interfaces:**
- Consumes: `origin/aq/smart-orbit.1` and swarm-work-model §§9–11.
- Produces: this gate branch containing the exact combined prerequisite history and an adversarial review record.

- [x] **Step 1: Merge the combined prerequisite head**

Run: `git merge --no-ff origin/aq/smart-orbit.1 -m "merge: integrate pool launch claim and hardening fixes"`

Expected: a clean merge containing bright-stone's UUID session identity, agile-ridge's `FOR UPDATE OF tasks SKIP LOCKED`, and pool-hardening.

- [x] **Step 2: Review the merged diff against the design**

Run: `git diff origin/main...HEAD -- src/orchestrator/pools.py src/database/queries/claim_queries.py src/commands/claim_commands.py src/sessions/spec.py src/sessions/reconciler.py src/doctor/pool_checks.py tests/test_pool_lifecycle_integration.py`

Expected: pool IDs are canonical UUIDs while readable session names stay separate; PostgreSQL locks only `tasks`; rollback, quarantine, claim fencing, fresh-context recycling, scale-down, and agent retirement match §§10–11.

- [x] **Step 3: Record the reviewed candidate identity**

Run: `aq task comment smart-orbit.3 --body "Review subject: origin/aq/smart-orbit.1 merged into aq/smart-orbit.3; reviewed UUID launch identity, PostgreSQL task-only row locking, claim/recycle/drain paths, and doctor classifications against swarm-work-model §§9–11."`

Expected: a durable task comment naming the exact combined head.

### Task 2: Re-run the prerequisite tests on PostgreSQL

**Files:**
- Test: `tests/test_claim_queries.py`
- Test: `tests/test_pool_reconciler.py`
- Test: `tests/test_pool_lifecycle_integration.py`
- Test: `tests/test_pool_doctor.py`

**Interfaces:**
- Consumes: `POSTGRES_TEST_DSN` prepared by `tests.pg_dsn.ensure_worker_postgres_dsn`.
- Produces: direct evidence that the routing outer join is claimable and the full pull loop passes on PostgreSQL.

- [x] **Step 1: Verify PostgreSQL is available**

Run: `test -n "$POSTGRES_TEST_DSN" && pg_isready -d "$POSTGRES_TEST_DSN"`

Expected: PostgreSQL reports accepting connections.

- [x] **Step 2: Run the lock regression and full lifecycle integration module**

Run: `pytest -q tests/test_claim_queries.py::TestClaimTransaction::test_routed_work_query_locks_only_tasks_on_postgres tests/test_pool_lifecycle_integration.py`

Expected: both SQLite and PostgreSQL parameter arms pass; no PostgreSQL arm is skipped.

- [x] **Step 3: Run focused pool launch and doctor suites**

Run: `pytest -q tests/test_pool_reconciler.py tests/test_pool_doctor.py`

Expected: all tests pass.

- [x] **Step 4: Record PostgreSQL evidence**

Run: `aq task comment smart-orbit.3 --body "PostgreSQL gate: routed outer-join claim regression and full pool lifecycle integration passed with the PostgreSQL parameter arms enabled; focused reconciler and pool doctor suites also passed."`

Expected: a durable test-evidence comment.

### Task 3: Build the isolated live candidate daemon

**Files:**
- Create temporarily outside git: a candidate config and vault copy under a `mktemp -d` directory.
- Read: `~/.agent-queue/config.yaml`
- Read: `~/.agent-queue/vault/agent-types/worker-standard-high-codex.md`
- Read: one installed Claude worker profile in `~/.agent-queue/vault/agent-types/`.

**Interfaces:**
- Consumes: the current production configuration shape and existing installed harness profiles.
- Produces: a local candidate daemon on a non-production port and dedicated PostgreSQL database.

- [x] **Step 1: Create a dedicated temporary root and PostgreSQL database**

Run: `mktemp -d -t aq-pool-gate-a-XXXXXXXX`

Expected: a unique explicit path recorded for cleanup; create a unique database name derived from `smart_orbit_3` and the process id using PostgreSQL administrative commands.

- [x] **Step 2: Copy only the needed vault/profile structure and write an isolated config**

Expected config differences: unique API port, unique data/workspace/vault paths, dedicated PostgreSQL URL, `sessions.provider: tmux`, `swarm.enabled: true`, and a short but non-zero `swarm.scale_down_grace` suitable for observing scale-down.

- [x] **Step 3: Start the candidate daemon from this worktree**

Run shape: `python -m src.main <temporary-config-path>`

Expected: health endpoint becomes ready, migrations complete on the dedicated PostgreSQL database, and logs are captured under the temporary root.

### Task 4: Demonstrate a real Codex pool loop

**Files:**
- Modify temporarily: copied vault project override for `worker-standard-high-codex`.
- Observe: candidate daemon structured log.

**Interfaces:**
- Consumes: real Codex harness binary and the candidate daemon's local-operator CLI scope.
- Produces: launch, claim, prime, complete, claim-next/no-ready-work, scale-down, retired-agent, and doctor-clean evidence.

- [x] **Step 1: Set the Codex project profile to pool lifecycle**

Set `lifecycle: pool`, `min_active: 0`, and `max_active: 2` in the copied vault profile, then reload the candidate daemon config/profile catalog.

- [x] **Step 2: Create one minimal smoke task pinned to `worker-standard-high-codex`**

The task description must instruct the worker to run `aq prime`, make no repository changes, close successfully with `--claim-next`, and drain when the next claim returns `no_ready_work`.

- [x] **Step 3: Observe the complete Codex lifecycle**

Run repeatedly against the candidate API: `aq pool status --project-id agent-queue`, `aq session list`, targeted task/session reads, and daemon log filtering.

Expected sequence: a pool session launches, claims the smoke task, renders pool prime, closes it, performs claim-next, reports `no_ready_work`, remains idle through the configured grace, receives drain, stops, and leaves its agent row retired/reusable as specified.

- [x] **Step 4: Run pool/claim doctor checks**

Run: `aq doctor --json --check claims.holder_consistency --check pools.disabled --check pools.orphan_agents --check pools.preparing_stuck --check pools.stuck`

Expected: every registered check is `ok` after the worker stops.

- [x] **Step 5: Restore the Codex profile to task lifecycle**

Set `lifecycle: task` and remove pool-only bounds in the copied vault, reload, and confirm `aq pool status --project-id agent-queue` contains no Codex pool row.

### Task 5: Repeat the live loop with Claude

**Files:**
- Modify temporarily: copied vault project override for an installed Claude worker profile.
- Observe: candidate daemon structured log.

**Interfaces:**
- Consumes: real Claude harness binary and canonical UUID `--session-id` behavior.
- Produces: a second full loop proving the launch fix on the harness that requires UUID session IDs.

- [x] **Step 1: Set the chosen Claude project profile to pool lifecycle with `max_active: 2`**

Expected: `aq pool status` shows demand/bounds for the Claude profile.

- [ ] **Step 2: Create and observe one Claude smoke task — FAILED GATE**

Expected: the session row ID is a canonical UUID, its readable name uses the `p-<profile>--<project>--<suffix>` form, the actual Claude command receives that UUID via `--session-id`, and the same claim/prime/close/no-ready-work/scale-down/drain sequence completes.

- [x] **Step 3: Run doctor and restore the Claude profile to task lifecycle**

Expected: doctor is clean and `aq pool status` has no rows after both profiles are restored.

### Task 6: Final verification, cleanup, and delivery

**Files:**
- Verify all files changed from `origin/main`.
- Remove the temporary daemon root and drop only the dedicated PostgreSQL database created in Task 3.

**Interfaces:**
- Consumes: all review, test, and live evidence.
- Produces: a pushed gate branch and explicit aq task close.

- [x] **Step 1: Stop the candidate daemon and remove isolated state**

Expected: the candidate daemon exits; its tmux sessions are gone; only the explicit temporary directory and dedicated database are removed; production daemon/config/database/vault remain unchanged.

- [x] **Step 2: Run the one broader pool/claim area sweep**

Before the command run: `aq task heartbeat smart-orbit.3`

Run: `pytest -q tests/test_claim_queries.py tests/test_claim_commands.py tests/test_pool_sizing.py tests/test_pool_reconciler.py tests/test_pool_reconciler_carveouts.py tests/test_pool_lifecycle_integration.py tests/test_pool_doctor.py tests/test_session_reconciler.py tests/test_session_spec.py`

Expected: every selected test passes with PostgreSQL arms enabled and no unexpected skips.

- [x] **Step 3: Run static checks on changed Python files**

Run: `ruff check $(git diff --name-only origin/main...HEAD -- '*.py')`

Expected: no lint findings.

- [x] **Step 4: Commit gate evidence and push the branch**

Run: `git status --short`, then commit only reviewed gate artifacts/fixes and `git push -u origin HEAD`.

Expected: `origin/aq/smart-orbit.3` contains the combined candidate plus gate evidence/fixes.

- [ ] **Step 5: Close the aq task `needs_context` and drain**

Run: `aq task close smart-orbit.3 --outcome needs_context --work-outcome partial --summary "PostgreSQL suites and the Codex live loop passed, but two Claude live runs exposed the pool close/release orphan race filed as smart-orbit.8; late trust-dialog readiness is filed as smart-orbit.7; both profiles restored and doctor clean."`

Run: `aq session drain-ack`

Expected: task closes with the substantive gap named so dependent cutover work does
not advance until the pool race is fixed and re-gated.
