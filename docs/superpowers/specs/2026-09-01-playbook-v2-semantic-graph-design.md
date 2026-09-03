# Playbook V2 Semantic Graph Design

**Status:** Approved design, revised after adversarial review
**Date:** 2026-09-01

## Summary

Playbook V2 makes the compiled graph a strict, typed execution contract that is both executable and explainable. Markdown remains the authoring source of truth. The compiler produces a semantic graph body, the server adds authoritative metadata and validates it, and the resulting JSON is the sole executable artifact.

Phase 0 establishes the security boundary before the graph model changes: compiler output cannot claim authoritative metadata, every AI state runs as an authenticated capability principal, tool and command authorization is enforced at dispatch as well as in the model-visible schema, and delegation cannot widen capabilities. These invariants are carried forward by the V2 engine rather than left behind in the old runner.

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
- pre-pause result bindings are held only in memory, so resumed runs can lose values needed by later steps;
- profile allowlists mix harness tools and orchestrator commands even though agent sessions cannot enforce the latter through a harness flag;
- two runtime-status enums disagree, cancellation has no legal transition, and command success is inferred inconsistently;
- mutable artifact files have no authoritative activation record or retained content-addressed history.

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
9. Reject unknown executable fields and require every validated field to have a lossless canonical rendering.
10. Preserve historical artifacts and run traces for inspection without preserving obsolete executable command APIs.
11. Preserve capability confinement across inline LLM calls, agent tasks, CLI/MCP command calls, and nested delegation.
12. Persist enough typed run state to resume waits and loops without losing pre-pause results.

## Non-goals

- Supporting multiple live versions of a command API.
- Generating an independent natural-language summary and treating it as executable truth.
- Replacing the graph with a simplified recipe or linear checklist.
- Making LLM or agent outputs deterministic.
- Allowing a run to resume against a newly compiled graph with different semantics.
- Keeping V1 artifacts executable indefinitely.
- Automatically rebuilding or activating a playbook after a command contract changes.
- Treating a model-visible tool list, a prompt, or a harness flag as an authorization boundary.
- Supporting parallel or nested `ForEachStep` execution in the initial V2 release.

## Terminology

**Playbook definition graph:** The persisted rules, typed nodes, and typed edges that describe the routine.

**Playbook run lifecycle:** The separate runtime state machine for running, paused, completed, failed, timed out, or cancelled runs.

**Deterministic topology:** The set of nodes, legal outcomes, and transitions is fixed in the compiled artifact.

**AI state:** A visible `LlmStep` or `AgentTaskStep` whose result can be nondeterministic within a declared output contract.

**Command contract:** The current public input, output, outcome, behavioral, and presentation contract for a registered orchestrator command.

**Capability policy:** The resolved, fingerprinted set of harness tools, orchestrator commands, and MCP tools available to one AI principal. These namespaces are distinct and are never stored in one ambiguous string list.

**Run snapshot:** The mutable, authoritative continuation state persisted at each durable execution boundary.

**Execution receipt:** An immutable audit record of one durable boundary within a started, completed or interrupted step attempt. An attempt that reaches outside the engine (command, LLM, agent task) has one `attempt_start` receipt written before its first external side effect and one `step` receipt at completion; an LLM attempt may also have ordered `tool_turn`, schema-retry `llm_call`, and `interrupted` receipts. Receipts explain history; they are not replayed to reconstruct continuation state.

**Artifact hash:** The SHA-256 digest of the canonical installed JSON bytes. A run always points to this immutable artifact identity.

## Canonical V2 artifact

The V2 artifact is a discriminated, strict schema. It contains common metadata, first-class rules, a typed step map, and the execution-contract and capability-policy fingerprints against which it was compiled.

`purpose` describes the result contract expected by the caller, such as `routine` or `assignment_routing`; it does not select a different runner. Typed steps determine execution behavior.

```json
{
  "schema_version": 2,
  "id": "default-pipeline",
  "version": 5,
  "scope": { "type": "system" },
  "source_hash": "sha256:...",
  "compiled_at": "2026-09-01T00:00:00Z",
  "purpose": "routine",
  "rules": [],
  "steps": {},
  "compiled_against": {
    "commands": {
      "ensure_task": "sha256:..."
    },
    "profiles": {
      "reviewer": "sha256:..."
    }
  }
}
```

### Metadata ownership

Markdown owns author-facing purpose, descriptions, rule names, and desired behavior.

The compiler emits only the semantic body: rules, steps, expressions, declared results, and source references.

The server owns and merges identity, scope, artifact version, source hash, compilation time, and command/profile fingerprints. Operational activation metadata, including `enabled`, health, and the active artifact hash, lives in `playbook_activations` rather than inside the immutable artifact. Compiler output is never expected to invent any of these fields.

The server also extracts an authoritative identifier inventory from frontmatter and exact backticked identifiers in the Markdown. Every command name, event name, profile ID, context key, result name, dedup-key pattern, and external field name emitted by the compiler must appear in that inventory. Internal artifact-local step IDs may be compiler-generated. An external identifier absent from the source is a compile error, never a model guess.

The installed JSON artifact is the only executable source of truth. Markdown must be recompiled before an edit can execute.

### Strictness

The runtime model and published JSON Schema have one source. Unknown fields fail validation. Every supported field must survive serialization round-trip. The loader never silently discards executable data.

`schema_version` versions the artifact format only. It does not imply support for multiple current command APIs. V1 becomes historical and read-only after cutover.

The generated runtime schema is served directly by the backend. A checked-in documentation snapshot is regenerated in CI, and CI fails when regeneration changes it. Activation validates with the same runtime model that generates that schema; a hand-written loader is not a second interpretation path.

## Phase 0: security and authority baseline

Phase 0 lands before the V2 schema and executor migration. It restores and tests the security promises that already exist in pieces but are not consistently enforced by the current compiler-agent and agent-session paths.

### Authoritative compilation boundary

The compiler agent is untrusted with respect to authority. It proposes only the semantic body and source references. The server discards any compiler-supplied identity, scope, triggers, filters, enabled state, artifact version, hashes, activation state, or capability snapshots. It reconstructs artifact fields from trusted source metadata and registries and preserves operational state from the activation record.

Activation accepts a compiler artifact only when:

- every executable identifier is present in the source identifier inventory;
- every `source_ref` resolves to the source span that names the relevant behavior;
- all referenced commands, profiles, events, and fields resolve;
- the server has merged authoritative metadata and recomputed canonical hashes;
- strict validation, explanation coverage, and the human structural-diff review succeed.

The current orphaned frontmatter-merge behavior is restored on the actual install path as the first Phase 0 change. Tests prove that a compiler cannot invent, omit, or widen `profile_id`, scope, triggers, filters, or enabled state.

### Capability model

Every `LlmStep` and `AgentTaskStep` names a profile explicitly. There is no unscoped AI execution in V2. The server resolves that profile into three independent allowlists:

- `harness_tools`: local model-client capabilities such as read, edit, shell, or web access;
- `commands`: contracted orchestrator commands such as `ensure_task` or `task_close`;
- `mcp_tools`: fully qualified third-party MCP tools.

An empty list means none. Wildcards and “missing means default tools” are invalid in a V2 capability policy. The artifact records the resolved profile fingerprint; any profile change requires review and rebuild before a new run can use it.

Tool schemas are narrowed to the policy for model guidance, and the dispatcher independently authorizes every invocation. A model-generated call not present in the active policy is rejected before handler lookup. This dispatch check is the security boundary.

Every playbook run, inline LLM call, and agent session receives an authenticated execution principal containing the run, rule, step, task, profile, and capability-policy identity. CLI and MCP requests carry a short-lived token for that principal. The server authorizes orchestrator commands from the principal; access through `Bash`, an MCP transport, or another adapter cannot bypass the command allowlist.

Delegation is monotonic. An `AgentTaskStep` or an AI-invoked task-creation command may select only a profile whose three capability sets are subsets of the caller's policy. Missing caller identity, an unknown profile, or an incomparable policy fails closed. The subset check is performed by the server, not inferred from prompt text.

Phase 0 adds end-to-end tests for compiler authority, off-list dispatch rejection, empty-policy lockdown, missing-profile failure, CLI/MCP principal propagation, and recursive no-widening delegation. Unit tests that replay the expected subset algorithm without invoking the real handler are not sufficient acceptance evidence.

## Authoring contract

Playbook sources are English prose with YAML frontmatter. Embedded JSON graph blocks are disallowed. The prose is normative; the compiler agent owns prose-to-typed-graph translation, and the server owns validation and authority.

Authors use controlled English only for executable identifiers: command names, profiles, events, fields, result bindings, outcome labels, and literal keys appear exactly in backticks or frontmatter. Prose can describe structure freely, but the compiler never invents an executable identifier. If prose leaves a guard, transition target, failure path, input source, or output choice ambiguous, compilation fails with a question tied to the relevant `source_ref`. There are no silent defaults for missing executable behavior.

The authoring loop is:

1. edit prose;
2. compile to a proposed typed graph;
3. read the contract-derived intent cards and structural diff as the proofread surface;
4. revise the prose when the proposal does not match intent;
5. explicitly activate the reviewed artifact.

Compilation is allowed to be nondeterministic; activation is not automatic. Command contracts are expected to change rarely, and each resulting rebuild is manually reviewed as a whole. Step IDs are artifact-local and may change across rebuilds. Cross-version analytics use rule IDs and source references when possible and otherwise accept version fragmentation.

Bundled sources, including `default-pipeline.md`, are converted to normative prose and lose their embedded JSON blocks before cutover. A structural fixture verifies the approved compiled rules, steps, and edges for each bundled playbook. Pipeline and ordinary playbooks use the same compiler entry point.

## First-class rules

A rule preserves the structure that is currently flattened into pipeline metadata.

```json
{
  "id": "per-branch-final-review",
  "title": "Branch final review",
  "description": "Request and route a final review when required.",
  "trigger": {
    "event": "task.completed",
    "filter": { "task.kind": "implementation" }
  },
  "guard": {
    "type": "equals",
    "left": { "type": "event_ref", "path": "review_scope" },
    "right": { "type": "literal", "value": "branch" }
  },
  "entry_step": "ensure-review-task",
  "source_ref": { "section": "Branch final review" }
}
```

Rules give the compiler, validator, engine, and graph projector the same input-event and grouping information. Multiple rules may use the same event. The UI displays them as separate clusters on one event-scoped canvas.

Every event type registers a payload schema. A trigger filter is a subscription-level conjunction of literal equality or membership tests against that schema; it cheaply decides whether to offer an event to a rule. A guard is a full typed deterministic expression evaluated after delivery. Both survive compilation and appear in the inspector. When judgment requires an LLM, the rule enters an explicit `LlmStep` instead of hiding an LLM evaluation inside natural-language edge text.

Each matching rule creates an independent run with its own status, snapshot, and receipts. Runs produced from one incoming event share a `dispatch_id`, but one rule's failure cannot abort another rule. Event deduplication uses `(scope, playbook_id, rule_id, event_id)`; rebuilding a playbook does not silently replay an event already consumed by that rule.

A rule owns a closed subgraph. Transitions cannot cross into another rule's steps, and result bindings cannot cross rule boundaries. Reusable source prose or compiler helpers can remove authoring repetition, but the artifact duplicates shared terminal or utility steps per rule so the event-scoped graph never requires cross-cluster edges.

## Typed values and conditions

Opaque interpolation strings are replaced by a typed value-expression union:

- `LiteralValue`
- `EventRef`
- `ContextRef`
- `ResultRef`
- `LoopItemRef`
- `ListValue`, whose items are typed values
- `ObjectValue`, whose fields are typed values
- `TemplateValue`, composed from typed value parts and producing only a string
- `CoalesceValue`, which provides an explicit typed fallback

This preserves dynamic behavior while making references statically validatable and deterministically explainable. For example, `{{event.project_id}}` becomes an `EventRef` whose display label can be rendered as “this event's project,” while `waiter_task_ids: ["{{outputs.dep.id}}"]` becomes a `ListValue` containing a typed reference rather than a string template.

`EventRef` paths validate against the rule's registered event schema. Context references validate against an engine context schema. Result references validate against the producing step's output schema. The compiler performs all-paths definite-assignment analysis across branches: a `ResultRef` is legal only where its binding is guaranteed to exist. Optional behavior uses an explicit `CoalesceValue` or an existence condition; it is never inferred.

A missing or type-invalid value fails before the command or AI call with reserved outcome `input_resolution_failed`. The engine never injects an `UNRESOLVED` marker and never coerces a missing value to an empty string.

Loop item bindings are lexical, cannot shadow event, context, result, or outer loop names, and are not visible after the loop. Initial V2 forbids nested `ForEachStep`, making definite assignment and persisted loop state finite and inspectable.

Conditions use a typed expression tree for comparisons, boolean composition, existence, and collection predicates. A condition cannot contain undeclared natural-language LLM behavior.

## Typed step family

Every step has common fields: an artifact-local internal ID, human title and optional description, a type discriminator, required source reference, type-specific configuration, optional result binding, and typed transitions or a terminal outcome.

Business outcomes come from a command or step schema. The engine additionally reserves `input_resolution_failed`, `unavailable`, `contract_violation`, `state_limit_exceeded`, `interrupted`, `timed_out`, and `cancelled`. Step-specific schemas can add reserved outcomes such as LLM budget or provider failures. A step maps every business outcome and either maps each applicable reserved outcome or supplies one visible `runtime_error` target. The graph projector emits labeled edges for every possible outcome even when several labels share a target.

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
    "failure": "review-unavailable",
    "runtime_error": "review-unavailable"
  }
}
```

The command invocation contains no nested control-flow dictionary. Result binding and transitions are explicit step fields.

The top-level `compiled_against.commands` map is the authoritative fingerprint record. Steps reference a command by name so the same compatibility fact is not duplicated across nodes.

### LlmStep

Performs an inline LLM request inside the current playbook run. It declares a profile, typed prompt inputs, output schema, tool-use policy, retry policy, result binding, and transitions keyed by typed outcomes.

LLM branching must use declared structured output, such as an enum. The graph visibly identifies the node as an AI state and labels every legal outcome edge.

Every LLM step declares `max_calls`, `max_output_tokens`, `max_total_tokens`, and `timeout_seconds`. Token accounting uses provider-reported input and output usage. A provider adapter that cannot report usage cannot run a step with a hard total-token budget. The reserved outcomes are `invalid_output` after schema retries are exhausted, `budget_exceeded`, `provider_error`, `timed_out`, and `cancelled`.

When tool use is enabled, every tool call is authorized by the step principal and routed through the same contracted command or MCP dispatch boundary used outside the LLM. Model-visible schemas are a narrowed projection, not the enforcement mechanism. `LLMClient.run_tools` reports each completed tool turn through an awaited callback carrying the zero-based turn index, tool-call IDs, a digest of the canonical tool results, provider-reported usage, and the two-message transcript delta. The engine supplies that callback and is the only caller of `commit_boundary`: it appends the delta to the pinned run snapshot and atomically writes one `tool_turn` receipt before the client may start another provider call.

An interrupted provider or tool call is never silently replayed. Cancellation observed inside a turn reports an `interrupted` turn payload; the engine atomically writes an `interrupted` receipt, appends an accounting-only turn record (known usage and no transcript delta), records an `operator_decision_id`, and pauses the run without binding output or selecting an edge. The interrupted call therefore consumes the same call/token budget after restart and advances the next turn index without replaying partial conversation. If cancellation races a completed turn entering storage, a per-run writer latch orders the completed turn first, then exactly one cancellation boundary, and prevents another provider call. The engine also passes a live cancellation signal through the step context; the client checks it immediately before every tool dispatch, so grace expiry may leave already-started work running but never launches a new contracted side effect afterward. A schema-invalid provider response that will be retried is also committed first as `llm_call`, including its usage, assistant response, and corrective user message, so every call made before another provider request survives a restart whether or not tools are enabled. The final step boundary adds the not-yet-durable final call to the run budget without double-counting prior turn receipts. Recovery treats a persisted `running` LLM snapshot at its current step as an ambiguous in-flight call and performs the same pause before provider I/O. Only an explicit operator `retry` clears that decision and continues from durable transcript deltas. Provider and tool-dispatch deadlines remain `timed_out`; only external cancellation in the durable callback mode becomes an interruption, while callers that omit the callback retain ordinary `CancelledError` propagation.

### AgentTaskStep

Creates an orchestrated agent task. It declares the profile, objective, input bindings, whether to wait for completion, result binding, timeout, cancellation policy, and transitions for `dispatched`, `completed`, `failed`, `timed_out`, and `cancelled` as applicable.

This is separate from `LlmStep` because it has different scheduling, persistence, cost, waiting, cancellation, and failure semantics.

The child session receives a principal derived from the parent run, and its profile must be a capability subset of the parent AI principal when the parent is an AI state. Cancellation does not kill shared or reused work implicitly: `cancel_child` is explicit and defaults to false.

### DecisionStep

Branches deterministically on an existing typed value or condition. It contains explicit cases and an optional default. It does not make a hidden LLM request.

### WaitStep

Pauses for a human response, external event, or task completion. It declares the wait kind, an exact typed correlation key computed at pause time, optional timeout, result binding, and timeout transition. Event waits are part of the canonical model and must survive round-trip.

The engine atomically persists the run snapshot, wait registration, correlation key, and paused status. Event ingestion writes to a durable inbox before matching waits. Registration checks that inbox in the same transaction, eliminating the race in which an event arrives between the wait decision and persisted pause. A dedicated wait scheduler owns `deadline_at`; TimerService triggers are not synthesized for per-run waits. The earlier of the wait deadline and run deadline wins, and the receipt records which deadline fired.

### ForEachStep

Iterates a declared body sequentially over a typed collection with an explicit item binding, completion behavior, failure policy, and continuation. The allowed failure policies are `halt`, `continue`, and `collect`. The aggregate result contains ordered item results, outcomes, and collected errors.

A `WaitStep` or `AgentTaskStep` is legal inside the body. Because execution is sequential, at most one iteration is active. The run snapshot stores the loop step, current index, item binding, partial aggregate, and resume step. Nested and parallel loops are rejected in initial V2. The run overlay shows a traversal count on the node and exposes individual iterations in the inspector rather than drawing forty copies of the same node.

### TerminalStep

Completes the rule or playbook with a typed outcome and optional typed result.

## Command contracts

V2 starts with the commands used by active playbooks, approximately the current pipeline whitelist plus assignment-routing commands, rather than attempting to contract every registered command at once. A `CommandStep` can reference only a contracted command. Coverage grows when an author needs another command in a playbook.

There is one active contract and handler adapter for each contracted command name.

```python
CommandRegistration(
    name="ensure_task",
    contract=CommandContract(
        inputs=...,
        result=...,
        outcomes=...,
        guarantees=...,
        execution=...,
        security=...,
        receipt=...,
        presentation=...,
    ),
    invoke=ensure_task_adapter,
    preview=preview_ensure_task,
)
```

A command cannot register without an execution contract and invocation adapter. The fingerprint-bearing execution contract declares:

- accepted inputs, types, required fields, and reference compatibility;
- a closed result schema and closed set of business outcomes;
- observable guarantees such as idempotency, deduplication, or create-versus-reuse behavior;
- side-effect class, retry safety, timeout behavior, and an optional idempotency-key field;
- the orchestrator capability required to invoke the command;
- receipt projection and sensitive-field classifications.

Presentation is a separate non-fingerprinted object containing readable field labels, structured effect clauses, examples, and help text. Improving a label cannot stale a playbook.

Effect clauses are data, not prose guesses. Each clause contains a typed predicate over resolved inputs and a semantic operation such as `create`, `reuse`, `link`, `wait`, or `update`. For `ensure_task`, the explanation can therefore state “create or reuse by this deduplication key” only when `dedup_key` is present. A lossless canonical field/value rendering is always available when no richer clause applies.

Handlers do not return duck-typed success dictionaries to the V2 engine. The adapter returns `CommandResult(outcome, value)`; the engine validates both against the contract before binding a result or selecting an edge. Missing fields, unknown fields, unknown outcomes, or type mismatches produce reserved outcome `contract_violation` and an error receipt. No executor infers success from the presence or absence of a `success` key.

An optional `preview` adapter performs no writes or external side effects. It can use a read-only database snapshot to distinguish effects such as create versus reuse and can return a typed simulated result. A command without preview support remains valid for live execution but becomes an unresolved boundary in dry-run.

The execution fingerprint is SHA-256 over canonical UTF-8 JSON with sorted object keys, normalized schema representation, and no presentation fields. Internal refactors and copy changes do not invalidate playbooks. Changes to inputs, results, outcomes, guarantees, security requirements, retry behavior, or reference support change the fingerprint and require a manually reviewed rebuild.

CI cannot infer arbitrary behavior changes from source diffs. Instead, each contracted command has adapter conformance tests, outcome fixtures, and behavioral tests for its declared guarantees. Review policy requires a contract update when observable behavior changes. This is an explicit engineering discipline, not a mechanically provable invariant.

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

The frontend lays out this structure but does not reinterpret command semantics. Every executable field contributes to a canonical explanation record generated from the validated step model. CI has an exhaustiveness test that fails when a new step type or executable field lacks a renderer. Runtime activation relies on strict model validation, not on optional copy templates: when rich presentation metadata is absent, the renderer uses a lossless canonical field/value representation. It never hides the field or invents intent.

This separates reliability from copy quality. A label or help-text improvement does not block activation or change an execution fingerprint, while the operator can always see every executable value. Conditional effect clauses are evaluated from the same resolved typed inputs used by the executor.

The intent card is read-only. Editing occurs through the Markdown source or a structured editor that modifies semantic fields and recompiles the artifact. AI cards show the profile, resolved capability namespaces, capability fingerprint, budgets, and delegation policy. Advanced mode exposes exact typed JSON, expressions, artifact-local IDs, execution fingerprints, and redaction decisions.

## Compilation and activation flow

1. An author edits Markdown.
2. The server parses trusted frontmatter and extracts the exact identifier inventory.
3. The compiler agent emits a proposed typed semantic body with source references.
4. The server discards proposed authoritative fields and merges trusted metadata.
5. The validator resolves event schemas, command registrations, profiles, value references, output bindings, loop scopes, and edge targets.
6. The server resolves capability snapshots and generates execution and profile fingerprints.
7. The explanation service generates the canonical intent projection using its CI-verified exhaustive renderer.
8. The server presents source-to-intent and structural diffs of rules, steps, profiles, capabilities, and transitions.
9. Explicit activation atomically points the playbook at the fully validated immutable artifact.

A failed compilation or rebuild never partially replaces an installed artifact.

## Storage and activation

Compiled JSON remains serialized to disk, but active identity is content-addressed rather than “the latest file at this path.” Canonical artifacts are written under `compiled/artifacts/<sha256>.json`. The server writes and fsyncs a temporary file, renames it to the immutable hash path, and only then updates the database activation pointer in a transaction. A crash can leave an unreferenced immutable file for garbage collection; it cannot expose a half-written active artifact.

The database adds:

- `playbook_artifacts`: artifact hash, path, schema version, source hash, referenced execution/profile digests, creation time, and validation summary;
- `playbook_activations`: scope and playbook ID, active artifact hash, enabled state, health, reason, and activation time;
- `playbook_runs`: one row per rule run, artifact hash, lifecycle, current step, typed snapshot, deadline, dispatch/event identity, and summary;
- `playbook_step_receipts`: immutable durable-boundary records, ordered within an attempt by `turn_index` and `receipt_kind`;
- `playbook_waits`: durable correlation keys and deadlines;
- `playbook_pending_events`: events held while an activation is stale or unavailable.

A run reads its graph from its pinned artifact hash. Historical overlays always render that exact retained artifact, never the current activation. Step IDs need only be stable inside one artifact. Active artifacts and artifacts referenced by retained runs are never collected.

Retention is configurable with these defaults: full receipts and completed run snapshots are retained for 90 days; pinned runs are retained until explicitly unpinned; run summaries and aggregate health metrics are retained indefinitely; unreferenced inactive artifacts are retained for at least 90 days and the most recent ten versions per playbook. Deletion is reference-checked and auditable.

Assignment routing keeps its existing input/options hash cache outside the engine. A cache hit does not create another playbook run or receipt set, preventing scheduler cycles from multiplying identical routing records.

## Run-state persistence

The mutable run snapshot is authoritative for continuation. Receipts are immutable inspection records and are never replayed to reconstruct state.

The snapshot persists at every step boundary and before entering a wait:

- artifact hash, rule ID, current step, lifecycle, attempt, and run deadline;
- validated event and context values;
- typed result bindings from all completed predecessor steps;
- the `ForEachStep` frame: collection digest, current index, item binding, partial aggregate, and resume step;
- wait kind, correlation key, deadline, and claimed event identity;
- completed LLM transcript/tool turns and AgentTask identity needed to continue safely;
- cancellation request and executor-specific acknowledgement state.

A bound result contains only the step's validated declared output, not an arbitrary handler dictionary. One result is limited to 256 KiB of canonical JSON and one run snapshot to 4 MiB by default; exceeding either produces `state_limit_exceeded` before the next step. Contracts mark sensitive fields. Sensitive material is represented by an opaque handle when needed downstream and is never written raw to a receipt. Receipt display is default-deny: an unmarked field is redacted.

State mutation and its receipt commit atomically, and `commit_boundary` is the engine's only write to run state: every snapshot version advance inserts exactly one receipt in the same transaction, the attempt-start fence included. A side-effecting command whose contract declares keyed idempotency receives a value for the contract's key field from the first of three sources: the step's `idempotency_key` override, the step's own binding of the key field, and — only when the author supplied neither — the deterministic attempt key `<run_id>:<step_id>:<attempt>`. The authored value always wins, because a keyed field is a semantic identity rather than an execution token: `ensure_task`'s `dedup_key` is what makes two `task.completed` events about one task converge on a single review task, and `task_batch_commit`'s `proposal_id` is a real row id that an attempt key would falsify. Attempt identity is carried on the receipt in every case, never in the argument. Receipt identity extends attempt identity with `(turn_index, receipt_kind)`: ordinary and pre-existing receipts use `receipt_kind="step"` and `turn_index=-1`; completed LLM tool turns use `tool_turn`; schema-invalid calls that precede a retry use `llm_call`; an ambiguous call uses `interrupted` at the next zero-based index; and the fence committed before a command, LLM or agent-task attempt's first external side effect uses `attempt_start` with outcome `started`, no `completed_at`, and `turn_index` equal to the zero-based start ordinal of the attempt identity (a replay or operator retry that deliberately reuses the attempt number starts it again at the next ordinal, so uniqueness holds without inventing an attempt). The attempt idempotency key remains unchanged across all of an attempt's receipts. Recovery treats an attempt as in flight when the latest attempt-scoped receipt (`attempt_start`, `step`, `interrupted`, `operator_decision`) for the current step is an open `attempt_start`.

After an ambiguous process interruption, a retry-safe command can be replayed with that key; a non-retry-safe command and every in-flight LLM call pause with `operator_decision_required` rather than executing twice. The operator must record one explicit resolution: accept a supplied typed outcome and continue, retry, fail the run, or cancel it. The interrupted receipt and resolution receipt share an opaque `operator_decision_id`; it is a reference into the decision held in the run snapshot, not a foreign key to a second source of truth.

There is one lifecycle enum: `running`, `paused`, `cancelling`, `completed`, `failed`, `timed_out`, and `cancelled`. Cancellation is legal from running or paused. A paused run cancels immediately. An in-flight executor enters `cancelling`, receives a best-effort cancellation signal, and becomes `cancelled` after acknowledgement or the cancellation grace deadline. `AgentTaskStep.cancel_child` controls whether owned child work is also cancelled.

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

Assignment routing uses the same engine and returns a declared typed `AssignmentRoutingResult` for the scheduler. The routing source declares its prompt inputs, profile, no-tool policy, and response schema on an ordinary `LlmStep`. The scheduler adapter owns candidate construction, task-projection synchronization, and the existing input/options hash cache; those are caller concerns, not hidden runner modes. Engine options control dry-run and tracing only and cannot alter graph semantics.

### Determinism boundary

The compiled artifact fixes topology, allowed capabilities, value bindings, output schemas, and legal transitions. `CommandStep` and deterministic conditions follow deterministic control semantics. `LlmStep` and `AgentTaskStep` may produce nondeterministic results, but those calls are explicit nodes with constrained outputs and visible outgoing edges.

### Dry-run

Dry-run uses the same graph selection and traversal. Its result is a bounded path tree whose nodes are `resolved`, `simulated`, or `unresolved`, with the reason and possible outgoing outcomes for each unresolved boundary. Default limits are 32 paths and 1,000 visited step instances; reaching either limit returns `truncated`, never a false completion.

Only executor policy changes:

- a command validates inputs and calls its pure preview adapter when one exists; previews may read through a database snapshot but cannot write or perform external side effects;
- a command without preview support is unresolved, and downstream result references remain symbolic;
- deterministic expressions execute normally;
- waits are reported without persisting a pause;
- LLM and agent states are unresolved by default and fork symbolically across their declared outcomes;
- an explicit `invoke_ai` option can make real, metered AI calls, but command execution remains preview-only;
- a loop over a resolved collection is expanded sequentially within the global bounds; an unresolved collection stops at an unresolved loop boundary.

Dry-run cannot use a different runner or silently treat an unrecognized node as terminal.

### Execution receipts

Every run pins the artifact hash and records structured receipts containing the rule and step ID, step type, receipt kind and turn index, redacted resolved inputs, principal and profile fingerprint, command or AI operation, validated outcome, redacted result projection, selected transition, timestamps, attempt, token usage, idempotency key, wait/cancellation facts, timeout information, and an optional operator-decision reference. Ordering is `(snapshot_version, turn_index, started_at, receipt_id)`; `snapshot_version` remains the authoritative total order because every boundary advances it.

Receipt redaction is driven by contracts and is default-deny. Unmarked inputs and results are redacted; fields appear only when explicitly classified as safe, summarized, or represented by an opaque identifier. The internal run snapshot and user-visible receipt have different projections.

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

Edges remain first-class and visibly connect outcome ports to target nodes. Labels use exact typed outcomes such as `success`, `failure`, `approve`, `revise`, `completed`, and `timed_out`. Every transition record produces one selectable edge with a visible label anchored to its source outcome port. Edges may overlap geometrically only when each remains independently selectable and every label remains visible; projection tests assert that rendered edge identity and count match the artifact's transition identity and count.

Because each rule owns a closed subgraph, shared terminals are duplicated inside their rule clusters. The UI never draws cross-cluster terminal spaghetti and never merges semantically distinct steps merely because their labels match.

### Selected-node inspector

Selecting a node opens the full contract-derived intent card. The inspector is absent or collapsed when nothing is selected, so it does not reserve a large empty portion of the canvas.

The inspector shows operation meaning, input sources, output binding, legal outcomes, target titles, profile identity, capability namespaces, budgets, retry/idempotency behavior, and receipt redaction. Exact expressions and raw JSON live in Advanced.

### Run overlay

A selected historical or live run loads the run's retained artifact hash, highlights the exact path from execution receipts, and shows outcomes on traversed nodes and edges. The overlay is never projected onto a newer activation. For repeated loop traversals, the inspector selects an iteration and receipt while the graph retains one definition node.

## Compatibility, rebuild, and failure behavior

`enabled` and computed `health` are independent. Health is:

- `ready`: every referenced command and profile is available and fingerprint-compatible;
- `needs_rebuild`: a referenced execution contract or capability profile changed, or an intentionally removed command/profile no longer exists;
- `unavailable`: the compatible provider is expected but transiently failed to load or connect;
- `invalid`: the artifact, metadata, or storage record is corrupt or violates the V2 schema.

At startup, activation, run start, and resume, the system compares stored per-command and per-profile execution fingerprints to the current registries. Presentation metadata is excluded. Registry bootstrap records whether a command provider is loaded, intentionally absent, or expected-but-unavailable, so a plugin startup failure does not masquerade as an API change.

A mismatch marks the activation `needs_rebuild` and prevents new runs. The dashboard identifies changed commands or profiles and derives affected nodes by scanning the artifact; no per-node compatibility ledger is stored. Rebuild recompiles Markdown against current contracts, validates it, shows the full structural and capability diff, and requires explicit human activation. The system never auto-activates an LLM rebuild.

An already-dispatched command may finish. Before the next step executes, compatibility is checked. A command or profile mismatch fails the run with structured reason `execution_contract_changed` and identifies the changed dependency. The run is not resumed against the rebuilt graph; an operator starts a fresh run from the new artifact.

A transiently unavailable dependency holds new event-driven work in `playbook_pending_events` and pauses an in-progress run at the next boundary with reason `dependency_unavailable`. When compatibility returns, the event or run can continue against the same artifact. A synchronous caller such as assignment routing receives a typed unavailable result and applies its existing caller-owned retry or fallback policy.

Events for a `needs_rebuild` activation are queued rather than dropped. Pending events preserve the original event ID and arrival order, default to a seven-day retention window, and replay subject to rule-level deduplication after activation of the reviewed rebuild. Expiry creates an auditable dropped-event record and operator alert. An operator can explicitly discard or replay a pending event; activation alone never duplicates an event already consumed.

Historical artifacts and traces remain readable. They are not executable against obsolete command contracts.

## Steady-state operations

Command contracts are expected to change rarely. A release that changes a bundled command's execution contract must include freshly compiled, reviewed bundled artifacts. Deployment validation fails before service cutover when those artifacts do not match; production does not attempt an automatic LLM rebuild.

When a plugin fails to load, dependent activations become `unavailable`, not `needs_rebuild`. Events queue and dashboards identify the provider failure. When the same contract returns, work resumes without recompilation. An intentional plugin uninstall is a contract removal and requires affected playbooks to be rebuilt or disabled.

A surprising rebuild diff leaves the old artifact active when still compatible, or leaves the activation in `needs_rebuild` when incompatible. The reviewer can reject the proposal and edit the Markdown; there is no partial activation.

Receipt retention and content-addressed artifact collection run as bounded background maintenance. Health aggregates are computed incrementally from receipts so dashboards do not scan millions of rows. The assignment-routing cache prevents unchanged scheduler cycles from producing duplicate runs.

## Migration and cutover

Migration avoids a permanent dual-runtime architecture:

0. **Security baseline:** restore authoritative server-side metadata merge on the live compiler/install path; introduce typed capability namespaces, authenticated principals, dispatch-time authorization, and recursive no-widening tests.
1. **Contracts and schemas:** add event payload schemas and execution contracts for the approximately 10–15 commands used by active playbooks. Add adapter conformance and preview tests.
2. **Early operator value:** use the contract registry and explanation service to render read-only rich inspectors for deterministic V1 pipeline nodes. This exercises intent cards before V2 cutover without treating the projection as executable truth or changing V1 execution.
3. **Durable substrate:** add the strict generated V2 schema, content-addressed artifacts, activation records, run snapshots, receipts, waits, pending events, and unified lifecycle.
4. **Unified engine:** add type-specific executors, symbolic dry-run, rule-per-run identity, and the single engine boundary while V1 remains the production path.
5. **Bundled migration:** rewrite bundled Markdown as normative prose without JSON graph blocks, compile it through the agent path, human-review structural and capability diffs, and verify approved fixtures.
6. **User migration:** compile each project playbook and require a human to review its source-to-intent and structural diff. A project playbook that cannot compile is explicitly disabled with an operator-visible reason; it does not block unrelated playbooks or system cutover after acknowledgement.
7. **Drain:** stop admitting new V1 runs, while the V1 runtime continues only for already-running or paused runs pinned to V1 graphs. V1 runs are never converted in place. Operators receive a list of remaining waits and choose to resolve or cancel them.
8. **Cutover:** once no V1 run remains and every enabled playbook is ready V2, atomically switch all orchestrator entry points. Acknowledged-disabled project playbooks are not considered active.
9. **Removal:** disable V1 execution, remove new V1 artifact production, and retain V1 artifacts and traces for read-only inspection.

The per-tool-turn amendment is a daemon-only additive migration after the Package 3 receipt revision. It adds `receipt_kind TEXT NOT NULL DEFAULT 'step'` (`step | tool_turn | llm_call | interrupted | operator_decision`, later widened by the attempt-start amendment to admit `attempt_start` and the `started` outcome), `turn_index INTEGER NOT NULL DEFAULT -1`, and nullable `operator_decision_id`; replaces the one-receipt-per-attempt unique constraint with uniqueness over `(run_id, step_id, iteration, attempt, turn_index, receipt_kind)`; and adds a run/step/attempt/turn ordering index. Existing rows require no rewrite beyond the database defaults and remain the sole `step/-1` receipt for their attempts. Downgrade refuses to proceed while post-amendment multi-boundary rows exist, then restores the original uniqueness constraint; V1 tables are never changed. Only the daemon/operator migration path may apply this revision.

Cutover occurs only when every enabled Markdown playbook has a ready V2 artifact, no V1 run remains, and no enabled activation is stale. The migration window is the only temporary dual-runtime period.

## Validation and testing

### Model and schema invariants

- Runtime types and the served JSON Schema share one source; CI regeneration keeps the documentation snapshot exact.
- Every supported step type validates against the published schema.
- Every supported field survives JSON round-trip.
- Unknown fields are rejected.
- Fixtures cover commands, LLM calls, agent tasks, decisions, waits, list/object/coalesce expressions, loops, and terminals.
- Every event reference and trigger filter validates against a registered event payload schema.
- Every shipped playbook validates against the same schema used at activation.

### Compiler invariants

- Compiler-agent output is valid as a semantic body.
- Server-owned metadata and capability snapshots are reconstructed before full-artifact validation.
- Compiler-supplied authoritative fields are discarded, and executable identifiers absent from source are rejected.
- All shipped Markdown sources compile into V2.
- Source references survive compilation.
- Ambiguous prose returns source-linked questions rather than defaults.
- Invalid references, missing targets, not-definitely-assigned results, namespace shadowing, undeclared outputs, and hidden natural-language AI transitions fail compilation.
- Bundled Markdown contains no embedded JSON graph block.

### Command and explanation invariants

- A V2 command cannot register without an execution contract and typed invocation adapter.
- Adapter results conform to declared outcomes and closed output schemas.
- Execution-contract changes alter the fingerprint; presentation-only changes do not.
- Every executable field has a canonical explanation projection, enforced by an exhaustive CI test.
- Sensitive and unmarked fields are redacted from receipts by default.
- Golden intent fixtures cover current shipped commands, including `ensure_task`.
- Changed contracts stale affected playbooks while unrelated playbooks remain ready.
- A missing expected plugin marks dependents unavailable; an intentional removal marks them needs-rebuild.

### Capability invariants

- Direct LLM schemas contain only the resolved policy, and dispatch rejects an off-list call independently.
- Empty capability namespaces mean no capabilities.
- Every CLI and MCP command request carries a server-verifiable execution principal.
- A child task cannot select a profile broader than its caller in any capability namespace.
- Missing principal identity or profile resolution fails closed.
- Changing a profile's capabilities changes its fingerprint and requires rebuild.

### Engine invariants

- Event, manual, dry-run, assignment, and resume paths call the same engine.
- Each matched rule has an isolated run and shares only a dispatch ID with sibling matches.
- Live and dry-run select the same rules, nodes, and edges for identical resolved outcomes.
- Dry-run respects path/step bounds and never reports completed beyond an unresolved boundary.
- Command, LLM, and agent results conform to declared schemas and reserved outcomes.
- Wait states, pre-wait bindings, LLM turns, and mid-loop state serialize and resume without data loss.
- Event arrival before, during, and after wait registration produces one deterministic resume.
- Cancellation has tested transitions from running, paused, and in-flight agent execution.
- Execution receipts identify every traversed node, selected edge, loop iteration, and artifact hash.
- A completed LLM tool turn advances the snapshot and writes exactly one `tool_turn` receipt before the next provider call begins.
- An interrupted LLM call writes one `interrupted` receipt, pauses with an operator-decision reference, and performs no automatic provider or tool replay.
- An explicit retry reconstructs the conversation only from completed transcript deltas and continues after the last committed turn.
- An incompatible in-progress run fails before invoking the changed command.
- A transiently unavailable dependency pauses or queues without demanding rebuild.

### Storage invariants

- Activation never points to a partially written artifact.
- A historical run renders only against its pinned retained artifact.
- Snapshot and receipt commits are atomic at step boundaries.
- The attempt-start fence before a command, LLM or agent-task attempt's first external side effect is itself a receipted boundary; no snapshot version advances without a receipt.
- Snapshot and receipt commits are also atomic at each LLM tool-turn boundary; all boundaries retain pinned run, rule, and artifact identity and the existing wait-ownership checks.
- Existing single-receipt attempts read as `receipt_kind="step", turn_index=-1` after migration.
- Retention never collects an active, pinned, or otherwise referenced artifact.
- Pending stale events replay in arrival order under rule-level deduplication or expire with an audit record.

### UI invariants

- Event filtering preserves every reachable branch.
- Rich cards cover every step type.
- Rendered edge identities, count, ports, and labels match typed transitions exactly.
- Rule clusters contain no cross-cluster transitions or shared terminal nodes.
- Node cards and the inspector consume the same explanation payload.
- Advanced mode exposes canonical data without making it the default explanation.
- A run overlay matches recorded receipts and artifact hash.

### Cutover acceptance

- No executable V1 artifacts remain.
- No running or paused V1 run remains.
- Every enabled Markdown playbook has a ready V2 artifact; every disabled migration failure is acknowledged.
- No enabled playbook has stale command or profile fingerprints.
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

- English Markdown without embedded graph JSON remains the authoring source of truth.
- Strict V2 JSON is the executable source of truth.
- Rules, triggers, guards, nodes, values, and edges are typed.
- Commands, inline LLM requests, and orchestrated agent tasks remain valid playbook states.
- AI behavior is visible, profile-bound, dispatch-authorized, and incapable of widening delegated capabilities.
- One current command API is supported; rare command or profile contract changes require a manual, human-reviewed rebuild.
- One engine serves all orchestrator entry points and dry-run.
- The graph keeps complete topology, scopes by input event, and explains behavior inside rich nodes.
- Intent rendering is exhaustive and derived from the same validated artifact and contract objects that execute; copy is not an activation dependency.
- Mutable run snapshots provide continuation state; immutable receipts provide inspection and overlays.
- Artifacts are content-addressed, activation is atomic, and historical overlays use the exact pinned graph.
- Events held during staleness or transient unavailability are queued with explicit retention and audit behavior.
- V1 remains historical and read-only after migration.
