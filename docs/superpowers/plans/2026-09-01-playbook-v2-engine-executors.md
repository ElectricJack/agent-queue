# Playbook V2 — Package 4: unified engine and typed executors

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`. §5 is a task list with red/green boundaries; §3 is a **frozen interface contract** that the three specialist executor tasks (T-2 command, T-13/T-14 LLM, T-8 agent task) share. Do not renegotiate §3 inside a task — amend it in a dedicated commit that updates every dependent task in the same change.

**Package:** 4 of 7 — *Unified engine and typed executors*
**Roadmap:** `docs/superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md` §5 "Package 4"
**Spec:** `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md` — §"Typed step family", §"Run-state persistence", §"Unified execution engine", §"Engine invariants"
**Consumes:** Packages 0, 1, 2, 3
**Produces:** one `PlaybookEngine`, seven executors, durable lifecycle semantics, bounded dry-run, side-effect-free shadow mode, and a provider-reported token-usage channel.

**Drafting note.** This plan was written *ahead of* Packages 1–3 landing, against `origin/main` at `a1f8b9ae`, under an explicit supervisor instruction to parallelise plan authoring. Every symbol this package consumes is therefore classified in §2.3 as **observed** (live in the tree today, with a line number) or **expected** (owed by an earlier package, with the module the roadmap assigns it to). The implementation task's *first* commit is the reconciliation commit described in §2.4: it re-runs the reconciliation script, amends this document where the tree disagrees, and only then starts T-1.

---

## 1. Scope

### 1.1 What this package does

Replaces three divergent execution paths with one engine that walks the strict V2 artifact:

| Concern | Today | After Package 4 |
|---|---|---|
| Deterministic playbooks | `src/playbooks/pipeline_runner.py::PipelineRunner` | `PlaybookEngine` + `CommandExecutor`/`DecisionExecutor`/`ForEachExecutor`/`TerminalExecutor` |
| LLM playbooks | `src/playbooks/runner.py::PlaybookRunner` (93 KB, LLM chooses the edge) | `LlmExecutor` — the LLM produces a *declared structured output*; the **artifact** chooses the edge |
| Assignment routing | `src/orchestrator/assignment_routing.py:459` constructs its own `PlaybookRunner` | `PlaybookEngine.run_rule(...)`; the coordinator keeps its caches |
| Dry-run | `PlaybookRunner.dry_run` (`runner.py:2270`) — the real runner with a `FakeProvider`, following "the first natural-language transition" | `ExecutionMode.dry_run` — same graph, same validator, same transitions; only the executors change |
| Cancellation | `_cmd_cancel_playbook_run` (`playbook_commands.py:508`) writes a DB row a live run then overwrites | `cancelling` → acknowledged → `cancelled`, receipted |

### 1.2 What this package explicitly does **not** do

- **It does not cut over.** V1 entry points stay authoritative. Every V2 path in §5 sits behind `playbooks.v2_engine` (§9), default `false`. Package 7 owns the switch and the deletion of `runner.py`, `pipeline_runner.py`, `runner_context.py`, `runner_events.py`, `runner_transitions.py`, `token_tracker.py`.
- **It does not define the artifact model** (Package 2) **or the storage** (Package 3). Where it needs a field those packages did not ship, §10 gives the exact conditional Alembic revision rather than a local reimplementation.
- **It does not add API endpoints or dashboard code.** Package 5 projects receipts and overlays. Package 4 only guarantees the receipt *contents* Package 5 reads (§3.3).
- **It does not rebuild playbook sources.** Package 6 owns the Markdown rewrite. Package 4's fixtures are hand-written artifacts (§6), not compiler output.
- **It does not contract new commands.** `CommandStep` may reference only a command Package 1 registered.

### 1.3 Exit gate this plan must satisfy

> Every V2 step kind runs through one engine with durable boundaries. Live, dry-run, and shadow modes traverse the same graph, and shadow mode can compare decisions without producing side effects.

§13 maps each clause to the test that proves it.

---

## 2. Live-tree reconciliation

### 2.1 Deviations from the roadmap's Package 4 file list

The roadmap's file list was written from the module map, not from the tree. These are the differences, each load-bearing.

| Roadmap says | Live tree | Consequence for this plan |
|---|---|---|
| Create `tests/playbooks/test_v2_engine.py` (and five siblings) | **`tests/playbooks/` does not exist.** Every suite is flat: `tests/test_playbook_runner.py`, `tests/test_pipeline_runner.py`, … | All new suites are flat `tests/test_*.py`. Names are fixed in §5. Packages 0 and 5 recorded the same deviation; this package does not create the directory either, so the layout stays uniform. |
| Modify `src/playbooks/handler.py` | `handler.py` is the **vault-change** handler (`on_playbook_changed`, `register_playbook_handlers`) — it compiles and installs Markdown. It contains no execution. | **Removed from the Modify list.** Nothing in Package 4 touches it. Package 2/3 own install-time artifact production. |
| Modify `src/playbooks/run_task.py` | `sync_playbook_run_task` (`run_task.py:33`) + `playbook_status_to_task_status` (`:25`) — maps a run status onto its projection task. | **Kept.** §4.9: the V2 lifecycle adds `cancelling`, which needs a mapping. |
| Modify `src/playbooks/services.py` | `PlaybookServices` (`services.py:20`) is `llm` + `node_tools()`; `for_tests(llm)` at `:48`. | **Kept**, but §3.8: the `LlmExecutor` takes an `LLMClient` directly and does **not** grow a second services object. `services.py` gains only the usage-aware call path. |
| §4.5 step 1 needs `step.capability_narrowing` | **Package 2 shipped `AgentTaskStep` without it** (`src/playbooks/definition.py:235`): the model carries `profile_id`, `objective`, `inputs`, `wait_for_completion`, `cancel_child`, `timeout_seconds`, `retry`, `save_result_as` and `transitions`, and nothing else. | **T-8 added it** as an additive optional `CapabilityNarrowing` (three `list \| None` namespaces, `None` = narrow nothing, `[]` = none) plus a regenerated `src/playbook_v2_schema.json`. Roadmap §2's "delegated agent-task permissions are the intersection of parent permissions, child profile permissions, and explicit per-step narrowing" is not implementable without the third term. Package 6's compiler owes the authoring surface for it. |
| §4.5 step 6 / §7.4 assume a contracted way to cancel a child | **There is no contracted task-cancellation command.** `src/commands/contracts/builtin.py` registers `create_task`, `ensure_task`, `edit_task`, `add_dependency`, `gate_create`, `gate_resolve`, `list_tasks`, `get_downstream_tasks`, `task_batch_commit`, `task_route` — `stop_task` (`task_commands.py:2546`) is uncontracted. | `cancel_child` dispatches `CHILD_CANCEL_COMMAND` (`stop_task`) **through the contract registry** when it is contracted and otherwise logs and leaves the child running. Reaching `CommandHandler` directly would skip the dispatch-boundary authorization that makes the narrowed principal mean anything, and a surviving child is the fail-safe direction — which is what `cancel_child=False`, the default, does anyway. |
| — | **`src/workflow_stage_resume_handler.py:195`** `WorkflowStageResumeHandler._resume_run` → `PlaybookRunner.resume_from_event` (`:290`) | **Added to the Modify list.** It is a live resume site the roadmap omits. Package 7 §1.4 already records it as site 5; if Package 4 leaves it un-abstracted, Package 7's switch strands workflow-stage runs. |
| — | `src/orchestrator/core.py:1782` constructs the **`TimerService`** (`src/timer_service.py:185`) | §4.6: the spec forbids synthesising TimerService triggers for per-run waits. The wait scheduler is a new owner; §4.6 states the boundary. |
| Modify "AI-provider and task lifecycle seams" | `src/llm/types.py::ChatResponse` carries **content blocks only**; `src/llm/providers/anthropic.py:147` builds `ChatResponse(content=content)` and discards `resp.usage`; `openai.py` and `google.py` do the same; `grep -n usage src/llm/**` returns nothing. `src/playbooks/token_tracker.py:36::_estimate_tokens` is `chars // 4`. | This is the **largest single deviation in the package** and is scoped as its own commit (§4.11, T-13/T-14). The spec is unambiguous: "Token accounting uses provider-reported input and output usage. A provider adapter that cannot report usage cannot run a step with a hard total-token budget." Package 4 adds the usage channel **and** the fail-closed rule. |

### 2.2 Behaviours in the live tree that the V2 engine must *not* inherit

Each is a real line, and each has a test in §5 asserting the V2 engine does the opposite.

1. **Success inferred from dict shape.** `pipeline_runner.py:145`:
   ```python
   success = not (result.get("success") is False or "error" in result)
   ```
   A command that legitimately returns `{"error": None}` is read as a failure; a command that returns `{}` is read as a success. Spec: "No executor infers success from the presence or absence of a `success` key." → §3.2, T-2.
2. **Loop variables in the shared binding namespace.** `pipeline_runner.py:172-186` writes `self.outputs[var] = item` into the *same* dict as step outputs and `pop`s it in a `finally`. Scoping is a convention, and a step output named `task` and a loop item named `task` silently collide. → §3.4/§4.7, T-7.
3. **Dry-run is a different traversal.** `runner.py:2270-2332`: dry-run "follows the first natural-language transition without an LLM call" and executes `wait_for_human` nodes as if they returned. It answers a different question from live execution. → §4.10, T-11.
4. **Cancellation is advisory.** `playbook_commands.py:511-519`, in its own docstring: "A live run that gets cancelled will finish its current node and then, on its next persistence write, silently overwrite the `cancelled` status back to `running`." → §4.9, T-9.
5. **One run row covers every matching rule.** `core.py:944-957` builds one `PipelineRunner` per rule and then forces `pipeline_runner.run_id = primary_runner.run_id`, so a five-rule dispatch produces one row whose failure semantics are "failure of any rule fails the whole run". Spec: "One matching event may create multiple rule runs, but each run executes exactly one rule." → §4.2, T-1.
6. **`playbook_runs.status` has no `cancelling`.** `src/database/tables.py:955` `ck_playbook_runs_status` admits `running, paused, completed, failed, timed_out, cancelled`. The V2 lifecycle needs `cancelling`. → §10.

### 2.3 Symbols this package imports

**Observed — live in `origin/main` at `a1f8b9ae`.** Line numbers are real.

| Symbol | Live location | Package 4 uses it for |
|---|---|---|
| `CommandHandler.execute(name, args)` | `src/commands/handler.py` | The only operational dispatch path; the `CommandExecutor` reaches it *through* Package 1's adapter, never directly |
| `PlaybookServices`, `.llm`, `.node_tools()`, `.for_tests()` | `src/playbooks/services.py:20`, `:27`, `:48` | `LlmExecutor` construction in tests |
| `LLMClient.complete` / `.run_tools` / `.with_provider` | `src/llm/client.py:141`, `:154`, `:91` | `LlmExecutor`; `with_provider(FakeProvider())` in every LLM test |
| `LLMCallSpec`, `resolve_call` | `src/llm/spec.py:16`, `:44` | Profile → model resolution for `LlmStep` |
| `ChatResponse`, `TextBlock`, `ToolUseBlock` | `src/llm/types.py` | The type §4.11 extends with usage |
| `FakeProvider` | `src/llm/fake.py:22` | Deterministic LLM fixtures |
| `EventBus.subscribe` / emit | `src/orchestrator/core.py` wiring, `src/playbooks/resume_handler.py:105` | Wait matching and lifecycle events |
| `PlaybookRunStatus`, `playbook_run_transition`, `TERMINAL_STATUSES` | `src/playbooks/state_machine.py:102`, `:126` | §4.9 extends the machine with `cancelling` |
| `sync_playbook_run_task`, `playbook_status_to_task_status` | `src/playbooks/run_task.py:33`, `:25` | Run→task projection, `sync_task_projection=False` for routing |
| `AssignmentRoutingCoordinator._route_batch`, `_batch_key`, `_catalog_hash`, `_attempt_event_id`, `validate_assignment_response` | `src/orchestrator/assignment_routing.py:434`, `:389`, `:186`, `:400`, `:78` | §4.12 — all four stay caller-owned |
| `PlaybookResumeHandler._resume_run` | `src/playbooks/resume_handler.py:179` | Resume site 4 |
| `WorkflowStageResumeHandler._resume_run` | `src/workflow_stage_resume_handler.py:195` | Resume site 5 (roadmap omission, §2.1) |
| `Orchestrator._on_playbook_trigger` | `src/orchestrator/core.py:800` | Dispatch sites 1–2 |
| `_cmd_run_playbook` / `_run_pipeline_playbook` / `_cmd_dry_run_playbook` / `_cmd_cancel_playbook_run` / `check_paused_playbook_timeouts` | `src/commands/playbook_commands.py:857`, `:977`, `:1032`, `:508`, `:1765` | Dispatch site 6 |
| `PlaybooksConfig` | `src/config.py` (`enabled: bool = False`, `validate()` returns `[]`) | §9 adds four fields |
| `PipelineEngine` test helper | `tests/conftest.py:322` | The V1 arm of §5's differential tests; Package 6 reuses it for parity |

**Expected — owed by Packages 1–3.** If a name differs when implementation starts, amend this table *and* every §3/§5 reference in the reconciliation commit (§2.4). Do not ship a local reimplementation.

| Symbol | Owner | Module the roadmap assigns | Package 4 uses it for |
|---|---:|---|---|
| `ExecutionPrincipal`, `PrincipalKind`, `.narrow(policy, *, reason)`, `.enforced`, `principal_context`, `current_principal`, `TRUSTED_LOCAL`, `service(name)` | 0 | `src/commands/principal.py` | The principal every executor carries; `narrow` is the *only* delegation transform (§7.2) |
| `CapabilityPolicy`, `DENY_ALL`, `.intersect`, `.is_subset_of`, `.fingerprint()` | 0 | `src/profiles/capabilities.py` | Agent-task narrowing; LLM tool-policy projection |
| `authorize_command`, `AuthzDecision`, `denial_result` | 0 | `src/commands/authorization.py` | Dispatch-boundary authorization is *already* enforced there; the engine never re-implements it |
| `CommandResult[R]` (`outcome`, `value`, `summary`, `classification(contract)`), `RESERVED_OUTCOMES`, `UnknownOutcome` | 1 | `src/commands/contracts/models.py` | §3.2 |
| `CommandContract`, `ExecutionContract` (`outcomes`, `capability`, `side_effect`, `idempotency`, `retry_safe`, `timeout_seconds`, `sensitive_args`, `sensitive_result_fields`, `receipt_projection`, `supports_preview`), `.fingerprint()` | 1 | `src/commands/contracts/models.py` | Runtime re-validation, receipt redaction, idempotency keys, preview policy |
| `CommandRegistration` (`invoke`, `preview`), `ContractRegistry.require/get/fingerprint`, `CONTRACTS` | 1 | `src/commands/contracts/registry.py` | The *only* way a `CommandStep` reaches a handler |
| `PlaybookDefinition`, `Rule`, `CommandStep`, `LlmStep`, `AgentTaskStep`, `DecisionStep`, `WaitStep`, `ForEachStep`, `TerminalStep` | 2 | `src/playbooks/definition.py` | The discriminated union every executor dispatches on |
| Typed value union + `resolve_value(value, scope)`; condition evaluator | 2 | `src/playbooks/expressions.py` | §3.4 input resolution and `DecisionStep` |
| `ArtifactRef`, `ArtifactStore.load(sha) -> PlaybookDefinition` | 3 | `src/playbooks/artifact_store.py` | Pinning; compatibility check at run start and every resume |
| `ActivationHealth`, activation repository | 3 | `src/playbooks/activation.py`, `src/database/queries/playbook_artifact_queries.py` | `dispatch_event` resolves *enabled + ready* activations only |
| `RunSnapshot` (bindings, loop frame, wait, cancellation, `version`), `RunRepository.commit_boundary(snapshot, receipt, pending_wait_changes)` | 3 | `src/playbooks/run_state.py` | §3.3 |
| `StepReceipt` | 3 | `src/playbooks/receipts.py` | §3.3 |
| `WaitSpec`, `WaitRepository.register(wait_spec, snapshot_version)`, match/claim | 3 | `src/playbooks/waits.py` | §4.6 |
| `playbook_pending_events` table + queries | 3 | `src/database/queries/playbook_run_queries.py` | §4.13 dependency-unavailable queueing |

### 2.4 The reconciliation commit — run this **before T-1**

```bash
python - <<'PY'
import importlib, inspect, sys
WANT = {
  "src.commands.principal": ["ExecutionPrincipal", "PrincipalKind", "principal_context", "current_principal"],
  "src.profiles.capabilities": ["CapabilityPolicy", "DENY_ALL"],
  "src.commands.contracts.models": ["CommandResult", "CommandContract", "ExecutionContract", "RESERVED_OUTCOMES"],
  "src.commands.contracts.registry": ["CommandRegistration", "ContractRegistry", "CONTRACTS"],
  "src.playbooks.definition": ["PlaybookDefinition", "CommandStep", "LlmStep", "AgentTaskStep",
                               "DecisionStep", "WaitStep", "ForEachStep", "TerminalStep"],
  "src.playbooks.expressions": ["resolve_value"],
  "src.playbooks.artifact_store": ["ArtifactRef", "ArtifactStore"],
  "src.playbooks.activation": ["ActivationHealth"],
  "src.playbooks.run_state": ["RunSnapshot", "RunRepository"],
  "src.playbooks.receipts": ["StepReceipt"],
  "src.playbooks.waits": ["WaitSpec", "WaitRepository"],
}
missing = []
for mod, names in WANT.items():
    try:
        m = importlib.import_module(mod)
    except Exception as exc:
        missing.append(f"MODULE {mod}: {exc}"); continue
    for n in names:
        if not hasattr(m, n):
            missing.append(f"SYMBOL {mod}.{n}")
print("\n".join(missing) or "all expected symbols present")
sys.exit(1 if missing else 0)
PY

# The six V1 sites Package 7 will switch — confirm none moved.
grep -n "PipelineRunner\|PlaybookRunner" src/orchestrator/core.py src/orchestrator/assignment_routing.py \
     src/playbooks/resume_handler.py src/workflow_stage_resume_handler.py src/commands/playbook_commands.py

# The usage deviation — expect zero hits before T-13, non-zero after.
grep -rn "usage" src/llm/ || echo "no provider usage channel (expected before T-13)"
```

Amend §2.3 and every affected §3/§5 reference in the same commit, message `docs: reconcile package 4 plan against the live tree`. If a Package 0–3 symbol is *absent*, the correct move is to stop and escalate to the roadmap owner, not to define it here: Package 4 owning a Package 3 type is exactly the "two permanent APIs" outcome the roadmap forbids.

### 2.5 Amendments applied — the C0 reconciliation, 2026-09-02

§2.4's script and the greps were re-run against `origin/main` `a0e4e552`. Packages 0, 1, 2, 3 and 5 have all merged, so every "expected" row of §2.3 is now observed. The symbol check reports **one** miss and the shipped shapes differ from §3 in nine further places. Each is applied to this document below and implemented as written; nothing here is a local reimplementation of another package's type.

1. **`resolve_value` and the condition evaluator do not exist.** Package 2 shipped `expressions.py` as the typed value/condition *models* plus `validation.py`'s static passes; there is no runtime resolver anywhere in `src/playbooks/`. Roadmap §3 assigns "typed values, templates, references, comparisons, and condition trees" to `src/playbooks/expressions.py`, so Package 4 adds `resolve_value(value, scope)` and `evaluate_condition(condition, scope)` **there**, not in the engine — an engine-local copy is exactly the "two permanent APIs" outcome §2.4 forbids. Package 2's §20 item 8 import discipline is preserved: the resolver needs only `typing` and the module's own models, so `test_expressions_module_has_no_intra_package_imports` still passes. A reference that cannot be resolved raises `ValueResolutionError`, which the engine maps to `input_resolution_failed` (§3.4 step 4) and `DecisionExecutor` maps likewise (§4.14).

2. **`StepReceipt` is Package 3's shape, and its `outcome` is a different vocabulary from §3.6's.** §3.3.3's table was written against field names Package 3 did not ship. The live record (`src/playbooks/receipts.py`) is `receipt_id, run_id, artifact_sha256, rule_id, step_id, step_kind, outcome, started_at, snapshot_version, iteration, attempt, idempotency_key, contract_fingerprint, principal, inputs, result, selected_transition, error, error_code, tokens_in, tokens_out, cost_usd, wait_id, timed_out, cancelled_at, completed_at, duration_ms`, and `outcome` is constrained by `RECEIPT_OUTCOMES` to `success | failure | skipped | timeout | cancelled | operator_decision_required` — a *classification*, not a step outcome. §3.3.3 is therefore re-expressed as a mapping onto those columns:

   | §3.3.3 wanted | Package 4 writes |
   |---|---|
   | `step_type` | `step_kind` |
   | `mode` | not a receipt column; dry-run and shadow receipts are in-memory only (§3.3.5), so the mode is implied by which recorder holds them |
   | `principal_kind`, `profile_id`, `capability_fingerprint` | the `principal` mapping, as `{"kind", "profile_id", "capability_fingerprint", "describe"}` |
   | `outcome` (a §3.6 name) | `error_code` for a reserved outcome, and the outcome name is the tail of `selected_transition` (`transition_id(rule, step, outcome)`); `outcome` itself carries the `RECEIPT_OUTCOMES` classification |
   | `selected_transition` as `(label, target)` | `transition_id(rule_id, step_id, outcome)`, which is what Package 5's overlay joins on |
   | `usage` | `tokens_in` / `tokens_out` / `cost_usd` |
   | `deadline_fired` | `timed_out` plus the `error` string naming which deadline |
   | `cancellation` | `cancelled_at` plus `error` |
   | `operation`, `child_task_id`, `diagnostics` | no columns exist; `operation` and the diagnostics go in `error` for a failure and are otherwise dropped, and `child_task_id` is carried on the snapshot's `agent_task_ids` (Package 3's field) rather than the receipt |

   `iteration` defaults to `-1` outside a loop, not `None`, and `idempotency_key` is Package 3's four-part `run:step:iteration:attempt` (its §9.1 amendment 2), not §3.3.2's three-part key. §3.3.2's three-part form would collide across loop iterations, so the four-part key is authoritative and §3.3.2 is amended to it.

3. **`project_for_receipt` is not added.** Package 3 already ships default-deny projection as `receipts.project_receipt(inputs, result, *, receipt_projection, sensitive_args, sensitive_result_fields, input_projection, run_id)`, whose no-classification default is total redaction. `base.py` exposes `project_step_receipt(...)`, a thin adapter that reads the projection off `ExecutionContract` and delegates; it holds no redaction logic of its own. §3.3.4's contract is unchanged — an unmarked field is still dropped rather than masked, and a sensitive field becomes Package 3's opaque `sensitive:<32hex>` handle rather than §3.3.4's `{"__redacted__": ...}` literal.

4. **Steps have no `declared_targets()`.** `src/playbooks/definition.py:487` `step_targets(step) -> {field pointer: target step id}` is the live equivalent, and §3.1.3's `allowed` set is `frozenset(step_targets(step).values())`. It is empty for `CommandStep`… `TerminalStep` only in the sense that a terminal declares no transitions; a command *does* have transition targets, so §3.1.3's "a `GOTO` from a command is a `contract_violation` by construction" is enforced by an explicit `GOTO_CAPABLE_STEPS = {DecisionStep, ForEachStep}` check rather than by an empty target set. T-5's `test_command_executor_cannot_goto` asserts that check.

5. **The golden fixture's second rule is `sweep-on-spec-approved` on `spec.approved`**, per Package 2's amendment 7 (`spec.created` was never a registered event). Every §5 and §6 reference to `sweep-on-spec-approved` / `spec-created.json` reads `sweep-on-spec-approved` / `spec-approved.json`.

6. **`TerminalOutcome` is `completed | failed | cancelled`.** There is no `timed_out` terminal, so T-5's `test_terminal_outcome_maps_onto_the_run_lifecycle` is parameterised three ways. `timed_out` remains a *run* lifecycle, reached from §3.4 step 3, never from a terminal step.

7. **`commit_boundary` takes a `WaitChangeSet`**, not §3.3.1's `Sequence[WaitChange]`, and its default is `EMPTY_WAIT_CHANGES`.

8. **Bindings are keyed by `save_result_as`, and `bind_step_output`'s `step_id=` parameter is that binding name.** `validation.py:1090` builds its producer map from `save_result_as`, and `BindingRef.binding` reads it. §3.1.1's `BindingScope.bindings` and `RunSnapshot.bindings` are therefore the same namespace, keyed the same way; the engine calls `bind_step_output(snapshot, step_id=step.save_result_as, ...)`. The parameter name is Package 3's and is not renamed here.

9. **Event/rule idempotency rides the shipped `(dispatch_id, rule_id)` unique index.** §14.1 item 3 anticipated a three-column `(playbook_id, rule_id, event_id)` index; Package 3 shipped `uq_playbook_v2_runs_dispatch_rule` on `(dispatch_id, rule_id)` instead. Rather than add a revision, `dispatch_id` is made **deterministic in the event id** — `sha256("v2-dispatch|" + event_id)[:12]` when the event carries one, `uuid4().hex[:12]` when it does not. Sibling rule runs still share it (§3.5), and a replay of the same event now collides on the database's own partial unique index, which is a stronger guarantee than the pre-read §4.2 step 4 describes. §10 therefore needs no conditional revision and §14.1 item 3 is closed. An event without an `event_id` is not deduplicated, and `DispatchResult` reports `deduplicated: tuple[str, ...]` naming the rules whose runs already existed.

10. **The snapshot spells cancellation as `cancel_requested_at: float | None`**, not §10.1's `cancel_requested: bool` + `cancel_ack_state`. §3.4 step 2 and §4.9 read `snapshot.cancel_requested_at is not None`; the acknowledgement state is the lifecycle itself (`cancelling` vs `cancelled`) plus the receipt's `cancelled_at`. `RunLifecycle`, `TERMINAL_LIFECYCLES`, `LEGAL_TRANSITIONS` and `validate_transition` already ship in `run_state.py` with `cancelling` and the `paused -> cancelled` edge, so §4.9's "state_machine.py gains `cancelling`" is already satisfied and `src/playbooks/state_machine.py` (V1's) is **not** touched by Package 4.

11. **`authorize_command` needs a `resolver` and a `mode`.** §3.4 step 5's two-argument call does not exist; the live signature is `authorize_command(name, principal, *, resolver, mode)` and it returns an `AuthzDecision`. `EngineServices` therefore carries the `CommandResolver` and the enforcement mode alongside the handler, and the engine passes both through. It still implements no capability check of its own (§7.1).

### 2.6 Amendments applied — the C2 wait and loop reconciliation, 2026-09-02

Six further disagreements between §4.6/§4.7 and what Packages 2 and 3 shipped. Each is applied below and implemented as written; none is a local reimplementation of another package's type.

12. **The wait and loop suites live in `tests/test_v2_engine.py`.** §5.2 names `tests/test_v2_waits.py` and `tests/test_v2_foreach.py`; the roadmap's Package 4 Files list — which the tasks carrying this work name as the scope authority — creates only `test_v2_engine.py`, `test_command_executor.py`, `test_llm_executor.py`, `test_agent_task_executor.py`, `test_v2_dry_run.py` and `test_v2_restart_resume.py`. The roadmap wins, so §5.2's test *names* are kept verbatim in `test_v2_engine.py` (classes `TestDurableWaits`, `TestWaitScheduler`, `TestSequentialLoops`), and `test_v2_engine_repository.py` gains the same assertions against the real repository, because the double cannot prove that a wait row and its snapshot commit in one transaction.

13. **The loop aggregate is Package 2's `FOREACH_RESULT_SCHEMA`, not §4.7's `{items, outcomes, errors}`.** `definition.py` ships `{total, succeeded, failed, items: [{index, outcome, value, error}]}` and the binding type-checker walks it, so an artifact binding a loop result type-checks against *that*. The per-item `value` is left `None` — a body's output is already durable in `snapshot.bindings`, and copying it into the aggregate would duplicate the payload inside the 256 KiB result cap. One consequence follows and is recorded rather than hidden: because no per-item value is copied, the aggregate is bounded by `max_iterations`, so §4.7's "a `collect` over a large collection can legitimately end in `state_limit_exceeded`" is reached through a *body step's* binding instead. `max_iterations` itself is enforced at loop entry as `state_limit_exceeded`, before any body runs.

14. **`test_loop_item_cannot_shadow_a_binding` is dropped; the structure is stronger than the assertion.** §4.7 asks that `with_loop_item` *raise* on a name collision, but Package 2 shipped `ResolutionScope` as four separate namespaces in which a `LoopRef` cannot reach a binding and a `BindingRef` cannot reach a loop item, whatever they are called — and §5.2's own `test_loop_item_lives_in_its_own_namespace` asserts both are readable and distinct, which the raising version would contradict. The shipped structure makes shadowing unrepresentable rather than merely refused, so the namespace test is the one that ships.

15. **The loop frame carries the returning edge, and `RunSnapshot` carries a per-step attempt counter.** §4.7's locked rule needs `(producing_step_id, producing_outcome)` to survive a crash *between* the two frame boundaries, and a wait writes two receipts for one step instance — the suspension and the resume — which collide on `uq_playbook_step_receipts_attempt` at `attempt=1`. Package 3's value types therefore gain, additively and defaulted: `LoopFrame.last_step_id | last_outcome | last_failed`, and `RunSnapshot.attempts: Mapping[str, int]` keyed `"<step_id>:<iteration>"`. Both round-trip through `to_body`/`from_body`; neither needs a migration, because both live inside the existing `snapshot` JSON column. The engine classifies the returning edge once, when it records it, so the loop executor stays a pure state transition over the frame and never looks up a contract.

16. **`ExecutorResult` and `StepContext` gain the loop channel.** An executor writes nothing durable, so the foreach executor hands its computed frame back: `ExecutorResult.loop_frame` and `ExecutorResult.clear_loop`, read by `StepContext.loop_frame`. `clear_loop` is a separate flag rather than `loop_frame=None` because "no change" and "the loop ended" are different instructions, and conflating them would leave a finished loop's item readable on the continuation path. This is a §3.1 change and is landed by T-7, which §3.7 already assigns `base.py`.

17. **A suspension receipt classifies as `skipped`.** `RECEIPT_OUTCOMES` (§2.5 item 2) has no `paused` member. A pause decides nothing — no edge is selected, no binding is written, and the resume boundary carries the real outcome — so calling it `success` would make every open wait look like a finished step. `WaitStep.wait_kind` is also the *author's* vocabulary (`event | human | task | timer`) while `WaitSpec.kind` is storage's (`event | timer | human | agent_task`); `wait.py::WAIT_KIND_STORAGE` is the single mapping between them, so a new kind cannot be added on one side only.


### 2.7 Amendments applied — the C2 cancellation reconciliation, 2026-09-02

Three disagreements between §4.9/§10.1 and what Packages 1 and 3 shipped. Each is applied below and implemented as written.

18. **T-9's tests live in `tests/test_v2_engine.py`, not `tests/test_v2_cancellation.py`.** The same reasoning as §2.6 item 12, and the same resolution: the roadmap's Package 4 file list is the scope authority and creates no such file, so §5.2's test *names* are kept verbatim in `test_v2_engine.py` (class `TestCancellation`), and `test_v2_engine_repository.py` gains the paused-run assertion against the real repository, because the double cannot prove that the wait row and the terminal snapshot commit in one transaction.

19. **The receipt's cancellation discriminator rides `result["cancellation"]`.** §4.9 asks for "the receipt's `cancellation` field"; `playbook_step_receipts` (Package 3, §6.4) has no such column and its `StepReceipt` field names are that table's column names exactly. Adding a column contradicts §10's "Package 4 owns no tables", so the discriminator goes into the receipt's existing JSON `result` projection under the key `cancellation`, whose two values are `acknowledged` and `grace_expired` (`engine.py`'s `CANCELLATION_KEY`, `CANCELLATION_ACKNOWLEDGED`, `CANCELLATION_GRACE_EXPIRED`). Together with `receipt.cancelled_at` — which §2.5 item 10 already made the acknowledgement's timestamp — the pair says both *when* the run stopped and *whether the executor gave it back*, which is what §4.9 wanted the column for.

20. **Cancelling a paused run goes through `commit_boundary`, not `request_cancel`.** §4.9's paused row wants "cancelled, **one receipt**, wait deregistered in the same boundary", and `request_cancel` deliberately writes no receipt (it is the *intent* write, shared with callers that have no artifact loaded). `PlaybookEngine._cancel_paused` therefore builds the cancellation attempt over the run's current step and commits it with `WaitChangeSet(clear_run_waits=True)`, which is one transaction carrying the terminal snapshot, its receipt and the wait's retirement. Two consequences are recorded rather than hidden: the `cancel_reason` / `cancel_requested_by` *columns* are only written by `request_cancel`, so on this path the reason rides `receipt.error` and the canceller rides `receipt.principal`; and the boundary is a second attempt on the wait step, which is exactly what §2.6 item 15's per-step attempt counter exists to number.

21. **The engine keeps an in-process registry of live walks.** §4.9's "running, executor in flight" row is not reachable from durable state alone: a walk that is inside `await executor.execute(...)` cannot observe a row that changed underneath it, and `request_cancel` advances the snapshot version, so the walk's next boundary would lose its CAS and report `interrupted` rather than `cancelled`. `PlaybookEngine._live: dict[str, _RunControl]` (registered by `_walk` for `LIVE` only, dropped in a `finally`) carries the post-cancel snapshot the walk must adopt, the in-flight `(executor, step, ctx)`, the one-shot `signalled` flag §4.9 requires, and a lock plus a `settled` event so that exactly one of the two possible writers — the walk on acknowledgement, `cancel` on grace expiry — writes the terminal boundary. Nothing on it is durable: a restart drops the registry and the run is still `cancelling` in the database, so the next boundary in whichever process picks the run up ends it through §3.4 step 2. The handle buys promptness within one process and nothing else. Grace expiry does **not** cancel the executor's task — the engine ends *runs*, and killing work it did not start would leave a half-finished side effect attributed to nobody.

### 2.8 Amendment applied — per-tool-turn durable boundaries, 2026-09-02

One further disagreement between C3 and Package 3's frozen receipt identity is applied below and implemented as written.

22. **Per-tool-turn durability amends Package 3's receipt cardinality, not attempt identity.** `LLMClient.run_tools(..., on_tool_turn=...)` awaits a `ToolTurnReceipt` callback after tool results are appended and before another provider call. The payload is `kind`, zero-based `turn_index`, `tool_call_ids`, `results_digest`, per-call `usage`, and the serializable transcript delta. `StepContext` carries the resume deltas, the engine-owned callback, and a live cancellation event checked before tool dispatch; executors still cannot import or invoke the engine directly. `StepReceipt` adds `receipt_kind`, `turn_index`, and `operator_decision_id`. Its database identity is `(run_id, step_id, iteration, attempt, turn_index, receipt_kind)`, while every receipt from the attempt retains the same four-part idempotency key. Existing rows are `step/-1/NULL`.

---

## 3. Locked interfaces — the parallelism contract

Roadmap §7: *"Package 4 executors may be developed independently after `Executor`, `CommandResult`, `RunRepository`, and receipt types are fixed."* This section **is** that fixing. **T-2** (command), **T-13/T-14** (LLM) and **T-8** (agent task) may run fully in parallel once §3 is checked in, because everything they share is here and nothing here is negotiable inside those tasks.

Changing anything in §3 after T-1 lands requires: a dedicated commit, an update to every dependent task in §5, and a re-run of `aq test tests/test_v2_engine.py tests/test_command_executor.py tests/test_llm_executor.py tests/test_agent_task_executor.py -q`.

### 3.1 `Executor` — `src/playbooks/executors/base.py`

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable


class ExecutionMode(StrEnum):
    LIVE = "live"
    DRY_RUN = "dry_run"
    SHADOW = "shadow"


class StepControl(StrEnum):
    #: Engine selects `step.transitions[result.outcome]`.
    ADVANCE = "advance"
    #: Engine jumps to `result.goto_step_id`, which MUST be one of the step's
    #: statically declared targets (§3.1.3). Only `decision` and `foreach` may use it.
    GOTO = "goto"
    #: Engine persists `result.wait` and pauses the run.
    SUSPEND = "suspend"
    #: Engine ends the run with `result.terminal_outcome`.
    TERMINATE = "terminate"
    #: Engine pauses with reason `operator_decision_required` (§4.8). No transition
    #: is selected and no binding is written.
    OPERATOR_DECISION = "operator_decision"
    #: Dry-run / shadow only: the boundary could not be resolved. The engine forks
    #: symbolically across every declared outgoing outcome (§4.10).
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-reported usage. See §4.11 — `reported=False` is a hard gate."""
    input_tokens: int = 0
    output_tokens: int = 0
    #: True only when the provider adapter returned real counts. An estimate
    #: sets `reported=False`, and a step with a hard `max_total_tokens`
    #: refuses to run against a provider that cannot report (§4.11).
    reported: bool = False

    @property
    def total(self) -> int: ...
    def __add__(self, other: "TokenUsage") -> "TokenUsage": ...   # reported = a.reported and b.reported


@dataclass(frozen=True, slots=True)
class ExecutorResult:
    control: StepControl
    #: ALWAYS set. Either a declared outcome for this step type, or a member of
    #: ENGINE_RESERVED_OUTCOMES (§3.6). Never empty, never None.
    outcome: str
    #: The step's declared output, already validated by the executor against
    #: `step.output_schema` / the contract's result model. `None` when the step
    #: declares no `save_result_as`.
    value: Any | None = None
    goto_step_id: str | None = None
    wait: "WaitSpec | None" = None            # src/playbooks/waits.py (Package 3)
    terminal_outcome: str | None = None
    usage: TokenUsage | None = None
    llm_calls: int = 0                    # aggregate calls represented by usage
    idempotency_key: str | None = None
    #: Receipt projections. ALREADY REDACTED by the executor per §3.3.4 —
    #: the engine writes them verbatim and performs no further redaction.
    receipt_inputs: Mapping[str, Any] = field(default_factory=dict)
    receipt_result: Mapping[str, Any] = field(default_factory=dict)
    #: Short operation descriptor for the receipt, e.g. "command:ensure_task"
    #: or "llm:reviewer/claude-opus-5". Never contains argument values.
    operation: str | None = None
    #: Set by AgentTaskExecutor before it suspends (§4.5).
    child_task_id: str | None = None
    #: Human-readable, non-sensitive notes for the receipt and for dry-run
    #: `unresolved` reasons.
    diagnostics: tuple[str, ...] = ()


@runtime_checkable
class Executor(Protocol):
    step_type: ClassVar[str]      # "command" | "llm" | "agent_task" | "decision" | "wait" | "foreach" | "terminal"
    mode: ClassVar[ExecutionMode]
    #: Structural marker asserted by T-12: every executor registered for
    #: SHADOW must declare True, and the assertion is on the class, not a call.
    no_side_effects: ClassVar[bool]

    async def execute(self, step: Any, ctx: "StepContext") -> ExecutorResult: ...


@runtime_checkable
class Cancellable(Protocol):
    """Optional. The engine calls this at most once per in-flight step (§4.9)."""
    async def request_cancel(self, step: Any, ctx: "StepContext") -> None: ...
```

#### 3.1.1 `StepContext` — everything an executor may read

```python
@dataclass(frozen=True, slots=True)
class BindingScope:
    """Four separate namespaces. A loop item can never shadow a binding because
    they do not share a dict — this is the structural fix for §2.2 item 2."""
    event: Mapping[str, Any]
    context: Mapping[str, Any]
    bindings: Mapping[str, Any]                 # save_result_as -> validated value
    loop: Mapping[str, Any] = field(default_factory=dict)   # item_binding -> item; at most one key

    def with_loop_item(self, name: str, item: Any) -> "BindingScope": ...
    def with_binding(self, name: str, value: Any) -> "BindingScope": ...   # raises on reassignment


@dataclass(frozen=True, slots=True)
class StepContext:
    run_id: str
    dispatch_id: str                 # shared by sibling rule runs from one event (§4.2)
    artifact_ref: "ArtifactRef"
    artifact: "PlaybookDefinition"
    rule_id: str
    step_id: str
    attempt: int                     # 1-based
    iteration_index: int | None      # None outside a foreach body
    mode: ExecutionMode
    principal: "ExecutionPrincipal"
    scope: BindingScope
    run_deadline_at: float | None
    step_deadline_at: float | None
    cancel_requested: bool
    cancel_event: asyncio.Event | None
    services: "EngineServices"
```

`EngineServices` is a frozen dataclass of exactly: `contracts: ContractRegistry`, `llm: LLMClient`, `handler: CommandHandler`, `db: DatabaseBackend | None`, `bus: EventBus | None`, `clock: Callable[[], float]`, `artifact_store: ArtifactStore`. **An executor receives nothing else.** No orchestrator, no playbook manager, no config object — a step that needs a config value reads it off the artifact, which is what makes the artifact the determinism boundary.

`ctx.services.db` is `None` in `SHADOW` and is a **read-only handle** in `DRY_RUN` (§4.10).

#### 3.1.2 Executor selection — mode picks the implementation, not a branch

```python
# src/playbooks/executors/__init__.py
EXECUTORS: Mapping[ExecutionMode, Mapping[str, Executor]]

def executor_for(step_type: str, mode: ExecutionMode) -> Executor: ...   # raises UnknownStepType
```

Per the spec, "Mode selects executor implementations; it does not select a different graph or validator." Concretely:

| Step type | `LIVE` | `DRY_RUN` | `SHADOW` |
|---|---|---|---|
| `command` | `LiveCommandExecutor` | `PreviewCommandExecutor` | `ShadowCommandExecutor` |
| `llm` | `LiveLlmExecutor` | `SymbolicLlmExecutor` (or `LiveLlmExecutor` when `invoke_ai=True`) | `SymbolicLlmExecutor` |
| `agent_task` | `LiveAgentTaskExecutor` | `SymbolicAgentTaskExecutor` | `SymbolicAgentTaskExecutor` |
| `decision` | `DecisionExecutor` — **the same instance in all three modes** | ← | ← |
| `wait` | `LiveWaitExecutor` | `ReportingWaitExecutor` | `ReportingWaitExecutor` |
| `foreach` | `ForEachExecutor` — **the same instance in all three modes** | ← | ← |
| `terminal` | `TerminalExecutor` — **the same instance in all three modes** | ← | ← |

Decision, foreach and terminal are shared deliberately: they contain no I/O, so a separate dry-run copy could only diverge. T-11's `test_deterministic_executors_are_identical_across_modes` asserts `executor_for(k, LIVE) is executor_for(k, DRY_RUN) is executor_for(k, SHADOW)` for those three keys — object identity, so a future refactor that clones them fails.

#### 3.1.3 The `GOTO` rule — runtime output cannot invent a target

The engine validates, **before** jumping:

```python
allowed = step.declared_targets()      # Package 2 model method
if result.goto_step_id not in allowed:
    -> reserved outcome "contract_violation", error receipt, run fails
```

`declared_targets()` is `{case.goto for case in cases} | {default}` for `DecisionStep` and `{body_entry, continuation} | set(transitions.values())` for `ForEachStep`. Every other step type returns `frozenset()`, so a `GOTO` from a command, LLM, agent-task, wait or terminal executor is a `contract_violation` by construction. This is the mechanical form of the global constraint "Runtime output cannot alter control flow unless the typed step contract explicitly exposes the referenced field."

### 3.2 `CommandResult` consumption — the exact algorithm

Package 1 owns `CommandResult`. Package 4 owns **how it is consumed**, and the rule is total: there is exactly one function, in one file, and no other code in `src/playbooks/` reads `.outcome` or `.value` off a raw handler dict.

`src/playbooks/executors/command.py`:

```python
async def _consume(
    result: CommandResult[Any],
    registration: CommandRegistration,
    step: CommandStep,
) -> ExecutorResult:
```

Steps, in order, each with its own failure mode. **No step may be reordered**; T-2 parameterises over all six.

1. **Type.** `isinstance(result, CommandResult)` — a handler that returned a bare dict never reaches here (Package 1's adapter is the only caller of `CommandHandler.execute`), but the check is asserted because the failure is otherwise silent. Violation → `contract_violation`.
2. **Outcome membership.** `result.outcome` must be in `{o.name for o in contract.execution.outcomes} | RESERVED_OUTCOMES`. Violation → `contract_violation` (never `runtime_error`: an unknown outcome is a contract fault, not a transient one).
3. **Result-model conformance.** `type(result.value)` must be `contract.execution.result_model`, and `result.value.model_dump()` must round-trip through it. `extra="forbid"` means an unknown field is already a `ValidationError` at construction; the round-trip catches a `model_construct` bypass. Violation → `contract_violation`.
4. **Transition coverage.** `result.outcome` must be a key of `step.transitions`, **or** `step.transitions` must contain `runtime_error`. Package 2 validates this statically; Package 4 re-checks because an artifact can be pinned across a contract change. Violation → `contract_violation`.
5. **Binding.** If `step.save_result_as` is set, `value` is `result.value`. Otherwise `value` is `None`. The bound object is the **declared result model**, never the handler's dict — spec: "A bound result contains only the step's validated declared output, not an arbitrary handler dictionary."
6. **Size.** `len(canonical_json(value)) <= 256 * 1024`. Violation → `state_limit_exceeded` (§4.8) — an error receipt and a failed run, **never** a truncated binding.

`classification()` is used for **receipts and metrics only**. Transition selection is by `outcome` name, never by classification: a command with two failure-classified outcomes (`not_found`, `conflict`) must be able to route them to different steps. T-2's `test_two_failure_outcomes_take_different_edges` pins this, and is the direct replacement for `pipeline_runner.py:145`.

**Reserved-outcome mapping from Package 1.** Package 1's `RESERVED_OUTCOMES` is `{contract_violation, unauthorized, runtime_error}` and is produced by the *adapter*. Package 4's `ENGINE_RESERVED_OUTCOMES` (§3.6) is a superset produced by the *engine*. The three overlap exactly and are passed through unchanged; the engine never rewrites an adapter-produced reserved outcome into a different one.

### 3.3 `RunRepository` and receipt usage

#### 3.3.1 One call, one boundary

Package 3 owns:

```python
async def commit_boundary(
    snapshot: RunSnapshot,
    receipt: StepReceipt,
    pending_wait_changes: Sequence[WaitChange],
) -> RunSnapshot: ...     # returns the persisted snapshot with version+1
```

Package 4's rule: **`commit_boundary` is the only write the engine makes to run state, and it is called exactly once per durable boundary.** Ordinary steps have one boundary per attempt. An LLM step with tools has one boundary for each completed tool turn, one `llm_call` boundary for each schema-invalid response before a retry, plus its final step boundary; an interruption has an `interrupted` boundary instead of a final step boundary. Every boundary advances `snapshot.version`, and every successful call has exactly one receipt, so T-3's counting invariant remains `calls == len(receipts)`. The attempt idempotency key is unchanged across its boundaries; uniqueness adds `(turn_index, receipt_kind)` rather than inventing a new attempt.

`commit_boundary` raises `SnapshotVersionConflict` when `snapshot.version` no longer matches the stored row. The engine's response is **never** to retry the write: it re-reads the snapshot, and if the run is still at the same step it fails the run with `interrupted` and an error receipt. Two writers at one step boundary means two engine instances think they own the run, and silently merging them is how a side-effecting command runs twice. T-3's `test_version_conflict_fails_the_run_and_receipts_it`.

#### 3.3.2 Attempt identity and the idempotency key

Fixed by the spec, and Package 4 does not vary it:

```
attempt_key = f"{run_id}:{step_id}:{attempt}"
```

For a step inside a `ForEachStep` body the iteration is part of the identity:

```
attempt_key = f"{run_id}:{step_id}:{iteration_index}:{attempt}"
```

The key is passed to a command **only when `contract.execution.idempotency.mode == "keyed"`**, written into the argument named by `idempotency.key_field`. For `mode="none"` and `mode="natural"` the key is recorded on the receipt but not passed — inventing an argument a contract does not declare would fail Package 1's arg-model validation anyway. T-2's `test_keyed_command_receives_the_attempt_key` and `test_unkeyed_command_receives_no_extra_argument`.

#### 3.3.3 Receipt fields Package 4 must populate

Package 5 projects these; Package 6 and 7 measure them. Every field is mandatory unless marked optional.

| Field | Source | Notes |
|---|---|---|
| `run_id`, `rule_id`, `step_id`, `step_type` | `StepContext` | |
| `artifact_sha256` | `ctx.artifact_ref` | Pins the overlay to the exact executed artifact |
| `attempt` | `ctx.attempt` | 1-based |
| `iteration_index` | `ctx.iteration_index` | `None` outside a loop body |
| `receipt_kind` | engine | `step`, `tool_turn`, `llm_call`, `interrupted`, or `operator_decision` |
| `turn_index` | engine/client | `-1` for compatibility step receipts; zero-based for LLM turn boundaries |
| `operator_decision_id` | engine | optional opaque link shared by an interruption and its resolution |
| `mode` | `ctx.mode` | Shadow and dry-run receipts exist **in memory only** (§3.3.5) |
| `principal_kind`, `profile_id`, `capability_fingerprint` | `ctx.principal`, `principal.policy.fingerprint()` | |
| `contract_fingerprint` | `CONTRACTS.fingerprint(step.command)` | `None` for non-command steps |
| `operation` | `ExecutorResult.operation` | |
| `outcome` | `ExecutorResult.outcome` | Validated against §3.6 before the write |
| `selected_transition` | engine | `(outcome_label, target_step_id)`; `None` for `SUSPEND`/`OPERATOR_DECISION` |
| `inputs` | `ExecutorResult.receipt_inputs` | Already redacted (§3.3.4) |
| `result` | `ExecutorResult.receipt_result` | Already redacted |
| `usage` | `ExecutorResult.usage` | `None` for non-LLM steps; §4.11 |
| `llm_calls` | `ExecutorResult.llm_calls` | aggregate count; engine subtracts already-durable turn boundaries before updating run budget |
| `idempotency_key` | §3.3.2 | Always recorded, even when not passed |
| `started_at`, `completed_at`, `duration_ms` | `ctx.services.clock` | |
| `deadline_fired` | engine | `"run"`, `"step"`, `"wait"` or `None` — spec: "the receipt records which deadline fired" |
| `cancellation` | engine | `None`, `"requested"`, `"acknowledged"`, `"grace_expired"` |
| `child_task_id` | `ExecutorResult.child_task_id` | optional |
| `diagnostics` | `ExecutorResult.diagnostics` | optional |

#### 3.3.4 Redaction is default-deny and happens in the executor

Spec: "Receipt display is default-deny: an unmarked field is redacted."

`receipt_inputs` and `receipt_result` are built by one shared helper so the three specialist executor tasks cannot each invent their own:

```python
# src/playbooks/executors/base.py
def project_for_receipt(
    values: Mapping[str, Any],
    *,
    allowed: Collection[str],
    sensitive: Collection[str] = (),
) -> dict[str, Any]:
    """Emit only `allowed` keys. `sensitive` keys become {"__redacted__": "sha256:<12hex>"}
    when the field is in `allowed`, and are omitted entirely when it is not.
    Everything else is dropped, not masked — a masked key still leaks that the
    field was populated."""
```

- Command steps: `allowed = contract.execution.receipt_projection`, `sensitive = contract.execution.sensitive_result_fields`. `receipt_projection=()` means **nothing is projected** — Package 1 documents this explicitly and Package 4 honours it.
- LLM steps: `allowed` is the top-level keys of `step.output_schema["properties"]` **minus** `step.sensitive_output_fields`. The prompt is never projected; only `prompt_digest` (`sha256` of the rendered prompt) appears in `diagnostics`.
- Agent-task steps: `allowed = {"task_id", "status"}`. The child's produced content is never copied into the parent receipt.

T-4's `test_receipt_never_contains_an_unprojected_field` walks every fixture receipt and asserts its key set is a subset of the declared projection.

#### 3.3.5 Dry-run and shadow never touch the repository

`RunRepository` and `WaitRepository` are **not present** in `EngineServices` for `DRY_RUN` or `SHADOW`: the engine constructs an `InMemoryRunRecorder` for those modes and passes the real repository only for `LIVE`. This is the structural half of the no-side-effect proof; T-12 is the behavioural half.

### 3.4 The engine step boundary — exact ordering

This is the contract that lets an executor author reason locally. `PlaybookEngine._advance_one_step(snapshot)` does exactly this, in this order:

1. **Compatibility.** Compare `snapshot.artifact_ref.command_fingerprints` and `.profile_fingerprints` to the live registries. A mismatch → fail the run with `execution_contract_changed`, write an error receipt, **do not invoke the step** (spec: "An incompatible in-progress run fails before invoking the changed command"). A *transient* provider absence → pause with `dependency_unavailable` (§4.13).
2. **Cancellation check.** If `snapshot.cancel_requested` and the run is `running`, transition to `cancelling`, call `request_cancel` on the in-flight executor if any, and commit a boundary with outcome `cancelled`.
3. **Deadline check.** `min(run_deadline_at, step_deadline_at, wait_deadline_at)`; if passed → outcome `timed_out`, `deadline_fired` records which.
4. **Resolve inputs.** `resolve_value` (`expressions.py`; added by this package, §2.5 item 1) over `step.inputs` against `ctx.scope`. A missing or type-invalid reference → `input_resolution_failed` **before** the executor runs. The engine never injects an `UNRESOLVED` marker and never coerces to `""`.
5. **Authorize.** For `CommandStep`, `authorize_command(name, ctx.principal)` (Package 0). A denial → `unauthorized`, error receipt, transition selection proceeds normally so an artifact can route a denial. The engine does **not** implement its own capability check.
6. **Execute.** `await executor_for(step.type, ctx.mode).execute(step, ctx)`, wrapped so that an unexpected exception becomes `runtime_error` with the exception type (not its message — a message can carry an argument value) in `diagnostics`. For a live tool-enabled `LlmStep`, `StepContext.on_tool_turn` is an engine-owned awaited callback. A completed payload appends its two-message delta to `snapshot.llm_turns`, commits a `tool_turn` receipt, replaces the executor's working snapshot with the returned version, and emits only after the commit. An `interrupted` payload appends an accounting-only turn with no transcript delta, sets `operator_decision`, commits the interruption, and leaves the snapshot paused. Callback/storage failures cross a distinct exception boundary and are never classified as provider failures.
7. **Validate the result.** `outcome ∈ declared ∪ ENGINE_RESERVED_OUTCOMES`; `control`/field coherence (`SUSPEND` requires `wait`; `GOTO` requires `goto_step_id ∈ step.declared_targets()`; `TERMINATE` requires `terminal_outcome`). Violation → `contract_violation`.
8. **Bind.** If `step.save_result_as`, `scope.with_binding(name, value)` — which raises on reassignment, because bindings are immutable. Then the 256 KiB check, then the 4 MiB whole-snapshot check; either breach → `state_limit_exceeded`.
9. **Select the transition.** `step.transitions[outcome]`, falling back to `step.transitions["runtime_error"]` **only** for a member of `ENGINE_RESERVED_OUTCOMES`. A business outcome with no edge is a `contract_violation`, not a silent completion — this is the replacement for `pipeline_runner.py:151-158`, where a missing `on_success` key ends the run as `completed`.
10. **Commit.** Build the next `RunSnapshot` (new step, new bindings, new loop frame, new version) and the final `step` receipt, and call `commit_boundary(snapshot, receipt, wait_changes)` once. Tool-turn boundaries already returned their advanced snapshot from step 6, so the final CAS is based on that version. Steps 8–10 are the final atomic unit; the only permitted writes during step 6 are the engine-owned LLM boundary callback above.
11. **Emit.** After a successful commit only, emit `playbook.v2.step.completed` on the bus. An event before the commit would let a subscriber observe a step that a crash then un-happens.

Two properties fall out and are asserted directly:

- **Crash between 6 and 10 loses the attempt, never the run.** The stored snapshot still points at the step, `attempt` is unchanged, and the restart re-runs it. For a `retry_safe` command that is correct; for a non-retry-safe one, §4.8 stops and asks an operator. T-15.
- **The executor cannot skip a boundary.** There is no import path from an executor back into the engine. An ordinary executor returns. The LLM executor may only await the boundary capability supplied in `StepContext`; that capability is implemented by the engine and commits before returning control to the client's loop.

### 3.5 `PlaybookEngine` — the public surface

```python
# src/playbooks/engine.py
class PlaybookEngine:
    def __init__(self, *, services: EngineServices, runs: RunRepository | None,
                 waits: WaitRepository | None, activations: ActivationRepository) -> None: ...

    async def dispatch_event(
        self, event: Mapping[str, Any], principal: ExecutionPrincipal,
        mode: ExecutionMode = ExecutionMode.LIVE,
    ) -> DispatchResult: ...

    async def run_rule(
        self, artifact_ref: ArtifactRef, rule_id: str, event: Mapping[str, Any],
        principal: ExecutionPrincipal, mode: ExecutionMode = ExecutionMode.LIVE,
    ) -> RunOutcome: ...

    async def resume(
        self, run_id: str, cause: ResumeCause, principal: ExecutionPrincipal,
    ) -> RunOutcome: ...

    async def cancel(self, run_id: str, principal: ExecutionPrincipal, *,
                     cancel_children: bool | None = None) -> RunOutcome: ...

    async def dry_run(
        self, artifact_ref: ArtifactRef, event: Mapping[str, Any],
        principal: ExecutionPrincipal, *, invoke_ai: bool = False,
        max_paths: int = 32, max_step_visits: int = 1000,
    ) -> DryRunTree: ...
```

`DispatchResult` carries `dispatch_id`, `rules_selected: tuple[str, ...]`, `run_ids: tuple[str, ...]`, and `pending: tuple[PendingEventRef, ...]`. Package 6's shadow-parity harness reads `rules_selected` and the recorded `commands`; Package 5's pending-event `dispatch` action calls `dispatch_event`. Both are named in their plans, so the field names are locked here.

`ResumeCause` is a closed union: `EventArrived(event_id, payload)`, `TimerFired(wait_id)`, `ChildTaskCompleted(task_id, status)`, `HumanDecision(decision, payload)`, `OperatorResolution(kind, payload)`.

`cancel_children=None` means "use each `AgentTaskStep.cancel_child`", which defaults to `False`. An explicit `True` still cannot cancel a child the run does not own (§7.4).

### 3.6 Reserved outcomes — one table, closed set

```python
# src/playbooks/executors/base.py
ENGINE_RESERVED_OUTCOMES: frozenset[str] = frozenset({
    "input_resolution_failed", "unavailable", "contract_violation",
    "state_limit_exceeded", "interrupted", "timed_out", "cancelled",
    "unauthorized", "runtime_error",
    "invalid_output", "budget_exceeded", "provider_error",
})
```

| Outcome | Produced by | Meaning |
|---|---|---|
| `input_resolution_failed` | engine, step 4 | A typed reference was missing or type-invalid |
| `unauthorized` | Package 0 via step 5 | Capability denied at the dispatch boundary |
| `contract_violation` | §3.2 / step 7 | Result, outcome, or control directive did not match the contract |
| `state_limit_exceeded` | step 8 | 256 KiB result or 4 MiB snapshot exceeded |
| `unavailable` | §4.13 | A dependency is transiently absent |
| `interrupted` | restart path | An in-flight attempt did not complete and cannot be safely replayed |
| `timed_out` | step 3 | Run, step or wait deadline fired |
| `cancelled` | §4.9 | Cancellation acknowledged |
| `runtime_error` | step 6 | Unexpected executor exception; also the artifact's catch-all edge label |
| `invalid_output` | `LlmExecutor` | Structured output failed schema validation after retries |
| `budget_exceeded` | `LlmExecutor` | `max_calls` / `max_output_tokens` / `max_total_tokens` breached |
| `provider_error` | `LlmExecutor` | Provider call failed |

**Naming note for Package 7.** Its plan §3.5 row 9 calls this outcome `output_invalid`. The spec (§"LlmStep") and Package 5's shared fixture both use **`invalid_output`**. `invalid_output` is authoritative; Package 7's reconciliation commit must rename its row. Recorded here so the discrepancy is not discovered at cutover.

A step is not required to map every reserved outcome, but Package 2 requires it to map each *applicable* one **or** declare a `runtime_error` target. Package 4 re-checks at step 9 and treats an unmappable reserved outcome as `contract_violation`, so a stale artifact fails loudly instead of ending as `completed`.

### 3.7 What the three parallel executor tasks own, and nothing else

| Task | Owns | Must not touch |
|---|---|---|
| **T-2** | `src/playbooks/executors/command.py`, `tests/test_command_executor.py` | `base.py`, `engine.py`, any other executor |
| **T-13 + T-14** | `src/playbooks/executors/llm.py`, `tests/test_llm_executor.py`, and the usage channel (§4.11) in `src/llm/` | `base.py`, `engine.py`, `command.py`, `agent_task.py` |
| **T-8** | `src/playbooks/executors/agent_task.py`, `tests/test_agent_task_executor.py`, and — per §5.4's *Green* — the `SUSPEND` boundary and the `ChildTaskCompleted` branch of `engine.py` | `base.py`, `command.py`, `llm.py` |

`base.py`, `engine.py`, `decision.py`, `wait.py`, `foreach.py`, `terminal.py` and `__init__.py` are all landed by T-1, T-5, T-6 and T-7 **before** the three parallel tasks start. Each parallel task adds exactly one executor module, one test module, and one line to the `EXECUTORS` table. A merge conflict is therefore confined to that one table line, by construction.

---

## 4. Design decisions

### 4.1 One engine, six call sites

Package 4 makes the V2 path *reachable* at each site behind `playbooks.v2_engine`; Package 7 makes it authoritative. The shape at every site is identical, so Package 7's switch is a one-line edit per site rather than a rewrite:

```python
if v2_engine_enabled(self.config):
    return await <engine call>
<existing V1 body, untouched>
```

| # | Site | Live symbol | V2 call |
|---|---|---|---|
| 1 | `src/orchestrator/core.py:800` (pipeline branch, fork at `:837`) | `Orchestrator._on_playbook_trigger` | `engine.dispatch_event(hydrated_event, service_principal, LIVE)` |
| 2 | `src/orchestrator/core.py:800` (LLM branch, `:1065`) | same method | same call — **the fork disappears**; one `dispatch_event` serves both kinds |
| 3 | `src/orchestrator/assignment_routing.py:459` | `AssignmentRoutingCoordinator._route_batch` | `engine.run_rule(...)` — §4.12 |
| 4 | `src/playbooks/resume_handler.py:260` | `PlaybookResumeHandler._resume_run` | `engine.resume(run_id, HumanDecision(...), principal)` |
| 5 | `src/workflow_stage_resume_handler.py:290` | `WorkflowStageResumeHandler._resume_run` | `engine.resume(run_id, EventArrived(...), principal)` |
| 6 | `src/commands/playbook_commands.py:857/:977/:1032/:324/:508/:1765` | `_cmd_run_playbook`, `_run_pipeline_playbook`, `_cmd_dry_run_playbook`, `_cmd_resume_playbook`, `_cmd_cancel_playbook_run`, `check_paused_playbook_timeouts` | `run_rule` / `dry_run` / `resume` / `cancel` |

The single most visible consequence is at site 2: `core.py:837`'s `if graph.get("kind") == "pipeline":` and the ~200 lines of pipeline-specific hydration, rule cloning and run-row bookkeeping below it (`:838-1048`) have **no V2 counterpart**. Rule selection, `event.task` hydration and per-rule run creation all move inside `dispatch_event`, where they are shared by every playbook kind. The `hydrated_event` construction (`core.py:850-866`) moves to `PlaybookEngine._hydrate_event` verbatim, including its `asdict(task_row)` fallback, because Package 6's parity harness compares V1 and V2 over the same events and a hydration difference would show up as a false rule-selection difference.

### 4.2 Rule-per-run dispatch

`dispatch_event` (`ExecutionMode.LIVE`):

1. Resolve activations: enabled, `ActivationHealth.ready`, matching the event type and scope. `needs_rebuild` or `invalid` → the event goes to `playbook_pending_events` (§4.13), never dropped.
2. For each activation, load the artifact **by hash** through `ArtifactStore.load` (which verifies the file hash and strict schema). A load failure → `unavailable`, the event is queued.
3. Evaluate every rule's trigger filter with the Package 2 condition evaluator against the hydrated event.
4. Allocate one `dispatch_id` (`uuid4().hex[:12]`), then create **one run per matching rule**. Idempotency is `(playbook_id, rule_id, event_id)`, not `(playbook_id, event_id)` — the existing partial unique index at `src/database/tables.py:946` is `(playbook_id, event_id)` and is a V1 table; Package 3's V2 run table carries the three-column index. §10 records the dependency.
5. Each run is an independent `asyncio.Task`. One rule failing does not fail its siblings; `DispatchResult.run_ids` lists all of them.

This is the direct fix for §2.2 item 5. T-1's failing assertions:

- `test_two_matching_rules_produce_two_runs` — an event matching both fixture rules yields `len(run_ids) == 2` with distinct `run_id`s and a shared `dispatch_id`.
- `test_sibling_failure_does_not_fail_the_other_run` — rule A's command raises; rule B still reaches its terminal.
- `test_same_event_id_dispatched_twice_creates_no_new_runs` — replays the event and asserts `run_ids` is unchanged and no second receipt exists.

### 4.3 `CommandExecutor` — `src/playbooks/executors/command.py`

`LiveCommandExecutor.execute`:

1. `registration = ctx.services.contracts.require(step.command)` — `UnknownContract` → `contract_violation`. A `CommandStep` naming an uncontracted command cannot execute, which is the runtime half of the spec's "A `CommandStep` can reference only a contracted command."
2. Build `args = registration.contract.execution.args_model(**resolved_inputs)`. A `ValidationError` here is the roadmap's "Validate runtime arguments again at the command boundary" — the compiler validated the *types of the references*; this validates the *resolved values*, which the compiler cannot see. Violation → `input_resolution_failed` (the value was wrong), not `contract_violation` (the contract was fine).
3. Attach the attempt key when `idempotency.mode == "keyed"` (§3.3.2).
4. `result = await registration.invoke(args, ctx.principal)` under `asyncio.timeout(contract.execution.timeout_seconds or step.timeout_seconds)`. A timeout → `timed_out`.
5. `_consume(...)` per §3.2.

`PreviewCommandExecutor` calls `registration.preview` when `supports_preview`, and otherwise returns `control=UNRESOLVED` with `diagnostics=("no preview adapter",)`. `ShadowCommandExecutor` **records the intended call and returns `UNRESOLVED` without invoking anything**, including the preview adapter — a preview may read a database snapshot, and shadow mode runs against production. Its recording is what Package 6's parity harness compares (`DispatchResult.commands`: an ordered tuple of `(step_id, command_name, canonical_args)`).

### 4.4 `LlmExecutor` — `src/playbooks/executors/llm.py`

The structural change from `runner.py` is that **the model no longer picks the edge**. `runner.py`'s transition logic (`runner_transitions.py`, 25 KB) asks the LLM which natural-language transition matches; the V2 `LlmStep` declares `output_schema`, and the engine reads a declared field from the validated structured output.

`LiveLlmExecutor.execute`:

1. Resolve the profile: `step.profile_id` → profile → `LLMCallSpec(intelligence_class=...)` → `resolve_call` (`src/llm/spec.py:44`). A missing profile → `unavailable` (a profile can be transiently unloaded), a profile whose capability policy is not a subset of the run principal's → `unauthorized`.
2. **Budget pre-flight (§4.11).** If `step.budget.max_total_tokens` is set and the resolved provider does not report usage, return `budget_exceeded` with `diagnostics=("provider does not report usage",)` **without calling the provider**. Spec: "A provider adapter that cannot report usage cannot run a step with a hard total-token budget."
3. Render the prompt from `step.inputs` via `resolve_value`. Record `prompt_digest` only.
4. Call under `asyncio.timeout(step.budget.timeout_seconds)`. Tools, if `step.tools` is non-empty, are the **intersection** of the step's declared tools and `ctx.principal.policy.aq_commands`, and every tool call routes through the same `CONTRACTS`-registered adapter and `authorize_command` the `CommandExecutor` uses. The model-visible schema is a projection; the dispatch check is the enforcement. §7.3.
5. Validate the response against `step.output_schema`. On failure, retry up to `step.retry.max_schema_retries` (default 1) with the validation error appended; exhausted → `invalid_output`.
6. Accumulate usage across every call and tool turn into `ExecutorResult.usage`. A breach of `max_calls`, `max_output_tokens` or `max_total_tokens` → `budget_exceeded`, and the partial transcript is still receipted.
7. The outcome is `validated_output[step.outcome_field]` (Package 2 requires `outcome_field` to name an enum property of `output_schema`). The value bound is the whole validated output.

**Tool turns are durable.** `LLMClient.run_tools` accepts `on_tool_turn: Callable[[ToolTurnReceipt], Awaitable[None]] | None`, `initial_turn_index`, and an already reconstructed message list. After all results for one provider response are appended, it awaits a payload containing `kind="tool_turn"`, the zero-based index, call IDs, `sha256` of the canonical result blocks, that provider call's `TokenUsage`, and the assistant/user transcript delta. The engine callback commits this delta and a `tool_turn` receipt before `run_tools` may issue another provider request. The result-cap check still applies to bound final output (256 KiB); transcript deltas remain subject to the 4 MiB snapshot cap and are never copied raw into receipts.

If external cancellation interrupts provider I/O or any tool dispatch in durable callback mode, the client awaits one `kind="interrupted"` payload with the next turn index, known call IDs, a digest of only completed result blocks, and any known usage. The engine commits it with a fresh `operator_decision_id`, appends an accounting-only `llm_turns` entry, pauses, and the executor returns `OPERATOR_DECISION` without a second receipt. The accounting entry consumes call/token budget and advances identity but contributes no transcript delta. Cancellation is latched before its first repository await and serialized with callback storage: a callback already committing a completed turn writes that turn, then exactly one cancellation boundary, and stops before another provider request. Provider and tool-dispatch deadlines instead raise `TimeoutError` and take the `timed_out` outcome; legacy callers without the durable callback continue to receive `CancelledError`. On process restart, a `running` snapshot still pointing at an `LlmStep` is treated conservatively as the same ambiguity before provider I/O. An explicit `OperatorResolution(kind="retry")` clears the decision and reconstructs messages from the prompt plus committed `tool_turn` and `llm_call` deltas, continuing at `last_turn_index + 1`. Before every schema-validation retry, with or without tools, the invalid response, its usage, and the corrective user message are committed as `llm_call`, preserving complete call accounting and the monotonically increasing turn index. The final step boundary adds only calls and tokens not already represented by those turn boundaries to `RunSnapshot.budget`, while its receipt retains aggregate attempt usage. `max_turns` is handled as `budget_exceeded` before schema validation, so exhausting a tool-loop call budget never invents a zero-usage `llm_call`. T-14 pins multi-turn cardinality, interruption, and restart continuation.

`SymbolicLlmExecutor` returns `UNRESOLVED` with `possible_outcomes` = the enum values of `output_schema[step.outcome_field]` plus every reserved outcome the step maps, and the engine forks across them (§4.10).

### 4.5 `AgentTaskExecutor` — `src/playbooks/executors/agent_task.py`

Distinct from `LlmStep` because it schedules, persists, waits, costs and cancels differently.

1. **Narrow the principal.** `child_policy = parent.policy.intersect(child_profile.policy).intersect(step.capability_narrowing)`; `child_principal = ctx.principal.narrow(child_policy, reason=f"agent_task:{ctx.step_id}")`. Three-way intersection, exactly the roadmap's "Delegated agent-task permissions are the intersection of parent permissions, child profile permissions, and explicit per-step narrowing." When the parent is itself an AI state, the spec additionally requires `child_profile.policy.is_subset_of(parent_ai_policy)`; a violation is `unauthorized` at execute time, not a silent narrowing. §7.2.
2. **Create the task** through the contracted `create_task` command with `child_principal`, so the child task's creation is authorized and receipted like any other command.
3. **Persist before suspending.** `ExecutorResult(control=SUSPEND, child_task_id=..., wait=WaitSpec(kind="task", correlation_key=task_id, deadline_at=now+step.timeout_seconds))`. The engine commits the boundary — snapshot, receipt and wait registration in one transaction — *before* the run is considered paused. A crash between task creation and the commit leaves an orphan child task and a run still at the step; §4.8's ambiguity rule applies, and the operator sees the orphan because the `create_task` receipt is already durable from the boundary that authorized it.
4. `wait_for_completion=False` returns `control=ADVANCE, outcome="dispatched"` instead, with no wait.
5. **Reconcile idempotently.** `resume(run_id, ChildTaskCompleted(task_id, status), ...)` maps the child's terminal status onto `completed` / `failed` / `timed_out` / `cancelled`. **The registered wait is the idempotency token**: the first delivery clears it in the same boundary that takes the edge, so a second delivery finds no wait to reconcile and returns `duplicate_child_completion` with no receipt and no transition. Reconciliation deliberately does *not* re-enter the executor — re-running an `AgentTaskStep` would create a second child task. A status outside the mapped set is `runtime_error`, never a guessed `failed`. A second delivery of the same `(run_id, step_id, attempt, task_id)` is a no-op that writes no receipt — T-8's `test_duplicate_child_completion_is_a_noop`.
6. **Cancellation.** §4.9: `cancel_child` defaults to `False`, so cancelling a parent leaves the child running by default. The child is never granted authority by cancellation.

### 4.6 `WaitExecutor` and the wait scheduler — `src/playbooks/executors/wait.py`

The race the spec closes: an event arrives between "decide to wait" and "the pause is persisted".

`LiveWaitExecutor` computes the typed correlation key from `ctx.scope` and returns `control=SUSPEND`. The engine then, **in one transaction** (`commit_boundary` with `pending_wait_changes=[Register(spec)]`):

1. writes the snapshot with `lifecycle="paused"` and the wait fields;
2. writes the receipt;
3. `WaitRepository.register(spec, snapshot.version)`, which in the same transaction scans `playbook_pending_events` (the durable inbox) for an already-arrived match and, if found, returns `matched_immediately` so the engine resumes instead of pausing.

Package 3 owns `register`'s compare-and-set; Package 4 owns the rule that **event ingestion writes the inbox before matching waits**, never the other way round. T-6's three assertions: `test_event_before_registration_resumes_immediately`, `test_event_during_registration_resumes_exactly_once`, `test_event_after_registration_resumes_exactly_once` — all three end with exactly one resume receipt.

**Deadlines.** A dedicated `WaitScheduler` (in `engine.py`, not a new module) owns `deadline_at`. It polls `WaitRepository.due(now)` on the orchestrator cycle. It does **not** create `TimerService` entries: `src/timer_service.py:185` is a playbook-*trigger* scheduler whose entries are cron-like and operator-visible, and per-run waits are neither. The earlier of the wait deadline and the run deadline wins, and `StepReceipt.deadline_fired` records which (§3.3.3).

`ReportingWaitExecutor` (dry-run and shadow) returns `control=UNRESOLVED` with `possible_outcomes = set(step.transitions)`, and the dry-run tree marks the node `unresolved` with reason `wait_not_persisted`. It never registers a wait and never advances on a guessed outcome — a wait's result is by definition external, so picking one would make the rest of the path fiction. Spec: "waits are reported without persisting a pause."

### 4.7 `ForEachExecutor` — `src/playbooks/executors/foreach.py`

Sequential, one active iteration, nesting rejected at compile time (Package 2), so the loop frame is finite and inspectable.

The executor is a pure state transition over the loop frame and returns `GOTO`:

| Frame state | Returns |
|---|---|
| No frame → resolve `step.collection` (a list; anything else → `input_resolution_failed`); empty list | `GOTO continuation`, outcome `completed`, aggregate `[]` |
| No frame, non-empty | `GOTO body_entry`, outcome `iterating`, frame `{index: 0, item_binding, aggregate: []}` |
| Frame present, body returned success | append to aggregate; `index+1 < len` → `GOTO body_entry`; else `GOTO continuation`, outcome `completed` |
| Frame present, body returned failure, policy `halt` | `GOTO transitions["failed"]`, outcome `failed` |
| Frame present, body returned failure, policy `continue` | append outcome only; advance index |
| Frame present, body returned failure, policy `collect` | append `{index, outcome, error}` to `aggregate.errors`; advance index; at the end outcome is `completed` with a non-empty `errors` list |

**How the executor learns the iteration's outcome.** The body is an ordinary sub-path whose last step transitions *back into the loop node* — in the §6.1 fixture that is `check-gate` → `for-each-task`, and `check-gate` is a `DecisionStep` whose outcome is a case label, not a success/failure word. So classification cannot be "read the outcome name". The locked rule is:

> When a transition targets the run's owning `ForEachStep`, the engine records `(producing_step_id, producing_outcome)` on the loop frame. The iteration is **failed** when that outcome is a member of `ENGINE_RESERVED_OUTCOMES`, **or** is a declared outcome the producing step's contract classifies `OutcomeClass.FAILURE`. It is **successful** otherwise.

This makes an author's intent explicit and local: routing a body step's failure edge *back to the loop node* is how one says "this failure is per-item"; routing it to a terminal is how one says "this failure ends the rule". T-7's `test_decision_returning_to_the_loop_counts_as_success` and `test_command_failure_edge_returning_to_the_loop_counts_as_failed` pin both halves.

Two rules the live `pipeline_runner._run_for_each` (`:165-186`) breaks and this one keeps:

- **The loop item lives in `scope.loop`, not `scope.bindings`.** `with_loop_item` returns a new scope; there is no `pop` and no `finally`, so a failure branch cannot read a stale item. T-7's `test_loop_item_cannot_shadow_a_binding` builds an artifact with a binding named `task` and an item binding named `task` and asserts Package 2 rejects it at compile time *and* that `with_loop_item` raises if it somehow reaches runtime.
- **The frame is committed on both sides of every body transition.** Entering iteration *n* and leaving it are two boundaries. A crash mid-body restarts iteration *n*, never *n+1*. T-15's `test_restart_mid_loop_resumes_the_same_iteration` kills the process between the two boundaries and asserts `iteration_index` is unchanged and the aggregate has *n* entries.

The aggregate binding is `{"items": [...], "outcomes": [...], "errors": [...]}` — ordered, and subject to the same 256 KiB limit, which is why `collect` over a large collection can legitimately end in `state_limit_exceeded` rather than a truncated result.

### 4.8 Ambiguous interruption — stop, do not guess

Spec: "a retry-safe command can be replayed with that key; a non-retry-safe command pauses with `operator_decision_required` rather than executing twice."

On restart, for a snapshot whose lifecycle is `running` and whose current step has an attempt with a *started* but no *completed* receipt, or an LLM snapshot still pointing at the step after its last `tool_turn` boundary:

| Step / contract | Behaviour |
|---|---|
| `retry_safe=True` **or** `idempotency.mode in {"natural", "keyed"}` | Replay as the **same** attempt number with the same key. The receipt for the interrupted attempt is written with outcome `interrupted`, then a new receipt records the replay. |
| Anything else — including every `LlmStep` with an in-flight provider/tool call, and every `AgentTaskStep` whose child-task creation may or may not have landed | Write an `interrupted` receipt carrying an `operator_decision_id`, pause with reason `operator_decision_required`, bind nothing, and select no transition. |

The operator records exactly one resolution, and the resolution is itself receipted: `accept(outcome, value)`, `retry` (new attempt number), `fail`, `cancel`. The command surface is `playbook_run_resolve` — **Package 5 owns the endpoint and UI**; Package 4 owns `PlaybookEngine.resume(run_id, OperatorResolution(kind, payload), principal)` and the receipt. Package 5's plan already lists `playbook_run_overlay`; the resolution command is added to its scope by this plan's §12 note.

An operator resolution requires the `playbook_admin` capability and is refused for a `PLAYBOOK`-kind principal — a playbook cannot resolve its own ambiguity. §7.5.

### 4.9 Cancellation and the lifecycle

One enum: `running`, `paused`, `cancelling`, `completed`, `failed`, `timed_out`, `cancelled`. `src/playbooks/state_machine.py` gains `cancelling` and the two legal edges into it (`running → cancelling`, and `paused → cancelled` directly). `TERMINAL_STATUSES` is unchanged.

| From | `cancel()` does |
|---|---|
| `paused` | Immediate: `cancelled`, one receipt, wait deregistered in the same boundary |
| `running`, no executor in flight | Sets `cancel_requested`; the next step boundary (§3.4 step 2) transitions to `cancelled` |
| `running`, executor in flight | `cancelling`; `request_cancel` on the `Cancellable` executor; `cancelled` on acknowledgement or when `cancellation_grace_seconds` (config, default 30) expires, whichever first. The receipt's `cancellation` field records `acknowledged` vs `grace_expired` |
| terminal | Refused with the existing `Run '<id>' already <status>` shape from `playbook_commands.py:544` |

This replaces §2.2 item 4 entirely: the engine reads `cancel_requested` from the snapshot it is about to write, so a live run cannot overwrite a cancellation. T-9's `test_cancel_during_a_live_command_does_not_get_overwritten` is the direct regression test for the docstring at `playbook_commands.py:511-519`.

`run_task.py::playbook_status_to_task_status` gains a `cancelling` mapping (to the same task status as `running`, since the projection task is still occupied).

### 4.10 Dry-run — the same graph, bounded

`dry_run` builds the *identical* `PlaybookEngine`, `EXECUTORS[DRY_RUN]`, and `ArtifactStore.load`ed artifact, and walks it with a work-list rather than a single cursor.

- **Bounds.** `max_paths=32`, `max_step_visits=1000` (config-overridable, §9). Reaching either sets `DryRunTree.truncated=True`. A truncated tree **never** reports a path as `completed`; the frontier node is `unresolved` with reason `path_limit` / `visit_limit`. T-11's `test_truncation_never_reports_completed`.
- **Forking.** A `control=UNRESOLVED` result forks the work list across `possible_outcomes`, each becoming its own path with the symbolic result reference kept unbound. Downstream `ResultRef`s on a forked path stay symbolic and render as `unresolved` rather than failing.
- **Node states.** `resolved` (a deterministic executor, or a preview adapter that returned a typed simulated result), `simulated` (a preview adapter ran), `unresolved` (with `reason` and `possible_outcomes`).
- **`invoke_ai=True`** swaps `SymbolicLlmExecutor` for `LiveLlmExecutor` **only**; commands stay preview-only in every case. There is no option that makes dry-run write.
- **No unrecognised node is terminal.** An unknown step type raises `UnknownStepType`. `runner.py`'s dry-run silently treats unhandled shapes as the end of the walk; the V2 version fails.
- **Shadow walks the same work list.** `run_rule(mode=SHADOW)` does not drive the live cursor — every shadow executor answers an external boundary with `UNRESOLVED`, and a cursor would pause at the first command and compare nothing downstream (found by the Package 4 exit gate, task `wise-apex-40`). It calls the one `_traverse_symbolic` that `dry_run` uses, with `EXECUTORS[SHADOW]`, bounded by `max_symbolic_paths` and `max_step_visits`, records each intended command into the `InMemoryRunRecorder` (deduplicated by canonical triple across forks), and finishes the in-memory run `COMPLETED` with outcome `completed` / `unresolved` / `truncated`. The tree is returned as `RunOutcome.traversal` and aggregated as `DispatchResult.traversals` for Package 6. The shared traversal also mirrors the live walk's stage-5 authorization routing and the reserved-outcome `runtime_error` edge fallback, so a parity reader sees the edge live would take.

The strongest dry-run assertion is the parity one, T-11's `test_live_and_dry_run_select_the_same_edges`: run the fixture artifact live against a scripted command handler, then dry-run it with preview adapters scripted to the *same* outcomes, and assert the ordered `(step_id, outcome, target)` triples are equal. That is the spec's "Live and dry-run select the same rules, nodes, and edges for identical resolved outcomes", and it is the assertion that fails if anyone ever reintroduces a dry-run-specific traversal.

### 4.11 The provider usage channel — the package's largest deviation

**Observed.** `src/llm/types.py::ChatResponse` is `content: list[TextBlock | ToolUseBlock]` and nothing else. `src/llm/providers/anthropic.py:147` returns `ChatResponse(content=content)`, discarding `resp.usage`, which the Anthropic SDK does return. `openai.py` and `google.py` are the same. `grep -rn usage src/llm/` is empty. The only token accounting in the tree is `src/playbooks/token_tracker.py:36::_estimate_tokens`, which is `sum(len(t) for t in texts) // 4`.

**Required.** Spec, `LlmStep`: "Token accounting uses provider-reported input and output usage. A provider adapter that cannot report usage cannot run a step with a hard total-token budget."

**Change (T-13), additive and backward-compatible:**

```python
# src/llm/types.py
@dataclass
class ChatResponse:
    content: list[TextBlock | ToolUseBlock]
    usage: TokenUsage | None = None       # NEW, defaults to None
```

**Where `TokenUsage` is defined.** §3.1 lists it under `src/playbooks/executors/base.py` for readability, but defining it there would make `src/llm/` import `src/playbooks/`, inverting the layering. The definition therefore lives in **`src/llm/types.py`** and `base.py` re-exports it. T-13 lands it in that position; §3.1's listing is a presentation convenience and the import direction is the binding rule.

Per provider:

| Provider | Source | Populates |
|---|---|---|
| `anthropic.py:147` | `resp.usage.input_tokens`, `resp.usage.output_tokens` | `TokenUsage(..., reported=True)` |
| `openai.py` | `resp.usage.prompt_tokens`, `.completion_tokens` | `TokenUsage(..., reported=True)` |
| `google.py` | `resp.usage_metadata.prompt_token_count`, `.candidates_token_count`; absent on some models | `reported=True` when present, else `usage=None` |
| `fake.py::FakeProvider` | `add_response(resp, usage=...)` — new optional argument, default `None` | Lets a test drive both the reported and unreported paths |

`LLMClient.complete` / `run_tools` (`src/llm/client.py:141`, `:154`) propagate `usage` onto `LLMResponse` and `LLMRunResult`; `run_tools` sums across turns.

**The fail-closed rule.** `LiveLlmExecutor` step 2 (§4.4). T-13's failing assertions:

- `test_step_with_total_token_budget_refuses_an_unreporting_provider` — a `FakeProvider` returning `usage=None` and a step with `max_total_tokens=8000` yields `budget_exceeded` and **zero** provider calls (`FakeProvider.calls == []`).
- `test_step_without_total_token_budget_runs_on_an_unreporting_provider` — the same provider with `max_total_tokens=None` runs, and the receipt carries `usage.reported is False`.
- `test_receipt_usage_is_provider_reported_not_estimated` — asserts the receipt's `usage.input_tokens` equals the provider's number and is *not* `len(prompt)//4`.

**Scope boundary.** `token_tracker.py` is **not** modified: it belongs to `runner.py` and Package 7 deletes both. V2 usage goes to receipts, and the existing `record_token_usage` ledger write (`runner.py:320-355`) is reproduced in the engine's commit path so cost reporting keeps working — with `usage.total` instead of an estimate, and skipped (as today) when the event has no `project_id`, because `token_ledger.project_id` is a non-null FK.

### 4.12 Assignment routing keeps its caches

Spec: "Assignment routing keeps its existing input/options hash cache outside the engine. A cache hit does not create another playbook run or receipt set."

`AssignmentRoutingCoordinator` keeps, unchanged: `_batch_key` (`:389`), `_catalog_hash` (`:186`), `_catalog`/`_options`/`cached_options_hash` (`:299`-`:325`), `_attempt_event_id` (`:400`), `_existing_response` (`:413`), `_retry`/`_task_retry` (`:425`), `_commit`'s full re-validation under `with_for_update` (`:509`), and `validate_assignment_response` (`:78`). Only the four lines constructing `PlaybookRunner` (`:459-467`) change:

```python
outcome = await engine.run_rule(
    artifact_ref=activation.artifact_ref,
    rule_id=ASSIGNMENT_RULE_ID,
    event=event,
    principal=service_principal("assignment-routing"),
    mode=ExecutionMode.LIVE,
)
response = outcome.result_value            # the declared AssignmentRoutingResult
```

Two rules this plan adds:

- **The cache key includes artifact identity.** `_batch_key`'s payload gains `"artifact_sha256": activation.artifact_ref.artifact_sha256`, so activating a rebuilt routing artifact invalidates cached decisions instead of serving decisions made by the previous graph. The roadmap requires this ("make cache keys include artifact identity"); it is a one-line change to `_batch_key` and a one-line change to `tests/test_assignment_routing_coordinator.py`.
- **A synchronous caller gets a typed unavailable result.** When the activation is `needs_rebuild` or the artifact will not load, `run_rule` returns `RunOutcome(status="unavailable")` and the coordinator applies its **existing** `_note_failure` retry/backoff rather than raising. Spec: "A synchronous caller such as assignment routing receives a typed unavailable result and applies its existing caller-owned retry or fallback policy."

`sync_task_projection=False` and `tool_overrides=[]` (`assignment_routing.py:465-466`) become `run_rule(..., project_task=False)`: routing runs must not create projection tasks, and the routing `LlmStep` declares no tools in the artifact rather than having them stripped by a caller argument.

### 4.13 Unavailable dependencies and pending events

A dependency that is *intentionally gone* (an uninstalled plugin's command) is `needs_rebuild`; one that is *transiently absent* (a plugin that failed to load) is `unavailable`. Package 3 owns the health computation; Package 4 owns the two runtime behaviours:

- **New work queues.** `dispatch_event` writes the event to `playbook_pending_events` with its original event ID and arrival order, and returns it in `DispatchResult.pending`. Nothing is dropped.
- **In-progress work pauses at the next boundary** with reason `dependency_unavailable` and outcome `unavailable`. When compatibility returns, `resume` continues **against the same artifact** — never against a rebuilt one, which would be the in-place translation the roadmap forbids.

Package 5 owns the operator surface for pending events and re-enters through `dispatch_event`; that dependency is already recorded in its plan §3.1.

### 4.14 `DecisionExecutor` and `TerminalExecutor`

Both are pure and mode-independent (§3.1.2).

`DecisionExecutor` evaluates each `case.when` in declared order with the Package 2 condition evaluator over `ctx.scope`, and returns `GOTO case.goto` for the first true case, else `GOTO default`. It makes no LLM call — there is no code path from `decision.py` to `ctx.services.llm`, and T-5 asserts it by patching `services.llm` with an object that raises on attribute access. A condition that raises (a type error the compiler could not see) → `input_resolution_failed`.

`TerminalExecutor` returns `control=TERMINATE, terminal_outcome=step.outcome`, plus `value=resolve_value(step.result)` when the terminal declares a typed result. The engine maps the terminal outcome onto the run lifecycle: `completed` → `completed`, `failed` → `failed`, `cancelled` → `cancelled`, `timed_out` → `timed_out`.

---

## 5. Task and commit sequence

### 5.0 Map to the roadmap's commit sequence

The roadmap names six commits. This plan uses **eight**, and both additions are recorded here rather than absorbed silently:

| Roadmap commit | This plan |
|---|---|
| — | **C0** `docs: reconcile package 4 plan against the live tree` (§2.4) — **added**, because this plan was written before Packages 1–3 landed |
| 1. `feat: execute command decision and terminal v2 steps` | **C1** — T-1 … T-5 |
| 2. `feat: execute durable waits and foreach loops` | **C2** — T-6, T-7, **and T-9 (cancellation)**. Cancellation lands here rather than later because the `paused → cancelled` edge is a wait-state concern and the wait tests need the `cancelling` lifecycle to exist |
| 3. `feat: execute budgeted llm steps` | **C3** — T-13 (usage channel), T-14 (`LlmExecutor`) |
| 4. `feat: execute narrowed agent task steps` | **C4** — T-8 |
| 5. `feat: add bounded dry run and side effect free shadow mode` | **C5** — T-11, T-12 |
| 6. `test: prove restart and idempotency boundaries` | **C6** — T-10, T-15 |
| — | **C7** `feat: route v2 playbook entry points behind a flag` — T-16, T-17. **Added**, because the roadmap's Modify list names six call sites but no commit that touches them. Without C7 the engine is unreachable and Package 6 cannot run its shadow-parity harness |

**Parallelism.** C1 must land first — it defines `base.py` and `engine.py`. After C1, **C3 (T-13+T-14), C4 (T-8) and C2 (T-6/T-7/T-9) are fully independent** and may be three concurrent branches: each adds one executor module, one test module, and one row of the `EXECUTORS` table (§3.7). C5 requires all of C2–C4 (it forks across every step kind). C6 requires C5. C7 requires C6.

**Test command for every task:** `aq test <files> -q`, never bare `pytest` past one file (CLAUDE.md). Run the file you are iterating on with `-x`; run the package sweep (§12.2) once, at the end.

---

### 5.1 C1 — `feat: execute command decision and terminal v2 steps`

#### T-1 — the engine walks a graph and starts one run per rule

*Red:* `tests/test_v2_engine.py` (new).

- `test_two_matching_rules_produce_two_runs` — dispatch `task.completed` (§6.2 event 1) against the §6.1 artifact, which has two rules; assert `len(result.run_ids) == 2`, ids distinct, `dispatch_id` shared. **Fails with `ModuleNotFoundError: src.playbooks.engine`.**
- `test_sibling_failure_does_not_fail_the_other_run` — the scripted handler raises for `ensure_task` only; assert the `sweep-on-spec-approved` run still reaches `sweep-done`.
- `test_same_event_id_dispatched_twice_creates_no_new_runs`.
- `test_unknown_step_type_raises_rather_than_terminating` — an artifact with `"type": "frobnicate"` raises `UnknownStepType`; it does not end the walk (§2.2 item 3's failure mode).
- `test_business_outcome_without_an_edge_is_a_contract_violation` — a `CommandStep` whose contract declares `conflict` but whose `transitions` omits both `conflict` and `runtime_error`; assert the run fails with `contract_violation` and **not** `completed`. This is the direct replacement for `pipeline_runner.py:151-158`.

*Green:* `src/playbooks/engine.py` with `PlaybookEngine.__init__`, `dispatch_event`, `run_rule`, `_advance_one_step` (§3.4 steps 1–11), `_hydrate_event` (lifted from `core.py:850-866`), `DispatchResult`, `RunOutcome`, `ResumeCause`, `WaitScheduler` stub; `src/playbooks/executors/__init__.py` with `EXECUTORS`/`executor_for`; `src/playbooks/executors/base.py` per §3.1.

*Verify:* `aq test tests/test_v2_engine.py -q`

#### T-2 — `CommandExecutor` (parallel-safe; owns `command.py` only)

*Red:* `tests/test_command_executor.py` (new). Parameterised over the six §3.2 checks:

- `test_bare_dict_result_is_a_contract_violation`
- `test_undeclared_outcome_is_a_contract_violation` — an adapter returning `outcome="weird"`.
- `test_result_of_the_wrong_model_is_a_contract_violation`
- `test_two_failure_outcomes_take_different_edges` — a contract with `not_found` and `conflict`, both `OutcomeClass.FAILURE`, routed to different steps; assert the receipts' `selected_transition` differ. **This is the assertion that would pass trivially against `pipeline_runner.py:145` only by accident, and fails outright the moment anyone reintroduces classification-based routing.**
- `test_empty_dict_result_is_not_treated_as_success` — the exact `pipeline_runner.py:145` bug, expressed as a `CommandResult` whose `value` is an empty result model with outcome `not_found`; assert the failure edge is taken.
- `test_result_over_256_kib_is_state_limit_exceeded` — a result model carrying a 300 KiB string; assert `state_limit_exceeded`, an error receipt, and **no binding written**.
- `test_keyed_command_receives_the_attempt_key` / `test_unkeyed_command_receives_no_extra_argument`.
- `test_runtime_arguments_are_revalidated_at_the_boundary` — a resolved value of the wrong type for the args model; assert `input_resolution_failed`, and that `registration.invoke` was never awaited.
- `test_uncontracted_command_cannot_execute` — `contract_violation`, adapter not called.

*Green:* `src/playbooks/executors/command.py` with `LiveCommandExecutor`, `PreviewCommandExecutor`, `ShadowCommandExecutor`, `_consume`.

*Verify:* `aq test tests/test_command_executor.py -q`

#### T-3 — one commit per durable boundary

*Red:* in `tests/test_v2_engine.py`.

- `test_exactly_one_commit_per_durable_boundary` — a counting `RunRepository` double; run the fixture to completion and assert `double.commit_calls == len(double.receipts)` and that every receipt has a distinct `(step_id, iteration_index, attempt, turn_index, receipt_kind)`.
- `test_no_durable_write_happens_before_the_boundary` — the repository double raises on `commit_boundary`; assert the scripted command **was** called (the effect is real) but no snapshot row advanced and no bus event was emitted. Proves the §3.4 step-11 ordering.
- `test_version_conflict_fails_the_run_and_receipts_it` — the double raises `SnapshotVersionConflict` once; assert the run ends `failed` with outcome `interrupted` and one error receipt, and that `commit_boundary` was **not** retried.

*Green:* the `commit_boundary` call site in `_advance_one_step`, plus `SnapshotVersionConflict` handling.

#### T-4 — receipts are complete and default-deny

*Red:* `tests/test_v2_receipts.py` (new).

- `test_receipt_carries_every_mandatory_field` — parameterised over §3.3.3's mandatory rows; asserts each is non-`None` for a completed command step.
- `test_receipt_never_contains_an_unprojected_field` — for every receipt in a full fixture run, `set(receipt.inputs) <= set(contract.execution.receipt_projection)`.
- `test_empty_receipt_projection_projects_nothing` — a contract with `receipt_projection=()`; assert `receipt.result == {}` even though the command returned a populated model.
- `test_sensitive_field_is_hashed_not_masked_and_unallowed_is_dropped`.
- `test_artifact_sha256_on_every_receipt_matches_the_pinned_ref`.

*Green:* `project_step_receipt` in `base.py` (§2.5 item 3, delegating to Package 3's `project_receipt`) and the receipt build in `_advance_one_step`.

#### T-5 — decision and terminal

*Red:* in `tests/test_v2_engine.py`.

- `test_decision_takes_the_first_true_case` and `test_decision_falls_through_to_default`.
- `test_decision_makes_no_llm_call` — `ctx.services.llm` replaced by an object whose `__getattr__` raises; the step still resolves.
- `test_decision_goto_outside_declared_targets_is_a_contract_violation` — a hand-built `ExecutorResult` with a foreign `goto_step_id` (§3.1.3).
- `test_command_executor_cannot_goto` — a `GOTO` from a command step is `contract_violation`, because `CommandStep.declared_targets()` is empty.
- `test_terminal_outcome_maps_onto_the_run_lifecycle` — parameterised over `completed`/`failed`/`cancelled` (§2.5 item 6: there is no `timed_out` terminal).

*Green:* `decision.py`, `terminal.py`, and the shared-instance registration in `__init__.py`.

*Commit C1 verify:*
```bash
aq test tests/test_v2_engine.py tests/test_command_executor.py tests/test_v2_receipts.py -q
ruff check src/playbooks/engine.py src/playbooks/executors tests/test_v2_engine.py \
           tests/test_command_executor.py tests/test_v2_receipts.py
```

---

### 5.2 C2 — `feat: execute durable waits and foreach loops`

#### T-6 — the wait race is closed in one transaction

*Red:* `tests/test_v2_waits.py` (new). The three orderings, each ending with **exactly one** resume receipt:

- `test_event_before_registration_resumes_immediately` — write the matching event to the pending-event inbox first, then reach the wait; assert `register` returns `matched_immediately` and the run never enters `paused`.
- `test_event_during_registration_resumes_exactly_once` — a `WaitRepository` double whose `register` awaits a barrier that the test releases only after injecting the event; assert one resume and one receipt.
- `test_event_after_registration_resumes_exactly_once`.
- `test_wait_deadline_and_run_deadline_the_earlier_wins` — parameterised both ways; assert `receipt.deadline_fired` is `"wait"` and `"run"` respectively.
- `test_wait_does_not_create_a_timer_service_entry` — a `TimerService` double that raises on any add; the wait is registered and the double is untouched.

*Green:* `src/playbooks/executors/wait.py` (`LiveWaitExecutor`, `ReportingWaitExecutor`) and `WaitScheduler` in `engine.py`.

#### T-7 — loops are scoped and resumable

*Red:* `tests/test_v2_foreach.py` (new).

- `test_loop_item_lives_in_its_own_namespace` — a step inside the body binds `save_result_as: "task"` while the item binding is also `task`; assert `scope.loop["task"]` and `scope.bindings["task"]` are both readable and distinct. Against the live `pipeline_runner` shape this is impossible; the assertion is the point.
- `test_loop_item_is_not_visible_after_the_loop` — a `ResultRef` to the item on the continuation path is `input_resolution_failed`.
- `test_failure_policy_halt_continue_collect` — three parameterisations over §4.7's table, asserting the aggregate shape.
- `test_frame_is_committed_on_both_sides_of_the_body` — count boundaries for a 3-item loop: `1 (enter) + 3 × (1 body + 1 return) = 7`.
- `test_empty_collection_goes_straight_to_continuation`.
- `test_non_list_collection_is_input_resolution_failed`.
- `test_aggregate_over_256_kib_is_state_limit_exceeded`.

*Green:* `src/playbooks/executors/foreach.py` and the loop-frame fields in the snapshot build.

#### T-9 — cancellation is real

*Red:* `tests/test_v2_cancellation.py` (new).

- `test_cancel_a_paused_run_is_immediate` — one boundary, wait deregistered, status `cancelled`.
- `test_cancel_during_a_live_command_does_not_get_overwritten` — a scripted command that blocks on a barrier; call `cancel`; release; assert the final status is `cancelled` and **not** overwritten to `running`/`completed`. This is the regression test for `playbook_commands.py:511-519`'s own docstring.
- `test_in_flight_executor_gets_one_cancel_signal` — a `Cancellable` double; assert `request_cancel` awaited exactly once.
- `test_grace_expiry_still_reaches_cancelled` — the double never acknowledges; assert `cancelled` after `cancellation_grace_seconds` with `receipt.cancellation == "grace_expired"`.
- `test_cancel_a_terminal_run_is_refused` — same message shape as `playbook_commands.py:544`.
- `tests/test_playbook_state_machine.py` gains `test_cancelling_transitions` for the two new edges.

*Green:* `cancelling` in `src/playbooks/state_machine.py`, `PlaybookEngine.cancel`, `Cancellable` wiring, `playbook_status_to_task_status` mapping.

*Commit C2 verify:*
```bash
aq test tests/test_v2_waits.py tests/test_v2_foreach.py tests/test_v2_cancellation.py \
        tests/test_playbook_state_machine.py -q
ruff check src/playbooks/executors src/playbooks/engine.py src/playbooks/state_machine.py src/playbooks/run_task.py
```

---

### 5.3 C3 — `feat: execute budgeted llm steps`

#### T-13 — the provider usage channel (§4.11)

*Red:* `tests/test_llm_usage.py` (new).

- `test_anthropic_adapter_reports_usage` — a stubbed SDK response with `usage.input_tokens=1200`, `usage.output_tokens=300`; assert `ChatResponse.usage == TokenUsage(1200, 300, reported=True)`. **Fails today:** `anthropic.py:147` constructs `ChatResponse(content=content)` and `ChatResponse` has no `usage` field, so this is an `AttributeError` before it is an assertion failure.
- `test_openai_adapter_reports_usage`, `test_google_adapter_reports_usage_when_present`, `test_google_adapter_reports_none_when_absent`.
- `test_run_tools_sums_usage_across_turns` — three turns; assert the summed `TokenUsage` and `reported=True`.
- `test_usage_addition_is_reported_only_when_both_are` — `TokenUsage(reported=True) + TokenUsage(reported=False)` has `reported is False`.
- `test_fake_provider_can_report_and_not_report`.

*Green:* `TokenUsage` in `src/llm/types.py`; `usage` on `ChatResponse`, `LLMResponse`, `LLMRunResult`; population in all three adapters; the `usage=` argument on `FakeProvider.add_response`; the re-export from `src/playbooks/executors/base.py`.

Every change is additive with a default of `None`, so `tests/llm/` and every existing `FakeProvider` caller stay green. Confirm with `aq test tests/llm -q` and `aq test tests/test_playbook_runner.py -q` in the same commit.

#### T-14 — `LlmExecutor` (parallel-safe; owns `llm.py` only)

*Red:* `tests/test_llm_executor.py` (new).

- `test_step_with_total_token_budget_refuses_an_unreporting_provider` — assert outcome `budget_exceeded` **and** `FakeProvider.calls == []`. Zero calls is the assertion that matters: a fail-closed rule that still makes the call is not fail-closed.
- `test_step_without_total_token_budget_runs_on_an_unreporting_provider` — runs; `receipt.usage.reported is False`.
- `test_receipt_usage_is_provider_reported_not_estimated` — the receipt's numbers equal the provider's and differ from `len(prompt)//4`.
- `test_structured_output_drives_the_edge_not_the_model` — the model returns `{"risk": "high"}` plus a paragraph of prose asking to go elsewhere; assert the `high` edge is taken. The prose is deliberately adversarial: it is the compact form of the `runner_transitions.py` behaviour being removed.
- `test_invalid_output_retries_then_gives_up` — two malformed responses with `max_schema_retries=1`; assert `invalid_output` and exactly two provider calls.
- `test_max_calls_and_max_output_tokens_breach_is_budget_exceeded` (two parameterisations).
- `test_tool_calls_are_authorized_at_dispatch_not_by_the_published_schema` — publish a narrowed tool list, then have the fake model call a tool **outside** it; assert the call is denied by `authorize_command` and the step outcome is `unauthorized`. §7.3.
- `test_completed_tool_turn_is_a_durable_boundary` — two completed tool turns; assert two ordered `tool_turn` receipts, two snapshot-version advances before the final `step` receipt, stable artifact/run/attempt identity, and one provider request starts only after the preceding boundary callback returns.
- `test_interrupted_provider_call_is_not_replayed` — a snapshot with a started-but-uncompleted LLM attempt; assert one `interrupted` receipt with `operator_decision_id`, lifecycle `paused`, and that the provider was not called.
- `test_interrupted_tool_dispatch_pauses_at_the_last_complete_turn` — cancel during a later tool dispatch; assert partial results are represented only by a digest, no partial transcript delta is persisted, and the final step receipt is absent.
- `test_retry_after_restart_continues_from_last_committed_tool_turn` — reconstruct the engine around the persisted snapshot, resolve with `retry`, and assert the next provider request contains the completed turn transcript without re-dispatching its tools.
- `test_profile_not_a_subset_of_the_principal_is_unauthorized`.

*Green:* `src/playbooks/executors/llm.py` (`LiveLlmExecutor`, `SymbolicLlmExecutor`).

*Commit C3 verify:*
```bash
aq test tests/test_llm_usage.py tests/test_llm_executor.py -q
aq test tests/llm tests/test_playbook_runner.py -q      # no regression in the V1 LLM path
ruff check src/llm src/playbooks/executors/llm.py tests/test_llm_usage.py tests/test_llm_executor.py
```

---

### 5.4 C4 — `feat: execute narrowed agent task steps`

#### T-8 — `AgentTaskExecutor` (parallel-safe; owns `agent_task.py` only)

*Red:* `tests/test_agent_task_executor.py` (new).

- `test_child_policy_is_the_three_way_intersection` — parent grants `{a,b,c}`, child profile `{b,c,d}`, step narrowing `{c,d}`; assert the child principal's `aq_commands` is exactly `{c}` and `provenance` ends with `agent_task:<step_id>`.
- `test_child_cannot_widen_in_any_namespace` — parameterised over `harness_tools`, `aq_commands`, `plugin_tools`; a child profile broader than the parent yields the intersection, never the union.
- `test_ai_parent_requires_the_child_profile_to_be_a_subset` — `unauthorized`, and `create_task` never called.
- `test_child_task_id_is_persisted_before_the_run_is_paused` — a repository double that records ordering; assert `child_task_id` is present in the committed snapshot *and* the receipt before `lifecycle == "paused"` is observable.
- `test_duplicate_child_completion_is_a_noop` — deliver `ChildTaskCompleted` twice; assert one resume receipt and one transition.
- `test_wait_for_completion_false_advances_on_dispatched`.
- `test_child_timeout_takes_the_timed_out_edge`.
- `test_cancel_child_defaults_to_false` — cancelling the parent leaves the child task's status untouched; the opt-in case cancels it.
- `test_cancellation_grants_no_new_authority` — the cancel path's `cancel_task` call carries the **narrowed** child principal, not the parent's.

*Green:* `src/playbooks/executors/agent_task.py` (`LiveAgentTaskExecutor`, `SymbolicAgentTaskExecutor`) and the `ChildTaskCompleted` branch of `PlaybookEngine.resume`.

*Commit C4 verify:*
```bash
aq test tests/test_agent_task_executor.py tests/test_v2_engine.py -q
ruff check src/playbooks/executors/agent_task.py tests/test_agent_task_executor.py
```

---

### 5.5 C5 — `feat: add bounded dry run and side effect free shadow mode`

#### T-11 — bounded dry-run on the real graph

*Red:* `tests/test_v2_dry_run.py` (new).

- `test_live_and_dry_run_select_the_same_edges` — the parity assertion of §4.10; ordered `(step_id, outcome, target)` triples must be equal.
- `test_deterministic_executors_are_identical_across_modes` — object identity for `decision`, `foreach`, `terminal` (§3.1.2).
- `test_path_limit_truncates_without_reporting_completed` — an artifact with a 6-way decision inside a loop, `max_paths=4`; assert `truncated is True` and no path is `completed`.
- `test_visit_limit_truncates` — `max_step_visits=10` over a 40-item loop.
- `test_command_without_preview_is_unresolved_and_downstream_refs_stay_symbolic`.
- `test_llm_forks_across_declared_outcomes` — the fixture's `classify-risk` has 7 declared edges; assert 7 child paths.
- `test_wait_is_reported_not_persisted` — the `WaitRepository` double raises on `register`; the dry-run completes.
- `test_invoke_ai_true_still_previews_commands` — the command adapter double raises on `invoke`; the dry-run with `invoke_ai=True` completes.
- `test_dry_run_writes_nothing` — `RunRepository`/`WaitRepository` doubles raise on every method.

#### T-12 — shadow mode has zero side effects, structurally and behaviourally

*Red:* `tests/test_v2_shadow.py` (new).

- `test_every_shadow_executor_declares_no_side_effects` — `all(e.no_side_effects for e in EXECUTORS[SHADOW].values())`, asserted on the **class attribute**, so it holds without running anything.
- `test_shadow_makes_no_command_ai_task_gate_or_bus_call` — doubles for `CommandHandler.execute`, `LLMClient.complete`, `create_task`, `gate_create` and `EventBus.emit` that each raise `AssertionError`; a full shadow dispatch over the §6.2 corpus still produces a complete `DispatchResult`.
- `test_shadow_does_not_call_a_preview_adapter_either` — the distinguishing case against dry-run (§4.3): a preview double that raises; shadow completes.
- `test_shadow_records_rules_selected_and_commands` — the two fields Package 6's parity harness reads; assert names, order and canonicalised args.
- `test_shadow_and_live_select_the_same_rules_for_the_corpus` — over §6.2, `shadow.rules_selected == live.rules_selected` for every event.

*Green:* `PreviewCommandExecutor`/`ShadowCommandExecutor` split, `SymbolicLlmExecutor`, `SymbolicAgentTaskExecutor`, `ReportingWaitExecutor`, `InMemoryRunRecorder`, `DryRunTree`, and the `EXECUTORS` table for all three modes.

*Commit C5 verify:*
```bash
aq test tests/test_v2_dry_run.py tests/test_v2_shadow.py -q
ruff check src/playbooks/executors src/playbooks/engine.py tests/test_v2_dry_run.py tests/test_v2_shadow.py
```

---

### 5.6 C6 — `test: prove restart and idempotency boundaries`

#### T-10 — ambiguous interruption stops rather than guessing

*Red:* `tests/test_v2_restart_resume.py` (new), part 1.

- `test_retry_safe_command_replays_with_the_same_attempt_key` — assert the replay carries `<run>:<step>:1`, not `:2`, and that two receipts exist (`interrupted` then the replay).
- `test_non_retry_safe_command_pauses_for_an_operator` — `operator_decision_required`, no binding, no transition, and the adapter not called a second time.
- `test_each_operator_resolution_is_receipted` — parameterised over `accept`/`retry`/`fail`/`cancel`.
- `test_a_playbook_principal_cannot_resolve_its_own_run` — `unauthorized` (§7.5).

#### T-15 — restart at all five boundaries

*Red:* `tests/test_v2_restart_resume.py`, part 2. Parameterised over **command, LLM, agent-task, wait, loop**: build the snapshot as a crash would leave it, construct a *fresh* `PlaybookEngine` against the same repository, resume, and assert (a) no duplicate acknowledged attempt, (b) bindings intact, (c) the run reaches its terminal.

- `test_restart_mid_loop_resumes_the_same_iteration` — §4.7.
- `test_restart_before_the_wait_boundary_re_registers_the_wait_once`.
- `test_restart_after_child_task_creation_does_not_create_a_second_child`.
- `test_receipts_identify_every_traversed_node_edge_iteration_and_artifact` — walks the full receipt set and asserts it reconstructs the executed path exactly, which is what Package 5's overlay depends on.

Two of these need a **real process boundary**, not a fresh object: `test_restart_mid_loop_...` and `test_restart_after_child_task_creation_...` run the engine in a `multiprocessing` child against a shared SQLite file, `SIGKILL` it at a scripted boundary, and resume in the parent. They carry `@pytest.mark.integration` because `aq test` deselects `integration` by default; §12.2's package sweep passes `-m integration` explicitly for this file. The other parameterisations use a fresh in-process engine and stay in the default selection.

*Green:* the restart branch of `PlaybookEngine.resume`, `OperatorResolution` handling, and `PlaybookEngine.resume`'s interrupted-attempt detection.

*Commit C6 verify:*
```bash
aq test tests/test_v2_restart_resume.py -q
aq test tests/test_v2_restart_resume.py -m integration -q
ruff check src/playbooks/engine.py tests/test_v2_restart_resume.py
```

---

### 5.7 C7 — `feat: route v2 playbook entry points behind a flag`

#### T-16 — six sites, one selector, V1 untouched when the flag is off

*Red:* `tests/test_v2_entry_points.py` (new).

- `test_every_site_reaches_the_engine_when_the_flag_is_on` — parameterised over §4.1's six rows; patch `PlaybookRunner` and `PipelineRunner` with mocks that raise on call, set `playbooks.v2_engine=True`, drive the site, assert the engine was called.
- `test_every_site_uses_v1_when_the_flag_is_off` — the mirror; assert the engine double was **not** called. This is the rollback-boundary test: Package 4 must be disable-able without converting stored V1 runs.
- `test_resume_sites_branch_on_the_run_not_the_flag` — a paused row with a V2 artifact hash resumes through the engine even with the flag off, and a row without one resumes through V1 even with it on. Package 7 §3.4 depends on this shape existing; landing it here means Package 7's switch is a config change, not a redesign.
- `test_workflow_stage_resume_is_wired` — site 5, the roadmap omission (§2.1).

*Green:* `v2_engine_enabled(config)` in `src/playbooks/services.py`, the four config fields (§9), and the preamble at each of the six sites.

#### T-17 — assignment routing keeps its caches and gains artifact identity

*Red:* `tests/test_assignment_routing_v2.py` (new).

- `test_cache_key_includes_the_artifact_hash` — two activations of different artifacts for the same tasks/options produce different `_batch_key` values. **Fails today:** `_batch_key` (`assignment_routing.py:389`) hashes only project, playbook, tasks and options hash.
- `test_a_cache_hit_creates_no_run_and_no_receipts` — second call with an unchanged catalog; assert zero new runs and zero new receipts.
- `test_unavailable_activation_returns_a_typed_result_not_an_exception` — assert `_note_failure` was called with the existing backoff and no traceback escaped.
- `test_routing_run_creates_no_projection_task` — the `project_task=False` path.
- `test_existing_coordinator_behaviour_is_unchanged` — re-runs `tests/test_assignment_routing_coordinator.py`'s cases with the flag on and asserts identical decisions.

*Green:* the `_route_batch` swap (§4.12), the `_batch_key` payload addition, and `run_rule(..., project_task=False)`.

*Commit C7 verify:*
```bash
aq test tests/test_v2_entry_points.py tests/test_assignment_routing_v2.py -q
aq test tests/test_assignment_routing_coordinator.py tests/test_playbook_commands.py \
        tests/test_playbook_resume_handler.py tests/test_dry_run_playbook.py \
        tests/test_cancel_playbook_run.py -q
ruff check src/orchestrator src/commands/playbook_commands.py src/playbooks src/workflow_stage_resume_handler.py
```

---

## 6. Fixture data

### 6.1 The artifact — reused verbatim from Package 5

`tests/fixtures/playbooks/v2/review-pipeline.artifact.json`, defined in full in Package 5's plan §10.1. **Package 4 does not fork it, extend it, or write a second engine-specific artifact.** Reusing the same bytes is what makes "the engine executed the graph the API renders" true by construction rather than by review: if Package 4 needed a differently shaped artifact to execute, that difference would be a real semantic disagreement and should surface as a failing test in one package, not as two fixtures that quietly diverge.

What it already gives this package, per step kind:

| Package 4 needs | The fixture supplies |
|---|---|
| Two rules on two events | `review-on-task-completed` (`task.completed`), `sweep-on-spec-approved` (`spec.created`) → T-1's multi-run dispatch |
| `CommandStep` with a template input | `ensure-review-task` (`ensure_task`) → T-2 |
| `LlmStep` with a budget and every reserved edge | `classify-risk`: `max_calls: 2`, `max_output_tokens: 1024`, `max_total_tokens: 8000`, `timeout_seconds: 120`; edges `low`, `high`, `invalid_output`, `budget_exceeded`, `provider_error`, `timed_out`, `cancelled` → T-13/T-14 and the 7-way dry-run fork |
| `AgentTaskStep` with `cancel_child: false` | `escalate` (`wait_for_completion: true`, `timeout_seconds: 3600`) → T-8 |
| `WaitStep` with a typed correlation key | `await-approval` (`binding_ref` → `review.task_id`, `timeout_seconds: 86400`) → T-6 |
| `ForEachStep` with `failure_policy: collect` | `for-each-task` over `downstream.tasks`, item binding `task`, `body_entry: open-gate`, `continuation: sweep-done` → T-7 |
| A body that re-enters the loop node via a decision | `open-gate` → `check-gate` → `for-each-task` (both case and default) → the §4.7 iteration-outcome rule |
| Convergence and a loop-back edge | `classify-risk:low` and `escalate:completed` → `await-approval`; `await-approval:revise` → `ensure-review-task` → T-11's path bounds |
| Three terminals in one rule | `review-unavailable`, `cancelled-end`, `done` → T-5's lifecycle mapping |

**One addition Package 4 makes, in its own file.** T-2 needs a command with *two* failure-classified outcomes routed to different steps, which the shared fixture does not contain. Rather than mutate it, Package 4 adds `tests/fixtures/playbooks/v2/two-failure-outcomes.artifact.json`: a single rule, one `CommandStep` on `list_tasks` whose contract double declares `ok` (SUCCESS), `not_found` (FAILURE) and `conflict` (FAILURE), with `transitions: {ok: done, not_found: no-tasks, conflict: retry-later, runtime_error: failed-end}`. Four terminals. That is the smallest artifact that makes classification-based routing fail.

### 6.2 Event corpus — `tests/fixtures/playbooks/v2/engine-events/*.json`

Six realistic bus payloads, matching `src/event_schemas.py`'s declared required/optional fields for each type. They are the input to T-1, T-12 and every mode-parity assertion.

| File | Event | Purpose |
|---|---|---|
| `task-completed-code.json` | `task.completed` with `task_type: "code"` | Both fixture rules' guards: rule 1 matches |
| `task-completed-docs.json` | `task.completed` with `task_type: "docs"` | Rule 1's filter rejects — the negative guard case |
| `spec-approved.json` | `spec.approved` | Rule 2 matches |
| `task-completed-and-spec-created.json` | a synthetic event carrying both types' fields | Not a real bus event; drives `test_two_matching_rules_produce_two_runs` deterministically without depending on two dispatches |
| `spec-approved-empty-downstream.json` | `spec.approved` where `list_tasks` returns `[]` | T-7's empty-collection path |
| `task-completed-no-project.json` | `task.completed` with `project_id` absent | The `record_token_usage` skip path (§4.11) and the `sync_playbook_run_task` no-project branch |

Each carries a stable `event_id` so idempotency assertions are reproducible.

### 6.3 Contract doubles — `tests/fixtures/contracts/engine_contracts.py`

The engine tests must not depend on Package 1's *real* `ensure_task` behaviour, or a Package 1 change breaks Package 4's suite for reasons unrelated to the engine. The doubles are real `CommandContract` objects over toy Pydantic models, registered into a **fresh `ContractRegistry()`** per test (Package 1's `register()` refuses replacement, so the module singleton is never mutated):

```python
class EnsureTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    title: str
    dedup_key: str | None = None

class EnsureTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    created: bool

ENSURE_TASK = CommandContract(
    execution=ExecutionContract(
        name="ensure_task",
        args_model=EnsureTaskArgs, result_model=EnsureTaskResult,
        outcomes=(OutcomeSpec(name="created", classification=OutcomeClass.SUCCESS),
                  OutcomeSpec(name="reused",  classification=OutcomeClass.SUCCESS),
                  OutcomeSpec(name="rejected",classification=OutcomeClass.FAILURE)),
        capability="ensure_task",
        side_effect=SideEffectClass.CREATE,
        idempotency=IdempotencySpec(mode="keyed", key_field="dedup_key"),
        retry_safe=True,
        receipt_projection=("task_id", "created"),
    ),
    presentation=CommandPresentation(title="Ensure a review task exists",
                                     summary="Create or reuse the matching task"),
)
```

Plus `LIST_TASKS` (`retry_safe=True`, `idempotency.mode="natural"`, `receipt_projection=("count",)`), `GATE_CREATE` (`retry_safe=False`, `idempotency.mode="none"` — the non-retry-safe case T-10 needs), and `TWO_FAILURES` (§6.1). A scripted `invoke` adapter returns queued `CommandResult`s and records every call, so `test_two_failure_outcomes_take_different_edges` and every "adapter was not called" assertion have one recording surface.

### 6.4 Snapshot fixtures — `tests/fixtures/playbooks/v2/snapshots/*.json`

Five hand-written `RunSnapshot` payloads representing the exact states a crash leaves behind, one per T-15 parameterisation: `mid-command.json` (started receipt, no completion), `mid-llm-turn.json` (one completed tool turn, one in flight), `mid-agent-task.json` (`child_task_id` set, lifecycle still `running`), `pre-wait.json` (wait computed, `register` never returned), `mid-loop.json` (`{index: 1, aggregate: [one entry]}`). Hand-written rather than captured, so the test asserts against a *stated* expectation of what a crash looks like instead of against whatever the implementation happened to write.

---

## 7. Security analysis

Package 4 introduces the first V2 code that actually invokes commands, calls models, and creates tasks. Five boundaries.

### 7.1 The engine is not an authorization decision point

Every command invocation goes through Package 1's registration adapter, which reaches `CommandHandler.execute`, where Package 0's `authorize_command` already runs. §3.4 step 5 calls `authorize_command` *additionally*, before the adapter, so an artifact can route a denial to a visible edge — but the engine never grants. There is no code path in `src/playbooks/` that constructs an `ExecutionPrincipal` with a policy wider than the one it received. T-16's `test_engine_never_widens_a_policy` asserts it statically: `grep`-style AST check that `narrow(` is the only `CapabilityPolicy`-producing call in `src/playbooks/`, and that `CapabilityPolicy(` and `.union(` appear nowhere in the package.

### 7.2 Delegation narrows three ways and cannot widen

§4.5 step 1. The intersection is computed with `CapabilityPolicy.intersect`, which is the only transform the type exposes (Package 0 documents that there is no widening method). The AI-parent subset rule is checked separately because intersection alone would silently *narrow* a too-broad child rather than refusing it, and the spec says the child's profile "must be a capability subset of the parent AI principal" — a requirement, not a clamp. T-8's `test_ai_parent_requires_the_child_profile_to_be_a_subset`.

### 7.3 A model-visible tool list is a projection, never the gate

The `LlmStep`'s published tool schemas are `step.tools ∩ principal.policy.aq_commands`. A model that calls a tool outside that list is denied at `authorize_command`, exactly as an off-list call from any other principal would be. The projection exists to reduce confusion, not to enforce. T-14's `test_tool_calls_are_authorized_at_dispatch_not_by_the_published_schema` publishes a narrow list and then calls outside it, which is the only way to prove the two are independent.

### 7.4 Cancellation grants nothing

`PlaybookEngine.cancel(..., cancel_children=True)` issues the child cancellation with the **narrowed child principal** recorded on the snapshot, not the caller's. A parent whose own authority has since been reduced cannot use cancellation as a way to act on a child it could no longer create. `cancel_child` defaults to `False`, so shared or reused child work is never killed implicitly. T-8's `test_cancellation_grants_no_new_authority`.

### 7.5 Operator resolution is an operator action

`OperatorResolution` requires the `playbook_admin` AQ capability and is refused for `PrincipalKind.PLAYBOOK`. Without that rule, a playbook that reached `operator_decision_required` could call `playbook_run_resolve` on itself and accept an outcome nobody verified — turning "stop and ask a human" into "make one up". T-10's `test_a_playbook_principal_cannot_resolve_its_own_run`.

### 7.6 Redaction is default-deny and is not the executor author's judgement call

§3.3.4. One helper, three call sites, one test that walks every fixture receipt. The failure mode this guards against is an executor author adding `receipt_inputs=resolved_inputs` for debuggability and leaking an API token that a contract marked sensitive. `project_for_receipt` cannot express that: it takes an allow-list, not a deny-list.

### 7.7 Non-goals, stated so they are not assumed

- Package 4 does not audit the **content** of an LLM's output for prompt injection. The mitigation the design relies on is structural: the output must validate against a declared schema, and only a declared enum field selects an edge. Prose in the response cannot move the run (T-14's adversarial-prose case).
- Package 4 does not sandbox command side effects. A contracted command with `SideEffectClass.DELETE` deletes; the control is which capability an artifact's principal holds, which is Package 0's.

---

## 8. Observability and operator failure behaviour

### 8.1 What the operator sees when a run stops

Every stop is a receipt plus a run status, and there are exactly six shapes. Each names the next action, because "the run failed" without one is what makes an operator restart something they should have inspected.

| Situation | Status | Receipt outcome | Reason field | Operator action |
|---|---|---|---|---|
| Contract or profile fingerprint moved mid-run | `failed` | `contract_violation` | `execution_contract_changed` + the changed dependency name | Rebuild and review the artifact; start a **fresh** run. The old run is never resumed against a new graph |
| Plugin transiently gone | `paused` | `unavailable` | `dependency_unavailable` + provider name | Restore the provider; the run resumes against the same artifact |
| Ambiguous interruption of a non-retry-safe command | `paused` | `interrupted` | `operator_decision_required` + the attempt key | One explicit resolution (§4.8) |
| Budget breach | `failed` (or the mapped edge) | `budget_exceeded` | which limit, and the observed usage | Raise the budget in the source and rebuild, or accept the mapped edge |
| Structured output invalid after retries | mapped edge | `invalid_output` | the validation error path, not the model's text | Fix the schema or the prompt in the source |
| Result or snapshot too large | `failed` | `state_limit_exceeded` | which limit, and the observed size | Narrow the step's declared output |

### 8.2 Bus events and metrics

New bus events, all emitted **after** a successful `commit_boundary` (§3.4 step 11): `playbook.v2.run.started`, `playbook.v2.step.completed`, `playbook.v2.run.paused`, `playbook.v2.run.resumed`, `playbook.v2.run.finished`. Payloads carry `run_id`, `dispatch_id`, `rule_id`, `step_id`, `attempt`, `artifact_sha256`, `outcome` — and no resolved values, since a bus payload is not receipt-redacted.

Two timings Package 7's cutover acceptance table reads by name, so they are fixed here:

- **`playbook.dispatch_ms`** — event arrival → the first run's first `commit_boundary`.
- **`playbook.resume_ms`** — the causing event's arrival → the resumed run's next `commit_boundary`.

Both go through the existing `metrics.tick` bus convention (`src/metrics/sampler.py`), never `log_event`, per CLAUDE.md.

Package 7 also reads a **`RunRepository.commit_boundary` conflict counter** (its acceptance measure 5). Package 3 owns the counter; Package 4's obligation is only that it never swallows a `SnapshotVersionConflict` (§3.3.1), which is what makes the counter meaningful.

Logs carry `artifact_sha256`, `rule_id`, `run_id`, `step_id`, `attempt`, `principal_kind`, `profile_id` and `contract_fingerprint`, per roadmap §10, inside the existing `CorrelationContext(run_id=...)` the V1 runners already use (`core.py:1005`, `playbook_commands.py:955`).

---

## 9. Feature flags — owner, default, removal package

All four live on `PlaybooksConfig` (`src/config.py`), which today is `enabled: bool = False` plus a `validate()` returning `[]`.

| Field | Default | Meaning | Removal |
|---|---|---|---|
| `v2_engine: bool` | `False` | Makes the V2 path reachable at the six §4.1 sites. **This is Package 4's entire rollback boundary:** setting it `False` restores V1 with no database downgrade, because V2 runs live in Package 3's separate tables | **Package 7**, commit 4 (`refactor: remove v1 playbook execution runtime`), where it is replaced by `playbooks.runtime` |
| `v2_dry_run_max_paths: int` | `32` | Roadmap default | Permanent — an operational bound, not a migration flag |
| `v2_dry_run_max_step_visits: int` | `1000` | Roadmap default | Permanent |
| `cancellation_grace_seconds: int` | `30` | §4.9 | Permanent |

`PlaybooksConfig.validate()` gains: both dry-run bounds must be `>= 1`; `cancellation_grace_seconds` must be `>= 0`; and `v2_engine` may not be `True` while `enabled` is `False`, because the whole playbook subsystem is paused by that flag and a half-enabled state would make "the engine did nothing" ambiguous. `tests/test_config.py` gains one case per rule.

Package 7's plan §3.8 lists `playbooks.v2_api` and `playbooks.v2_activation_writes` as its own removals; `v2_engine` is added to that list by this plan and must appear in Package 7's D1-c reconciliation.

---

## 10. Storage — per-tool-turn receipt amendment

Package 3 owns `playbook_artifacts`, `playbook_activations`, the V2 run/snapshot table, `playbook_step_receipts`, `playbook_waits` and `playbook_pending_events`. Package 4 changes no V1 table and adds no new V2 table, but C3 requires one daemon-only additive revision because Package 3 shipped a one-receipt-per-attempt constraint.

The revision chains after the current head and adds to `playbook_step_receipts`:

| Column | Definition | Compatibility |
|---|---|---|
| `receipt_kind` | `TEXT NOT NULL DEFAULT 'step'`, checked in `step|tool_turn|llm_call|interrupted|operator_decision` | every existing receipt is an ordinary final step boundary |
| `turn_index` | `INTEGER NOT NULL DEFAULT -1` | `-1` preserves the ordering and identity of every single-receipt attempt |
| `operator_decision_id` | nullable `TEXT` | existing receipts have no decision |

It replaces `uq_playbook_step_receipts_attempt` with `uq_playbook_step_receipts_boundary` on `(run_id, step_id, iteration, attempt, turn_index, receipt_kind)` and adds `idx_playbook_step_receipts_turn` on `(run_id, step_id, iteration, attempt, turn_index)`. Repository reads order first by `snapshot_version`, then `turn_index`, `started_at`, and `receipt_id`. The downgrade is supported only after deleting post-amendment `tool_turn`, `llm_call`, `interrupted`, and `operator_decision` rows; it then restores the old constraint. The application never runs this migration from a worker scope—only `aq db upgrade` in daemon/operator scope applies it.

### 10.1 Fields Package 4 requires from Package 3

| Field | On | Used by |
|---|---|---|
| `lifecycle` admitting `cancelling` | V2 run table | §4.9 |
| `cancel_requested: bool`, `cancel_ack_state: text\|null` | snapshot | §4.9 |
| `version: int` (optimistic concurrency) | snapshot | §3.3.1 |
| `loop_frame: json\|null` with `{step_id, index, item_binding, aggregate, last_body_step_id, last_body_outcome}` | snapshot | §4.7 |
| `llm_transcript: json\|null` | snapshot | §4.4 tool turns |
| `child_task_id: text\|null` | snapshot **and** receipt | §4.5 |
| `usage_input_tokens`, `usage_output_tokens`, `usage_reported: bool` | receipt | §4.11 |
| `receipt_kind: text`, `turn_index: int`, `operator_decision_id: text\|null` | receipt | §4.4 durable tool turns and §4.8 interruption linkage |
| `deadline_fired: text\|null`, `cancellation: text\|null` | receipt | §3.3.3 |
| `iteration_index: int\|null`, `attempt: int`, `idempotency_key: text` | receipt | §3.3.2 |
| Unique index on `(playbook_id, rule_id, event_id)` where `event_id IS NOT NULL` | V2 run table | §4.2 |

The existing V1 index is `(playbook_id, event_id)` (`src/database/tables.py:946`) and the existing status check constraint is `('running','paused','completed','failed','timed_out','cancelled')` (`:955`) — **neither is altered by Package 4**. V1 rows keep V1 semantics so historical runs stay readable after Package 7.

### 10.2 The conditional engine-field revision, if Package 3 ships without them

Only if §2.4's reconciliation shows a field above is missing. One revision with the slug `playbook_v2_engine_fields`, chaining after the then-current head:

```python
def upgrade() -> None:
    with op.batch_alter_table("playbook_run_snapshots") as b:
        b.add_column(sa.Column("cancel_requested", sa.Boolean(), nullable=False,
                               server_default=sa.text("0")))
        b.add_column(sa.Column("cancel_ack_state", sa.Text(), nullable=True))
        b.add_column(sa.Column("loop_frame", sa.Text(), nullable=True))
        b.add_column(sa.Column("llm_transcript", sa.Text(), nullable=True))
        b.add_column(sa.Column("child_task_id", sa.Text(), nullable=True))
    with op.batch_alter_table("playbook_step_receipts") as b:
        b.add_column(sa.Column("usage_input_tokens", sa.Integer(), nullable=False,
                               server_default=sa.text("0")))
        b.add_column(sa.Column("usage_output_tokens", sa.Integer(), nullable=False,
                               server_default=sa.text("0")))
        b.add_column(sa.Column("usage_reported", sa.Boolean(), nullable=False,
                               server_default=sa.text("0")))
        b.add_column(sa.Column("deadline_fired", sa.Text(), nullable=True))
        b.add_column(sa.Column("cancellation", sa.Text(), nullable=True))
        b.add_column(sa.Column("child_task_id", sa.Text(), nullable=True))

def downgrade() -> None:
    # Mirror image; every column is nullable or server-defaulted and additive,
    # so the downgrade loses V2 engine detail and nothing else. V1 tables are
    # untouched in both directions.
    with op.batch_alter_table("playbook_step_receipts") as b:
        for c in ("child_task_id", "cancellation", "deadline_fired", "usage_reported",
                  "usage_output_tokens", "usage_input_tokens"):
            b.drop_column(c)
    with op.batch_alter_table("playbook_run_snapshots") as b:
        for c in ("child_task_id", "llm_transcript", "loop_frame",
                  "cancel_ack_state", "cancel_requested"):
            b.drop_column(c)
```

`src/database/tables.py` is edited **in the same commit** as the revision, per CLAUDE.md. Storage is the one place where "add it if it is missing" would otherwise become two competing schemas, so the escalation rule in §2.4 applies with full force: prefer amending Package 3's revision over adding this one, and add this one only when Package 3 has already merged.

Migration tests upgrade a database containing a Package 3 `step/-1` receipt, verify it remains readable, insert two `tool_turn` rows for the same attempt at distinct indices, reject a duplicate boundary, and verify ordering. They exercise Alembic through a test-owned connection; workers never run `upgrade` or `stamp` against their configured database.

### 10.3 The `cancelling` lifecycle value

Whether it is a CHECK constraint or an application-level enum is Package 3's decision. Package 4's requirement is only that the value round-trips. If Package 3 used a CHECK constraint listing the six V1 statuses, the conditional revision above also drops and recreates it with seven — and because PostgreSQL and SQLite differ on constraint alteration, that must go through `batch_alter_table`, which is why every statement above already does.

---

## 11. SQLite and PostgreSQL

- **Every DDL above is additive and uses `batch_alter_table`**, which is the project's existing pattern for SQLite's lack of `ALTER COLUMN`. `server_default=sa.text("0")` for booleans matches the codebase's existing boolean-default convention (`tests/test_migration_boolean_defaults.py` exists precisely to police it).
- **No enum types.** Lifecycle and outcome are `Text`, matching every existing status column. A PostgreSQL `CREATE TYPE`/`DROP TYPE` pair would need separate up/down handling and would make the downgrade lossy.
- **The engine's atomicity requirement is `commit_boundary`'s, not the engine's own.** Package 4 opens no transaction; it calls one repository method. On SQLite that is `db.immediate()` (the pattern at `assignment_routing.py:519`); on PostgreSQL it is an ordinary transaction with `SELECT … FOR UPDATE` on the snapshot row. Both satisfy §3.3.1's version check.
- **Postgres is production** (project convention; local instance on `:5533`). The restart tests in T-15 use a shared **SQLite file** because they need a `SIGKILL`-able child process with a trivially shareable database; the *concurrency* assertions (`SnapshotVersionConflict`, the wait race) additionally run against PostgreSQL, where they are meaningful — SQLite's single-writer locking can mask a lost-update bug that PostgreSQL's `READ COMMITTED` exposes. Concretely, `tests/test_v2_waits.py` and `tests/test_v2_restart_resume.py` are parameterised over the project's existing database fixture so CI's PostgreSQL job covers both.
- **`asyncio.timeout` and `SIGKILL` are POSIX-shaped.** The integration-marked restart tests are skipped on non-POSIX platforms with an explicit `pytest.mark.skipif`, not silently.

---

## 12. Verification

### 12.1 Per-task

Each task's *Verify* block in §5. Iterate with `-x` on the single file being changed.

### 12.2 Package sweep — run once, before the exit gate

```bash
# New suites
aq test tests/test_v2_engine.py tests/test_command_executor.py tests/test_v2_receipts.py \
        tests/test_v2_waits.py tests/test_v2_foreach.py tests/test_v2_cancellation.py \
        tests/test_llm_usage.py tests/test_llm_executor.py tests/test_agent_task_executor.py \
        tests/test_v2_dry_run.py tests/test_v2_shadow.py tests/test_v2_restart_resume.py \
        tests/test_v2_entry_points.py tests/test_assignment_routing_v2.py -q

# The restart coverage the default marker set deselects
aq test tests/test_v2_restart_resume.py -m integration -q

# Existing suites this package must not regress (V1 stays authoritative)
aq test tests/test_playbook_runner.py tests/test_pipeline_runner.py \
        tests/test_playbook_commands.py tests/test_playbook_resume_handler.py \
        tests/test_playbook_run_idempotency.py tests/test_playbook_state_machine.py \
        tests/test_dry_run_playbook.py tests/test_cancel_playbook_run.py \
        tests/test_playbook_run_events.py tests/test_playbook_run_bus_events.py \
        tests/test_assignment_routing_coordinator.py tests/test_default_pipeline.py \
        tests/llm tests/test_config.py -q

# Orchestrator seam
aq test tests/ -k "playbook and (trigger or dispatch)" -q

ruff check src/playbooks src/orchestrator src/commands src/llm tests
```

Expected outcome: all green. `aq test` exit code 75 means no test slot was free — retry; it is not a failure.

### 12.3 What is deliberately *not* run

No full-repo `pytest`. Package 4 adds no API surface, so `openapi.json` does not move and neither generated client needs regeneration — if a change in this package *does* move `openapi.json`, that is a signal the package has grown an API surface it should not have, and §1.2 applies. No dashboard tests: Package 5 owns every dashboard file.

---

## 13. Mapping to the roadmap's Package 4 exit gate

> **Every V2 step kind runs through one engine with durable boundaries. Live, dry-run, and shadow modes traverse the same graph, and shadow mode can compare decisions without producing side effects.**

| Exit-gate clause | Proof |
|---|---|
| *Every V2 step kind* | `tests/test_v2_engine.py::test_every_step_kind_has_a_registered_executor` (parameterised over the seven discriminator values in `src/playbooks/definition.py`, so adding an eighth step type fails this test) + the seven executors' own suites |
| *runs through one engine* | T-16's six-site parameterisation; §7.1's static check that no second traversal exists in `src/playbooks/` |
| *with durable boundaries* | T-3 (one commit per boundary, no write before the boundary, no retry on conflict), T-14 (ordered LLM turn boundaries), T-15 (restart at command, LLM, agent-task, wait and loop) |
| *Live, dry-run, and shadow traverse the same graph* | T-11's `test_live_and_dry_run_select_the_same_edges`; T-12's `test_shadow_and_live_select_the_same_rules_for_the_corpus`; §3.1.2's object-identity assertion for the three deterministic executors |
| *shadow can compare decisions* | T-12's `test_shadow_records_rules_selected_and_commands` — the two `DispatchResult` fields Package 6's parity harness consumes |
| *without producing side effects* | T-12's class-attribute assertion (structural) **and** the five raising doubles (behavioural), plus §3.3.5's absence of `RunRepository` from shadow's `EngineServices` |

### 13.1 Roadmap Required-outcome checklist

| Roadmap outcome | Where |
|---|---|
| Start exactly one durable run per matching rule | §4.2, T-1 |
| Execute `CommandStep` only through its registered contract and handler | §4.3, T-2 |
| Validate runtime arguments again at the command boundary | §4.3 step 2, T-2 |
| Execute `LlmStep` with profile, schema, budget, timeout, receipt | §4.4, T-14 |
| Record provider-reported usage; conservative estimates otherwise | §4.11, T-13 — **and the fail-closed rule for hard total-token budgets** |
| Execute `AgentTaskStep` with an intersection-narrowed principal | §4.5, §7.2, T-8 |
| Persist child identity before suspension; reconcile idempotently | §4.5 steps 3 and 5, T-8 |
| Propagate cancellation without granting authority | §4.9, §7.4, T-8/T-9 |
| Evaluate decisions and templates without arbitrary code execution | §4.14, T-5 |
| Persist loop cursor and scope on both sides of each body transition | §4.7, T-7 |
| Event waits, timer waits, timeout edges, restart-safe matching | §4.6, T-6, T-15 |
| Explicit success/failure/retry/cancellation/terminal outcomes | §3.6, §4.9, §4.14 |
| Stop for operator action on ambiguous external outcomes | §4.8, T-10 |
| Bound dry-run to 32 paths / 1,000 visits | §4.10, §9, T-11 |
| Preview executors against the real graph, contracts, validator, transitions | §3.1.2, §4.10, T-11 |
| Shadow with zero command, AI, task, gate or external side effects | §4.3, §3.3.5, T-12 |
| Pipeline and assignment routing on the same engine | §4.1 sites 1–3, §4.12, T-16/T-17 |
| Assignment-routing cache stays caller-owned; keys include artifact identity | §4.12, T-17 |
| Receipts and lifecycle events sufficient for overlays and parity | §3.3.3, §8.2, T-4 |

### 13.2 Rollback boundary

Setting `playbooks.v2_engine=False` returns every one of the six sites to its V1 body. No stored V1 run is converted, no V1 table is altered, and V2 rows remain for inspection in Package 3's separate tables. The one change that is *not* behind the flag is the provider usage channel (§4.11) — it is additive with a `None` default and is a strict improvement to the V1 path's accounting as well, so it is deliberately not gated. T-13 confirms `tests/llm` and `tests/test_playbook_runner.py` stay green with it in place.

---

## 14. Roadmap §9 quality-bar coverage

| Requirement | Where |
|---|---|
| Exact paths and symbols based on the live tree | §2.3 (observed vs expected, with line numbers), §2.4 reconciliation script |
| Test-first steps with the failing assertion described | §5 — every task states its red assertions and, where the failure is not an assertion failure, what it fails *with* (e.g. T-1's `ModuleNotFoundError`, T-13's `AttributeError`) |
| Representative fixture data, not placeholders | §6 — the Package 5 artifact reused verbatim, a six-event corpus, real `CommandContract` doubles, five hand-written crash snapshots |
| API request and response examples when the package changes an endpoint | **Not applicable, and deliberately so.** Package 4 adds no endpoint and does not move `openapi.json` (§1.2, §12.3). The one command surface it implies — `playbook_run_resolve` for §4.8 — is explicitly assigned to Package 5, whose plan owns its DTOs |
| Alembic upgrade and downgrade behaviour when the package changes storage | §10 — Package 4 adds the per-turn receipt columns and boundary uniqueness in one daemon-only revision; existing rows retain `step/-1/NULL`, and downgrade refuses while multi-boundary rows exist |
| SQLite and PostgreSQL considerations | §11 |
| Security analysis for any new boundary or identity flow | §7, seven subsections including two stated non-goals |
| Observability and operator failure behaviour | §8 — six operator-visible stop shapes, each with the next action; five bus events; the two timings Package 7 reads by name |
| Feature-flag ownership and named removal package | §9 — four fields, defaults, and Package 7 commit 4 named for `v2_engine` |
| Per-task verification commands and expected outcomes | §5 per task, §12.2 for the sweep |
| Small commit boundaries | §5.0 — eight commits, with the two additions to the roadmap's six recorded and justified |
| Explicit mapping to the package exit gate | §13, plus §13.1's Required-outcome checklist and §13.2's rollback boundary |

### 14.1 Open items this plan hands to other packages

Each is a decision Package 4 cannot make alone; each names its owner so it is not discovered at integration time.

1. **`invalid_output` vs `output_invalid`** — Package 7's plan §3.5 row 9 uses the wrong spelling. Owner: Package 7's reconciliation commit. §3.6.
2. **`playbook_run_resolve`** — the operator-resolution command and UI for §4.8. Owner: Package 5. Package 4 ships `PlaybookEngine.resume(..., OperatorResolution(...))` and its receipt; without Package 5's surface, the only way to resolve an ambiguous run is a direct command invocation, which is acceptable for the Package 4 exit gate but not for cutover.
3. **The three-column run idempotency index** `(playbook_id, rule_id, event_id)` — §4.2 depends on it. Owner: Package 3. If Package 3 shipped the two-column V1 shape on the V2 table, §10.2's conditional revision must also carry the index change, and `test_two_matching_rules_produce_two_runs` is the test that fails first.
4. **`v2_engine` in Package 7's D1-c removal list** — §9. Owner: Package 7.
