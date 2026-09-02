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

---

## 3. Locked interfaces — the parallelism contract

Roadmap §7: *"Package 4 executors may be developed independently after `Executor`, `CommandResult`, `RunRepository`, and receipt types are fixed."* This section **is** that fixing. T-COMMAND (§5.3), T-LLM (§5.5) and T-AGENT (§5.6) may run fully in parallel once §3 is checked in, because everything they share is here and nothing here is negotiable inside those tasks.

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

Package 4's rule: **`commit_boundary` is the only write the engine makes to run state, and it is called exactly once per step attempt.** Not zero times (an attempt with no receipt is invisible after a crash), not twice (two receipts for one attempt breaks the idempotency key). T-3's `test_exactly_one_commit_per_attempt` wraps the repository in a counting double and asserts `calls == len(receipts)` over a full run, including the failure and cancellation paths.

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
| `mode` | `ctx.mode` | Shadow and dry-run receipts exist **in memory only** (§3.3.5) |
| `principal_kind`, `profile_id`, `capability_fingerprint` | `ctx.principal`, `principal.policy.fingerprint()` | |
| `contract_fingerprint` | `CONTRACTS.fingerprint(step.command)` | `None` for non-command steps |
| `operation` | `ExecutorResult.operation` | |
| `outcome` | `ExecutorResult.outcome` | Validated against §3.6 before the write |
| `selected_transition` | engine | `(outcome_label, target_step_id)`; `None` for `SUSPEND`/`OPERATOR_DECISION` |
| `inputs` | `ExecutorResult.receipt_inputs` | Already redacted (§3.3.4) |
| `result` | `ExecutorResult.receipt_result` | Already redacted |
| `usage` | `ExecutorResult.usage` | `None` for non-LLM steps; §4.11 |
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
4. **Resolve inputs.** `resolve_value` (Package 2) over `step.inputs` against `ctx.scope`. A missing or type-invalid reference → `input_resolution_failed` **before** the executor runs. The engine never injects an `UNRESOLVED` marker and never coerces to `""`.
5. **Authorize.** For `CommandStep`, `authorize_command(name, ctx.principal)` (Package 0). A denial → `unauthorized`, error receipt, transition selection proceeds normally so an artifact can route a denial. The engine does **not** implement its own capability check.
6. **Execute.** `await executor_for(step.type, ctx.mode).execute(step, ctx)`, wrapped so that an unexpected exception becomes `runtime_error` with the exception type (not its message — a message can carry an argument value) in `diagnostics`.
7. **Validate the result.** `outcome ∈ declared ∪ ENGINE_RESERVED_OUTCOMES`; `control`/field coherence (`SUSPEND` requires `wait`; `GOTO` requires `goto_step_id ∈ step.declared_targets()`; `TERMINATE` requires `terminal_outcome`). Violation → `contract_violation`.
8. **Bind.** If `step.save_result_as`, `scope.with_binding(name, value)` — which raises on reassignment, because bindings are immutable. Then the 256 KiB check, then the 4 MiB whole-snapshot check; either breach → `state_limit_exceeded`.
9. **Select the transition.** `step.transitions[outcome]`, falling back to `step.transitions["runtime_error"]` **only** for a member of `ENGINE_RESERVED_OUTCOMES`. A business outcome with no edge is a `contract_violation`, not a silent completion — this is the replacement for `pipeline_runner.py:151-158`, where a missing `on_success` key ends the run as `completed`.
10. **Commit.** Build the next `RunSnapshot` (new step, new bindings, new loop frame, new version) and the `StepReceipt`, and call `commit_boundary(snapshot, receipt, wait_changes)` **once**. Steps 8–10 are the atomic unit; nothing between step 6 and step 10 writes anything durable.
11. **Emit.** After a successful commit only, emit `playbook.v2.step.completed` on the bus. An event before the commit would let a subscriber observe a step that a crash then un-happens.

Two properties fall out and are asserted directly:

- **Crash between 6 and 10 loses the attempt, never the run.** The stored snapshot still points at the step, `attempt` is unchanged, and the restart re-runs it. For a `retry_safe` command that is correct; for a non-retry-safe one, §4.8 stops and asks an operator. T-15.
- **The executor cannot skip a boundary.** There is no path from an executor back into the engine. An executor that wants to continue returns; it does not call the engine.

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
| **T-COMMAND** (§5.3) | `src/playbooks/executors/command.py`, `tests/test_command_executor.py` | `base.py`, `engine.py`, any other executor |
| **T-LLM** (§5.5) | `src/playbooks/executors/llm.py`, `tests/test_llm_executor.py`, and the usage channel (§4.11) in `src/llm/` | `base.py`, `engine.py`, `command.py`, `agent_task.py` |
| **T-AGENT** (§5.6) | `src/playbooks/executors/agent_task.py`, `tests/test_agent_task_executor.py` | `base.py`, `engine.py`, `command.py`, `llm.py` |

`base.py`, `engine.py`, `decision.py`, `wait.py`, `foreach.py`, `terminal.py` and `__init__.py` are all landed by T-1/T-5/T-6 **before** the three parallel tasks start. Each parallel task adds exactly one executor module, one test module, and one line to the `EXECUTORS` table. A merge conflict is therefore confined to that one table line, by construction.
