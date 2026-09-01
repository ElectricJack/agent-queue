# Playbook V2 Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this roadmap work-package by work-package. Each work package requires its named child implementation plan before code changes begin. Checkboxes in this roadmap track package-level gates, not individual coding steps.

**Goal:** Replace the current loosely typed playbook implementations with one secure, durable, typed semantic graph that preserves explicit branching, makes every node's behavior understandable, and guarantees that displayed intent is derived from the same contracts used at execution time.

**Architecture:** Establish compiler authority and capability enforcement first; add typed command and event contracts plus contract-derived explanations; define the strict V2 artifact and compiler; add content-addressed storage and durable run state; move all playbook kinds onto one executor-based engine; expose the same semantic graph through the API and dashboard; rebuild and review every playbook; then drain and remove the V1 runtimes.

**Tech Stack:** Python 3.12+, Pydantic 2, SQLAlchemy async, Alembic, pytest, Ruff, React 19, TypeScript 5.7, TanStack React Query, XYFlow, Dagre, Vitest.

**Spec:** `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md`

---

## 1. How to use this roadmap

This is the cross-system delivery map, not a substitute for executable child plans. The Playbook V2 change spans the command boundary, authentication, profiles, compilation, persistence, runtime execution, API models, generated clients, dashboard behavior, migration, and final deletion of legacy code. Each work package below therefore has its own required child plan.

Before beginning a work package:

1. Confirm every package listed under **Consumes** has passed its exit gate.
2. Write the named child plan with task-sized red/green/refactor steps, exact symbols, test fixtures, and commit points.
3. Reconcile its file list against the live tree and this roadmap.
4. Do not weaken an earlier invariant to make a later package easier.
5. Stop at the package exit gate and review the evidence before advancing.

The roadmap intentionally permits V1 and V2 to coexist during migration. It does not permit two permanent command APIs or indefinite compatibility layers.

---

## 2. Global constraints

Every child plan and implementation commit must preserve these constraints.

### Authoring and artifacts

- Authors write English Markdown. They do not embed JSON action objects.
- The compiler emits a canonical, strict V2 JSON artifact.
- Pydantic models are the source of truth for the artifact schema.
- Generated JSON Schema and API models are downstream projections, not parallel definitions.
- V2 models use `extra="forbid"` throughout.
- Stored artifacts are content-addressed and immutable.
- Activation is an explicit database operation against a reviewed artifact hash.
- Compilation never activates an artifact automatically.
- A command contract change invalidates affected artifacts; operators rebuild and review them.
- The system supports one current command API only. It does not execute old command-contract versions.
- The compiled artifact preserves source references so operators can move from a node to its authoring prose.

### Security and authority

- Playbook-authored frontmatter cannot override server-owned profile, capability, budget, approval, or environment fields.
- Every AI-executing step names an existing `profile_id`.
- An empty capability set means no capabilities.
- Wildcard capabilities are prohibited.
- Command authorization is enforced at the server dispatch boundary before either a built-in or plugin handler runs.
- Tool discovery and tool-schema publication use the same capability policy as execution.
- Request identity, task identity, profile identity, and capability policy are server-derived.
- Delegated agent-task permissions are the intersection of parent permissions, child profile permissions, and explicit per-step narrowing.
- Runtime output cannot alter control flow unless the typed step contract explicitly exposes the referenced field.

### Semantics and execution

- One matching event may create multiple rule runs, but each run executes exactly one rule.
- Rules cannot branch to or reference steps owned by another rule.
- Every edge displayed in the graph is an executable transition, and every executable transition is displayed.
- Bindings are definitely assigned before use and are immutable.
- Loop variables are scoped, cannot shadow existing bindings, and nested loops are rejected in V2.
- Command arguments, conditions, result references, and templates are typed and statically validated.
- The same engine and graph model serve pipeline and assignment-routing playbooks.
- Assignment-routing selection caches remain caller-owned; they are not hidden engine state.
- Dry-run uses the real compiled graph and contracts while substituting side-effect-free executors.
- LLM and agent-task steps are explicit typed nodes with profiles, budgets, capabilities, outputs, and transitions.
- Cancellation, waits, retries, loop progress, and resume state survive process restarts.

### Persistence and observability

- The immutable artifact is not a runtime snapshot.
- A run snapshot stores mutable execution state.
- An execution receipt records one attempt, its contract fingerprint, inputs, output summary, timing, and outcome.
- Snapshot advancement and receipt insertion occur in one transaction at a step boundary.
- Command idempotency uses run, step, iteration, and attempt identity.
- Oversized raw results are rejected or externalized according to the child plan; they are never silently truncated into an invalid binding.
- Activation health distinguishes ready, question-required, invalid, disabled, and stale-contract states.
- Pending events are retained, visible, and operable; they are not silently dropped.
- Historical V1 runs remain readable after V1 execution is removed.

### UI and API

- The primary experience remains a branched state-machine graph.
- Event groups may separate canvases, but branches and convergence remain visible as edges.
- Node cards explain what happens inside each node.
- Intent text is generated from typed command and event contracts, never independently authored.
- The selected-node inspector exposes bindings, condition logic, command effects, outputs, retry behavior, AI profile and budget, and every transition.
- Run overlays are pinned to the exact artifact executed.
- Compact summaries and an Advanced view are both available.
- API clients are regenerated from the checked-in OpenAPI snapshot.
- Dashboard server state uses the existing React Query conventions.

### Delivery discipline

- All schema changes use Alembic and work on SQLite and PostgreSQL.
- No work package advances until its required tests and exit gate pass.
- Temporary feature flags and adapters declare their removal package when introduced.
- Existing unrelated working-tree changes are preserved.
- Commits stay package-scoped.
- Commit locally; do not push from this roadmap unless separately requested.

---

## 3. Target module map

The following module boundaries are the default ownership model. A child plan may refine filenames after inspecting the live tree, but it must document any deviation.

### Command boundary

- `src/commands/principal.py` — immutable execution identity and request-local principal context.
- `src/commands/authorization.py` — capability matching, delegation narrowing, and dispatch authorization.
- `src/commands/contracts/models.py` — `CommandContract`, argument/result schemas, effect clauses, and fingerprints.
- `src/commands/contracts/registry.py` — command registration and lookup.
- `src/commands/contracts/builtin.py` — contracts for built-in playbook commands.
- `src/commands/contracts/preview.py` — side-effect-free preview rendering.
- `src/profiles/capabilities.py` — normalized harness, AQ command, and plugin capability policy.

### Definition and compilation

- `src/playbooks/definition.py` — strict V2 artifact models.
- `src/playbooks/expressions.py` — typed values, templates, references, comparisons, and condition trees.
- `src/playbooks/authoring.py` — Markdown proposal input and source-location model.
- `src/playbooks/validation.py` — structural, dataflow, capability, and contract validation.
- `src/playbooks/explanation.py` — exhaustive contract-derived intent renderer.
- `src/playbooks/artifact_store.py` — canonical serialization and content-addressed files.
- `src/playbooks/activation.py` — activation records, health, and artifact selection.

### Durable execution

- `src/playbooks/run_state.py` — snapshots, statuses, loop frames, budgets, and resume state.
- `src/playbooks/receipts.py` — attempt identity and immutable execution receipts.
- `src/playbooks/waits.py` — durable event/timer wait registration and matching.
- `src/playbooks/engine.py` — event dispatch, single-rule execution, resume, cancellation, and dry-run.
- `src/playbooks/executors/base.py` — typed executor protocol and common result.
- `src/playbooks/executors/command.py`
- `src/playbooks/executors/llm.py`
- `src/playbooks/executors/agent_task.py`
- `src/playbooks/executors/decision.py`
- `src/playbooks/executors/wait.py`
- `src/playbooks/executors/foreach.py`
- `src/playbooks/executors/terminal.py`

### Migration and UI

- `src/playbooks/migration.py` — V1 inventory, rebuild, comparison, and readiness reporting.
- `src/database/queries/playbook_artifact_queries.py` — artifacts and activations.
- `src/database/queries/playbook_run_queries.py` — V2 snapshots, receipts, waits, and pending events.
- `src/api/models/playbook.py` — artifact, graph, diff, activation, run, and health DTOs.
- `dashboard/src/pages/playbook-graph/*` — semantic graph, node cards, inspector, diff, and overlays.

Legacy execution modules stay in place through Package 6. Package 7 removes:

- `src/playbooks/pipeline_compiler.py`
- `src/playbooks/pipeline_runner.py`
- `src/playbooks/runner.py`
- `src/playbooks/runner_context.py`
- `src/playbooks/runner_events.py`
- `src/playbooks/runner_transitions.py`
- `src/playbooks/token_tracker.py`

---

## 4. Locked cross-package interfaces

Child plans may add fields but must not silently rename or contradict these interfaces.

### `CapabilityPolicy`

A normalized immutable policy with three separate namespaces:

- `harness_tools`
- `aq_commands`
- `plugin_tools`

It provides exact membership checks, set intersection, subset validation, canonical serialization, and a deny-by-default empty value. No namespace accepts `*`.

### `ExecutionPrincipal`

Server-derived identity carried through direct API execution, orchestrator calls, playbook runs, and agent-task delegation. At minimum it contains:

- principal kind;
- session or service identity;
- current task identifier when present;
- effective profile identifier;
- effective `CapabilityPolicy`;
- parent run and step identity for delegation;
- provenance describing how the effective policy was narrowed.

A request body cannot populate or widen these fields.

### `CommandResult[T]`

The typed playbook-facing result envelope:

- `value: T`
- `summary: str`
- stable outcome metadata required by transitions;
- no arbitrary control-flow target.

Existing dict-returning handlers may use adapters during migration, but typed playbook execution consumes `CommandResult`.

### `CommandContract[A, R]`

The single source of truth for:

- command name;
- argument model `A`;
- result model `R`;
- required AQ capability;
- idempotency behavior;
- effect clauses used for intent rendering;
- preview support;
- sensitive-field policy;
- canonical execution fingerprint.

Presentation-only labels do not affect the execution fingerprint.

### `CommandRegistration`

Binds one `CommandContract` to one operational handler and optional preview handler. Registration fails when names conflict or a playbook-visible handler lacks a contract.

### `ArtifactRef`

Identifies an immutable artifact with:

- playbook identifier;
- artifact SHA-256;
- schema generation;
- command-contract fingerprint;
- source digest;
- compiler build identity.

The hash is computed from canonical artifact bytes.

### `ActivationHealth`

One of:

- `ready`
- `question_required`
- `invalid`
- `disabled`
- `stale_contract`

Health includes machine-readable reasons and operator-facing explanations.

### `ArtifactStore`

- `put(definition) -> ArtifactRef` writes canonical bytes atomically and returns the immutable reference.
- `load(artifact_sha256) -> PlaybookDefinition` verifies the file hash and strict schema before returning it.

### `ExecutionMode`

At minimum:

- `live`
- `dry_run`
- `shadow`

Mode selects executor implementations; it does not select a different graph or validator.

### `PlaybookEngine`

- `dispatch_event(event, principal, mode)` resolves enabled activations and starts one run per matching rule.
- `run_rule(artifact_ref, rule_id, event, principal, mode)` creates and advances one rule run.
- `resume(run_id, cause, principal)` resumes from a persisted wait or agent-task boundary.

### `RunRepository`

`commit_boundary(snapshot, receipt, pending_wait_changes)` atomically persists the next snapshot, one attempt receipt, and related wait changes.

### `WaitRepository`

`register(wait_spec, snapshot_version)` and matching operations use transactional compare-and-set behavior so an event cannot be lost between registration and suspension.

---

## 5. Work packages

## Package 0 — Security and compiler authority baseline

**Child plan:** `docs/superpowers/plans/2026-09-01-playbook-v2-phase0-security.md`

**Purpose:** Close authority and capability gaps before richer playbooks can expand their blast radius.

**Consumes:** Approved V2 design spec and current V1 behavior.

**Produces:** A trusted compiler boundary, normalized capability model, server-derived principal, dispatch authorization, and inheritance tests that later packages can rely on.

### Files

Create:

- `src/commands/principal.py`
- `src/commands/authorization.py`
- `src/profiles/capabilities.py`
- `tests/commands/test_execution_principal.py`
- `tests/commands/test_command_capability_authorization.py`
- `tests/playbooks/test_playbook_compiler_authority.py`

Modify:

- `src/commands/handler.py`
- `src/commands/session_commands.py`
- `src/api/auth.py`
- `src/api/middleware.py`
- `src/api/dependencies.py`
- `src/api/execute.py`
- `src/profiles/models.py`
- `src/profiles/parser.py`
- `src/profiles/sync.py`
- `src/sessions/spec.py`
- `src/sessions/tmux.py`
- `src/playbooks/compiler.py`
- `src/playbooks/pipeline_compiler.py`
- existing task-inheritance, session-token, allowlist, and API-execute tests.

### Required outcomes

- [ ] Restore authoritative frontmatter merging in the live compiler and install paths.
- [ ] Define which metadata fields are server-owned and reject or ignore author attempts to override them with a diagnostic.
- [ ] Split profile permissions into harness tools, AQ commands, and plugin tools.
- [ ] Add a migration adapter that reads existing `allowed_tools` without granting new rights.
- [ ] Make empty permissions deny all and reject wildcard values at parse and sync time.
- [ ] Introduce `ExecutionPrincipal` and a request-local context that replaces `_caller_profile_id` as an authorization source.
- [ ] Extend session tokens and request scope with server-derived task, profile, and policy identity.
- [ ] Strip all client-supplied principal and scope fields at the API boundary.
- [ ] Enforce AQ and plugin command capabilities before handler dispatch.
- [ ] Filter command/tool schemas with the exact same policy used by dispatch.
- [ ] Implement no-widen delegation helpers based on policy intersection.
- [ ] Add recursive real-handler tests proving parent, child-profile, and per-step restrictions compose.
- [ ] Preserve direct administrative/service execution through explicit trusted principals rather than implicit bypasses.

### Required verification

- `pytest tests/playbooks/test_playbook_compiler_authority.py -q`
- `pytest tests/commands/test_command_capability_authorization.py tests/commands/test_execution_principal.py -q`
- `pytest tests/api/test_execute.py tests/commands/test_session_commands.py -q`
- targeted profile, session, task-inheritance, and plugin-dispatch suites named by the child plan;
- `ruff check src/commands src/profiles src/api src/sessions src/playbooks tests`.

### Exit gate

A hostile Markdown playbook, API request, delegated task, or plugin command cannot widen server-owned identity, budgets, profiles, or capabilities. Tool discovery and actual execution agree for every tested principal.

### Rollback boundary

All new enforcement is behind one internal principal-construction seam. Reverting Package 0 restores the prior request path without any database downgrade. Do not begin Package 1 until production-equivalent profiles have been audited for explicit capabilities.

### Commit sequence

1. `test: capture compiler authority and capability invariants`
2. `feat: introduce capability policy and execution principal`
3. `feat: enforce command authorization at dispatch`
4. `test: cover session and delegated permission inheritance`

---

## Package 1 — Command contracts, event contracts, and truthful intent

**Child plan:** `docs/superpowers/plans/2026-09-01-playbook-v2-contracts-intent.md`

**Purpose:** Make executable behavior and human-readable intent two projections of the same typed contract.

**Consumes:** Package 0 principal and capability policy.

**Produces:** Typed contracts for the current playbook command surface, richer event schemas, deterministic fingerprints, exhaustive explanations, and an early readability improvement in the V1 inspector.

### Files

Create:

- `src/commands/contracts/__init__.py`
- `src/commands/contracts/models.py`
- `src/commands/contracts/registry.py`
- `src/commands/contracts/builtin.py`
- `src/commands/contracts/preview.py`
- `src/playbooks/explanation.py`
- `tests/commands/contracts/test_registry.py`
- `tests/commands/contracts/test_builtin_contracts.py`
- `tests/playbooks/test_explanation.py`

Modify:

- `src/commands/handler.py`
- handlers for the ten currently whitelisted pipeline commands;
- `src/event_schemas.py`
- `src/playbooks/pipeline_compiler.py`
- `src/playbooks/graph_view.py`
- `src/api/models/playbook.py`
- `dashboard/src/pages/playbook-graph/PlaybookNodeInspector.tsx`
- `dashboard/src/pages/playbook-graph/PlaybookStepNode.tsx`
- relevant API and dashboard graph tests.

### Required outcomes

- [ ] Register contracts for `create_task`, `ensure_task`, `edit_task`, `add_dependency`, `gate_create`, `gate_resolve`, `list_tasks`, `get_downstream_tasks`, `task_batch_commit`, and `task_route`.
- [ ] Define typed argument and result models for each command.
- [ ] Wrap legacy dict-returning handlers only at the contract boundary; do not create a second operational handler.
- [ ] Derive required capabilities from command registrations.
- [ ] Define a canonical execution fingerprint that excludes presentation-only copy.
- [ ] Enrich event schemas with typed nested fields, descriptions, and sensitivity metadata.
- [ ] Represent intent as typed effect clauses with a canonical fallback.
- [ ] Make explanation rendering exhaustive over every registered effect-clause type.
- [ ] Fail contract registration when an effect cannot be rendered.
- [ ] Redact sensitive arguments consistently in explanations, previews, and receipts.
- [ ] Adapt the existing pipeline graph inspector to show contract-derived action, inputs, outputs, and transitions without changing V1 execution.
- [ ] Add a contract test proving the displayed explanation and invoked contract share the same registration and fingerprint.

### Required verification

- `pytest tests/commands/contracts -q`
- `pytest tests/playbooks/test_explanation.py -q`
- targeted graph-view and API playbook-model tests;
- `npm test -- --run dashboard/src/pages/playbook-graph` from `dashboard/`;
- `ruff check src/commands/contracts src/playbooks/explanation.py tests/commands/contracts tests/playbooks/test_explanation.py`.

### Exit gate

Every command usable by a playbook has one typed registration. The UI cannot display separately authored intent for those commands, and contract/explanation exhaustiveness tests fail when a new effect kind is introduced without a renderer.

### Rollback boundary

The contract adapter can be removed while leaving operational handlers unchanged. The V1 inspector enhancement is presentation-only and can be reverted independently.

### Commit sequence

1. `test: specify playbook command contracts`
2. `feat: register typed built-in command contracts`
3. `feat: derive playbook intent from contracts`
4. `feat: expose contract intent in the current graph inspector`

---

## Package 2 — Strict V2 definition model and Markdown compiler

**Child plan:** `docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md`

**Purpose:** Introduce the canonical semantic graph and a compiler that proposes reviewable artifacts without activating them.

**Consumes:** Packages 0 and 1.

**Produces:** Strict models, typed expressions, whole-graph validation, generated JSON Schema, source mapping, proposal/diff flow, and shadow compilation of active sources.

### Files

Create:

- `src/playbooks/definition.py`
- `src/playbooks/expressions.py`
- `src/playbooks/authoring.py`
- `src/playbooks/validation.py`
- `scripts/generate-playbook-schema.py`
- `tests/playbooks/test_definition.py`
- `tests/playbooks/test_expressions.py`
- `tests/playbooks/test_v2_validation.py`
- `tests/playbooks/test_v2_compiler.py`
- `tests/playbooks/fixtures/v2/`

Modify:

- `src/playbooks/compiler.py`
- `src/playbooks/models.py`
- `src/playbook_schema.json`
- compiler API/command entry points;
- compiler prompt templates and their tests.

### Required outcomes

- [ ] Define `PlaybookDefinition`, rules, triggers, source references, policies, and all seven step variants as a discriminated Pydantic union.
- [ ] Define typed literal, event reference, binding reference, loop reference, template, comparison, boolean, and existence expressions.
- [ ] Preserve exact `on_success`, `on_failure`, decision, loop, wait, and terminal edges.
- [ ] Require one owner rule for every step and prohibit cross-rule transitions.
- [ ] Validate unique identifiers, entry nodes, reachability, terminal paths, event fields, command arguments, result references, and output shapes.
- [ ] Perform definite-assignment analysis across branches and convergence.
- [ ] Reject binding reassignment, loop-variable shadowing, nested loops, and out-of-scope loop references.
- [ ] Inventory identifiers explicitly and preserve backticks plus source locations in prose diagnostics.
- [ ] Validate every AI profile and its effective capability subset at compile time.
- [ ] Generate `src/playbook_schema.json` from the Pydantic model with a deterministic script.
- [ ] Add a compiler proposal object containing artifact, diagnostics, unresolved questions, source digest, semantic diff, and contract fingerprint.
- [ ] Require explicit review/activation outside the compiler.
- [ ] Shadow-compile current active Markdown sources and report questions without affecting runtime behavior.
- [ ] Use deterministic fixtures for every step kind, expression kind, validation error, and branching pattern.

### Required verification

- `pytest tests/playbooks/test_definition.py tests/playbooks/test_expressions.py tests/playbooks/test_v2_validation.py tests/playbooks/test_v2_compiler.py -q`
- run `python scripts/generate-playbook-schema.py` twice and confirm a clean diff after the first generation;
- existing V1 compiler tests remain green;
- `ruff check src/playbooks scripts/generate-playbook-schema.py tests/playbooks`.

### Exit gate

A prose source can produce a reviewable V2 proposal whose graph is strict, source-mapped, fully validated, and fingerprinted, but no proposal can become active as a side effect of compilation.

### Rollback boundary

V2 definitions and compiler outputs are additive. Removing the V2 proposal entry point leaves V1 compilation and execution intact.

### Commit sequence

1. `test: define strict playbook v2 model invariants`
2. `feat: add typed expressions and whole-graph validation`
3. `feat: compile markdown into v2 proposals`
4. `build: generate playbook v2 json schema`
5. `test: shadow compile active playbook sources`

---

## Package 3 — Content-addressed artifacts and durable run state

**Child plan:** `docs/superpowers/plans/2026-09-01-playbook-v2-durable-state-storage.md`

**Purpose:** Make reviewed definitions immutable and make every resumable execution boundary durable.

**Consumes:** Package 2 definitions and artifact references.

**Produces:** Artifact files, activation records, V2 run snapshots, receipts, waits, pending events, retention policy, and activation health.

### Files

Create:

- `src/playbooks/artifact_store.py`
- `src/playbooks/activation.py`
- `src/playbooks/run_state.py`
- `src/playbooks/receipts.py`
- `src/playbooks/waits.py`
- `src/database/queries/playbook_artifact_queries.py`
- `src/database/queries/playbook_run_queries.py`
- one or more Alembic revisions named by the child plan;
- `tests/playbooks/test_artifact_store.py`
- `tests/playbooks/test_activation.py`
- `tests/playbooks/test_run_repository.py`
- `tests/playbooks/test_wait_repository.py`
- `tests/database/test_playbook_v2_migrations.py`

Modify:

- `src/database/tables.py`
- `src/database/queries/playbook_queries.py` only where compatibility reads are required;
- `src/playbooks/store.py`
- `src/playbooks/health.py`
- API models and configuration for retention and size limits.

### Required outcomes

- [ ] Canonically serialize definitions and compute the artifact SHA-256 from those bytes.
- [ ] Write artifact files atomically, verify after write, and refuse hash collisions or mutated content.
- [ ] Store artifact metadata and explicit activations in the database.
- [ ] Model activation enablement separately from artifact validity.
- [ ] Add V2 run tables for artifact-pinned snapshots, attempts/receipts, waits, and pending events.
- [ ] Persist bindings, loop frames, selected paths, LLM budget consumption, agent-task linkage, cancellation state, and snapshot version.
- [ ] Enforce a 256 KiB default per stored result and a 4 MiB default per snapshot.
- [ ] Define explicit oversize failure/externalization behavior in the child plan.
- [ ] Persist snapshot advancement and its receipt in one transaction.
- [ ] Derive attempt idempotency from run, step, loop iteration, and attempt number.
- [ ] Register waits transactionally so matching events cannot race past suspension.
- [ ] Implement optimistic concurrency for resume and cancellation.
- [ ] Expose `ready`, `question_required`, `invalid`, `disabled`, and `stale_contract` health.
- [ ] Retain pending events for 7 days and execution receipts for 90 days by default, with configurable cleanup jobs.
- [ ] Prove migrations upgrade and downgrade on SQLite and PostgreSQL-compatible SQL.

### Required verification

- `pytest tests/playbooks/test_artifact_store.py tests/playbooks/test_activation.py -q`
- `pytest tests/playbooks/test_run_repository.py tests/playbooks/test_wait_repository.py -q`
- `pytest tests/database/test_playbook_v2_migrations.py -q`
- repository migration smoke tests on SQLite;
- configured PostgreSQL migration suite in CI;
- `ruff check src/playbooks src/database tests/playbooks tests/database`.

### Exit gate

A reviewed artifact can be stored and activated by hash, and a synthetic run can cross every durable boundary, restart the process, and resume without losing state, duplicating an acknowledged attempt, or reading mutable playbook content.

### Rollback boundary

New tables and artifact directories are additive. Activations remain disabled by default, so rollback means disabling V2 reads and retaining the data for inspection.

### Commit sequence

1. `feat: store immutable playbook artifacts and activations`
2. `feat: persist v2 snapshots and execution receipts`
3. `feat: add durable waits and pending events`
4. `feat: report activation health and retention`
5. `test: verify sqlite and postgres migration behavior`

---

## Package 4 — Unified engine and typed executors

**Child plan:** `docs/superpowers/plans/2026-09-01-playbook-v2-engine-executors.md`

**Purpose:** Execute the semantic graph directly, including explicit LLM and agent-task nodes, without creating a second graph for dry-run or shadow evaluation.

**Consumes:** Packages 0 through 3.

**Produces:** One V2 engine, seven executor families, lifecycle semantics, bounded dry-run, and no-side-effect shadow parity.

### Files

Create:

- `src/playbooks/engine.py`
- `src/playbooks/executors/__init__.py`
- `src/playbooks/executors/base.py`
- `src/playbooks/executors/command.py`
- `src/playbooks/executors/llm.py`
- `src/playbooks/executors/agent_task.py`
- `src/playbooks/executors/decision.py`
- `src/playbooks/executors/wait.py`
- `src/playbooks/executors/foreach.py`
- `src/playbooks/executors/terminal.py`
- `tests/playbooks/test_v2_engine.py`
- `tests/playbooks/test_command_executor.py`
- `tests/playbooks/test_llm_executor.py`
- `tests/playbooks/test_agent_task_executor.py`
- `tests/playbooks/test_v2_dry_run.py`
- `tests/playbooks/test_v2_restart_resume.py`

Modify:

- `src/playbooks/handler.py`
- `src/playbooks/resume_handler.py`
- `src/playbooks/services.py`
- `src/playbooks/run_task.py`
- `src/orchestrator/core.py`
- `src/orchestrator/assignment_routing.py`
- `src/commands/playbook_commands.py`
- AI-provider and task lifecycle seams used by current orchestration.

### Required outcomes

- [ ] Start exactly one durable run for each matching rule.
- [ ] Execute `CommandStep` only through its registered contract and handler.
- [ ] Validate runtime arguments again at the command boundary.
- [ ] Execute `LlmStep` with its named profile, structured output schema, token/cost budget, timeout, and receipt.
- [ ] Record provider-reported usage when available and conservative estimates otherwise.
- [ ] Execute `AgentTaskStep` with a child `ExecutionPrincipal` narrowed by intersection.
- [ ] Persist child task identity before suspension and reconcile completion idempotently.
- [ ] Propagate cancellation to waiting child work without granting new authority.
- [ ] Evaluate decisions and templates without arbitrary code execution.
- [ ] Persist loop cursor and iteration scope before and after each body transition.
- [ ] Implement event waits, timer waits, timeout edges, and restart-safe matching.
- [ ] Make success, failure, retry, cancellation, and terminal outcomes explicit.
- [ ] Stop and require operator action for ambiguous external outcomes rather than guessing.
- [ ] Bound dry-run exploration to 32 paths and 1,000 simulated step visits by default.
- [ ] Use preview executors against the real graph, contracts, validator, and transition logic.
- [ ] Implement shadow mode with zero command, AI, task, gate, or external side effects.
- [ ] Move pipeline and assignment-routing V2 evaluation through the same engine.
- [ ] Keep assignment-routing cache ownership in the caller and make cache keys include artifact identity.
- [ ] Emit receipts and structured lifecycle events sufficient for overlays and parity analysis.

### Required verification

- `pytest tests/playbooks/test_v2_engine.py tests/playbooks/test_command_executor.py -q`
- `pytest tests/playbooks/test_llm_executor.py tests/playbooks/test_agent_task_executor.py -q`
- `pytest tests/playbooks/test_v2_dry_run.py tests/playbooks/test_v2_restart_resume.py -q`
- targeted orchestrator and assignment-routing suites;
- process-kill/restart integration coverage at command, LLM, agent-task, wait, and loop boundaries;
- `ruff check src/playbooks src/orchestrator src/commands tests/playbooks`.

### Exit gate

Every V2 step kind runs through one engine with durable boundaries. Live, dry-run, and shadow modes traverse the same graph, and shadow mode can compare decisions without producing side effects.

### Rollback boundary

V2 activations remain selectively disabled. V1 entry points remain authoritative until Package 7, so the engine can be removed or disabled without converting stored V1 runs.

### Commit sequence

1. `feat: execute command decision and terminal v2 steps`
2. `feat: execute durable waits and foreach loops`
3. `feat: execute budgeted llm steps`
4. `feat: execute narrowed agent task steps`
5. `feat: add bounded dry run and side effect free shadow mode`
6. `test: prove restart and idempotency boundaries`

---

## Package 5 — Semantic graph API and rich node experience

**Child plan:** `docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md`

**Purpose:** Preserve the familiar graph while making the behavior inside each node legible and trustworthy.

**Consumes:** Packages 1 through 4, especially explanation payloads, artifact identity, health, and receipts.

**Produces:** Artifact-aware graph APIs, diff and activation APIs, rich node cards, an exhaustive inspector, and exact-artifact run overlays.

### Files

Create or split as named by the child plan:

- V2 graph, artifact-diff, activation, health, pending-event, and run-overlay endpoints under `src/api/`;
- dashboard components for event clusters, intent sections, effect clauses, advanced detail, diff review, activation health, and run overlays;
- API and dashboard tests for each new surface.

Modify:

- `src/api/models/playbook.py`
- `dashboard/src/api/hooks.ts`
- generated dashboard API client files;
- `dashboard/src/pages/playbook-graph/types.ts`
- `dashboard/src/pages/playbook-graph/layout.ts`
- `dashboard/src/pages/playbook-graph/PlaybookGraphCanvas.tsx`
- `dashboard/src/pages/playbook-graph/PlaybookGraphView.tsx`
- `dashboard/src/pages/playbook-graph/PlaybookNodeInspector.tsx`
- `dashboard/src/pages/playbook-graph/PlaybookStepNode.tsx`
- graph fixtures and Vitest suites.

### Required outcomes

- [ ] Return event-grouped rule graphs with exact executable edges and stable node identifiers.
- [ ] Keep branching, convergence, loop-back, timeout, success, and failure edges visually distinct.
- [ ] Render compact node cards with action, key inputs, output binding, and transition summary.
- [ ] Render command effects exclusively from the backend explanation payload tied to the contract fingerprint.
- [ ] Show raw typed arguments, resolved event/binding references, result schema, retry and idempotency behavior in Advanced view.
- [ ] Show `profile_id`, effective capabilities, provider/model policy, budget, output schema, and task lifecycle for AI nodes.
- [ ] Show source locations and provide navigation information for the authoring Markdown.
- [ ] Show compile questions, invalid references, stale contracts, and disabled activations without hiding the graph.
- [ ] Add semantic artifact diff review before activation.
- [ ] Require explicit activation against an artifact hash and display the active hash.
- [ ] Overlay live/historical state only on the exact artifact pinned by the run.
- [ ] Display loop iterations and per-iteration receipts without collapsing them into one misleading status.
- [ ] Expose pending events and operator actions.
- [ ] Preserve the existing pan, zoom, fit, selection, and graph-layout ergonomics.
- [ ] Retain a compact default and an Advanced mode instead of replacing the graph with a simplified list.
- [ ] Regenerate API clients from the checked-in OpenAPI snapshot.
- [ ] Use React Query for artifact, activation, run, health, and pending-event server state.

### Required verification

- backend playbook API model and route tests named by the child plan;
- `scripts/regenerate-api-client.sh --from-file`;
- `scripts/regenerate-ts-client.sh --from-file`;
- `npm test -- --run dashboard/src/pages/playbook-graph` from `dashboard/`;
- `npm run lint`;
- `npm run typecheck`;
- `npm run build`;
- screenshot-based manual review of branching, convergence, loops, AI nodes, invalid nodes, diff review, and run overlays.

### Exit gate

An operator can answer, from the graph alone: what event enters this rule, what each node does, which data it reads and writes, which capabilities it uses, where each outcome goes, what artifact is active, and what happened in a selected run.

### Rollback boundary

The V2 API and UI routes are additive. The existing V1 graph stays available until Package 7. Activation writes are independently feature-gated from graph reads.

### Commit sequence

1. `feat: expose artifact aware semantic graph api`
2. `feat: render rich typed playbook nodes and exact edges`
3. `feat: add source inspector and advanced details`
4. `feat: add artifact diff and activation review`
5. `feat: overlay exact artifact execution receipts`
6. `test: cover graph semantics accessibility and generated clients`

---

## Package 6 — Playbook rebuild, review, and migration readiness

**Child plan:** `docs/superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md`

**Purpose:** Rebuild every shipped and project playbook under the one current command API, resolve compiler questions deliberately, and prove parity before cutover.

**Consumes:** Packages 0 through 5.

**Produces:** Human-reviewed V2 artifacts, a complete project inventory, shadow parity evidence, pending-event policy, and a signed cutover report.

### Files

Create:

- `src/playbooks/migration.py`
- `tests/playbooks/test_migration_inventory.py`
- `tests/playbooks/test_default_playbook_v2_artifacts.py`
- checked-in reviewed artifact fixtures in the location chosen by the child plan;
- an operator-facing cutover report template.

Modify:

- `src/prompts/default_playbooks/default-pipeline.md`
- `src/prompts/default_playbooks/default-assignment-routing.md`
- `src/prompts/default_playbooks/memory-consolidation.md`
- playbook compilation/installation commands;
- health and migration API surfaces;
- release checks and CI configuration.

### Required outcomes

- [ ] Remove embedded JSON action blocks from all shipped Markdown sources.
- [ ] Compile each shipped source into a strict V2 proposal.
- [ ] Resolve every compiler question through a documented human decision.
- [ ] Review the semantic diff, capability requirements, AI profiles, budgets, and transitions before activation.
- [ ] Check in deterministic fixture artifacts for shipped playbooks while production activation remains database-controlled.
- [ ] Inventory every project playbook and classify it as `ready`, `question_required`, `invalid`, or intentionally `disabled`.
- [ ] Require an explicit acknowledgement for intentionally disabled playbooks.
- [ ] Never batch-activate compiled proposals automatically.
- [ ] Surface events waiting on question-required or invalid playbooks according to the approved pending-event policy.
- [ ] Add a release check: a changed command execution fingerprint must ship rebuilt reviewed artifacts for all affected enabled playbooks.
- [ ] Run V1 and V2 in deterministic shadow comparison for supported events.
- [ ] Compare rule selection, node path, command arguments, outputs used for routing, terminal result, and authorization decision.
- [ ] Classify expected differences caused by intentional V2 semantics.
- [ ] Produce a cutover report listing artifacts, hashes, active contract fingerprint, unresolved issues, pending events, active V1 runs, and rollback readiness.

### Required verification

- `pytest tests/playbooks/test_migration_inventory.py tests/playbooks/test_default_playbook_v2_artifacts.py -q`
- full default-playbook compiler fixture suite;
- deterministic shadow-parity suite named by the child plan;
- capability audit over every activated artifact;
- clean release-check run with unchanged contracts;
- intentional contract-change fixture proving stale artifacts block readiness;
- `ruff check src/playbooks/migration.py tests/playbooks`.

### Exit gate

Every enabled playbook has one reviewed V2 artifact compatible with the current command contracts. Every non-ready playbook has a visible reason and operator decision. Shadow comparison has no unexplained behavior or authorization differences.

### Rollback boundary

No production entry point has switched yet. Reviewed artifacts and migration reports can remain stored while V1 continues to execute.

### Commit sequence

1. `feat: inventory v1 playbooks and migration readiness`
2. `refactor: rewrite shipped playbooks as prose authoring sources`
3. `build: add reviewed v2 artifact fixtures`
4. `test: compare v1 and v2 shadow decisions`
5. `ci: require artifact rebuilds after command contract changes`

---

## Package 7 — Drain, atomic cutover, rollback window, and V1 removal

**Child plan:** `docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md`

**Purpose:** Switch all playbook consumers atomically, preserve a bounded operational rollback, then remove the temporary dual runtime.

**Consumes:** Package 6 readiness report and all prior exit gates.

**Produces:** V2 as the only execution path, a completed rollback window, deleted V1 runtime code, and read-only historical compatibility.

### Files

Modify:

- `src/orchestrator/core.py`
- `src/orchestrator/assignment_routing.py`
- `src/playbooks/handler.py`
- `src/playbooks/resume_handler.py`
- `src/commands/playbook_commands.py`
- `src/api/models/playbook.py` and playbook routes;
- dashboard graph routing;
- operator documentation, metrics, and alerts;
- tests that currently instantiate V1 runners.

Delete after the rollback window:

- `src/playbooks/pipeline_compiler.py`
- `src/playbooks/pipeline_runner.py`
- `src/playbooks/runner.py`
- `src/playbooks/runner_context.py`
- `src/playbooks/runner_events.py`
- `src/playbooks/runner_transitions.py`
- `src/playbooks/token_tracker.py`
- V1-only fixtures, flags, adapters, and dead API fields identified by the child plan.

Retain:

- V1 historical read models and serializers needed to inspect completed runs.

### Required outcomes

- [ ] Block creation of new V1 runs before draining begins.
- [ ] List every active V1 run with current step, age, and operator options.
- [ ] Allow operators to wait, resolve, or cancel; do not translate an in-flight V1 run into V2 state.
- [ ] Reach zero active V1 runs.
- [ ] Reconfirm every enabled playbook has a ready V2 activation and current contract fingerprint.
- [ ] Reconfirm pending-event ownership and replay policy.
- [ ] Switch pipeline, assignment routing, resume, command, and API entry points in one coordinated release.
- [ ] Monitor authorization denials, run failures, wait latency, receipt conflicts, LLM budget failures, and parity-sensitive outcomes.
- [ ] Keep one release rollback window in which entry points can return to V1 without changing artifacts or V2 tables.
- [ ] Do not permit new V1 authoring or command-contract compatibility during that window.
- [ ] Close the rollback window only after the agreed observation period and acceptance metrics pass.
- [ ] Remove V1 execution modules, temporary flags, adapters, and duplicated tests.
- [ ] Preserve read-only display of historical V1 runs.
- [ ] Update architecture and operator documentation to describe V2 as the sole playbook system.
- [ ] Confirm no code path reads embedded JSON actions from Markdown.

### Required verification

- full Python test suite: `pytest -q`;
- `ruff check .` using repository exclusions;
- generated-client checks;
- `npm test -- --run` from `dashboard/`;
- `npm run lint`;
- `npm run typecheck`;
- `npm run build`;
- SQLite and PostgreSQL migration checks;
- cutover rehearsal with synthetic active V1 runs and pending V2 events;
- rollback rehearsal before the rollback window closes;
- performance checks for compile, graph load, dispatch, receipt writes, and resume at production-like graph sizes;
- repository search proving V1 execution imports and embedded JSON-action parsing are gone.

### Exit gate

V2 is the only system that can compile, activate, dispatch, execute, resume, and visualize current playbooks. Historical V1 runs remain readable, and all temporary dual-runtime mechanisms have been removed.

### Rollback boundary

Before the observation window closes, revert the coordinated entry-point switch only; do not delete V2 artifacts or data. After V1 deletion, rollback requires a new forward change and is no longer an operational toggle.

### Commit sequence

1. `ops: block new v1 runs and expose drain controls`
2. `feat: switch all playbook entry points to v2`
3. `ops: complete playbook v2 rollback observation window`
4. `refactor: remove v1 playbook execution runtime`
5. `docs: declare playbook v2 the sole runtime`

---

## 6. Milestone gates

| Milestone | Required packages | Evidence | Decision unlocked |
|---|---:|---|---|
| M0 — Authority safe | 0 | hostile-authoring, API spoofing, plugin dispatch, and delegation tests | Contracts may become enforcement inputs |
| M1 — Intent truthful | 1 | contract registry and exhaustive explanation tests | UI may present semantic intent |
| M2 — Definition stable | 2 | strict schema, dataflow validation, deterministic proposal fixtures | Artifacts may be persisted |
| M3 — State durable | 3 | hash verification, transaction, wait-race, restart, and migration tests | V2 execution may begin in isolation |
| M4 — Engine complete | 4 | all step executors, dry-run bounds, shadow no-side-effect proof | End-to-end V2 trials may begin |
| M5 — Operator legible | 5 | graph/API/UI tests and manual scenario review | Operators may review and activate artifacts |
| M6 — Fleet ready | 6 | full inventory, reviewed hashes, clean parity report, cutover report | Production cutover may be scheduled |
| M7 — Cut over | 7 through switch commit | zero active V1 runs and current activations | V2 becomes authoritative |
| M8 — Cleanup complete | 7 through deletion commits | full suites, no V1 execution imports, historical reads intact | Migration is complete |

No milestone may be waived by a feature flag. A feature flag may limit exposure while evidence is gathered, but the next milestone still requires the listed proof.

---

## 7. Sequencing and parallelism rules

The dependency spine is:

`0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7`

Limited parallel work is safe only within these boundaries:

- Package 1 backend contracts and early inspector components may proceed in parallel after `CommandContract` and explanation payload shapes are locked.
- Package 2 schema-generation work may proceed alongside compiler prompt work after the Pydantic model is accepted.
- Package 3 artifact storage and run-state schema may be separate branches only if they share an agreed `ArtifactRef` and migration ordering.
- Package 4 executors may be developed independently after `Executor`, `CommandResult`, `RunRepository`, and receipt types are fixed.
- Package 5 backend endpoints and dashboard components may proceed in parallel after API DTOs are checked in.
- Package 6 project inventory may start read-only before Package 5 completes, but reviewed activation cannot begin until the diff and health UI exists.
- Package 7 is intentionally serial and operator-led.

Changes to locked interfaces require updating all not-yet-completed child plans and re-running the most recent passed milestone gate.

---

## 8. Spec traceability

| Design requirement | Delivery package | Primary proof |
|---|---:|---|
| Authoritative compilation boundary | 0 | frontmatter authority tests |
| Split capability namespaces and deny-by-default | 0 | parser, sync, schema-filter, and dispatch tests |
| Server-derived principal and no-widen delegation | 0, 4 | API spoofing and real child-handler tests |
| English Markdown authoring without JSON actions | 2, 6 | compiler fixtures and shipped-source scan |
| Strict canonical artifact | 2 | Pydantic and generated-schema tests |
| Metadata ownership and source references | 0, 2 | compiler diagnostics and source-map fixtures |
| First-class independent rules | 2, 4 | validation and multi-match dispatch tests |
| Typed values, conditions, and dataflow | 2 | expression and definite-assignment tests |
| Seven typed step families | 2, 4 | model union and executor tests |
| Typed command contracts | 1, 4 | registration and runtime validation tests |
| Intent guaranteed by executable contracts | 1, 5 | exhaustive renderer and UI payload tests |
| Manual compile, diff, review, activation | 2, 3, 5 | proposal and activation tests |
| Content-addressed storage | 3 | canonical-byte and hash-integrity tests |
| Durable snapshots, waits, and receipts | 3, 4 | transaction, race, restart, and idempotency tests |
| Deterministic engine boundary | 4 | executor and routing tests |
| Bounded dry-run using the real graph | 4 | path/step limit and no-side-effect tests |
| Event-scoped branched canvas | 5 | graph edge and layout tests |
| Rich cards and selected-node inspector | 5 | component and manual scenario tests |
| Exact-artifact run overlay | 3, 5 | artifact pinning and overlay tests |
| No permanent command API versioning | 1, 6 | stale-contract release check |
| Pending-event visibility and retention | 3, 5, 6 | retention and operator workflow tests |
| Rebuild all playbooks before cutover | 6 | inventory and reviewed artifact report |
| Drain rather than translate active V1 runs | 7 | cutover rehearsal |
| Atomic entry-point cutover | 7 | coordinated release checklist |
| One-release rollback window | 7 | rollback rehearsal |
| Removal of V1 execution runtime | 7 | repository scan and full suites |

---

## 9. Child-plan quality bar

Each child implementation plan must include:

- exact paths and symbols based on the live tree;
- test-first steps with the failing assertion described before implementation;
- representative fixture data, not placeholders;
- API request and response examples when the package changes an endpoint;
- Alembic upgrade and downgrade behavior when the package changes storage;
- SQLite and PostgreSQL considerations;
- security analysis for any new boundary or identity flow;
- observability and operator failure behavior;
- feature-flag ownership and named removal package;
- per-task verification commands and expected outcomes;
- small commit boundaries;
- explicit mapping to this roadmap's package exit gate.

A child plan is not ready if it says “update related tests,” “wire as appropriate,” “similar to existing code,” or leaves a model field or migration decision unspecified.

---

## 10. Release and operational measures

Before production cutover, define concrete thresholds in the Package 7 child plan for:

- V1/V2 shadow rule-selection agreement;
- V1/V2 command-argument agreement after canonicalization;
- unexplained terminal-outcome differences;
- authorization denials by command and profile;
- duplicate receipt or snapshot-version conflicts;
- event-to-run dispatch latency;
- wait-resume latency;
- LLM budget and structured-output failure rates;
- agent-task orphan and cancellation rates;
- graph API latency and dashboard render time;
- pending-event count and maximum age.

The dashboard and logs must include artifact hash, rule ID, run ID, step ID, attempt, principal/profile ID, and contract fingerprint where applicable. Sensitive values follow the command contract's redaction policy.

---

## 11. Final definition of done

Playbook V2 is complete only when all statements below are true.

### Safety

- [ ] Authoring content cannot claim server authority.
- [ ] Empty or missing capabilities deny execution.
- [ ] No wildcard capability exists in stored profiles or active artifacts.
- [ ] Schema discovery and dispatch authorization agree.
- [ ] Delegated work cannot widen parent permission.
- [ ] Every active AI node names a valid profile and bounded budget.

### Correctness

- [ ] One strict model drives validation, schema generation, execution, and graph serialization.
- [ ] Every executable edge is visible and every visible edge is executable.
- [ ] Every command node invokes the same contract that produced its explanation.
- [ ] Every binding read is type-correct and definitely assigned.
- [ ] All run boundaries are restart-safe and idempotent.
- [ ] Dry-run and shadow mode use the production graph and transition logic.
- [ ] Pipeline and assignment routing share the engine.

### Usability

- [ ] Operators can inspect what happens inside every node without reading raw JSON.
- [ ] Branches, convergence, loops, failures, and timeouts remain visible on the canvas.
- [ ] Compact and Advanced views explain inputs, outputs, effects, capabilities, and next states.
- [ ] Diff review precedes activation.
- [ ] Run overlays identify the exact artifact and loop iteration executed.
- [ ] Invalid, stale, disabled, and question-required playbooks are visibly distinct.

### Migration

- [ ] Every shipped and project playbook has an explicit migration disposition.
- [ ] Every enabled playbook has a reviewed artifact compatible with current command contracts.
- [ ] There are zero active V1 runs at cutover.
- [ ] No in-flight V1 run was translated into V2 state.
- [ ] Pending events were retained and handled according to policy.
- [ ] The rollback window completed successfully.
- [ ] V1 execution modules, flags, adapters, and embedded JSON-action parsing are removed.
- [ ] Historical V1 runs remain readable.

### Verification

- [ ] Backend unit, integration, restart, authorization, migration, and parity suites pass.
- [ ] Dashboard unit, lint, typecheck, build, and scenario reviews pass.
- [ ] Generated clients and generated JSON Schema are current and reproducible.
- [ ] SQLite and PostgreSQL upgrade paths pass.
- [ ] Performance thresholds defined for cutover pass.
- [ ] Architecture, authoring, activation, recovery, and operator documentation are current.

---

## 12. First implementation move

Write `docs/superpowers/plans/2026-09-01-playbook-v2-phase0-security.md` from Package 0, using the current repository symbols and test suites. Review that child plan against the Phase 0 sections of the design spec and this roadmap before changing production code.

Do not start with the new graph renderer. The graph becomes trustworthy only after compiler authority, command authorization, and contract-derived intent are real invariants.
