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

**Tool turns are durable.** When `step.tools` is non-empty, each completed tool turn is a `commit_boundary` with a `tool_turn` receipt and the transcript in the snapshot, so a restart mid-conversation resumes after the last completed turn. An **in-flight provider call is never replayed**: the restart writes an `interrupted` receipt and, per §4.8, requires an explicit retry attempt. T-14's `test_interrupted_provider_call_is_not_replayed`.

`SymbolicLlmExecutor` returns `UNRESOLVED` with `possible_outcomes` = the enum values of `output_schema[step.outcome_field]` plus every reserved outcome the step maps, and the engine forks across them (§4.10).

### 4.5 `AgentTaskExecutor` — `src/playbooks/executors/agent_task.py`

Distinct from `LlmStep` because it schedules, persists, waits, costs and cancels differently.

1. **Narrow the principal.** `child_policy = parent.policy.intersect(child_profile.policy).intersect(step.capability_narrowing)`; `child_principal = ctx.principal.narrow(child_policy, reason=f"agent_task:{ctx.step_id}")`. Three-way intersection, exactly the roadmap's "Delegated agent-task permissions are the intersection of parent permissions, child profile permissions, and explicit per-step narrowing." When the parent is itself an AI state, the spec additionally requires `child_profile.policy.is_subset_of(parent_ai_policy)`; a violation is `unauthorized` at execute time, not a silent narrowing. §7.2.
2. **Create the task** through the contracted `create_task` command with `child_principal`, so the child task's creation is authorized and receipted like any other command.
3. **Persist before suspending.** `ExecutorResult(control=SUSPEND, child_task_id=..., wait=WaitSpec(kind="task", correlation_key=task_id, deadline_at=now+step.timeout_seconds))`. The engine commits the boundary — snapshot, receipt and wait registration in one transaction — *before* the run is considered paused. A crash between task creation and the commit leaves an orphan child task and a run still at the step; §4.8's ambiguity rule applies, and the operator sees the orphan because the `create_task` receipt is already durable from the boundary that authorized it.
4. `wait_for_completion=False` returns `control=ADVANCE, outcome="dispatched"` instead, with no wait.
5. **Reconcile idempotently.** `resume(run_id, ChildTaskCompleted(task_id, status), ...)` maps the child's terminal status onto `completed` / `failed` / `timed_out` / `cancelled`. A second delivery of the same `(run_id, step_id, attempt, task_id)` is a no-op that writes no receipt — T-8's `test_duplicate_child_completion_is_a_noop`.
6. **Cancellation.** §4.9: `cancel_child` defaults to `False`, so cancelling a parent leaves the child running by default. The child is never granted authority by cancellation.

### 4.6 `WaitExecutor` and the wait scheduler — `src/playbooks/executors/wait.py`

The race the spec closes: an event arrives between "decide to wait" and "the pause is persisted".

`LiveWaitExecutor` computes the typed correlation key from `ctx.scope` and returns `control=SUSPEND`. The engine then, **in one transaction** (`commit_boundary` with `pending_wait_changes=[Register(spec)]`):

1. writes the snapshot with `lifecycle="paused"` and the wait fields;
2. writes the receipt;
3. `WaitRepository.register(spec, snapshot.version)`, which in the same transaction scans `playbook_pending_events` (the durable inbox) for an already-arrived match and, if found, returns `matched_immediately` so the engine resumes instead of pausing.

Package 3 owns `register`'s compare-and-set; Package 4 owns the rule that **event ingestion writes the inbox before matching waits**, never the other way round. T-6's three assertions: `test_event_before_registration_resumes_immediately`, `test_event_during_registration_resumes_exactly_once`, `test_event_after_registration_resumes_exactly_once` — all three end with exactly one resume receipt.

**Deadlines.** A dedicated `WaitScheduler` (in `engine.py`, not a new module) owns `deadline_at`. It polls `WaitRepository.due(now)` on the orchestrator cycle. It does **not** create `TimerService` entries: `src/timer_service.py:185` is a playbook-*trigger* scheduler whose entries are cron-like and operator-visible, and per-run waits are neither. The earlier of the wait deadline and the run deadline wins, and `StepReceipt.deadline_fired` records which (§3.3.3).

`ReportingWaitExecutor` (dry-run and shadow) returns `control=ADVANCE, outcome="<the wait's declared timeout-free outcome>"`… **no**: it returns `control=UNRESOLVED` with `possible_outcomes = set(step.transitions)`, and the dry-run tree marks the node `simulated` with reason `wait_not_persisted`. It never registers a wait. Spec: "waits are reported without persisting a pause."

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

Two rules the live `pipeline_runner._run_for_each` (`:165-186`) breaks and this one keeps:

- **The loop item lives in `scope.loop`, not `scope.bindings`.** `with_loop_item` returns a new scope; there is no `pop` and no `finally`, so a failure branch cannot read a stale item. T-7's `test_loop_item_cannot_shadow_a_binding` builds an artifact with a binding named `task` and an item binding named `task` and asserts Package 2 rejects it at compile time *and* that `with_loop_item` raises if it somehow reaches runtime.
- **The frame is committed on both sides of every body transition.** Entering iteration *n* and leaving it are two boundaries. A crash mid-body restarts iteration *n*, never *n+1*. T-15's `test_restart_mid_loop_resumes_the_same_iteration` kills the process between the two boundaries and asserts `iteration_index` is unchanged and the aggregate has *n* entries.

The aggregate binding is `{"items": [...], "outcomes": [...], "errors": [...]}` — ordered, and subject to the same 256 KiB limit, which is why `collect` over a large collection can legitimately end in `state_limit_exceeded` rather than a truncated result.

### 4.8 Ambiguous interruption — stop, do not guess

Spec: "a retry-safe command can be replayed with that key; a non-retry-safe command pauses with `operator_decision_required` rather than executing twice."

On restart, for a snapshot whose lifecycle is `running` and whose current step has an attempt with a *started* but no *completed* receipt:

| Step / contract | Behaviour |
|---|---|
| `retry_safe=True` **or** `idempotency.mode in {"natural", "keyed"}` | Replay as the **same** attempt number with the same key. The receipt for the interrupted attempt is written with outcome `interrupted`, then a new receipt records the replay. |
| Anything else — including every `LlmStep` with an in-flight provider call, and every `AgentTaskStep` whose child-task creation may or may not have landed | `control=OPERATOR_DECISION`: pause with reason `operator_decision_required`, no binding, no transition. |

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

`TokenUsage` lives in `src/playbooks/executors/base.py` per §3.1 and is imported by `src/llm/types.py`… **no** — that inverts the dependency. `TokenUsage` is defined in **`src/llm/types.py`** and re-exported from `src/playbooks/executors/base.py`, so the LLM layer never imports the playbook layer. §3.1's listing shows it in `base.py` for readability; the reconciliation commit must place the definition in `src/llm/types.py` and leave a re-export.

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
