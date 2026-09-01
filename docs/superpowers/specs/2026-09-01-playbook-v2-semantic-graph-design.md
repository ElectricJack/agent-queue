# Playbook V2 Semantic Graph Design

**Status:** Approved design
**Date:** 2026-09-01

## Summary

Playbook V2 makes the compiled graph a strict, typed execution contract that is both executable and explainable. Markdown remains the authoring source of truth. The compiler produces a semantic graph body, the server adds authoritative metadata and validates it, and the resulting JSON is the sole executable artifact.

The same typed graph drives execution, dry-run, graph rendering, and the selected-node inspector. Human-readable intent is a deterministic projection of executable fields and command contracts; it is never a separately generated summary.

The graph UI keeps the existing node-and-edge topology. It filters large playbooks by input event, preserves every reachable branch, and makes the behavior inside each node legible through richer cards and a contract-derived inspector.

## Problem

The current playbook abstraction represents several execution models with one loosely typed JSON shape:

- ordinary LLM-driven playbooks;
- deterministic command pipelines;
- assignment-routing decisions;
- resumable human and event waits.

This produces special cases across compilation, validation, runners, orchestrator consumers, dry-run, graph projection, and the UI. It also makes the compiled artifact hard for an operator to understand.

The review identified concrete contract failures:

- the compiler profile omits metadata that deserialization requires;
- the published schema omits supported runtime fields and playbook kinds;
- `wait_for_event` is understood by the runner but lost during model round-trip;
- pipeline dry-run uses ordinary-playbook semantics and reports misleading success;
- pipeline rules are flattened into one node map, so the graph loses input-event and rule structure;
- nested action dictionaries mix inputs, templating, output binding, iteration, and control flow;
- natural-language transitions can invoke an LLM without that AI decision being visible as a graph state;
- multiple orchestrator paths independently select and interpret runners;
- definition traversal and the separate `PlaybookRun` lifecycle are both described as state machines.

The UI is therefore exposing storage structure rather than author or operator intent. Fixing only the presentation would preserve the underlying ambiguity.

## Goals

1. Preserve Markdown as the human-authored source and JSON as the deterministic runtime artifact.
2. Represent every executable behavior with a typed node and typed edge.
3. Support graphs that explicitly contain deterministic commands, LLM requests, orchestrated agent tasks, decisions, waits, loops, and terminal outcomes.
4. Make the graph topology and the behavior inside each node understandable without reading raw JSON.
5. Guarantee that rendered intent is derived from the same validated object that executes.
6. Give every orchestrator consumer one execution boundary.
7. Make live runs and dry-runs use the same traversal and runner selection.
8. Detect command-contract changes and require affected playbooks to rebuild.
9. Reject unknown or unrenderable executable fields instead of silently ignoring them.
10. Preserve historical artifacts and run traces for inspection without preserving obsolete executable command APIs.

## Non-goals

- Supporting multiple live versions of a command API.
- Generating an independent natural-language summary and treating it as executable truth.
- Replacing the graph with a simplified recipe or linear checklist.
- Making LLM or agent outputs deterministic.
- Allowing a run to resume against a newly compiled graph with different semantics.
- Keeping V1 artifacts executable indefinitely.

## Terminology

**Playbook definition graph:** The persisted rules, typed nodes, and typed edges that describe the routine.

**Playbook run lifecycle:** The separate runtime state machine for running, paused, completed, failed, timed out, or cancelled runs.

**Deterministic topology:** The set of nodes, legal outcomes, and transitions is fixed in the compiled artifact.

**AI state:** A visible `LlmStep` or `AgentTaskStep` whose result can be nondeterministic within a declared output contract.

**Command contract:** The current public input, output, outcome, behavioral, and presentation contract for a registered orchestrator command.

## Canonical V2 artifact

The V2 artifact is a discriminated, strict schema. It contains common metadata, first-class rules, a typed step map, and the command fingerprints against which it was compiled.

`purpose` describes the result contract expected by the caller, such as `routine` or `assignment_routing`; it does not select a different runner. Typed steps determine execution behavior.

```json
{
  "schema_version": 2,
  "id": "default-pipeline",
  "version": 5,
  "scope": { "type": "system" },
  "source_hash": "sha256:...",
  "compiled_at": "2026-09-01T00:00:00Z",
  "enabled": true,
  "purpose": "routine",
  "rules": [],
  "steps": {},
  "compiled_against": {
    "commands": {
      "ensure_task": "sha256:..."
    }
  }
}
```

### Metadata ownership

Markdown owns author-facing purpose, descriptions, rule names, and desired behavior.

The compiler emits only the semantic body: rules, steps, expressions, declared results, and source references.

The server owns and merges identity, scope, artifact version, source hash, compilation time, activation metadata, and command fingerprints. Compiler output is never expected to invent these fields.

The installed JSON artifact is the only executable source of truth. Markdown must be recompiled before an edit can execute.

### Strictness

The runtime model and published JSON Schema have one source. Unknown fields fail validation. Every supported field must survive serialization round-trip. The loader never silently discards executable data.

`schema_version` versions the artifact format only. It does not imply support for multiple current command APIs. V1 becomes historical and read-only after cutover.

## First-class rules

A rule preserves the structure that is currently flattened into pipeline metadata.

```json
{
  "id": "per-branch-final-review",
  "title": "Branch final review",
  "description": "Request and route a final review when required.",
  "trigger": { "event": "task.completed" },
  "guard": {
    "type": "equals",
    "left": { "type": "event_ref", "path": "review_scope" },
    "right": { "type": "literal", "value": "branch" }
  },
  "entry_step": "ensure-review-task",
  "source_ref": { "section": "Branch final review" }
}
```

Rules give the compiler, validator, engine, and graph projector the same input-event and grouping information. Multiple rules may use the same event. The UI may display them as separate clusters on one event-scoped canvas.

Triggers and guards are executable graph semantics, not UI-only metadata. A guard must use a typed deterministic condition. When judgment requires an LLM, the rule enters an explicit `LlmStep` instead of hiding an LLM evaluation inside natural-language edge text.

## Typed values and conditions

Opaque interpolation strings are replaced by a typed value-expression union:

- `LiteralValue`
- `EventRef`
- `ContextRef`
- `ResultRef`
- `TemplateValue`, composed from typed value parts

This preserves dynamic behavior while making references statically validatable and deterministically explainable. For example, `{{event.project_id}}` becomes an `EventRef` whose display label can be rendered as “this event's project.”

Conditions similarly use a typed expression tree for comparisons, boolean composition, existence, and collection predicates. A condition cannot contain undeclared natural-language LLM behavior.

## Typed step family

Every step has common fields: stable internal ID, human title and optional description, a type discriminator, optional source reference, type-specific configuration, optional result binding, and typed transitions or a terminal outcome.

### CommandStep

Invokes a registered orchestrator command.

```json
{
  "type": "command",
  "title": "Ensure a review task",
  "command": "ensure_task",
  "inputs": {
    "project_id": { "type": "event_ref", "path": "project_id" },
    "title": {
      "type": "template",
      "parts": [
        { "type": "literal", "value": "Review: " },
        { "type": "event_ref", "path": "title" }
      ]
    }
  },
  "save_result_as": "review",
  "transitions": {
    "success": "review-branch",
    "failure": "review-unavailable"
  }
}
```

The command invocation contains no nested control-flow dictionary. Result binding and transitions are explicit step fields.

The top-level `compiled_against.commands` map is the authoritative fingerprint record. Steps reference a command by name so the same compatibility fact is not duplicated across nodes.

### LlmStep

Performs an inline LLM request inside the current playbook run. It declares a profile, prompt inputs, output schema, budgets or policy, result binding, and transitions keyed by typed outcomes.

LLM branching must use declared structured output, such as an enum. The graph visibly identifies the node as an AI state and labels every legal outcome edge.

### AgentTaskStep

Creates an orchestrated agent task. It declares the profile, objective, input bindings, whether to wait for completion, result binding, timeout, and transitions for completed, failed, and timed-out outcomes.

This is separate from `LlmStep` because it has different scheduling, persistence, cost, waiting, cancellation, and failure semantics.

### DecisionStep

Branches deterministically on an existing typed value or condition. It contains explicit cases and an optional default. It does not make a hidden LLM request.

### WaitStep

Pauses for a human response, external event, or task completion. It declares the wait kind, correlation expression, optional timeout, result binding, and timeout transition. Event waits are part of the canonical model and must survive round-trip.

### ForEachStep

Iterates a declared body over a typed collection with an explicit item binding, completion behavior, failure policy, and continuation. This preserves current loop capability without nesting iteration semantics inside a command dictionary.

### TerminalStep

Completes the rule or playbook with a typed outcome and optional typed result.

## Command contracts

There is one active contract and handler for each command name.

```python
CommandRegistration(
    name="ensure_task",
    contract=EnsureTaskContract(...),
    handler=ensure_task,
)
```

A command cannot register without both parts. The contract declares:

- accepted inputs, types, required fields, and readable field labels;
- observable behavioral guarantees, including idempotency or deduplication;
- output schema and possible outcomes;
- retry, timeout, and side-effect characteristics;
- safe display behavior for secrets or large values;
- event, context, and result reference compatibility;
- semantic facts used by the explanation builder.

The contract fingerprint is computed from this public contract. Internal refactors that preserve the contract do not invalidate playbooks. Changes to inputs, outputs, outcomes, behavioral guarantees, or reference support change the fingerprint and require affected playbooks to rebuild.

CI treats observable handler changes without a corresponding contract change as a defect. The system cannot mathematically prove arbitrary implementation code is bug-free, but it can guarantee that the UI neither invents nor omits declared executable behavior.

## Intent explanation

Intent is a deterministic projection of the typed step and its current contract. It is not authored as separate prose and is not generated by an LLM.

The backend explanation service emits a structured view model:

```text
title: Ensure a review task exists
effect: Create or reuse the matching task
inputs:
  Project -> this event's project
  Title   -> "Review: " + event title
result:
  Save the task as "review"
outcomes:
  Success -> Review the branch
  Failure -> Review unavailable
```

The frontend lays out this structure but does not reinterpret command semantics. The explanation builder is exhaustive: if it cannot account for an executable field, compilation or activation fails. Unknown behavior is never silently hidden behind a friendly summary.

The intent card is read-only. Editing occurs through the Markdown source or a structured editor that modifies semantic fields and recompiles the artifact. Advanced mode exposes exact typed JSON, expressions, internal IDs, and command fingerprints.

## Compilation and activation flow

1. An author edits Markdown.
2. The compiler agent emits a typed semantic body with source references.
3. The server merges authoritative metadata.
4. The validator resolves command registrations, validates value references, checks output bindings and edge targets, and generates fingerprints.
5. The explanation builder verifies that every executable field is representable.
6. The server presents a structural diff of rules, steps, and transitions.
7. Activation atomically installs the fully validated artifact.

A failed compilation or rebuild never partially replaces an installed artifact.

## Unified execution engine

Every orchestrator entry point calls one `PlaybookEngine` boundary: event trigger, manual run, dry-run, assignment-routing request, and resume after a wait.

Consumers do not select a pipeline runner or ordinary playbook runner and do not interpret graph dictionaries themselves.

The engine validates artifact compatibility, selects matching first-class rules, traverses typed transitions, and delegates each node to a type-specific executor:

- `CommandExecutor`
- `LlmExecutor`
- `AgentTaskExecutor`
- `DecisionExecutor`
- `WaitExecutor`
- `ForEachExecutor`
- `TerminalExecutor`

Assignment routing uses the same engine and returns a declared typed result for the scheduler. Its role does not introduce a separate graph interpretation path.

### Determinism boundary

The compiled artifact fixes topology, allowed capabilities, value bindings, output schemas, and legal transitions. `CommandStep` and deterministic conditions follow deterministic control semantics. `LlmStep` and `AgentTaskStep` may produce nondeterministic results, but those calls are explicit nodes with constrained outputs and visible outgoing edges.

### Dry-run

Dry-run uses the same graph selection and traversal. Only executor policy changes:

- commands validate and describe effects without side effects;
- deterministic expressions execute normally;
- waits are reported without persisting a pause;
- LLM or agent states are either invoked under an explicit dry-run option or reported as unresolved;
- unresolved AI outcomes may produce a bounded set of possible paths rather than a false completed path.

Dry-run cannot use a different runner or silently treat an unrecognized node as terminal.

### Execution receipts

Every run pins the artifact hash and records structured receipts containing the step ID and type, safely redacted resolved inputs, command or AI profile, result or outcome, selected transition, timestamps, retry, and timeout information.

Receipts drive historical run inspection and graph path overlays.

## Graph and node experience

The graph remains the primary representation. The design does not replace it with a recipe view.

### Event-scoped canvas

The top-level selector filters by input event, such as `task.completed`, `spec.created`, or `commit.created`. All rules for the selected event appear as labeled clusters. Every reachable node and branch remains visible. An “All events” option remains available.

Filtering changes graph scope, not semantics. It never removes a branch reachable from a displayed rule.

### Rich node cards

Explain mode displays the important behavior inside each node:

- command name, important inputs, result binding, and success/failure ports;
- LLM profile, decision purpose, declared output choices, and outcome ports;
- agent profile, objective, wait behavior, result binding, and lifecycle outcomes;
- decision expression summary and cases;
- wait type, awaited event or human input, correlation, and timeout;
- loop collection and item binding;
- terminal outcome.

Internal IDs are secondary. Compact mode remains available for dense graphs.

Edges remain first-class and visibly connect outcome ports to target nodes. Labels use exact typed outcomes such as `success`, `failure`, `approve`, `revise`, `completed`, and `timed_out`. Equivalent edges may share a route only when their distinct labels remain visible.

### Selected-node inspector

Selecting a node opens the full contract-derived intent card. The inspector is absent or collapsed when nothing is selected, so it does not reserve a large empty portion of the canvas.

The inspector shows operation meaning, input sources, output binding, legal outcomes, and target titles. Exact expressions and raw JSON live in Advanced.

### Run overlay

A selected historical or live run highlights the exact path from execution receipts and shows outcomes on traversed nodes and edges. The definition and execution trace use the same graph identity.

## Compatibility, rebuild, and failure behavior

`enabled` and computed `health` are independent. Health is `ready`, `needs_rebuild`, or `invalid`.

At startup, activation, run start, and resume, the system compares the artifact's per-command fingerprints to the current registry. A changed or removed command marks only affected playbooks `needs_rebuild` and prevents new runs.

The dashboard identifies the changed command and affected nodes and provides a rebuild action. Rebuild recompiles Markdown against the current contracts, validates the result, shows the structural diff, and atomically installs the new artifact.

An already-dispatched command may finish. Before the next step executes, compatibility is checked. A mismatch fails the run with structured reason `command_contract_changed`. The run is not resumed against the rebuilt graph; an operator starts a fresh run from the new artifact.

Historical artifacts and traces remain readable. They are not executable against obsolete command contracts.

## Migration and cutover

Migration avoids a permanent dual-runtime architecture:

1. Introduce the strict V2 model and current command-contract registry.
2. Register contracts for every command used by active playbooks.
3. Add the unified engine and type-specific executors.
4. Compile every active Markdown source into V2 and resolve validation failures.
5. Enable the rich graph projection for V2 while retaining read-only V1 inspection.
6. Verify structural fixtures for shipped playbooks.
7. Atomically switch orchestrator entry points to the unified engine.
8. Disable V1 execution and remove new V1 artifact production.

Cutover occurs only when every active Markdown playbook has a ready V2 artifact and no active playbook is stale.

## Validation and testing

### Model and schema invariants

- Runtime types and published JSON Schema share one source.
- Every supported step type validates against the published schema.
- Every supported field survives JSON round-trip.
- Unknown fields are rejected.
- Fixtures cover commands, LLM calls, agent tasks, decisions, waits, loops, and terminals.
- Every shipped playbook validates against the same schema used at activation.

### Compiler invariants

- Compiler-agent output is valid as a semantic body.
- Server-owned metadata is merged before full-artifact validation.
- All shipped Markdown sources compile into V2.
- Source references survive compilation.
- Invalid references, missing targets, undeclared outputs, and hidden natural-language AI transitions fail compilation.

### Command and explanation invariants

- A handler cannot register without a contract.
- Public contract changes alter the fingerprint.
- Every executable field is consumed by the explanation builder.
- Sensitive fields are redacted according to contract policy.
- Golden intent fixtures cover current shipped commands, including `ensure_task`.
- Changed contracts stale affected playbooks while unrelated playbooks remain ready.

### Engine invariants

- Event, manual, dry-run, assignment, and resume paths call the same engine.
- Live and dry-run select the same rules, nodes, and edges for identical resolved outcomes.
- LLM and agent results conform to declared schemas.
- Wait states serialize and resume without data loss.
- Execution receipts identify every traversed node and selected edge.
- An incompatible in-progress run fails before invoking the changed command.

### UI invariants

- Event filtering preserves every reachable branch.
- Rich cards cover every step type.
- Edge labels match typed outcomes.
- Node cards and the inspector consume the same explanation payload.
- Advanced mode exposes canonical data without making it the default explanation.
- A run overlay matches recorded receipts and artifact hash.

### Cutover acceptance

- No executable V1 artifacts remain.
- Every active Markdown playbook has a ready V2 artifact.
- No active playbook has stale command fingerprints.
- The default pipeline's rules, nodes, and edges match its approved structural fixture.
- Focused and full test suites pass.

## Alternatives rejected

### UI-only semantic overlay

This would improve readability quickly but leave the compiler, schema, runners, dry-run, and consumers interpreting different shapes. It cannot guarantee that intent matches execution.

### Permanently versioned command APIs

Pinning `ensure_task@1`, `ensure_task@2`, and later versions would keep old artifacts executable but require indefinite compatibility support. The chosen design keeps one current API and requires affected playbooks to rebuild.

### Separate permanent playbook products

Maintaining independent pipeline, ordinary-playbook, and assignment-routing resources would make each local model clearer but preserve duplicated orchestration paths. Typed nodes and one engine retain mixed deterministic and AI routines while giving each behavior explicit semantics.

### Independently generated intent prose

An LLM-written or separately stored summary can drift from executable code. The chosen design renders intent only from typed executable data and current command contracts.

## Decision recap

- Markdown remains the authoring source of truth.
- Strict V2 JSON is the executable source of truth.
- Rules, triggers, guards, nodes, values, and edges are typed.
- Commands, inline LLM requests, and orchestrated agent tasks remain valid playbook states.
- AI behavior is visible and constrained rather than hidden in natural-language transitions.
- One current command API is supported; command-contract changes require rebuild.
- One engine serves all orchestrator entry points and dry-run.
- The graph keeps complete topology, scopes by input event, and explains behavior inside rich nodes.
- Intent rendering is fail-closed and derived from the same artifact that executes.
- V1 remains historical and read-only after migration.
