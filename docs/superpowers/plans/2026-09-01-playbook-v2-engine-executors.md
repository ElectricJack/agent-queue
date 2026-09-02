# Playbook V2 — Package 4: unified engine and typed executors

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`. §5 is a task list with red/green boundaries; §3 is a **frozen interface contract** that the three specialist executor tasks (T-COMMAND, T-LLM, T-AGENT) share. Do not renegotiate §3 inside a task — amend it in a dedicated commit that updates every dependent task in the same change.

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
