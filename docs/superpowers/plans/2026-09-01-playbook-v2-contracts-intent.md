# Playbook V2 — Package 1 child plan: Command contracts, event contracts, and truthful intent

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:test-driven-development` for every task below. Each task names the failing assertion before the implementation that satisfies it. Do not skip a red step.

**Roadmap:** `docs/superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md` — Package 1 (§5), global constraints (§2), module map (§3), locked interfaces (§4), milestone M1 (§6), parallelism rules (§7), quality bar (§9).

**Spec:** `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md` — "Command contracts" (`:325`), "Intent explanation" (`:370`), "Rich node cards" (`:504`), "Selected-node inspector" (`:522`), "Command and explanation invariants" (`:603`).

**Consumes:** Package 0, merged to `main` — `CapabilityPolicy` and `WILDCARD_CHARS` (`src/profiles/capabilities.py`), `ExecutionPrincipal` (`src/commands/principal.py`), `authorize_command` / `command_allowed` / `filter_tool_definitions` (`src/commands/authorization.py`).

**Produces:** typed contracts for the ten pipeline commands; a contract registry that is the single source of both execution and displayed intent; a deterministic execution fingerprint that presentation copy cannot move; typed nested event field contracts; an exhaustive contract-derived explanation renderer; and a V1 graph inspector that shows contract-derived intent instead of a raw JSON dump.

**Not in this package:** the V2 definition model and compiler (Package 2), artifact storage and receipts (Package 3), the V2 engine and executors (Package 4), the V2 graph API and canvas (Package 5). **No V1 execution behavior changes in this package.** Every backend change is additive; the only behavioral change reaching a user is what the inspector and the node card render.

**Written against `origin/main` at `1b835131`.** Every path and line number below was verified at that commit. Package 0 landed in `d0a4c905`, `5da2c436`, `88ed4db7`.

---

## 1. Why this package exists (what is actually broken today)

### 1.1 The node inspector dumps raw JSON where intent belongs

`dashboard/src/pages/playbook-graph/PlaybookNodeInspector.tsx:157-161` renders the compiled action with:

```tsx
{d.action && (
  <Section name="Action">
    <Payload value={d.action} />
  </Section>
)}
```

`Payload` (`:22`) is `JSON.stringify(value, null, 2)` in a `<pre>`. An operator inspecting the `create-review` node of `default-pipeline` sees the literal compiled dict — `{"command": "ensure_task", "args": {"dedup_key": "review:task:{{event.task_id}}", ...}, "on_success": "link-discovered-from", ...}` — and must already know what `ensure_task` does to read it. `PlaybookStepNode.tsx:17-29` (`actionCommand`, then `preview`) puts the bare command *name* on the card and nothing else.

This is the exact failure the exit gate names: displayed intent is not derived from the contract that executes, because there is no contract.

### 1.2 Success is duck-typed, and the two heuristics disagree

`src/playbooks/pipeline_runner.py` infers outcome from the shape of a dict, twice, differently:

| Site | Rule |
|---|---|
| `:146` (main loop) | `success = not (result.get("success") is False or "error" in result)` — **absent `success` key means success** |
| `:181` (`_run_for_each`) | `if not result.get("success"):` → failure — **absent `success` key means failure** |

`_cmd_add_dependency` (`src/commands/task_commands.py:2134`) returns `{"ok": True, "task_id": ..., "depends_on": ..., ...}` at `:2214` with **no `success` key**. `_cmd_edit_task` (`:2281`) returns `{"updated": ..., "fields": [...]}` at `:2433` — also no `success` key. `_cmd_list_tasks` (`:282`) returns `{"by_project", "tasks", "total", "project_count", "hidden_completed"}` — no `success` key.

So `add_dependency` on the main path is a success and inside a `for_each` is a failure, for identical input. `default-pipeline.md`'s `link-discovered-from` node calls `add_dependency` on the main path today; a future author who wraps it in a `for_each` silently inverts its branch. Nothing in the tree catches this.

Spec `:362`: *"No executor infers success from the presence or absence of a `success` key."* `CommandResult` with a declared, closed outcome set is that fix.

### 1.3 The whitelist is a bare name set

`PIPELINE_COMMAND_WHITELIST` (`src/playbooks/pipeline_compiler.py:42`) is a `frozenset[str]` of ten names, and `_validate_node` (`:126`) checks only membership (`:151-157`). `"args"` is checked for `isinstance(dict)` (`:158`) and nothing else. A pipeline naming `ensure_task` with `{"projct_id": ...}` compiles clean and fails at runtime, one event at a time.

### 1.4 Result references are unvalidated

`default-pipeline.md` binds `"output": {"as": "review"}` on the `ensure_task` node and then reads `{{outputs.review.task_id}}`. `_resolve_ref` (`src/playbooks/pipeline_runner.py:28`) walks a plain dict and returns `None` for any miss. There is no declaration anywhere in the tree that `ensure_task` returns a `task_id`, so a typo, a renamed result key, or a command whose result shape changes produces `None`, which becomes the empty string (`:56`) or a `None` argument, and the downstream command reports a confusing validation error instead of the real one.

### 1.5 Required capability is not derivable from a command

Package 0's dispatch gate asks `principal.policy.allows(namespace, name)` directly — see `command_allowed` (`src/commands/authorization.py:116-129`) and `authorize_command` (`:132-181`). The *command name* is the capability, because a command name is all the system has. There is nowhere to say "`task_route` requires the `task_route` capability and additionally resolves routing gates", nor to declare that `gate_resolve` refuses `routing` gates (`src/commands/gate_commands.py:190-197`) — a real, load-bearing behavioral guarantee that exists only as a comment and a runtime string.

### 1.6 Event schemas cannot describe the fields playbooks already read

`src/event_schemas.py` models an event as `{"required": [...], "optional": [...], "types": {...}}` (`EventSchema`, `:32`). `task.completed` (`:88`) declares exactly:

```python
"task.completed": {
    "required": ["task_id", "project_id", "title"],
    "optional": ["agent_id", "agent_type"],
},
```

`default-pipeline.md` reads `{{event.task.branch_name}}` and `{{event.task.pr_url}}` and gates two whole rules on `{"field": "event.task.branch_name", "truthy": true}`. Neither `task` nor its nested fields appear in any schema. There is no description, no nesting, and no sensitivity marking anywhere in the registry, so:

- nothing can validate an event reference at compile time (Package 2 needs this);
- the explanation renderer cannot say *"this event's task branch"* rather than `event.task.branch_name`;
- redaction has no input — every event field is equally unclassified.

### 1.7 `event.task` is dispatch-hydrated, and the registry has no way to say so

This is the finding that decides §3.7's design, and it is not in the roadmap.

`Orchestrator._emit_task_event` (`src/orchestrator/events.py:23-31`) emits exactly `{task_id, project_id, title}` plus keyword extras. It never emits `task`. `task.completed` is raised from `src/orchestrator/monitoring.py:433` with no extras at all, so the *emitted* payload carries no `task` key — which is why `validate_event(..., strict_extras=True)` passes today and why `task` is correctly absent from the schema's `optional` list.

`event.task` exists anyway, because the **pipeline dispatcher** adds it: `src/orchestrator/core.py:854-870` copies the bus payload and, when `task_id` is present and `task` is not, sets `hydrated_event["task"] = asdict(task_row)` from a fresh `db.get_task`. `Task.branch_name` (`src/models.py:450`) and `Task.pr_url` (`:457`) reach the playbook that way. `tests/test_session_close_emits_completed.py:121-129` documents the same coupling from the other end.

Two consequences the implementer must not get wrong:

1. **Adding `task` to `task.completed`'s `optional` list would be a lie.** `validate_event`'s `strict_extras` branch (`src/event_schemas.py:1072-1077`) builds its allow-list from `required + optional + META_FIELDS`; putting `task` there tells every emitter that sending a `task` key is legal, when in fact no emitter does and the dispatcher is the sole producer.
2. The registry still has to describe `task.*`, or the renderer cannot name the field an operator sees on the card. §3.7 therefore adds a `hydrated: true` marker on the field spec: the field is describable and resolvable, and `validate_event` — which reads only `required`, `optional`, and `types` — is untouched.

`asdict(task_row)` also means the **entire** `Task` row is in scope for `event.task.<anything>`. That is the concrete reason §3.8's redaction takes an event-path input and not just an argument name.

---

## 2. Live-tree reconciliation — deviations from the roadmap's file list

The roadmap (§3, §5) permits a child plan to refine filenames after inspecting the live tree and requires each deviation be documented. Every row was verified against `origin/main` at `1b835131`.

### 2.1 Create

| Roadmap says | Live tree | This plan |
|---|---|---|
| `src/commands/contracts/__init__.py` | — | **As written.** Re-exports `CommandContract`, `CommandRegistration`, `CommandResult`, `ContractRegistry`, `CONTRACTS`, `ContractRegistrationError`. |
| `src/commands/contracts/models.py` | — | **As written.** |
| `src/commands/contracts/registry.py` | — | **As written.** |
| `src/commands/contracts/builtin.py` | — | **As written.** |
| `src/commands/contracts/preview.py` | — | **As written.** |
| `src/playbooks/explanation.py` | — | **As written.** |
| `tests/commands/contracts/test_registry.py` | **`tests/commands/` does not exist.** The suite is flat `tests/test_<area>.py` (per `CLAUDE.md` "Testing"); there are no package-style subdirectories other than `tests/perf/`, `tests/llm/`, `tests/fixtures/`. | `tests/test_command_contracts_registry.py` |
| `tests/commands/contracts/test_builtin_contracts.py` | as above | `tests/test_builtin_command_contracts.py` |
| `tests/playbooks/test_explanation.py` | **`tests/playbooks/` does not exist.** | `tests/test_playbook_explanation.py` |
| — | — | **Added:** `tests/test_event_field_contracts.py`. The roadmap has no test row for its own "Enrich event schemas" outcome. |
| — | — | **Added:** `tests/test_contract_intent_parity.py`. The roadmap's last Required outcome ("displayed explanation and invoked contract share the same registration and fingerprint") is a cross-module invariant and belongs in neither single-module suite. |
| — | — | **Added:** `tests/fixtures/contracts/` — golden intent fixtures. `tests/fixtures/` exists and already holds `formulas/`, `task_graphs/`, `transcripts/`. |
| — | — | **Added:** `dashboard/src/pages/playbook-graph/explanation.ts`, `NodeExplanationCard.tsx`, and `__tests__/NodeExplanationCard.test.tsx`. The roadmap lists only edits to `PlaybookNodeInspector.tsx` / `PlaybookStepNode.tsx`; the inspector is 194 lines and the explanation view model would more than double it. `explanation.ts` additionally exists so Task B can start before Task A lands (§11). |

### 2.2 Modify

| Roadmap says | Live tree | This plan |
|---|---|---|
| `src/commands/handler.py` | `_current_scope_var` `:105`; `has_command` `:656` (the `getattr(self, f"_cmd_{name}")` probe is `:669`); `execute` `:811`. | **Minimal.** One read-only helper `contracted_commands()` plus a deferred registry import. `execute()`'s body is **not** changed in this package — see §4.4. |
| handlers for the ten whitelisted pipeline commands | `_cmd_list_tasks` `src/commands/task_commands.py:282`; `_cmd_create_task` `:1090`; `_cmd_add_dependency` `:2134`; `_cmd_edit_task` `:2281`; `_cmd_ensure_task` `:3432`; `_cmd_get_downstream_tasks` `:3535`; `_cmd_task_route` `:3564`; `_cmd_gate_create` `src/commands/gate_commands.py:20`; `_cmd_gate_resolve` `:165`; `_cmd_task_batch_commit` `src/commands/proposal_commands.py:208`. | **Not modified.** Roadmap Required outcome: *"Wrap legacy dict-returning handlers only at the contract boundary; do not create a second operational handler."* The ten `_cmd_*` bodies are untouched; adapters live in `src/commands/contracts/builtin.py`. |
| `src/event_schemas.py` | `EventSchema` TypedDict `:32`; `META_FIELDS` `:53`; `"task.completed"` `:88`; `EVENT_SCHEMAS` `:959`; `validate_event` `:1014`, its `strict_extras` branch `:1072`. | **Additive `fields` key** on `EventSchema` plus `resolve_event_path()` / `event_field_is_sensitive()` / `CONTRACTED_EVENT_TYPES`. `validate_event`'s behavior is unchanged (§3.7). |
| `src/playbooks/pipeline_compiler.py` | `PIPELINE_COMMAND_WHITELIST` `:42`; `_validate_node` `:126`; the whitelist membership check `:151`; the `args` isinstance check `:158`; `_normalize_nodes` `:222` (rule prefixing `:239`). | **Whitelist becomes derived**, not deleted: `PIPELINE_COMMAND_WHITELIST = CONTRACTS.names()`. No new compile-time argument validation in this package — that is Package 2's `src/playbooks/validation.py`, and adding it here would change which existing playbooks compile. |
| `src/playbooks/graph_view.py` | `build_nodes` `:260` (`node_data["details"] = node.to_dict()` at `:325`); `build_edges` `:332`; `build_graph_view` `:658`. | `build_nodes` attaches `explanation` to each node dict. Nothing else changes; `details` stays exactly as-is for Advanced view. `build_edges` is not touched. |
| `src/api/models/playbook.py` | `CompiledPlaybookNode` `:213`; `PlaybookGraphNode` `:237`; `PlaybookGraphEdge` `:257`; `PlaybookGraphViewResponse` `:279`; the operation map entry `"playbook_graph_view"` `:385`. | Adds the §3.6 explanation models and one optional `explanation` field on `PlaybookGraphNode`. Purely additive to the OpenAPI surface. |
| `dashboard/src/pages/playbook-graph/PlaybookNodeInspector.tsx` | 194 lines; `Payload` `:22`; `Payload value={d.action}` `:159`, `d.for_each` `:165`, `d.output` `:171`. | Replaces the Action `Payload` with `<NodeExplanationCard>`; the raw `d.action` moves under a collapsed **Advanced** disclosure. |
| `dashboard/src/pages/playbook-graph/PlaybookStepNode.tsx` | `actionCommand` `:17`; `preview` `:29`. | Card preview becomes `explanation.title` with the first effect as a second line; falls back to `actionCommand` then `prompt_preview`. |
| relevant API and dashboard graph tests | `tests/test_playbook_graph_view.py`, `tests/test_api_playbook_graph_view.py`, `tests/test_api_client_contract.py`, `dashboard/src/pages/playbook-graph/__tests__/{PlaybookNodeInspector,PlaybookGraphCanvas,PlaybookGraphSelection,PlaybookGraphView}.test.tsx` and `fixtures.ts`. | Named per task in §12. |
| — | `src/config.py:857` `PlaybooksConfig` (fields: `enabled: bool = False`); `build_graph_view` caller `src/commands/playbook_commands.py:1268` | **Added:** one `contract_intent: bool = True` flag (§9), threaded as a keyword — there is no module-level `get_config()` in this tree. |
| — | `openapi.json`, `packages/aq-client/`, `dashboard/src/api/client` | **Added:** regenerated. `CLAUDE.md` requires `./scripts/regenerate-api-client.sh --offline` and `./scripts/regenerate-ts-client.sh --offline` after **any** change to `src/api/models`, and `tests/test_api_client_contract.py::test_committed_openapi_json_matches_the_live_app_surface` fails otherwise. |

### 2.3 Verification-command deviations

| Roadmap command | Problem | This plan |
|---|---|---|
| `pytest tests/commands/contracts -q` | directory does not exist | `aq test tests/test_command_contracts_registry.py tests/test_builtin_command_contracts.py -q` |
| `pytest tests/playbooks/test_explanation.py -q` | directory does not exist | `aq test tests/test_playbook_explanation.py -q` |
| `npm test -- --run dashboard/src/pages/playbook-graph` from `dashboard/` | `dashboard/package.json:13` is already `"test": "vitest run"`, so `--run` is redundant, and the path is relative to `dashboard/`, not the repo root. | `npm test -- src/pages/playbook-graph` from `dashboard/` |
| `ruff check src/commands/contracts src/playbooks/explanation.py tests/commands/contracts tests/playbooks/test_explanation.py` | last two paths do not exist | see §14.1 for the full path list |

Per `CLAUDE.md`, use `aq test` rather than bare `pytest` for anything past a single file. `aq test` takes a box-wide slot and applies the default marker deselects; exit code 75 means no slot came free and is not a test failure.

---

## 3. Locked interfaces

Roadmap §7 permits Package 1's backend contracts and the early inspector work to proceed **in parallel** once `CommandContract` and the explanation payload shape are locked. §3 is that lock. Everything in it is normative: the two implementation tasks (§11) build against these signatures without further coordination. Per roadmap §7, a change to anything in §3 requires updating this plan and every not-yet-completed child plan, and re-running the most recent passed milestone gate (M0).

Additive fields are permitted. Renames, removals, and semantic changes are not.

### 3.1 `CommandContract` — `src/commands/contracts/models.py`

The contract splits into a **fingerprinted execution half** and a **non-fingerprinted presentation half**, exactly as the spec requires (`:358`, "Improving a label cannot stale a playbook").

Mapping to the spec's illustrative constructor (`:331-346`): `inputs` → `ExecutionContract.args_model`; `result` → `result_model`; `outcomes` → `outcomes`; `guarantees` → `side_effect` + `idempotency` + `retry_safe`; `execution` → `timeout_seconds` + `supports_preview`; `security` → `capability` + `sensitive_args` + `sensitive_result_fields`; `receipt` → `receipt_projection`; `presentation` → `CommandPresentation`.

```python
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_GENERATION: int = 1


class CommandArgs(BaseModel):
    """Base for every contracted argument model."""
    model_config = ConfigDict(extra="forbid", frozen=True)


class CommandValue(BaseModel):
    """Base for every contracted result model."""
    model_config = ConfigDict(extra="forbid", frozen=True)


A = TypeVar("A", bound=CommandArgs)
R = TypeVar("R", bound=CommandValue)


class SideEffectClass(StrEnum):
    READ = "read"            # no writes at all
    CREATE = "create"
    UPDATE = "update"
    LINK = "link"            # writes only graph edges
    RESOLVE = "resolve"      # closes/answers an existing object
    COMPOSITE = "composite"  # more than one of the above in one call


class OutcomeClass(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class OutcomeSpec(BaseModel):
    """One legal business outcome. `name` is the transition key."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    classification: OutcomeClass


class IdempotencySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    #: "none"      — repeating the call repeats the effect
    #: "natural"   — repeating is a no-op by the command's own semantics
    #: "keyed"     — repeating with the same `key_field` value is a no-op
    mode: Literal["none", "natural", "keyed"]
    key_field: str | None = None      # argument name; required iff mode == "keyed"


class ExecutionContract(BaseModel, Generic[A, R]):
    """Everything the fingerprint covers. No human-facing copy lives here."""
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: str
    args_model: type[A]
    result_model: type[R]
    outcomes: tuple[OutcomeSpec, ...]
    capability: str                       # entry in the aq_commands namespace
    side_effect: SideEffectClass
    idempotency: IdempotencySpec
    retry_safe: bool
    timeout_seconds: int | None = None
    effects: tuple[EffectClause, ...] = ()
    sensitive_args: frozenset[str] = frozenset()
    sensitive_result_fields: frozenset[str] = frozenset()
    #: Allow-list of result-model field names a receipt may carry (Package 3).
    #: EMPTY MEANS NOTHING IS PROJECTED — spec :609, "Sensitive and unmarked
    #: fields are redacted from receipts by default".
    receipt_projection: tuple[str, ...] = ()
    supports_preview: bool = False


class CommandPresentation(BaseModel):
    """Copy only. Changing anything here MUST NOT move the fingerprint."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str                                   # "Ensure a review task exists"
    summary: str                                 # one sentence, imperative
    arg_labels: dict[str, str] = {}              # arg name    -> "Project"
    outcome_labels: dict[str, str] = {}          # outcome     -> "Created"
    result_labels: dict[str, str] = {}           # result field-> "Task"
    subject_labels: dict[str, str] = {}          # EffectSubject value -> "dependency edge"
    help_url: str | None = None


class CommandContract(BaseModel, Generic[A, R]):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    execution: ExecutionContract[A, R]
    presentation: CommandPresentation

    @property
    def name(self) -> str: ...                   # == execution.name
    def fingerprint(self) -> str: ...            # see §3.3; cached on first call
```

**Reserved outcomes.** Three names are reserved and may not appear in `outcomes`; the engine (Package 4) produces them, not the adapter — except `contract_violation`, which the adapter produces here (§3.2) because it is the adapter that validates the result:

```python
RESERVED_OUTCOMES: frozenset[str] = frozenset(
    {"contract_violation", "unauthorized", "runtime_error"}
)
```

`ExecutionContract` validation (a Pydantic `model_validator(mode="after")`) rejects a contract that:

- declares any `RESERVED_OUTCOMES` member in `outcomes`;
- declares zero outcomes, or no outcome classified `SUCCESS`;
- declares two outcomes with the same `name`;
- sets `idempotency.mode == "keyed"` without a `key_field` present in `args_model.model_fields`, or sets `key_field` when `mode != "keyed"`;
- names a `sensitive_args` entry absent from `args_model.model_fields`;
- names a `sensitive_result_fields` or `receipt_projection` entry absent from `result_model.model_fields`;
- has an empty `capability`, or a `capability` containing a character in `WILDCARD_CHARS` (`src/profiles/capabilities.py:55`, `"*?"`);
- carries an effect clause whose `*_arg` / `key_arg` / `fields_arg` / `target_arg` names an argument absent from `args_model.model_fields`.

That last rule is what stops an effect clause from describing an argument the command does not take.

### 3.2 `CommandResult` and outcome mapping

```python
class CommandResult(BaseModel, Generic[R]):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: str          # a declared OutcomeSpec.name, or a RESERVED_OUTCOMES member
    value: R
    summary: str          # one line, already redacted, safe to log and display

    def classification(self, contract: CommandContract[Any, R]) -> OutcomeClass: ...
```

`classification` returns `FAILURE` for every `RESERVED_OUTCOMES` member and otherwise the declared `OutcomeSpec.classification`; it raises `UnknownOutcome` for a name in neither set. `CommandResult` carries **no** control-flow target: transitions live on the step, per roadmap §4.

The adapter signature is fixed:

```python
CommandContext = ExecutionPrincipal          # Package 0; re-exported, never redefined

InvokeAdapter = Callable[[CommandArgs, CommandContext], Awaitable[CommandResult[Any]]]
PreviewAdapter = Callable[[CommandArgs, CommandContext], Awaitable[CommandResult[Any]]]
```

Every built-in adapter in `src/commands/contracts/builtin.py` has the same three-step body and is the **only** place duck-typing is permitted, precisely so it is auditable in one file:

1. call the existing `CommandHandler.execute(name, args.model_dump(exclude_none=True))`;
2. classify the returned dict into a declared outcome with a per-command `_outcome_of(raw: dict) -> str` — never the generic `success`-key heuristic;
3. build `result_model` from an **explicit key projection** of the raw dict (never `model_validate(raw)` — see §5.1); on `KeyError`/`ValidationError`, return `CommandResult(outcome="contract_violation", ...)`.

This is the layer that fixes §1.2: `add_dependency`'s `{"ok": True, ...}` maps to outcome `linked`, in both the main loop and a `for_each`, because the outcome comes from the contract and not from a key probe.

### 3.3 Fingerprint canonicalization (locked byte-for-byte)

```python
def canonical_execution_document(ec: ExecutionContract) -> dict[str, Any]:
    return {
        "schema_generation": SCHEMA_GENERATION,
        "name": ec.name,
        "args_schema": canonical_json_schema(ec.args_model),
        "result_schema": canonical_json_schema(ec.result_model),
        "outcomes": [
            {"name": o.name, "classification": o.classification.value}
            for o in sorted(ec.outcomes, key=lambda o: o.name)
        ],
        "capability": ec.capability,
        "side_effect": ec.side_effect.value,
        "idempotency": {"mode": ec.idempotency.mode, "key_field": ec.idempotency.key_field},
        "retry_safe": ec.retry_safe,
        "timeout_seconds": ec.timeout_seconds,
        "effects": [clause.canonical() for clause in ec.effects],   # declaration order
        "sensitive_args": sorted(ec.sensitive_args),
        "sensitive_result_fields": sorted(ec.sensitive_result_fields),
        "receipt_projection": list(ec.receipt_projection),          # declaration order
        "supports_preview": ec.supports_preview,
    }


def execution_fingerprint(ec: ExecutionContract) -> str:
    blob = json.dumps(
        canonical_execution_document(ec),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()
```

Effect-clause order is significant (declaration order is the order the operator reads them, and `gate_create`'s two clauses are not commutative in the rendered card); outcome order is not, so outcomes are sorted.

`canonical_json_schema(model)` is the only subtle part and is locked as:

```python
_PRESENTATION_SCHEMA_KEYS: frozenset[str] = frozenset(
    {"title", "description", "examples", "deprecated", "$comment", "readOnly", "writeOnly"}
)

def canonical_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema(mode="validation", ref_template="#/$defs/{model}")
    return _strip(raw)
```

`_strip` recurses through dicts and lists and:

- **drops** every key in `_PRESENTATION_SCHEMA_KEYS`, at every depth. This is what makes a `Field(description=...)` edit fingerprint-neutral. Note that Pydantic emits a `"title"` for *every* field by default, so without this the fingerprint would move on a field rename that changes nothing but the generated title;
- **sorts** the `"required"` list;
- **preserves the order** of `"enum"`, `"anyOf"`, `"oneOf"`, `"allOf"`, and `"prefixItems"` — union member order is semantic in Pydantic's smart-union validation, so reordering it is a real change;
- keeps everything else verbatim, relying on `sort_keys=True` at dump time for object-key order (including `$defs`).

Fingerprint stability is asserted by a **golden** test, not only a differential one: `tests/test_builtin_command_contracts.py::test_golden_fingerprints` pins the literal `sha256:` string for each of the ten contracts against `tests/fixtures/contracts/fingerprints.json`, so an accidental canonicalization change is a red test with a readable diff rather than a silent artifact invalidation. Regenerating that file is a deliberate, reviewed act; the test failure message says so.

`ContractRegistry.registry_fingerprint()` is

```python
"sha256:" + sha256(
    json.dumps({name: self.fingerprint(name) for name in sorted(self.names())},
               sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
```

Package 2 stores it as `compiled_against.commands`; Package 3 compares it to compute `stale_contract` health.

### 3.4 Effect clauses — `src/commands/contracts/models.py`

Effect clauses are **data with a typed predicate** (spec `:360`), not prose. The union is closed and discriminated on `kind`. `subject` is a closed enum, not free text, so that it is genuinely execution-semantic and its *wording* lives in `CommandPresentation.subject_labels`:

```python
class EffectSubject(StrEnum):
    TASK = "task"
    TASK_GRAPH = "task_graph"
    TASK_LIST = "task_list"
    TASK_ROUTING = "task_routing"
    DOWNSTREAM_TASKS = "downstream_tasks"
    DEPENDENCY_EDGE = "dependency_edge"
    GATE = "gate"
    GATE_WAITER = "gate_waiter"
    ROUTING_GATE = "routing_gate"


class ClausePredicate(BaseModel):
    """A predicate over resolved arguments. `always` when both fields are None."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    arg_present: str | None = None                # "waiter_task_ids"
    arg_equals: tuple[str, Any] | None = None     # ("dep_type", "discovered-from")


class _Clause(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subject: EffectSubject
    when: ClausePredicate = ClausePredicate()

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CreateClause(_Clause):
    kind: Literal["create"] = "create"

class ReuseClause(_Clause):
    kind: Literal["reuse"] = "reuse"
    key_arg: str | None = None

class CreateOrReuseClause(_Clause):
    kind: Literal["create_or_reuse"] = "create_or_reuse"
    key_arg: str

class UpdateClause(_Clause):
    kind: Literal["update"] = "update"
    fields_arg: str | None = None                 # arg naming which fields change

class LinkClause(_Clause):
    kind: Literal["link"] = "link"
    from_arg: str
    to_arg: str
    relation_arg: str | None = None

class ResolveClause(_Clause):
    kind: Literal["resolve"] = "resolve"
    target_arg: str

class ReadClause(_Clause):
    kind: Literal["read"] = "read"

EffectClause = Annotated[
    CreateClause | ReuseClause | CreateOrReuseClause | UpdateClause
    | LinkClause | ResolveClause | ReadClause,
    Field(discriminator="kind"),
]

EFFECT_CLAUSE_TYPES: tuple[type[_Clause], ...] = (
    CreateClause, ReuseClause, CreateOrReuseClause, UpdateClause,
    LinkClause, ResolveClause, ReadClause,
)
```

`canonical()` is `model_dump(mode="json")`, which contains only `kind`, the enum value of `subject`, the predicate, and argument *names*. No presentation string lives on a clause, so a wording change never moves a fingerprint.

`EFFECT_CLAUSE_TYPES` is declared explicitly rather than derived from `typing.get_args`, because the annotated-union form makes `get_args` shape-dependent on the Pydantic version. `tests/test_command_contracts_registry.py::test_effect_clause_types_matches_the_union` asserts the tuple and the union agree, so the explicit list cannot silently drift.

**The `wait` clause is deferred.** The spec (`:360`) lists `wait` among the semantic operations. There is no wait step until Package 4's `WaitStep`, and a clause type with no producer and no renderer test would be dead code that still has to be kept exhaustive. `WaitClause` is added in **Package 4**, together with `WaitStep` and its renderer; §3.5's registration check is what forces the renderer to arrive with it.

**Canonical fallback.** A contract with `effects=()` is legal. The renderer then emits exactly one `ExplanationEffect` built from `side_effect` and the argument names — the spec's "lossless canonical field/value rendering" (`:360`, `:389`). Intent is never hidden and never invented.

### 3.5 `CommandRegistration` and the registry — `src/commands/contracts/registry.py`

```python
@dataclass(frozen=True, slots=True)
class CommandRegistration:
    name: str                                    # spec :333 names it explicitly
    contract: CommandContract[Any, Any]
    invoke: InvokeAdapter
    preview: PreviewAdapter | None = None


class ContractRegistry:
    def register(self, registration: CommandRegistration) -> None: ...
    def get(self, name: str) -> CommandRegistration | None: ...
    def require(self, name: str) -> CommandRegistration: ...      # raises UnknownContract
    def names(self) -> frozenset[str]: ...
    def fingerprint(self, name: str) -> str: ...
    def registry_fingerprint(self) -> str: ...
    def required_capability(self, name: str) -> str | None: ...   # None when unregistered


CONTRACTS: ContractRegistry = ContractRegistry()   # module singleton
```

`register()` raises `ContractRegistrationError` when:

1. the name is already registered (no silent replacement — tests build a fresh `ContractRegistry()`);
2. `registration.name != registration.contract.execution.name`;
3. `contract.execution.supports_preview` is `True` and `preview is None`, or `False` and `preview is not None`;
4. **any effect clause has no renderer** — `register()` calls `can_render(clause)` from `src/playbooks/explanation.py`. This is the roadmap's "Fail contract registration when an effect cannot be rendered".

**Import cycle, resolved explicitly.** `src/playbooks/explanation.py` imports clause types from `src/commands/contracts/models.py`; `registry.py` needs `can_render` from `explanation.py`. `registry.py` performs that import **inside `register()`**, not at module scope. This is deliberate and must not be "cleaned up": moving the renderer into `contracts/` would put playbook presentation inside the command boundary, and moving clause types into `playbooks/` would make the command layer depend on the playbook layer.

Renderer exhaustiveness is enforced twice: structurally by a `match` over the closed union ending in `case _ as unreachable: assert_never(unreachable)` in `render_effect`, and at runtime by `tests/test_playbook_explanation.py::test_every_clause_kind_has_a_renderer`, which iterates `EFFECT_CLAUSE_TYPES` and asserts `can_render` for a constructed instance of each. Adding a clause type without a renderer fails both, and `mypy`/`ruff` flag the non-exhaustive match.

### 3.6 Explanation view model — the payload the UI task builds against

This is the second half of roadmap §7's lock. The models live in `src/api/models/playbook.py` (so they are projected into `openapi.json` and both generated clients); the builder lives in `src/playbooks/explanation.py`.

```python
class ExplanationValue(BaseModel):
    """One argument value as the operator should read it."""
    #: literal | event_ref | binding_ref | loop_ref | template | unresolved
    kind: Literal["literal", "event_ref", "binding_ref", "loop_ref", "template", "unresolved"]
    #: Human-facing rendering. ALWAYS a non-empty string; never null.
    text: str
    #: Exact source expression for Advanced view, e.g. "{{event.task.branch_name}}".
    raw: str | None = None
    #: True when the contract or the event field registry marks this value
    #: sensitive; `text` is then the redaction placeholder and `raw` is null.
    redacted: bool = False


class ExplanationInput(BaseModel):
    field: str                    # argument name from args_model
    label: str                    # presentation label, falls back to `field`
    value: ExplanationValue
    required: bool = False


class ExplanationEffect(BaseModel):
    #: create | reuse | create_or_reuse | update | link | resolve | read
    operation: str
    #: Rendered sentence, e.g. 'Create or reuse a task keyed by "dedup_key"'.
    text: str
    #: Rendered predicate, e.g. "when waiter_task_ids is provided".
    #: Null means unconditional.
    condition: str | None = None
    #: The EffectSubject value; the UI groups by it and never parses `text`.
    subject: str | None = None


class ExplanationOutcome(BaseModel):
    #: For a V1 pipeline node this is the edge kind ("success" / "failure").
    #: Package 4 replaces it with the declared OutcomeSpec.name.
    outcome: str
    label: str
    classification: Literal["success", "failure"]
    target_node_id: str | None = None
    target_label: str | None = None


class ExplanationResultBinding(BaseModel):
    name: str                     # binding name, e.g. "review"
    #: Field names available on the bound result, from result_model.
    fields: list[str] = []


class ExplanationLoop(BaseModel):
    source_text: str              # "each item in downstream.tasks"
    item_binding: str             # "dep"
    source_raw: str | None = None


class NodeExplanation(BaseModel):
    #: "command" today. Package 4 adds llm | agent_task | decision | wait |
    #: foreach | terminal.
    kind: str
    title: str
    command: str | None = None
    contract_fingerprint: str | None = None
    capability: str | None = None
    effects: list[ExplanationEffect] = []
    inputs: list[ExplanationInput] = []
    result: ExplanationResultBinding | None = None
    outcomes: list[ExplanationOutcome] = []
    loop: ExplanationLoop | None = None
    idempotency: str | None = None       # "Repeating with the same deduplication key reuses the existing task"
    retry: str | None = None             # "Safe to retry" / "Not safe to retry"
    #: Executable argument keys with no richer rendering, listed verbatim.
    #: Never hidden — the operator sees the field name here even when
    #: the contract has no label for it.
    unrendered_fields: list[str] = []
```

`PlaybookGraphNode` gains exactly one field:

```python
class PlaybookGraphNode(BaseModel):
    ...
    details: CompiledPlaybookNode
    explanation: NodeExplanation | None = None      # NEW — null for uncontracted nodes
```

**Verbatim TypeScript shape.** Task B writes this into `dashboard/src/pages/playbook-graph/explanation.ts` on day one and later asserts it is assignable from the generated `NodeExplanation`, so it can start before the backend lands:

```ts
export interface ExplanationValue {
  kind: "literal" | "event_ref" | "binding_ref" | "loop_ref" | "template" | "unresolved";
  text: string;
  raw?: string | null;
  redacted?: boolean;
}
export interface ExplanationInput { field: string; label: string; value: ExplanationValue; required?: boolean }
export interface ExplanationEffect { operation: string; text: string; condition?: string | null; subject?: string | null }
export interface ExplanationOutcome {
  outcome: string; label: string; classification: "success" | "failure";
  target_node_id?: string | null; target_label?: string | null;
}
export interface ExplanationResultBinding { name: string; fields?: string[] }
export interface ExplanationLoop { source_text: string; item_binding: string; source_raw?: string | null }
export interface NodeExplanation {
  kind: string; title: string;
  command?: string | null; contract_fingerprint?: string | null; capability?: string | null;
  effects?: ExplanationEffect[]; inputs?: ExplanationInput[];
  result?: ExplanationResultBinding | null; outcomes?: ExplanationOutcome[];
  loop?: ExplanationLoop | null;
  idempotency?: string | null; retry?: string | null;
  unrendered_fields?: string[];
}
```

**Invariants the payload must satisfy** (each is a named test in §12):

1. `contract_fingerprint` equals `CONTRACTS.fingerprint(command)` for the same `command` the node would invoke. This is the "displayed explanation and invoked contract share the same registration" requirement.
2. `outcomes` has one entry per rendered **action** edge out of the node and no more:
   `{o.target_node_id for o in explanation.outcomes if o.target_node_id}` equals
   `{e["target"] for e in build_edges(pb) if e["source"] == nid and e["edge_type"] in {"success", "failure"}}`.
   The `edge_type` filter is required and is not decoration: `build_edges` (`src/playbooks/graph_view.py:380-387`) also emits a `timeout` edge from `node.on_timeout`, which is not an action outcome. Every executable action transition is displayed; every displayed one is executable.
3. Every key of the compiled `action["args"]` appears exactly once across `inputs` and `unrendered_fields`. Nothing executable is dropped.
4. `ExplanationValue.text` is never empty and never the literal `"None"`.
5. For every `field in contract.execution.sensitive_args`, the matching `ExplanationInput.value` has `redacted=True`, `raw is None`, and `text == "[redacted]"`.

### 3.7 Event field contracts — `src/event_schemas.py`

Additive. `validate_event` (`:1014`) keeps its exact current behavior; nothing that passes today starts failing, and nothing that fails today starts passing.

```python
class EventFieldSpec(TypedDict):
    type: str                                   # string|integer|number|boolean|object|array|null
    description: str
    sensitive: NotRequired[bool]                # default False
    #: True when the field is NOT emitted on the bus but is added by the
    #: pipeline dispatcher before a playbook sees the event
    #: (src/orchestrator/core.py:854-870).  Hydrated fields are describable
    #: and resolvable but are deliberately absent from `required`/`optional`,
    #: so `validate_event(..., strict_extras=True)` still rejects an emitter
    #: that invents them.  See §1.7.
    hydrated: NotRequired[bool]
    fields: NotRequired[dict[str, "EventFieldSpec"]]   # for type == "object"
    item: NotRequired["EventFieldSpec"]                # for type == "array"


class EventSchema(TypedDict):
    required: list[str]
    optional: list[str]
    types: NotRequired[dict[str, type | tuple[type, ...]]]   # unchanged
    fields: NotRequired[dict[str, EventFieldSpec]]           # NEW
```

New module functions:

```python
def resolve_event_path(event_type: str, path: str) -> EventFieldSpec | None:
    """Resolve a dotted reference such as "task.branch_name" against `fields`.

    Returns None when the event type is unregistered, has no `fields` block,
    or the path does not resolve.  Never raises.
    """

def event_field_is_sensitive(event_type: str, path: str) -> bool:
    """True when `path` — or ANY of its ancestors — is marked sensitive.

    Ancestor inheritance is the point: marking `task` sensitive redacts
    every `task.*` reference without enumerating the Task dataclass.
    """

CONTRACTED_EVENT_TYPES: frozenset[str]
```

**Scope decision.** Enriching all registered event types is out of proportion to this package and would be unreviewable. `CONTRACTED_EVENT_TYPES` is the closed set this package must fully describe — exactly the event types the shipped `default-pipeline.md` triggers on (its frontmatter `triggers:` block):

```python
CONTRACTED_EVENT_TYPES = frozenset({
    "task.completed",   # per-task-review, per-branch-final-review
    "spec.approved",    # spec-ingest-on-approve
    "proposal.ready",   # proposal-ready-gate
    "gate.resolved",    # commit-on-gate-resolve
})
```

Four, not six: `task.created` and `task.failed` are registered event types but no shipped pipeline reads a field from them, and contracting an event type nothing reads adds maintenance with no proof attached.

`tests/test_event_field_contracts.py::test_contracted_event_types_are_fully_described` asserts that for each of these, every name in `required` + `optional` has an entry in `fields` with a non-empty `description`, and that every nested path the shipped playbooks actually read resolves. The exhaustive list of paths `default-pipeline.md` reads, verified against the file:

| Event type | Paths read |
|---|---|
| `task.completed` | `project_id`, `task_id`, `title`, `task.branch_name`, `task.pr_url` |
| `spec.approved` | `project_id`, `spec_path` |
| `proposal.ready` | `project_id`, `proposal_id` |
| `gate.resolved` | `await_id` |

Uncontracted event types are unchanged and `resolve_event_path` returns `None` for them, which the renderer surfaces as an `unresolved` value — visible, never guessed.

Growing `CONTRACTED_EVENT_TYPES` is the mechanism by which Package 2's compile-time event-reference validation gains coverage; that validation is **not** added here.

### 3.8 Redaction policy

One module, used by explanations, previews, and (in Package 3) receipts:

```python
# src/commands/contracts/models.py
REDACTED: Final[str] = "[redacted]"

def redact_args(contract: CommandContract, args: Mapping[str, Any]) -> dict[str, Any]: ...
def redact_result(contract: CommandContract, value: CommandValue) -> dict[str, Any]: ...
```

Rules, locked:

- a field named in `sensitive_args` / `sensitive_result_fields` is replaced by `REDACTED` — not truncated, not hashed, not omitted (an omitted key is indistinguishable from an unset one);
- an argument whose source expression is a single `{{event.PATH}}` reference where `event_field_is_sensitive(event_type, PATH)` is true is redacted even when the argument itself is not marked. Ancestor inheritance (§3.7) means marking `task` sensitive covers every `task.*` reference, which matters because `asdict(task_row)` puts the whole row in scope (§1.7);
- redaction is applied **before** the value reaches `ExplanationValue`, so no code path can render an unredacted sensitive value by forgetting a call;
- `summary` on `CommandResult` is produced by the adapter from already-redacted material;
- `receipt_projection` is an allow-list, and an empty one projects nothing — the spec's "sensitive and unmarked fields are redacted from receipts by default" (`:609`). Package 3 consumes this; Package 1 only declares and tests it.

Of the ten commands, none carries a genuinely secret argument today, so `sensitive_args` is empty for all ten. To keep the mechanism from being dead code, `tests/test_playbook_explanation.py::test_sensitive_argument_is_redacted_everywhere` registers a synthetic contract with `sensitive_args={"token"}` in a fresh `ContractRegistry` and asserts the explanation, the preview stub, and `redact_args` all yield `REDACTED`, and that the secret literal appears nowhere in `NodeExplanation.model_dump_json()`. `test_event_sensitive_path_is_redacted` does the same for a synthetic event type whose `token` field is marked `sensitive: true`. This is deliberate: the policy must be proven before Package 3 depends on it.

### 3.9 Capability derivation

`ExecutionContract.capability` equals the command name for all ten. `src/commands/authorization.py` gains one helper and two one-word call-site edits:

```python
def required_capability(name: str) -> str:
    """The capability entry that gates *name*.

    Contracted commands declare it; everything else is gated by its own
    name, which is the pre-Package-1 behavior for every command.
    """
    from src.commands.contracts import CONTRACTS      # deferred: contracts import handler types
    return CONTRACTS.required_capability(name) or name
```

and, at the two places that consult the policy:

- `command_allowed` (`:129`): `principal.policy.allows(resolve_namespace(name, resolver), required_capability(name))`
- `authorize_command` (`:167`): `principal.policy.allows(namespace, required_capability(name))`

`filter_tool_definitions` (`:193`) calls `command_allowed` and so needs no edit — which is exactly the property that keeps discovery and execution using one policy (roadmap §2, "Tool discovery and tool-schema publication use the same capability policy as execution"). `denial_result` (`:184`) keeps reporting the *command* name, not the capability: the agent asked for a command and should be told about the command.

Because every contract sets `capability == name`, **this is behavior-preserving by construction**, and `tests/test_builtin_command_contracts.py::test_capability_equals_command_name_for_all_ten` pins it. The seam exists so a later contract can require a different (never broader) capability; a contract whose `capability` is not equal to its `name` is legal but must be justified in the commit that introduces it. Rollback is deleting the helper and reverting two expressions.

### 3.10 Non-goals of §3

To make the lock unambiguous for the parallel tasks, these are explicitly **not** part of Package 1 and must not be built against §3:

- routing any dispatch through `CommandRegistration.invoke` (Package 4);
- persisting a fingerprint anywhere (Package 2 stores it; Package 3 compares it);
- compile-time validation of arguments or event references against a contract (Package 2);
- any preview adapter with a real implementation (Package 4);
- `WaitClause` (Package 4).

---

## 4. The ten contracts

Each row is normative: `args_model` fields come from the live handler bodies and `src/tools/definitions.py`; `result_model` fields come from the live success returns. A field absent from a handler's return **must not** appear in a result model.

### 4.1 Argument and result models

Models live in `src/commands/contracts/builtin.py`, named `<Command>Args` / `<Command>Value` in PascalCase (`EnsureTaskArgs`, `EnsureTaskValue`).

| Command | `args_model` required | `args_model` optional | `result_model` | Outcomes (`classification`) |
|---|---|---|---|---|
| `create_task` | `title` | `project_id`, `description`, `priority`, `task_type`, `profile_id`, `intelligence_class`, `preferred_workspace_id`, `integration_mode`, `workspace_mode`, `requires_kinds`, `depends_on`, `parent_id`, `labels`, `reason`, `discovered_from`, `affinity_agent_id`, `affinity_reason`, `dedup_key` | `created: str`, `task_id: str`, `status: str`, `title: str`, `project_id: str`, plus the keys the handler adds conditionally (`gate_id`, `integration_mode`, `task_type`, `profile_id`, `intelligence_class`, `preferred_workspace_id`, `affinity_agent_id`, `affinity_reason`, `workspace_mode`, `requires_kinds`, `depends_on`, `reason`, `parent_id`, `labels`, `warning`), each `| None = None` | `created` (success), `rejected` (failure) |
| `ensure_task` | `dedup_key`, `title` | `project_id`, `description`, `priority`, `profile_id`, `intelligence_class`, `initial_status` | `task_id: str`, `created: bool` | `created` (success), `reused` (success), `rejected` (failure) |
| `edit_task` | `task_id` | `project_id`, `title`, `description`, `priority`, `task_type`, `status`, `max_retries`, `verification_type`, `profile_id`, `integration_mode`, `skip_verification`, `intelligence_class`, `affinity_agent_id`, `affinity_reason`, `workspace_mode` | `updated: str`, `fields: list[str]`, `old_status: str \| None`, `new_status: str \| None`, `warning: str \| None` | `updated` (success), `rejected` (failure) |
| `add_dependency` | `task_id`, `depends_on` | `dep_type`, `reason` | `ok: bool`, `task_id: str`, `depends_on: str`, `dep_type: str`, `reason: str \| None`, `task_title: str`, `depends_on_title: str` | `linked` (success), `already_linked` (success), `rejected` (failure) |
| `gate_create` | `project_id`, `gate_type`, `title` | `question`, `await_id`, `timeout_at`, `waiter_task_ids` | `gate_id: str \| None`, `gate: dict[str, Any] \| None`, `was_created: bool \| None`, `skipped: bool \| None`, `reason: str \| None`, `created: bool \| None` | `created` (success), `reused` (success), `skipped` (success), `rejected` (failure) |
| `gate_resolve` | `gate_id`, `resolved_by` | `resolution` | `gate_id: str`, `unblocked_task_ids: list[str]` | `resolved` (success), `refused_routing_gate` (failure), `rejected` (failure) |
| `list_tasks` | — | `project_id`, `status`, `display_mode`, `show_dependencies`, `limit` | `tasks: list[dict[str, Any]]`, `by_project: dict[str, list[dict[str, Any]]]`, `total: int`, `project_count: int`, `hidden_completed: int` | `listed` (success) |
| `get_downstream_tasks` | `task_id` | — | `tasks: list[DownstreamTask]` where `DownstreamTask` is `{id: str, title: str, status: str}` | `listed` (success), `rejected` (failure) |
| `task_batch_commit` | `proposal_id` | — | `task_ids: list[str]` | `committed` (success), `rejected` (failure) |
| `task_route` | `task_id`, `profile_id` | `intelligence_class`, `workspace_id` | `task_id: str`, `resolved_gate_ids: list[str]` | `routed` (success), `rejected` (failure) |

Notes the implementer must honor rather than "tidy":

- **`ensure_task`'s `project_id` is optional, not required.** `_cmd_ensure_task` reads `args.get("project_id") or self._active_project_id` (`src/commands/task_commands.py:3443`) and only rejects when both are empty (`:3445`). Declaring it required would narrow the command; the contract describes the handler, not the one caller. `default-pipeline.md` always passes it.
- **`ensure_task` accepts `initial_status`.** `:3504-3516` gates it behind a `playbook-run:` dedup-key prefix and rejects otherwise. Omitting it from `EnsureTaskArgs` with `extra="forbid"` would make the playbook-run presentation path unrepresentable in a contract.
- **`add_dependency`'s `ok` key stays.** The result model mirrors what the handler returns today (`:2214-2222`). Changing the handler is out of scope (roadmap: adapters only). The *outcome* is what callers branch on; `ok` is just a field.
- **`gate_create`'s result is genuinely a union of two shapes** — the "skipped" early return (`src/commands/gate_commands.py:63-70` and again at `:88-93`) has `gate_id=None, skipped=True, created=False`, and the created path (`:112-117`) has `gate_id`, `gate`, `was_created`. Model it as one class with optional fields rather than a Pydantic union: with `extra="forbid"`, a union produces an unreadable error when a new key appears, and the *outcome* is what discriminates.
- **`gate_resolve`'s routing refusal is a first-class outcome**, not a generic rejection (`:190-197`). It is the only refusal among the ten that a playbook could reasonably want its own edge for, and it is a documented cross-phase invariant ("routing gates can only be resolved via task_route").
- **`list_tasks` has no failure outcome** because `_cmd_list_tasks` (`src/commands/task_commands.py:282`) has no error return path. Declaring a `rejected` outcome it can never produce would be a lie in the contract.
- **`list_tasks` returns task summaries as plain dicts**, so `tasks` / `by_project` are typed `dict[str, Any]` rather than a nested model. Contracting the task-summary shape is a separate, larger change and is not required by any playbook, none of which binds `list_tasks` output today.
- **`create_task`'s hierarchy errors** (`:925`, `:1523-1526`, `{"error": "hierarchy.<code>: ...", "code": "hierarchy.<code>"}`) map to `rejected` via `_outcome_of`, with `code` carried into `summary`.

### 4.2 Effect clauses per command

```python
"create_task":          (CreateClause(subject=EffectSubject.TASK),)
"ensure_task":          (CreateOrReuseClause(subject=EffectSubject.TASK, key_arg="dedup_key"),)
"edit_task":            (UpdateClause(subject=EffectSubject.TASK),)
"add_dependency":       (LinkClause(subject=EffectSubject.DEPENDENCY_EDGE, from_arg="task_id",
                                    to_arg="depends_on", relation_arg="dep_type"),)
"gate_create":          (CreateClause(subject=EffectSubject.GATE),
                         LinkClause(subject=EffectSubject.GATE_WAITER,
                                    from_arg="waiter_task_ids", to_arg="await_id",
                                    when=ClausePredicate(arg_present="waiter_task_ids")),)
"gate_resolve":         (ResolveClause(subject=EffectSubject.GATE, target_arg="gate_id"),)
"list_tasks":           (ReadClause(subject=EffectSubject.TASK_LIST),)
"get_downstream_tasks": (ReadClause(subject=EffectSubject.DOWNSTREAM_TASKS),)
"task_batch_commit":    (CreateClause(subject=EffectSubject.TASK_GRAPH),)
"task_route":           (UpdateClause(subject=EffectSubject.TASK_ROUTING),
                         ResolveClause(subject=EffectSubject.ROUTING_GATE, target_arg="task_id"),)
```

`task_route`'s second clause is not decoration: `_cmd_task_route` resolves every open routing gate for the task and returns their ids (`src/commands/task_commands.py:3648-3655`). It is exactly the behavior §1.5 says has nowhere to live today.

The `ensure_task` clause is the spec's worked example (`:360`): the renderer emits *"Create or reuse a task keyed by `dedup_key`"* only because `key_arg` is present and resolves against `args_model`, never as free text.

### 4.3 Side-effect and idempotency declarations

| Command | `side_effect` | `idempotency` | `retry_safe` |
|---|---|---|---|
| `create_task` | `create` | `mode="none"` — see note | `False` |
| `ensure_task` | `create` | `mode="keyed", key_field="dedup_key"` | `True` |
| `edit_task` | `update` | `mode="natural"` | `True` |
| `add_dependency` | `link` | `mode="natural"` (a duplicate edge of the same type is rejected, `:2170-2178`) | `True` |
| `gate_create` | `create` | `mode="keyed", key_field="await_id"` | `False` |
| `gate_resolve` | `resolve` | `mode="natural"` (docstring at `:166`: "Resolve a gate (idempotent)") | `True` |
| `list_tasks` | `read` | `mode="natural"` | `True` |
| `get_downstream_tasks` | `read` | `mode="natural"` | `True` |
| `task_batch_commit` | `composite` | `mode="keyed", key_field="proposal_id"` | `False` |
| `task_route` | `composite` | `mode="natural"` | `True` |

**Note on `create_task`.** It accepts a `dedup_key` argument and stores it on the row, but it does **not** look it up: only `_cmd_ensure_task` calls `find_task_by_dedup_key` (`src/commands/task_commands.py:3483`). Declaring `mode="keyed"` for `create_task` would be a false guarantee that dry-run (Package 4) and the explanation would both repeat. `tests/test_builtin_command_contracts.py::test_create_task_is_not_idempotent` pins `mode == "none"` and asserts the string `find_task_by_dedup_key` does not appear in `inspect.getsource(CommandHandler._cmd_create_task)`.

**Note on `gate_create`'s key.** `await_id` is the dedup key the handler passes to `db.create_gate` (`src/commands/gate_commands.py:74-84`), which returns `(gate_id, was_created)`. `retry_safe=False` because the routing-gate carve-out at `:48-72` recomputes the unrouted waiter set on each call, so a retry is not guaranteed to be the same operation.

### 4.4 Why `CommandHandler.execute` is not modified

Roadmap Package 1 lists `src/commands/handler.py` under Modify, and the obvious reading is "route dispatch through the registry". This plan does **not** do that, for two reasons:

1. Package 0 already owns the dispatch seam (`execute` at `:811`, the scope `ContextVar` set at `:841` and reset at `:987`, and authorization). Adding a second interception in the same method in the very next package makes both rollback boundaries useless.
2. Routing the ten commands through adapters at dispatch would change V1 execution: every current caller (Discord, MCP, CLI, `/api/execute`, `PipelineRunner`) would start receiving `CommandResult` instead of a dict. That is Package 4's cutover, and doing it here would break the "no V1 execution behavior changes" constraint that makes this package revertible.

The only change to `handler.py` is an added read-only accessor:

```python
def contracted_commands(self) -> frozenset[str]:
    """Names with a typed contract.  Used by the graph view, not by dispatch."""
    from src.commands.contracts import CONTRACTS
    return CONTRACTS.names()
```

This is a documented deviation from the roadmap's Modify list. The capability consult in `authorization.py` (§3.9) is the package's only touch on a dispatch-adjacent code path, and it is behavior-preserving by construction.

---

## 5. Security analysis

### 5.1 New boundaries introduced

| Boundary | Trust direction | Failure mode | Mitigation |
|---|---|---|---|
| Contract registry → `authorization.required_capability` | registry is server-owned Python, compiled in, never author-supplied | a contract could declare a *narrower* or *wider* capability than the command name, silently changing who may dispatch it | §3.9 pins `capability == name` for all ten with a test; `ExecutionContract` validation rejects a capability containing `WILDCARD_CHARS` (`src/profiles/capabilities.py:55`), matching the policy's own rejection at `:113` and `:286`; `required_capability` falls back to the command name for every unregistered command, so no existing command's gate changes |
| Explanation renderer → dashboard | server → operator browser | leaking a sensitive argument value in rendered intent | §3.8 redacts before the value reaches `ExplanationValue`; `raw` is `None` on a redacted value so Advanced view cannot recover it; the assertion is "the literal does not appear in `model_dump_json()`", not "the field says redacted" |
| Adapter → result model | handler dict → typed value | a handler adds a key that `extra="forbid"` rejects, turning a working command into `contract_violation` | Adapters build the model from an **explicit key projection**, never `model_validate(raw)` on the whole dict; unknown keys are dropped by the projection, and `contract_violation` is reserved for *missing or mistyped declared* fields. `tests/test_builtin_command_contracts.py::test_unknown_handler_key_does_not_violate_the_contract` pins this. |
| Event field contracts → explanation | event payload is producer-supplied; `event.task` is `asdict(task_row)` (§1.7) | an event field carrying a secret that nothing marked sensitive | The registry is server-owned Python; a producer cannot add a field spec. `event_field_is_sensitive` inherits from ancestors, so one `sensitive` marker on `task` covers the whole row. Unregistered nested paths render as `unresolved` with the raw expression, never as a resolved value — the renderer has no event instance to resolve against (§5.3). |
| `PIPELINE_COMMAND_WHITELIST` becomes derived | registry → compiler | a contract registered for a non-playbook purpose silently widens what a playbook may call | §5.2's set-equality test pins the ten literal names |

### 5.2 What an author still cannot do

Package 1 does not widen the authoring surface. A playbook author gains no new command, no new capability, and no way to influence a contract: contracts are Python in `src/commands/contracts/builtin.py`, not vault markdown. `PIPELINE_COMMAND_WHITELIST` becoming `CONTRACTS.names()` keeps the same ten names, pinned by a new assertion in `tests/test_playbook_compiler_scope.py`:

```python
def test_whitelist_is_exactly_the_ten_contracted_commands():
    assert PIPELINE_COMMAND_WHITELIST == frozenset({
        "create_task", "ensure_task", "edit_task", "add_dependency",
        "gate_create", "gate_resolve", "list_tasks", "get_downstream_tasks",
        "task_batch_commit", "task_route",
    })
```

so a contract added for a non-playbook purpose fails the build rather than quietly becoming callable from a playbook.

### 5.3 Explanation rendering is not a new execution path

`render_node_explanation` is pure: it takes a compiled node dict and the registry and returns a `NodeExplanation`. It never calls a handler, never touches the database, and never resolves a template against live data — an `event_ref` renders as *"this event's project"*, not as a resolved project id, because the graph view has no event in hand. Preview adapters (`src/commands/contracts/preview.py`) are the only contract surface that would read state, they are read-only by definition, and **no preview adapter is registered in this package** (`supports_preview=False` for all ten): the seam and its test double exist so Package 4's dry-run has a contract to build against. This keeps `POST /api/playbook/graph-view` free of any new side effect and any new query.

### 5.4 Denial and error surfaces leak nothing new

A contract lookup miss returns `explanation=None`, not an error — a node naming an uncontracted command renders exactly as it does today. `ContractRegistrationError` is raised at import time in the daemon process (`register_builtin_contracts` runs from `src/commands/contracts/__init__.py`) and is never returned to a client; §8 covers what an operator sees when it fires. `denial_result` (`src/commands/authorization.py:184`) is unchanged and still reports only the command name.

---

## 6. Storage, migrations, and database portability

**This package changes no database schema.** There is no Alembic revision, no upgrade, and no downgrade.

- Nothing is added to `src/database/tables.py`. `alembic revision --autogenerate -m ...` after this package's commits must produce an **empty** migration; `T-16` in §14.1 runs it as a check and discards the file.
- No query module under `src/database/queries/` is touched.
- Contracts, fingerprints, and explanations are computed in-process from compiled-in Python and are not persisted anywhere. The first persisted fingerprint is Package 2's `compiled_against.commands` on the artifact; the first persisted redaction decision is Package 3's receipt.
- **SQLite and PostgreSQL:** no consideration applies, because no SQL is issued on any new path. The graph-view endpoint's existing queries are unchanged, and §5.3 forbids adding one. The suites named in §14 run on the default SQLite test database; nothing in this package is storage-dependent, so no PostgreSQL-specific run is required. (Per `project_postgres_primary`, a change that *did* touch storage or concurrency would need one.)
- **Determinism across processes:** the fingerprint is `sha256` over `json.dumps(..., sort_keys=True)` of pure Python data. It does not depend on dict insertion order, `PYTHONHASHSEED`, locale, or platform. `test_fingerprint_is_stable_across_processes` builds the same contract twice in the same process; `test_golden_fingerprints` is the cross-process proof, because the pinned strings were produced by an earlier process.

---

## 7. API surface change

One endpoint changes shape: `POST /api/playbook/graph-view` (`operationId: playbook_graph_view`; response model `PlaybookGraphViewResponse`, `src/api/models/playbook.py:279`, registered at `:385`). The change is purely additive — one optional `explanation` object per node.

`RESPONSE_EXCLUDE_NONE` (`src/api/codegen.py:70`) already contains `playbook_graph_view`, and it is applied as `response_model_exclude_none=cmd_name in RESPONSE_EXCLUDE_NONE` (`:522`). A node without a contract therefore omits `explanation` from the wire entirely rather than sending `"explanation": null`. No change is needed there; `T-15` asserts it.

**Request** (unchanged):

```json
POST /api/playbook/graph-view
{"playbook_id": "default-pipeline", "direction": "TD", "show_prompts": true}
```

**Response — one node, before this package:**

```json
{
  "id": "per-task-review-create-review",
  "type": "entry",
  "symbol": "▶",
  "label": "per-task-review-create-review",
  "position": {"x": 0, "y": 0},
  "colors": {"fill": "#1f6feb", "stroke": "#1f6feb", "text": "#ffffff"},
  "entry": true,
  "terminal": false,
  "wait_for_human": false,
  "prompt_preview": "ensure_task",
  "out_degree": 2,
  "details": {
    "entry": true,
    "action": {
      "command": "ensure_task",
      "args": {
        "project_id": "{{event.project_id}}",
        "dedup_key": "review:task:{{event.task_id}}",
        "title": "Review: {{event.title}}",
        "description": "Reviewing task: {{event.task_id}}\nBranch: {{event.task.branch_name}}\nPR: {{event.task.pr_url}}\n\nRead the diff and either approve (close this task with a summary) or reject (call reopen_with_feedback on the reviewed task, then close this task).",
        "profile_id": "reviewer"
      },
      "on_success": "per-task-review-link-discovered-from",
      "on_failure": "per-task-review-done",
      "output": {"as": "review"},
      "for_each": null
    }
  }
}
```

**Response — the same node, after this package** (`details` byte-identical, `explanation` added; `prompt_preview` unchanged — the card preview change is client-side):

```json
{
  "id": "per-task-review-create-review",
  "…": "unchanged fields elided",
  "details": {"…": "byte-identical to the block above"},
  "explanation": {
    "kind": "command",
    "title": "Ensure a review task exists",
    "command": "ensure_task",
    "contract_fingerprint": "sha256:<64 hex>",
    "capability": "ensure_task",
    "effects": [
      {"operation": "create_or_reuse",
       "text": "Create or reuse a task keyed by \"dedup_key\"",
       "condition": null,
       "subject": "task"}
    ],
    "inputs": [
      {"field": "project_id", "label": "Project", "required": false,
       "value": {"kind": "event_ref", "text": "this event's project",
                 "raw": "{{event.project_id}}", "redacted": false}},
      {"field": "dedup_key", "label": "Deduplication key", "required": true,
       "value": {"kind": "template", "text": "\"review:task:\" + this event's task",
                 "raw": "review:task:{{event.task_id}}", "redacted": false}},
      {"field": "title", "label": "Title", "required": true,
       "value": {"kind": "template", "text": "\"Review: \" + this event's title",
                 "raw": "Review: {{event.title}}", "redacted": false}},
      {"field": "description", "label": "Description", "required": false,
       "value": {"kind": "template",
                 "text": "\"Reviewing task: \" + this event's task + … + this event's task branch + …",
                 "raw": "Reviewing task: {{event.task_id}}\nBranch: …", "redacted": false}},
      {"field": "profile_id", "label": "Agent profile", "required": false,
       "value": {"kind": "literal", "text": "reviewer", "raw": null, "redacted": false}}
    ],
    "result": {"name": "review", "fields": ["task_id", "created"]},
    "outcomes": [
      {"outcome": "success", "label": "Success", "classification": "success",
       "target_node_id": "per-task-review-link-discovered-from",
       "target_label": "per-task-review-link-discovered-from"},
      {"outcome": "failure", "label": "Failure", "classification": "failure",
       "target_node_id": "per-task-review-done",
       "target_label": "per-task-review-done"}
    ],
    "loop": null,
    "idempotency": "Repeating with the same deduplication key reuses the existing task",
    "retry": "Safe to retry",
    "unrendered_fields": []
  }
}
```

Both generated clients must be regenerated after `src/api/models/playbook.py` changes — `./scripts/regenerate-api-client.sh --offline` and `./scripts/regenerate-ts-client.sh --offline` — or `tests/test_api_client_contract.py::test_committed_openapi_json_matches_the_live_app_surface` fails. New schema components appearing in `openapi.json`: `NodeExplanation`, `ExplanationValue`, `ExplanationInput`, `ExplanationEffect`, `ExplanationOutcome`, `ExplanationResultBinding`, `ExplanationLoop`.

No other endpoint changes. `POST /api/playbook/show-graph` renders text and is not touched.

---

## 8. Observability and operator failure behavior

| Failure | Where it surfaces | What the operator sees | What they do |
|---|---|---|---|
| A contract fails validation or registration at import | daemon start, `register_builtin_contracts` | `ContractRegistrationError` with the command name, the rule violated, and (for rule 4) the clause `kind` and `subject` that has no renderer. The daemon does not start. | Fix the contract. This is a code defect and cannot be caused by data or config. |
| A node's command has no contract | graph view | The node renders exactly as today — raw action JSON in an **open** Advanced block, no explanation card, no error. | Nothing, unless it is one of the ten; then the whitelist test in §5.2 has already failed in CI. |
| The renderer raises on a malformed compiled node | `build_nodes` | `logger.warning("graph-view: could not render intent for node %s of %s", nid, playbook.id, exc_info=True)` and `explanation` is omitted for that node only. **One bad node never blanks the graph.** | Read the log; the Advanced view still shows the raw action. |
| A registered effect clause has no renderer | daemon start (registration) *and* `tests/test_playbook_explanation.py::test_every_clause_kind_has_a_renderer` | Import-time error; in CI, a named failing test. | Add the renderer arm; the `assert_never` in `render_effect` names the missing type. |
| Golden fingerprints drift | `tests/test_builtin_command_contracts.py::test_golden_fingerprints` | A diff of the pinned vs computed `sha256:` strings, with the failure message stating that regenerating `tests/fixtures/contracts/fingerprints.json` is a reviewed act because it will stale artifacts from Package 2 onward. | Confirm the execution change was intended; regenerate and review. |
| `playbooks.contract_intent` is off | graph view | No `explanation` on any node; the inspector's Advanced block renders open. Identical to pre-package behavior. | Turn it back on (§9). |

Logging is the existing `logging` module, at the module logger already present in `src/playbooks/graph_view.py`. No new metric, no new event type, no `metrics.tick` producer, and no new bus event: nothing in this package runs on a schedule or per task, so there is nothing to sample.

---

## 9. Feature flag ownership and removal

One flag, added to the existing `PlaybooksConfig` (`src/config.py:857`):

```python
@dataclass
class PlaybooksConfig:
    enabled: bool = False
    #: Attach contract-derived intent to graph-view nodes (Playbook V2
    #: Package 1).  Presentation-only; the payload is additive and the
    #: renderer is pure.  REMOVED IN PACKAGE 5, when the V2 graph API
    #: replaces ``build_graph_view`` and intent stops being optional.
    contract_intent: bool = True
```

- **Owner:** Package 1, Task A.
- **Default:** `True`. The flag is a kill-switch, not a rollout gate: intent is additive, pure, and required by the package exit gate, so shipping it off would mean shipping a gate that does not hold in the default configuration.
- **Scope:** read in exactly one place — `_cmd_playbook_graph_view` (`src/commands/playbook_commands.py:1268`), which forwards it as a `contract_intent` keyword through `build_graph_view` to `build_nodes`. Nothing else branches on it, and no module-level config lookup is introduced.
- **Off behavior:** `explanation` is absent from every node; `PlaybookGraphNode.explanation` stays `None` and is omitted from the wire by `RESPONSE_EXCLUDE_NONE`; the dashboard falls back to the pre-package rendering path, which Task B keeps working and tests (`T-19`, `T-20`).
- **Removal package:** **Package 5.** Its child plan (`docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md`) must delete the field, the branch in `build_nodes`, and the off-path dashboard fallbacks. `tests/test_playbook_graph_view.py::test_contract_intent_flag_off_omits_explanation` is the test that must be deleted with it.

---

## 10. Fixtures

### 10.1 Backend fixture playbook — `tests/fixtures/contracts/pipeline-intent.md`

The real `default-pipeline.md` `per-task-review` rule, verbatim except that the long `description` is trimmed so a golden explanation fixture stays reviewable. It preserves every shape the renderer must handle: an `event_ref`, a `template`, a `binding_ref`, a `for_each` with a `loop_ref`, both edge kinds, and a terminal. `fetch-downstream` is kept — without it, `outputs.downstream` would be an undefined binding and the `loop_ref` fixture would be dishonest.

````markdown
---
id: contract-intent-fixture
kind: pipeline
role: fixture
scope: system
triggers:
  - task.completed
---

# Contract intent fixture

Trimmed copy of `default-pipeline`'s `per-task-review` rule. Do not edit
without regenerating the goldens in this directory.

```json
{
  "rules": [
    {
      "id": "per-task-review",
      "on": "task.completed",
      "when": {"field": "event.task.branch_name", "truthy": true},
      "entry": "create-review",
      "nodes": {
        "create-review": {
          "command": "ensure_task",
          "args": {
            "project_id": "{{event.project_id}}",
            "dedup_key": "review:task:{{event.task_id}}",
            "title": "Review: {{event.title}}",
            "description": "Branch: {{event.task.branch_name}}",
            "profile_id": "reviewer"
          },
          "output": {"as": "review"},
          "on_success": "link-discovered-from",
          "on_failure": "done"
        },
        "link-discovered-from": {
          "command": "add_dependency",
          "args": {
            "task_id": "{{outputs.review.task_id}}",
            "depends_on": "{{event.task_id}}",
            "dep_type": "discovered-from"
          },
          "on_success": "fetch-downstream",
          "on_failure": "done"
        },
        "fetch-downstream": {
          "command": "get_downstream_tasks",
          "args": {"task_id": "{{event.task_id}}"},
          "output": {"as": "downstream"},
          "on_success": "gate-downstream",
          "on_failure": "done"
        },
        "gate-downstream": {
          "command": "gate_create",
          "for_each": {"source": "outputs.downstream.tasks", "as": "dep"},
          "args": {
            "project_id": "{{event.project_id}}",
            "gate_type": "task",
            "title": "Awaiting review of {{event.task_id}}",
            "await_id": "{{outputs.review.task_id}}",
            "waiter_task_ids": ["{{outputs.dep.id}}"]
          },
          "on_success": "done",
          "on_failure": "done"
        },
        "done": {"terminal": true}
      }
    }
  ]
}
```
````

### 10.2 Golden explanation — `tests/fixtures/contracts/explanation-create-review.json`

The expected `NodeExplanation` for node id `per-task-review-create-review` — the rule-id prefix is applied by `_normalize_nodes` (`src/playbooks/pipeline_compiler.py:239`). `contract_fingerprint` is asserted against the registry at test time rather than pinned in the file, so a contract change breaks one focused test instead of every golden.

```json
{
  "kind": "command",
  "title": "Ensure a review task exists",
  "command": "ensure_task",
  "capability": "ensure_task",
  "effects": [
    {
      "operation": "create_or_reuse",
      "text": "Create or reuse a task keyed by \"dedup_key\"",
      "condition": null,
      "subject": "task"
    }
  ],
  "inputs": [
    {"field": "project_id", "label": "Project", "required": false,
     "value": {"kind": "event_ref", "text": "this event's project", "raw": "{{event.project_id}}", "redacted": false}},
    {"field": "dedup_key", "label": "Deduplication key", "required": true,
     "value": {"kind": "template", "text": "\"review:task:\" + this event's task", "raw": "review:task:{{event.task_id}}", "redacted": false}},
    {"field": "title", "label": "Title", "required": true,
     "value": {"kind": "template", "text": "\"Review: \" + this event's title", "raw": "Review: {{event.title}}", "redacted": false}},
    {"field": "description", "label": "Description", "required": false,
     "value": {"kind": "template", "text": "\"Branch: \" + this event's task branch", "raw": "Branch: {{event.task.branch_name}}", "redacted": false}},
    {"field": "profile_id", "label": "Agent profile", "required": false,
     "value": {"kind": "literal", "text": "reviewer", "raw": null, "redacted": false}}
  ],
  "result": {"name": "review", "fields": ["task_id", "created"]},
  "outcomes": [
    {"outcome": "success", "label": "Success", "classification": "success",
     "target_node_id": "per-task-review-link-discovered-from", "target_label": "per-task-review-link-discovered-from"},
    {"outcome": "failure", "label": "Failure", "classification": "failure",
     "target_node_id": "per-task-review-done", "target_label": "per-task-review-done"}
  ],
  "loop": null,
  "idempotency": "Repeating with the same deduplication key reuses the existing task",
  "retry": "Safe to retry",
  "unrendered_fields": []
}
```

`"this event's task branch"` is produced from the §3.7 `description` for `task.completed` → `task` → `branch_name`, which is why that description must read as a noun phrase, not a sentence.

### 10.3 Golden explanation — `tests/fixtures/contracts/explanation-gate-downstream.json`

Covers the `for_each` node, the conditional clause, and the `loop_ref` value kind. It pins:

- `loop` to `{"source_text": "each item in downstream.tasks", "item_binding": "dep", "source_raw": "outputs.downstream.tasks"}`;
- two effects, the second with `"condition": "when waiter_task_ids is provided"` from the `arg_present` predicate;
- the `waiter_task_ids` input with `"kind": "loop_ref"` and `"text": "each dep's id"`;
- `"result": null` (the node has no `output` binding);
- `"idempotency": "Repeating with the same await_id reuses the existing gate"` and `"retry": "Not safe to retry"`.

### 10.4 Fingerprint goldens — `tests/fixtures/contracts/fingerprints.json`

```json
{
  "add_dependency": "sha256:<64 hex>",
  "create_task": "sha256:<64 hex>",
  "edit_task": "sha256:<64 hex>",
  "ensure_task": "sha256:<64 hex>",
  "gate_create": "sha256:<64 hex>",
  "gate_resolve": "sha256:<64 hex>",
  "get_downstream_tasks": "sha256:<64 hex>",
  "list_tasks": "sha256:<64 hex>",
  "task_batch_commit": "sha256:<64 hex>",
  "task_route": "sha256:<64 hex>"
}
```

Written once, in `T-8`, from the implementation's own output, and then treated as a pinned constant. The test's failure message must say: *"Regenerating this file stales every Package-2 artifact compiled against the old registry fingerprint. Confirm the execution-contract change was intended before regenerating."*

### 10.5 Dashboard fixture

`dashboard/src/pages/playbook-graph/__tests__/fixtures.ts` gains `explanationNode`, transcribed from the §10.2 golden as a TS literal, plus a `pipelineGraph` variant whose nodes carry `explanation`. The existing `node()` helper keeps `explanation` undefined, so every current test continues to exercise the no-explanation fallback path — which is how the flag-off behavior in §9 stays covered.

---

## 11. Parallel task split (roadmap §7)

Two implementation tasks. §3 is the whole contract between them; neither needs to ask the other a question.

| | **Task A — backend contracts and intent** | **Task B — contract intent in the graph inspector** |
|---|---|---|
| Commits | 1–4 (§12.1–§12.4) | 5 (§12.5) |
| Owns | everything under `src/`, all new `tests/test_*.py`, `tests/fixtures/contracts/`, `openapi.json`, `packages/aq-client/`, `dashboard/src/api/client` (regenerated only) | everything under `dashboard/src/pages/playbook-graph/` |
| Blocked on | Package 0 exit gate (**merged** — `88ed4db7`) | **nothing** — starts immediately against §3.6's verbatim TS interface |
| Reconciliation | — | after Task A's commit 4 lands, Task B replaces its local `explanation.ts` interface with the generated `NodeExplanation` import and keeps one assignability assertion proving the two agree |

The only file both tasks could touch is `dashboard/src/api/client` — generated, owned by Task A, never hand-edited. Task B never edits `src/`.

**How Task B works before Task A lands.** `dashboard/src/pages/playbook-graph/explanation.ts` declares the §3.6 interface locally and re-exports it. Every component imports from there, not from `../../api/client` (which `dashboard/src/pages/playbook-graph/types.ts:2` already imports `PlaybookGraphNode` from). When Task A's regenerated client arrives, `explanation.ts` becomes:

```ts
import type { NodeExplanation as Generated } from "../../api/client";
export type NodeExplanation = Generated;

// Fails `npm run typecheck` if the generated model drifts from §3.6.
const _shapeCheck: Generated = {} as LocalNodeExplanation;
```

so the reconciliation is a two-line edit plus `npm run typecheck`, not a rewrite.

---

## 12. Tasks

Every task is red-then-green. Run the named failing test and see it fail for the stated reason before writing implementation.

### 12.1 Commit 1 — `test: specify playbook command contracts`

All five suites land failing, with `pytest.mark.xfail(strict=True)` on every test, removed in the commit that implements each. `strict=True` means an accidentally-passing test is a failure, which is what keeps a placeholder from being mistaken for coverage.

#### T-1 — `tests/test_command_contracts_registry.py`

Asserts against §3.1, §3.3, §3.4, §3.5:

- `test_register_rejects_duplicate_name` — two `CommandRegistration`s with the same name raise `ContractRegistrationError`. *Fails now with `ImportError: cannot import name 'ContractRegistry' from 'src.commands.contracts'`.*
- `test_register_rejects_name_mismatch` — `CommandRegistration(name="a", contract=<named "b">)` raises.
- `test_register_rejects_reserved_outcome` — a contract declaring outcome `contract_violation` raises at model validation.
- `test_register_rejects_contract_without_success_outcome`.
- `test_register_rejects_duplicate_outcome_names`.
- `test_register_rejects_keyed_idempotency_without_key_field` — and the converse (`key_field` set with `mode="natural"`).
- `test_register_rejects_sensitive_arg_not_in_args_model`.
- `test_register_rejects_receipt_projection_field_not_in_result_model`.
- `test_register_rejects_effect_clause_naming_an_unknown_arg` — `LinkClause(from_arg="nope", ...)` raises.
- `test_register_rejects_wildcard_capability` — `capability="task_*"` raises; reuses `WILDCARD_CHARS` from `src/profiles/capabilities.py`.
- `test_register_requires_preview_adapter_when_supports_preview` — and the converse.
- `test_register_rejects_unrenderable_clause` — registering a contract whose clause `can_render` rejects raises `ContractRegistrationError` naming the clause `kind`. Stays `xfail` through T-7 (whose `can_render` is the permissive stub) and turns green in T-13, when the real renderer is wired in; this is the roadmap outcome "Fail contract registration when an effect cannot be rendered".
- `test_effect_clause_types_matches_the_union` (§3.4).
- `test_fingerprint_is_stable_across_processes` — two `ExecutionContract`s built from structurally identical models produce the same string.
- `test_presentation_change_does_not_move_the_fingerprint` — mutate `CommandPresentation.title`, `arg_labels`, `outcome_labels`, `subject_labels`, and a `Field(description=...)` on the args model; assert the fingerprint is byte-identical. **This is the package's central assertion.**
- `test_execution_change_moves_the_fingerprint` — parameterized over: adding an arg field, changing an arg type, making an optional arg required, adding an outcome, changing a capability, flipping `retry_safe`, changing `idempotency.mode`, adding an effect clause, reordering two effect clauses, adding a `sensitive_args` entry, adding a `receipt_projection` entry. Each must produce a different fingerprint.
- `test_enum_member_order_is_significant` — reordering an `enum` in an args model changes the fingerprint (proves `_strip` does not sort enums).
- `test_required_list_order_is_not_significant`.
- `test_registry_fingerprint_covers_every_contract` — registering one more contract changes `registry_fingerprint()`.

**Verify:** `aq test tests/test_command_contracts_registry.py -q` → all xfail.

#### T-2 — `tests/test_builtin_command_contracts.py`

Asserts against §4:

- `test_all_ten_commands_are_registered` — `CONTRACTS.names() == PIPELINE_COMMAND_WHITELIST`.
- `test_every_contract_has_a_handler` — for each name, `getattr(CommandHandler, f"_cmd_{name}", None)` is not `None`, matching `has_command`'s own probe (`src/commands/handler.py:669`).
- `test_capability_equals_command_name_for_all_ten` (§3.9).
- `test_create_task_is_not_idempotent` (§4.3 note).
- `test_golden_fingerprints` — each of the ten equals the pinned string in `tests/fixtures/contracts/fingerprints.json`.
- `test_adapter_maps_add_dependency_ok_key_to_linked` — feed the adapter the literal dict `_cmd_add_dependency` returns (`{"ok": True, "task_id": "t1", "depends_on": "t2", "dep_type": "discovered-from", "reason": None, "task_title": "A", "depends_on_title": "B"}`, `src/commands/task_commands.py:2214`); assert `result.outcome == "linked"` and `result.classification(contract) is OutcomeClass.SUCCESS`. **This is the §1.2 regression test**: the same input must not classify differently in any caller.
- `test_adapter_maps_duplicate_dependency_error_to_already_linked` — the `"Dependency already exists: ..."` error dict (`:2170-2179`) maps to `already_linked`, classified `success`, not to `rejected`.
- `test_adapter_maps_gate_create_skip_to_skipped` — the `{"success": True, "skipped": True, "gate_id": None, "created": False, "reason": "all waiter tasks are already routed"}` early return (`src/commands/gate_commands.py:63-70`).
- `test_adapter_maps_gate_resolve_routing_refusal` — the routing-gate refusal (`:190-197`) maps to `refused_routing_gate`, not `rejected`.
- `test_adapter_maps_ensure_task_created_and_reused` — `{"success": True, "task_id": "x", "created": True}` → `created`; `created=False` → `reused`.
- `test_unknown_handler_key_does_not_violate_the_contract` (§5.1) — a raw dict with an extra `"telemetry"` key still produces the declared outcome.
- `test_missing_declared_result_field_is_a_contract_violation` — `{"success": True}` from `ensure_task` (no `task_id`) yields `outcome == "contract_violation"` and a `summary` naming the field.
- `test_no_adapter_touches_the_database` — every adapter is driven with a fake handler whose `execute` returns a canned dict and whose `db` attribute raises on any access.

**Verify:** `aq test tests/test_builtin_command_contracts.py -q` → all xfail.

#### T-3 — `tests/test_playbook_explanation.py`

Asserts against §3.4, §3.6, §3.8:

- `test_every_clause_kind_has_a_renderer` — iterates `EFFECT_CLAUSE_TYPES` and asserts `can_render` for a constructed instance of each. *Fails with `ImportError` now; later fails loudly when a clause type is added without a renderer.*
- `test_render_effect_is_exhaustive_over_the_union` — a synthetic object that is not a clause raises rather than falling through to a default string.
- `test_ensure_task_node_matches_the_golden` — compile `tests/fixtures/contracts/pipeline-intent.md`, render `per-task-review-create-review`, compare to `tests/fixtures/contracts/explanation-create-review.json` with `contract_fingerprint` substituted from the registry.
- `test_for_each_node_matches_the_golden` — the `gate-downstream` golden (§10.3).
- `test_every_arg_key_appears_in_inputs_or_unrendered` (§3.6 invariant 3).
- `test_explanation_value_text_is_never_empty` (invariant 4).
- `test_contract_with_no_effects_falls_back_to_canonical_rendering` — a synthetic contract with `effects=()` still produces exactly one `ExplanationEffect` whose `operation` is its `side_effect` value and whose text names every argument.
- `test_sensitive_argument_is_redacted_everywhere` (§3.8) — synthetic contract with `sensitive_args={"token"}`; asserts `text == "[redacted]"`, `raw is None`, and that the secret literal appears nowhere in `NodeExplanation.model_dump_json()`.
- `test_event_sensitive_path_is_redacted` — a synthetic event type whose `secret` field is `sensitive: true`; an argument bound to `{{event.secret}}` is redacted even though `sensitive_args` is empty.
- `test_uncontracted_command_renders_no_explanation` — a node naming `some_plugin_command` yields `None`.
- `test_terminal_node_renders_no_explanation`.
- `test_malformed_node_returns_none_and_does_not_raise` — a node whose `action["args"]` is a list.

**Verify:** `aq test tests/test_playbook_explanation.py -q` → all xfail.

#### T-4 — `tests/test_contract_intent_parity.py`

Asserts §3.6 invariants 1 and 2 across module boundaries:

- `test_displayed_fingerprint_is_the_registry_fingerprint` — for every node of the fixture playbook, `node["explanation"]["contract_fingerprint"] == CONTRACTS.fingerprint(node["details"]["action"]["command"])`.
- `test_explanation_outcomes_match_rendered_action_edges` — for every node,
  `{o["target_node_id"] for o in expl["outcomes"] if o["target_node_id"]}` equals
  `{e["target"] for e in build_edges(pb) if e["source"] == nid and e["edge_type"] in {"success", "failure"}}`.
- `test_no_node_displays_intent_without_a_registration` — any node with a non-null `explanation` has `CONTRACTS.get(command) is not None`.
- `test_default_pipeline_renders_intent_for_every_action_node` — run the same three assertions over the shipped `src/prompts/default_playbooks/default-pipeline.md`, not only the fixture. This is what makes M1 evidence about the real pipeline.

**Verify:** `aq test tests/test_contract_intent_parity.py -q` → all xfail.

#### T-5 — `tests/test_event_field_contracts.py`

Asserts §3.7:

- `test_contracted_event_types_are_fully_described` — for each of the four, every `required` + `optional` name has a `fields` entry with a non-empty `description`.
- `test_shipped_playbook_event_paths_resolve` — parameterized over the §3.7 table: `("task.completed", "task.branch_name")`, `("task.completed", "task.pr_url")`, `("task.completed", "project_id")`, `("task.completed", "task_id")`, `("task.completed", "title")`, `("spec.approved", "spec_path")`, `("proposal.ready", "proposal_id")`, `("gate.resolved", "await_id")`.
- `test_hydrated_field_is_not_in_required_or_optional` — `"task"` has `hydrated: True` in `fields` and appears in **neither** `required` nor `optional` for `task.completed` (§1.7).
- `test_resolve_event_path_returns_none_for_unregistered_event`.
- `test_resolve_event_path_returns_none_for_unknown_path`.
- `test_event_field_is_sensitive_inherits_from_ancestors` — marking a parent object sensitive makes every child path sensitive.
- `test_validate_event_behaviour_is_unchanged` — a payload table copied from `tests/test_emit_schema_compliance.py`, asserted to yield an identical error list with and without the `fields` addition, including `strict_extras=True` and including a payload that *does* carry `task`, which must still be reported as an unexpected field.

**Verify:** `aq test tests/test_event_field_contracts.py -q` → all xfail.

**Commit gate:**
```
aq test tests/test_command_contracts_registry.py tests/test_builtin_command_contracts.py \
        tests/test_playbook_explanation.py tests/test_contract_intent_parity.py \
        tests/test_event_field_contracts.py -q
```
→ all xfail, zero errors, zero unexpected passes (`XPASS` is a failure under `strict=True`).

---

### 12.2 Commit 2 — `feat: register typed built-in command contracts`

#### T-6 — `src/commands/contracts/models.py`

Write §3.1, §3.2, §3.3, §3.4, §3.8 verbatim. No registry, no adapters. Drop `xfail` from the model-level tests in T-1.

**Verify:** `aq test tests/test_command_contracts_registry.py -k "fingerprint or reject or enum or required_list or clause_types" -q` → green.

#### T-7 — `src/commands/contracts/registry.py` and `__init__.py`

Write §3.5. `__init__.py` re-exports `CommandContract`, `CommandRegistration`, `CommandResult`, `ContractRegistry`, `CONTRACTS`, `ContractRegistrationError`, and calls `register_builtin_contracts(CONTRACTS)` once at import. The deferred `can_render` import is stubbed to `lambda _clause: True` in this task and wired for real in T-13; leave a `# T-13` marker so the stub cannot be mistaken for the final state.

**Verify:** `aq test tests/test_command_contracts_registry.py -q` → green except the renderer-dependent tests.

#### T-8 — `src/commands/contracts/builtin.py`

Write the twenty models and ten registrations of §4.1–§4.3, plus `register_builtin_contracts(registry: ContractRegistry) -> None`.

Each adapter is at most twenty lines and follows §3.2's three steps exactly:

```python
async def _invoke_ensure_task(
    args: EnsureTaskArgs, ctx: CommandContext
) -> CommandResult[EnsureTaskValue]:
    raw = await _handler().execute("ensure_task", args.model_dump(exclude_none=True))
    outcome = _ensure_task_outcome(raw)
    if outcome == "rejected":
        return CommandResult(
            outcome=outcome,
            value=EnsureTaskValue(task_id="", created=False),
            summary=str(raw.get("error", "rejected")),
        )
    try:
        value = EnsureTaskValue(task_id=raw["task_id"], created=bool(raw["created"]))
    except (KeyError, TypeError, ValidationError) as exc:
        return CommandResult(
            outcome="contract_violation",
            value=EnsureTaskValue(task_id="", created=False),
            summary=f"ensure_task result did not match its contract: {exc}",
        )
    return CommandResult(
        outcome=outcome,
        value=value,
        summary=f"{'Created' if value.created else 'Reused'} task {value.task_id}",
    )
```

`_handler()` resolves the process `CommandHandler`; in tests it is monkeypatched with a fake returning the literal dicts from T-2. **No adapter reaches the database directly** (`test_no_adapter_touches_the_database`).

Write `tests/fixtures/contracts/fingerprints.json` from the implementation's output in this task, and review the ten values before committing.

#### T-9 — `src/commands/contracts/preview.py`

Defines `PreviewAdapter`, `PreviewUnavailable`, and a `no_preview` sentinel. Registers **no** preview adapters (§5.3). Includes `preview_stub` used only by T-3's synthetic-contract redaction test, so the seam has a live consumer and is not untested dead code.

#### T-10 — Derive the whitelist and the capability

- `src/playbooks/pipeline_compiler.py:42` becomes `PIPELINE_COMMAND_WHITELIST: frozenset[str] = CONTRACTS.names()`, with the literal ten kept in a comment above it and pinned by §5.2's test.
- `src/commands/authorization.py` gains `required_capability` and the two call-site edits of §3.9.
- `src/commands/handler.py` gains `contracted_commands()` (§4.4).

**Verify:**
```
aq test tests/test_command_contracts_registry.py tests/test_builtin_command_contracts.py -q
aq test tests/test_playbook_compiler_scope.py tests/test_playbook_runner.py tests/test_assignment_playbook_compiler.py -q
aq test tests/test_command_capability_authorization.py -q
ruff check src/commands/contracts src/playbooks/pipeline_compiler.py src/commands/authorization.py src/commands/handler.py
```
Expected: T-1 and T-2 green; the three playbook suites unchanged (the whitelist set is identical); the Package 0 authorization suite unchanged (every capability equals its command name).

---

### 12.3 Commit 3 — `feat: add typed event field contracts`

**Deviation from the roadmap's four-commit sequence:** this commit is new. The roadmap folds "Enrich event schemas" into an unnamed commit. Splitting it keeps `src/event_schemas.py` — a module every emitter validates against — revertible without touching the contract registry.

#### T-11 — `EventFieldSpec`, `resolve_event_path`, `event_field_is_sensitive`, `CONTRACTED_EVENT_TYPES`

Write §3.7. `validate_event` is not touched; its `strict_extras` allow-list still reads `required + optional + META_FIELDS` only.

**Verify:** `aq test tests/test_event_field_contracts.py -k "resolve or sensitive or unregistered" -q` → green.

#### T-12 — Populate `fields` for the four contracted event types

`task.completed` (`src/event_schemas.py:88`) becomes:

```python
"task.completed": {
    "required": ["task_id", "project_id", "title"],
    "optional": ["agent_id", "agent_type"],
    "fields": {
        "task_id":    {"type": "string", "description": "the completed task"},
        "project_id": {"type": "string", "description": "the project the task belongs to"},
        "title":      {"type": "string", "description": "the task title"},
        "agent_id":   {"type": "string", "description": "the agent that completed the task"},
        "agent_type": {"type": "string", "description": "the profile id of that agent"},
        "task": {
            "type": "object",
            "description": "the completed task row",
            # Added by the pipeline dispatcher (src/orchestrator/core.py:854-870),
            # never by an emitter — hence `hydrated` and NOT in `optional`.  §1.7.
            "hydrated": True,
            "fields": {
                "branch_name": {"type": "string", "description": "task branch"},
                "pr_url":      {"type": "string", "description": "task pull request"},
            },
        },
    },
},
```

Only the two nested fields a shipped playbook reads are described. The whole `Task` row is present at runtime; describing all of it would be unreviewable and would imply a stability guarantee the dataclass does not offer. `resolve_event_path("task.completed", "task.retry_count")` returns `None`, and the renderer shows `unresolved` with the raw expression — visible, not guessed.

Descriptions are noun phrases, lowercase, no trailing period: the renderer composes `"this event's " + description` (§13's value table), so `"the completed task"` yields *"this event's completed task"*.

`spec.approved`, `proposal.ready`, and `gate.resolved` gain `fields` blocks over their existing `required` + `optional` names, with no hydrated entries.

**Verify:**
```
aq test tests/test_event_field_contracts.py -q
aq test tests/test_emit_schema_compliance.py tests/test_event_schemas.py \
        tests/test_event_schema_registry_validation.py \
        tests/test_playbook_eventbus_subscription.py tests/test_playbook_manager_triggers.py -q
ruff check src/event_schemas.py tests/test_event_field_contracts.py
```
Expected: all green, and the four pre-existing event suites unchanged — that is the proof `validate_event` did not move.

---

### 12.4 Commit 4 — `feat: derive playbook intent from contracts`

#### T-13 — `src/playbooks/explanation.py`

The renderer. Public surface, locked:

```python
def can_render(clause: EffectClause) -> bool: ...

def render_effect(
    clause: EffectClause,
    args: Mapping[str, Any],
    presentation: CommandPresentation,
) -> ExplanationEffect: ...

def render_node_explanation(
    node_id: str,
    node: Mapping[str, Any],          # exactly PlaybookNode.to_dict()
    *,
    event_type: str | None = None,    # for §3.8 event-path redaction
    registry: ContractRegistry = CONTRACTS,
    node_labels: Mapping[str, str] | None = None,
) -> NodeExplanation | None: ...
```

`node` is the dict `PlaybookNode.to_dict()` produces (`src/playbooks/models.py:316`). For a pipeline node, `command`, `args`, `on_success`, `on_failure`, `output`, and `for_each` all live **inside** `node["action"]` — `_normalize_nodes` (`src/playbooks/pipeline_compiler.py:239-249`) puts them there and leaves the top-level `PlaybookNode.for_each` / `.output` as `None`. The renderer reads `node["action"]` first and falls back to the top-level keys, so an assignment-routing node (which uses the top-level fields) is not silently ignored.

`render_node_explanation` returns `None` for a terminal node, a node without an `action`, a node whose command has no registration, and any node whose shape raises — it catches, logs (§8), and returns `None`. It never raises to `build_nodes`.

Value classification is a pure function of the raw compiled argument:

| Raw shape | `ExplanationValue.kind` | `text` |
|---|---|---|
| not a string | `literal` | `json.dumps(value)` |
| no `{{ }}` | `literal` | the string |
| exactly one `{{event.PATH}}`, whole string | `event_ref` | `"this event's " + description` from §3.7 when `resolve_event_path` hits, else `"this event's " + PATH.replace(".", " ")` |
| exactly one `{{outputs.NAME}}` / `{{outputs.NAME.field}}` where `NAME` is the enclosing `for_each`'s `as` binding | `loop_ref` | `"each " + NAME + "'s " + field` |
| exactly one `{{outputs.NAME.field}}` otherwise | `binding_ref` | `NAME + "'s " + field` |
| mixed literal and `{{ }}` | `template` | quoted literal segments joined to the rendered refs with `" + "` |
| a `{{ }}` the classifier cannot parse | `unresolved` | the raw expression verbatim |

`unresolved` is the safety valve: the renderer never guesses and never drops. A list-valued argument whose single element is a reference (`["{{outputs.dep.id}}"]`, as `gate_create` uses) classifies as that element's kind — otherwise the `for_each` fixture's only loop reference would render as an opaque `literal`.

Then remove the T-7 `can_render` stub and wire `registry.register()` to the real one.

**Verify:** `aq test tests/test_playbook_explanation.py -q` → green.

#### T-14 — `src/playbooks/graph_view.py`

There is no module-level `get_config()` in this tree — config is injected (`src/config.py` exposes only `load_config`, and every reader goes through `self.config`). The flag is therefore **threaded as a keyword**, not read from a global:

- `build_nodes(playbook, positions, *, show_prompts=True, max_prompt_len=60, contract_intent: bool = True)`
- `build_graph_view(playbook, *, ..., contract_intent: bool = True)` — added to the existing keyword-only block (`:658-668`) and forwarded to `build_nodes`
- `_cmd_playbook_graph_view` (`src/commands/playbook_commands.py:1268`) passes `contract_intent=self.config.playbooks.contract_intent`; `self.config` is already used in that mixin (`:663`)

Both defaults are `True`, so every existing direct caller of `build_nodes` / `build_graph_view` — including the docstring examples at `src/playbooks/graph_view.py:27-40` and `src/playbooks/__init__.py:5` — keeps working unchanged.

In `build_nodes`, immediately after `node_data["details"] = node.to_dict()` (`:325`):

```python
if contract_intent:
    explanation = render_node_explanation(nid, node_data["details"], node_labels=labels)
    if explanation is not None:
        node_data["explanation"] = explanation.model_dump(mode="json")
```

`labels` is `{nid: nid}` today — V1 node ids are their own labels (`node_data["label"] = nid`, `:288`) — and the parameter exists so Package 5 can pass real titles without a signature change. `build_edges` is **not** touched.

**Verify:** `aq test tests/test_playbook_graph_view.py tests/test_api_playbook_graph_view.py -q`, including a new `test_contract_intent_flag_off_omits_explanation` (§9).

#### T-15 — `src/api/models/playbook.py`

Add the §3.6 models and `PlaybookGraphNode.explanation`. Assert that `RESPONSE_EXCLUDE_NONE` (`src/api/codegen.py:70`) still contains `playbook_graph_view`, so a `None` explanation is omitted from the wire rather than serialized as `null`.

**Verify:** `aq test tests/test_api_playbook_graph_view.py -q`

#### T-16 — Regenerate clients; prove no migration

```bash
./scripts/regenerate-api-client.sh --offline
./scripts/regenerate-ts-client.sh --offline
alembic revision --autogenerate -m "pkg1 no-op check"   # must be empty; delete the file
```

**Verify:** `aq test tests/test_api_client_contract.py -q` → `test_committed_openapi_json_matches_the_live_app_surface` green. Then `aq test tests/test_contract_intent_parity.py -q` → green. The autogenerated revision must contain no operations; delete it and do not commit it (§6).

---

### 12.5 Commit 5 — `feat: expose contract intent in the current graph inspector`

Task B. Presentation only; no `src/` file is touched.

#### T-17 — `dashboard/src/pages/playbook-graph/explanation.ts`

The §3.6 TypeScript interface, plus two pure helpers used by both components: `effectLine(e: ExplanationEffect): string` (appends `" — " + e.condition` when present) and `inputLine(i: ExplanationInput): string`. After Task A lands, replace the local interface with the generated import and the assignability check (§11).

#### T-18 — `NodeExplanationCard.tsx` and `__tests__/NodeExplanationCard.test.tsx`

A read-only card rendering, in order: `title`; `effects` (operation icon + `text`, with `condition` as a muted suffix); `inputs` as `label → value.text`; `result` as *Save as "review"* with the available fields listed; `outcomes` as `label → target_label`; `idempotency` and `retry` as a footer; `unrendered_fields` as a plainly labelled "Other fields" list.

Failing assertions:

- renders `Create or reuse a task keyed by "dedup_key"` for the §10.2 fixture;
- renders `this event's project` for `project_id`, and **not** `{{event.project_id}}`, in the default view;
- renders both outcome targets;
- renders `[redacted]` and **not** the secret, for a fixture input with `redacted: true`;
- lists every `unrendered_fields` entry;
- renders the loop line for the §10.3 fixture;
- returns `null` when `explanation` is undefined.

#### T-19 — `PlaybookNodeInspector.tsx`

Replace the `Action` `Payload` block (`:157-161`) with `<NodeExplanationCard explanation={node.explanation} />`, and move the raw `d.action`, `d.for_each`, and `d.output` payloads under a `<details>` **Advanced** disclosure, closed by default. When `node.explanation` is undefined the Advanced block renders **open**, so an uncontracted node is no worse off than today — which is also the flag-off path (§9).

Failing assertions in `__tests__/PlaybookNodeInspector.test.tsx`:

- a node with an explanation shows the effect text and does **not** show `{{event.project_id}}` in the default view;
- the raw action JSON is still reachable — the Advanced disclosure contains it;
- a node without an explanation renders the raw action visibly, as today (every existing assertion in this file must keep passing unchanged).

#### T-20 — `PlaybookStepNode.tsx`

`preview` becomes `explanation?.title ?? actionCommand(node.details.action) ?? node.prompt_preview` (`:29`), with the first effect's `text` as a second line when present. `actionCommand` (`:17`) stays — it is the fallback.

Failing assertion (add `__tests__/PlaybookStepNode.test.tsx`; the file does not exist today): the fixture card shows `Ensure a review task exists`, not `ensure_task`; a card without an explanation still shows `ensure_task`.

**Verify (from `dashboard/`):**
```
npm test -- src/pages/playbook-graph
npm run typecheck
npm run lint
npm run build
```

---

## 13. Mapping to the roadmap exit gate

Roadmap §5, Package 1:

> Every command usable by a playbook has one typed registration. The UI cannot display separately authored intent for those commands, and contract/explanation exhaustiveness tests fail when a new effect kind is introduced without a renderer.

| Exit-gate clause | Discharged by | Proof |
|---|---|---|
| Every command usable by a playbook has one typed registration | §4.1–§4.3 (ten contracts); §12.2 T-10 (`PIPELINE_COMMAND_WHITELIST = CONTRACTS.names()`) | `test_all_ten_commands_are_registered`; `test_whitelist_is_exactly_the_ten_contracted_commands`; `test_every_contract_has_a_handler` |
| …**one** registration, not two | §3.5 rule 1 (duplicate name raises) | `test_register_rejects_duplicate_name` |
| The UI cannot display separately authored intent for those commands | §3.6 (the payload is built only by `render_node_explanation`); §12.5 T-19/T-20 (components read `explanation`, never re-derive semantics) | `test_no_node_displays_intent_without_a_registration`; `test_displayed_fingerprint_is_the_registry_fingerprint`; `test_default_pipeline_renders_intent_for_every_action_node`; the `NodeExplanationCard` assertions |
| Exhaustiveness tests fail when a new effect kind lacks a renderer | §3.5 registration rule 4 + `assert_never` in `render_effect` | `test_every_clause_kind_has_a_renderer`; `test_render_effect_is_exhaustive_over_the_union`; `test_effect_clause_types_matches_the_union` |

Roadmap §5 Required outcomes, one row each:

| Required outcome | Where | Proof |
|---|---|---|
| Register contracts for the ten commands | §4.1 | `test_all_ten_commands_are_registered` |
| Typed argument and result models for each | §4.1 | `test_golden_fingerprints` (schemas are inside the fingerprint); the six adapter-mapping tests |
| Wrap legacy handlers only at the contract boundary | §3.2, §4.4 | `test_no_adapter_touches_the_database`; the ten `_cmd_*` bodies appear in no commit diff |
| Derive required capabilities from command registrations | §3.9 | `test_capability_equals_command_name_for_all_ten`; `tests/test_command_capability_authorization.py` unchanged |
| Canonical execution fingerprint excluding presentation copy | §3.3 | `test_presentation_change_does_not_move_the_fingerprint`; `test_execution_change_moves_the_fingerprint` |
| Event schemas with typed nested fields, descriptions, sensitivity | §3.7 | `test_contracted_event_types_are_fully_described`; `test_shipped_playbook_event_paths_resolve` |
| Intent as typed effect clauses with a canonical fallback | §3.4 | `test_contract_with_no_effects_falls_back_to_canonical_rendering` |
| Explanation rendering exhaustive over every registered clause type | §3.5 | `test_every_clause_kind_has_a_renderer` |
| Fail registration when an effect cannot be rendered | §3.5 rule 4 | `test_register_rejects_unrenderable_clause` (written in T-1, green from T-13) |
| Redact sensitive arguments consistently in explanations, previews, receipts | §3.8 | `test_sensitive_argument_is_redacted_everywhere`; `test_event_sensitive_path_is_redacted`; `receipt_projection` allow-list semantics pinned in `test_register_rejects_receipt_projection_field_not_in_result_model` |
| Adapt the V1 inspector without changing V1 execution | §12.5; §4.4 | `tests/test_playbook_runner.py` unchanged; every existing `PlaybookNodeInspector.test.tsx` assertion unchanged |
| Contract test: displayed explanation and invoked contract share registration and fingerprint | §3.6 invariant 1 | `tests/test_contract_intent_parity.py` |

Milestone **M1 — Intent truthful** (roadmap §6) requires "contract registry and exhaustive explanation tests". The evidence is §14.1's run plus `tests/test_contract_intent_parity.py::test_default_pipeline_renders_intent_for_every_action_node`, which proves the property on the shipped pipeline rather than only on a fixture.

---

## 14. Definition of done

### 14.1 Final verification run

Once, at the end of the package — not between tasks (`CLAUDE.md`, "One broader run at the end of a task, not during"):

```bash
# Contracts, explanation, parity, events
aq test tests/test_command_contracts_registry.py tests/test_builtin_command_contracts.py \
        tests/test_playbook_explanation.py tests/test_contract_intent_parity.py \
        tests/test_event_field_contracts.py -q

# The V1 surfaces this package must not move
aq test tests/test_playbook_runner.py tests/test_playbook_compiler_scope.py \
        tests/test_assignment_playbook_compiler.py tests/test_playbook_graph_view.py \
        tests/test_api_playbook_graph_view.py tests/test_default_pipeline.py \
        tests/test_pipeline_dispatch.py tests/test_review_pipeline_rules.py \
        tests/test_review_pipeline_e2e.py -q

# Package 0's gate, unchanged
aq test tests/test_command_capability_authorization.py -q

# Event emitters, unchanged
aq test tests/test_emit_schema_compliance.py tests/test_event_schemas.py \
        tests/test_event_schema_registry_validation.py \
        tests/test_playbook_eventbus_subscription.py tests/test_playbook_manager_triggers.py -q

# Generated clients
aq test tests/test_api_client_contract.py -q

ruff check src/commands/contracts src/commands/authorization.py src/commands/handler.py \
           src/playbooks/explanation.py src/playbooks/graph_view.py \
           src/playbooks/pipeline_compiler.py src/event_schemas.py src/config.py \
           src/api/models/playbook.py \
           tests/test_command_contracts_registry.py tests/test_builtin_command_contracts.py \
           tests/test_playbook_explanation.py tests/test_contract_intent_parity.py \
           tests/test_event_field_contracts.py
```

From `dashboard/`:

```bash
npm test -- src/pages/playbook-graph
npm run typecheck
npm run lint
npm run build
```

Expected: all green; every suite in the second and fourth blocks passing **without edits** is the evidence that V1 execution and event validation did not move.

### 14.2 Manual check

Start the daemon, open the Graph tab for `default-pipeline`, select `per-task-review-create-review`, and confirm the inspector reads as intent (`Ensure a review task exists` / `Create or reuse a task keyed by "dedup_key"` / `Project → this event's project`) with the raw JSON present under a closed **Advanced** disclosure. This is roadmap §6's "manual scenario review" for M1's UI half.

### 14.3 Rollback boundary

Roadmap §5: *"The contract adapter can be removed while leaving operational handlers unchanged. The V1 inspector enhancement is presentation-only and can be reverted independently."*

- Commit 5 reverts alone: it touches only `dashboard/src/pages/playbook-graph/`.
- Commit 4 reverts alone once 5 is reverted (or by setting `playbooks.contract_intent: false`, which needs no deploy of code).
- Commit 3 reverts alone: `src/event_schemas.py` is additive and nothing in commits 1–2 imports `resolve_event_path`.
- Commit 2 reverts by restoring the literal `PIPELINE_COMMAND_WHITELIST` and deleting `required_capability` plus its two call-site expressions. The ten `_cmd_*` handlers are byte-identical throughout, so nothing operational is restored — there is nothing to restore.

### 14.4 Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| The golden fingerprints are regenerated casually and Package 2 artifacts silently stale | medium, and grows after Package 3 | The failure message is explicit (§10.4); `registry_fingerprint()` is the single value Package 3 compares, so one review point covers all ten |
| `extra="forbid"` on a result model turns a handler change into `contract_violation` in production | low in this package (nothing dispatches through adapters yet), higher in Package 4 | Explicit key projection, never `model_validate(raw)` (§5.1); `test_unknown_handler_key_does_not_violate_the_contract` |
| The event `fields` block drifts from what emitters actually send | medium | `test_contracted_event_types_are_fully_described` couples `fields` to `required`+`optional`; `hydrated` keeps the dispatcher-added key out of the emitter allow-list (§1.7) |
| Task A and Task B diverge on the payload shape | low | §3.6 is verbatim in both languages; the assignability check in §11 fails `npm run typecheck` on any drift |
| The renderer's template classifier mishandles an argument shape no fixture covers | medium | `unresolved` is total: any unparsed `{{ }}` renders verbatim, and invariant 3 forbids dropping a key |

---

## 15. Emergent findings to file

These were found while writing this plan and are **not** in Package 1's scope. File each as a task rather than fixing it here.

1. **`_cmd_add_dependency` and `_cmd_edit_task` return no `success` key**, so they classify oppositely on the main pipeline path and inside a `for_each` (§1.2). Package 1 fixes this *for contracted execution* (Package 4 onward); the V1 `PipelineRunner` keeps the two disagreeing heuristics until Package 7 deletes it. Worth a task to align `pipeline_runner.py:146` and `:181` in the meantime, since a `for_each` over `add_dependency` is a one-line authoring change away.
2. **`event.task` is produced only by the pipeline dispatcher** (§1.7), so the assignment-routing path and any non-pipeline consumer of `task.completed` sees no `task` key at all. `src/playbooks/routing.py:107` builds its own `{**row, "task": row}` shape independently. One hydration point would be better than two; that is a Package 2 or Package 4 decision.
3. **`event.task` is `asdict(task_row)`** — the entire `Task` dataclass reaches playbook templates. Nothing constrains which fields an author may read. Package 2's typed event references are the real fix; §3.7's `sensitive` marker is the interim lever.
