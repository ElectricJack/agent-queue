# Module catalog — Playbooks V2

This shard maps every production module owned by Playbooks V2 to the user-facing explanation in [Playbooks V2](../../concepts/playbooks.md). The [CLI catalog](cli.md) owns command handlers; the [database catalog](database.md) owns tables and query modules.

## Events and workflow compatibility

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/event_bus.py`](../../../src/event_bus.py) | Delivers validated in-process events to named or wildcard subscribers and supports race-free one-shot waiters. | [Playbooks V2](../../concepts/playbooks.md) | Development raises invalid payloads; other environments warn. `tests/test_event_bus.py` |
| [`src/event_schemas.py`](../../../src/event_schemas.py) | Defines registered event payload fields, types, sensitivity, and nested-path lookup used by emitters and V2 validation. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_event_schemas.py` |
| [`src/workflow_pipeline_view.py`](../../../src/workflow_pipeline_view.py) | Projects a multi-stage workflow, its tasks, affinities, progress, and links into the dashboard pipeline DTO. | [Playbooks V2](../../concepts/playbooks.md) | A projection, not execution policy. `tests/test_workflow_pipeline_view.py` |
| [`src/workflow_stage_resume_handler.py`](../../../src/workflow_stage_resume_handler.py) | Bridges `workflow.stage.completed` into a V2 run resume when a durable workflow points to that run. | [Playbooks V2](../../concepts/playbooks.md) | Compatibility bridge; covered with engine workflow scenarios. |
| [`src/orphan_workflow_recovery.py`](../../../src/orphan_workflow_recovery.py) | Reconciles workflows whose linked V2 run, tasks, or pause state became stale across restart or interruption. | [Playbooks V2](../../concepts/playbooks.md) | Startup and periodic recovery; no standalone focused test file. |

## Source, artifacts, and activation

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/playbooks/__init__.py`](../../../src/playbooks/__init__.py) | Marks the V2 playbook package. | [Playbooks V2](../../concepts/playbooks.md) | Public behavior lives in submodules. |
| [`src/playbooks/authoring.py`](../../../src/playbooks/authoring.py) | Parses trusted Markdown/frontmatter and inventories identifiers a source grants to a proposal. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_playbook_v2_compiler.py` |
| [`src/playbooks/proposal.py`](../../../src/playbooks/proposal.py) | Converts a semantic rules/steps body plus trusted source and live snapshots into a reviewable proposal. | [Playbooks V2](../../concepts/playbooks.md) | Server computes provenance. `tests/test_playbook_v2_compiler.py` |
| [`src/playbooks/pipeline_lowering.py`](../../../src/playbooks/pipeline_lowering.py) | Deterministically lowers supported legacy pipeline-shaped source into a V2 semantic body and reports unsupported portions. | [Playbooks V2](../../concepts/playbooks.md) | Optional compatibility, not a V1 runtime. `tests/test_playbook_v2_compiler.py` |
| [`src/playbooks/definition.py`](../../../src/playbooks/definition.py) | Defines the strict V2 artifact model, typed steps/scopes, canonical serialization, and fingerprints. | [Playbooks V2](../../concepts/playbooks.md) | Generates the V2 schema. `tests/test_playbook_v2_definition.py` |
| [`src/playbooks/validation.py`](../../../src/playbooks/validation.py) | Validates graph structure, types, contracts, events, profiles, capabilities, and bounded schemas with complete diagnostics. | [Playbooks V2](../../concepts/playbooks.md) | Fails closed on unresolved names. `tests/test_playbook_v2_validation.py` |
| [`src/playbooks/artifact_ref.py`](../../../src/playbooks/artifact_ref.py) | Represents and validates the immutable artifact identity carried by activation and run state. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_playbook_artifact_ref.py` |
| [`src/playbooks/artifact_store.py`](../../../src/playbooks/artifact_store.py) | Stores, verifies, loads, lays out, and deletes canonical content-addressed artifact files. | [Playbooks V2](../../concepts/playbooks.md) | Hash verification prevents mutable-path substitution. `tests/test_playbook_artifact_store.py` |
| [`src/playbooks/artifact_tombstone.py`](../../../src/playbooks/artifact_tombstone.py) | Moves collected artifact files to reversible tombstones and restores or discards them safely. | [Playbooks V2](../../concepts/playbooks.md) | Used by retention. `tests/test_playbook_artifact_store.py` |
| [`src/playbooks/activation.py`](../../../src/playbooks/activation.py) | Computes activation health from enablement, validation, artifact integrity, command contracts, and profile capabilities. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_playbook_activation.py` |
| [`src/playbooks/artifact_diff.py`](../../../src/playbooks/artifact_diff.py) | Produces operator-facing semantic rows for differences between two immutable artifacts. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_playbook_artifact_diff.py` |
| [`src/playbooks/semantic_diff.py`](../../../src/playbooks/semantic_diff.py) | Computes typed definition, rule, step, edge, and field changes for artifact comparisons. | [Playbooks V2](../../concepts/playbooks.md) | Pure diff layer. `tests/test_playbook_artifact_diff.py` |
| [`src/playbooks/profiles.py`](../../../src/playbooks/profiles.py) | Loads shipped profiles and obtains their capability-policy fingerprints for V2 provenance. | [Playbooks V2](../../concepts/playbooks.md) | Checked by validation against live profile fingerprints. |
| [`src/playbooks/required.py`](../../../src/playbooks/required.py) | Seeds, imports, activates, verifies, and replays events for required reviewed system playbooks. | [Playbooks V2](../../concepts/playbooks.md) | Currently requires only default assignment routing. `tests/test_required_playbooks.py` |
| [`src/playbooks/retention.py`](../../../src/playbooks/retention.py) | Sweeps unreferenced artifacts, tombstones, orphan files, temporary files, and missing-artifact states. | [Playbooks V2](../../concepts/playbooks.md) | Never makes a live artifact disposable. `tests/test_playbook_artifact_store.py` |

## Execution, state, and views

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/playbooks/runtime.py`](../../../src/playbooks/runtime.py) | Loads ready activations, subscribes dispatch to the event bus, and accepts/replays integration lifecycle events. | [Playbooks V2](../../concepts/playbooks.md) | Sole production V2 runtime; covered by integration and engine scenarios. |
| [`src/playbooks/services.py`](../../../src/playbooks/services.py) | Builds engine dependencies, reads activation state, and resolves integration policy routes. | [Playbooks V2](../../concepts/playbooks.md) | Integration routes fail closed. `tests/test_playbook_services.py` |
| [`src/playbooks/routing.py`](../../../src/playbooks/routing.py) | Builds the immutable routing-activation snapshot and answers whether a task needs a routing gate. | [Playbooks V2](../../concepts/playbooks.md) | Assignment policy is an activated artifact. `tests/test_default_assignment_routing_playbook.py` |
| [`src/playbooks/engine.py`](../../../src/playbooks/engine.py) | Dispatches rules, walks steps, resumes/cancels runs, commits boundaries, and schedules expired waits. | [Playbooks V2](../../concepts/playbooks.md) | Live, dry-run and shadow share graph semantics. `tests/test_v2_engine.py` |
| [`src/playbooks/run_state.py`](../../../src/playbooks/run_state.py) | Defines frozen, versioned run snapshots, lifecycle errors, binding limits, and persistence protocols. | [Playbooks V2](../../concepts/playbooks.md) | Compare-and-set prevents concurrent advances. `tests/test_playbook_run_repository.py` |
| [`src/playbooks/receipts.py`](../../../src/playbooks/receipts.py) | Builds idempotency keys, redacted receipt projections, and stable transition identities. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_playbook_receipts.py` |
| [`src/playbooks/waits.py`](../../../src/playbooks/waits.py) | Defines durable wait specifications, correlations, repository protocol, and atomic wait changes. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_playbook_wait_repository.py` |
| [`src/playbooks/run_overlay.py`](../../../src/playbooks/run_overlay.py) | Projects a run, receipts, bindings, and pinned artifact into a redacted inspection overlay. | [Playbooks V2](../../concepts/playbooks.md) | Never renders a historical run on a newer artifact. `tests/test_playbook_run_overlay.py` |
| [`src/playbooks/graph_projection.py`](../../../src/playbooks/graph_projection.py) | Projects one typed artifact into the semantic graph DTO used by command/API/dashboard views. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_playbook_graph_projection.py` |
| [`src/playbooks/explanation.py`](../../../src/playbooks/explanation.py) | Renders command effects and typed values into safe human-readable graph-node explanations. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_playbook_explanation.py` |
| [`src/playbooks/expressions.py`](../../../src/playbooks/expressions.py) | Defines, validates, resolves, and type-checks the V2 value and condition expression language. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_playbook_v2_expressions.py` |
| [`src/playbooks/invocation.py`](../../../src/playbooks/invocation.py) | Carries the current run/step invocation in task-local context for downstream command work. | [Playbooks V2](../../concepts/playbooks.md) | Context only; it owns no persistent state. |
| [`src/playbooks/resume_handler.py`](../../../src/playbooks/resume_handler.py) | Bridges the legacy `human.review.completed` event into a V2 human-decision resume. | [Playbooks V2](../../concepts/playbooks.md) | Compatibility bridge; covered by engine resume scenarios. |

## Step executors

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [`src/playbooks/executors/__init__.py`](../../../src/playbooks/executors/__init__.py) | Selects the executor for each closed V2 step type and execution mode. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_v2_engine.py` |
| [`src/playbooks/executors/base.py`](../../../src/playbooks/executors/base.py) | Defines executor protocols, execution modes/controls, context, services, results, and receipt projection. | [Playbooks V2](../../concepts/playbooks.md) | Shared executor contract. |
| [`src/playbooks/executors/command.py`](../../../src/playbooks/executors/command.py) | Executes or safely previews/shadows registered AQ command steps and classifies their outcomes. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_v2_engine.py` |
| [`src/playbooks/executors/llm.py`](../../../src/playbooks/executors/llm.py) | Resolves a profile and executes bounded, schema-checked direct LLM steps or symbolic counterparts. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_llm_executor.py` |
| [`src/playbooks/executors/agent_task.py`](../../../src/playbooks/executors/agent_task.py) | Creates/cancels delegated tasks and intersects parent, child, and step capability policies. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_v2_engine.py` |
| [`src/playbooks/executors/decision.py`](../../../src/playbooks/executors/decision.py) | Evaluates ordered decision cases and selects the required default when none match. | [Playbooks V2](../../concepts/playbooks.md) | Deterministic in every mode. |
| [`src/playbooks/executors/foreach.py`](../../../src/playbooks/executors/foreach.py) | Enters, advances, bounds, and aggregates a typed loop. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_v2_engine.py` |
| [`src/playbooks/executors/wait.py`](../../../src/playbooks/executors/wait.py) | Computes live durable waits and maps resumption causes to declared outcomes; reports waits in non-live modes. | [Playbooks V2](../../concepts/playbooks.md) | `tests/test_v2_engine.py` |
| [`src/playbooks/executors/terminal.py`](../../../src/playbooks/executors/terminal.py) | Ends a run with its declared terminal outcome and optional typed result. | [Playbooks V2](../../concepts/playbooks.md) | Deterministic in every mode. |

## Schemas and shipped policy resources

| Resource family | Purpose | Component | Notes |
|---|---|---|---|
| [`src/playbook_v2_schema.json`](../../../src/playbook_v2_schema.json) | Generated JSON Schema for the strict V2 definition model. | [Playbooks V2](../../concepts/playbooks.md) | Regenerate with `scripts/generate-playbook-schema.py`; do not hand-edit. |
| [`src/playbook_schema.json`](../../../src/playbook_schema.json) | Retained V1 JSON Schema for validation compatibility only. | [Playbooks V2](../../concepts/playbooks.md) | V1 execution is removed. |
| [`src/prompts/default_playbooks/`](../../../src/prompts/default_playbooks/) | Shipped system-scope source policies, including default routing and default pipeline. | [Playbooks V2](../../concepts/playbooks.md) | Sources are not proof of local activation. |
| [`src/prompts/project_playbooks/agent-queue/`](../../../src/prompts/project_playbooks/agent-queue/) | Shipped project-scope agent-queue sources for CI sentinel and merge sweep policy. | [Playbooks V2](../../concepts/playbooks.md) | Project policy is optional/configured. |
| [`src/prompts/reviewed_playbooks/default-assignment-routing/`](../../../src/prompts/reviewed_playbooks/default-assignment-routing/) | Generated reviewed bundle for the required assignment-routing artifact, source, manifest, digest, and diagnostics. | [Playbooks V2](../../concepts/playbooks.md) | Imported as exact bytes; do not hand-edit. |

## Focused tests

```bash
aq test tests/test_playbook_v2_definition.py tests/test_playbook_v2_validation.py \
  tests/test_playbook_v2_compiler.py tests/test_playbook_v2_import.py \
  tests/test_playbook_activation.py tests/test_playbook_artifact_store.py \
  tests/test_v2_engine.py tests/test_event_bus.py tests/test_event_schemas.py \
  tests/test_workflow_pipeline_view.py tests/test_required_playbooks.py
```

* [Playbooks V2](../../concepts/playbooks.md) — concepts, lifecycle, operating procedure, and recovery.
* [Module catalog](README.md) — all subsystem shards.
