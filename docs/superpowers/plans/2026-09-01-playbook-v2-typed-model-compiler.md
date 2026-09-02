# Playbook V2 — Package 2 child plan: Strict V2 definition model and Markdown compiler

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` (or `superpowers:subagent-driven-development`) to run this plan task by task. Every task below is a red/green/refactor unit with a named failing assertion, a named implementation, and its own verification command. Do not reorder tasks across commit boundaries.

**Parent roadmap:** `docs/superpowers/plans/2026-09-01-playbook-v2-implementation-roadmap.md` § "Package 2 — Strict V2 definition model and Markdown compiler"
**Design spec:** `docs/superpowers/specs/2026-09-01-playbook-v2-semantic-graph-design.md` §§ "Canonical V2 artifact", "Authoring contract", "First-class rules", "Typed values and conditions", "Typed step family", "Compilation and activation flow"
**Branch:** `feature/playbook-v2-pkg2` (from `origin/main` @ `8335fa5d`)
**Consumes:** Package 0 — **landed** at `d0a4c905` (`CapabilityPolicy`, `capability_policy_for`; §3.1) — and Package 1 — **not landed** (`CommandContract`, contract registry, enriched event schemas; §3.2).
**Produces:** the strict `PlaybookDefinition` union, the typed expression model, whole-graph validation, generated JSON Schema, source mapping, a proposal/diff flow that cannot activate anything, and a shadow-compile report over the live sources.

> **Drafting note (2026-09-02, revised).** This plan was first drafted ahead of Packages 0 and 1 against `origin/main` `130be765`. It has since been re-verified line by line against `origin/main` `1b835131`..`8335fa5d` (the two differ only by Package 7's child-plan document — every cited source file is byte-identical), where the situation has changed in one material way: **Package 0's code has landed** (`d0a4c905`), so `src/profiles/capabilities.py` and `src/commands/principal.py` are real and §3.1 records their verified signatures rather than a hope. **Package 1 has not landed** — `src/commands/contracts/` does not exist and its child plan is not written — so the contract-registry seam of §3.3 is still the load-bearing one. §3 gives a reconciliation script to run **before commit 1**. §17 lists the amendments this plan requires in the Package 1 and Package 5 child plans; §20 lists what stays open.

---

## 1. Why this package exists (what is loose today)

Five facts about the live tree, each verified by reading it, not inferred from the roadmap.

### 1.1 There is no typed artifact model — there is a dataclass with `dict[str, Any]` escape hatches

`CompiledPlaybook` (`src/playbooks/models.py:381`) is a plain dataclass. Its graph is `nodes: dict[str, PlaybookNode]`, and `PlaybookNode` (`src/playbooks/models.py:276`) carries the entire pipeline execution payload in one untyped field:

```python
for_each: dict[str, Any] | None = None  # src/playbooks/models.py:306
output: dict[str, Any] | None = None    # src/playbooks/models.py:307
action: dict[str, Any] | None = None    # src/playbooks/models.py:312
```

`PlaybookNode.from_dict` (`src/playbooks/models.py:349`) reads named keys and silently drops everything else — an unknown key in an agent-authored artifact is not an error, it is data loss. `CompiledPlaybook.to_dict` (`:775`) then re-emits only the fields the dataclass knows, so the round-trip is lossy by construction. This is the exact inverse of the spec's strictness requirement ("Unknown fields fail validation. Every supported field must survive serialization round-trip. The loader never silently discards executable data").

### 1.2 Values are opaque interpolation strings

The shipped default pipeline (`src/prompts/default_playbooks/default-pipeline.md:60-100`) passes arguments as strings:

```json
"args": {
  "project_id": "{{event.project_id}}",
  "dedup_key": "review:task:{{event.task_id}}",
  "title": "Review: {{event.title}}",
  "waiter_task_ids": ["{{outputs.dep.id}}"]
}
```

Nothing validates that `event.project_id` exists on `task.completed`, that `outputs.dep` is in scope, or that `waiter_task_ids` wants a list of task ids. A typo resolves to the literal string `{{event.projct_id}}` or to an empty string, and the command runs anyway.

### 1.3 Conditions are a permissive mini-language that defaults to *true*

`eval_pipeline_when` (`src/playbooks/conditions.py:8`) supports `field`+`truthy`/`not_null`/`equals`/`is_null` and `all`/`any`. Its documented fallback is: *"Unrecognised shapes default to True (permissive: unknown conditions do not silently drop events)"* (`src/playbooks/conditions.py:33`, and the bare `return True` at `:89`). A misspelled guard key therefore fires the rule on every matching event.

### 1.4 The event schema and the event the runner actually delivers disagree

`src/orchestrator/core.py:855` hydrates the payload before pipeline dispatch:

```python
if self.db and hydrated_event.get("task_id") and "task" not in hydrated_event:
    task_row = await self.db.get_task(str(hydrated_event["task_id"]))
    hydrated_event["task"] = asdict(task_row)
```

The default pipeline's first rule guards on `{"field": "event.task.branch_name", "truthy": true}` (`src/prompts/default_playbooks/default-pipeline.md:56`) and interpolates `{{event.task.pr_url}}`. But `task.completed` (`src/event_schemas.py:88`, in `_TASK_SCHEMAS`, merged into `EVENT_SCHEMAS` at `:959`) declares exactly `required: [task_id, project_id, title]`, `optional: [agent_id, agent_type]` — no `task`. The nested object is real at runtime and invisible to the schema.

V2 validates every `EventRef` against the registered event schema. Unless Package 1 declares the hydrated `task` object, **the shipped pipeline cannot be transcribed to V2**. §17.1 names this as a required Package 1 amendment; §7.3 makes the shadow-compile report the place it surfaces.

### 1.5 The compiler agent is the schema authority, and the schema is hand-written

`src/playbook_schema.json` is generated by `generate_json_schema()` — a hand-written dict-builder at `src/playbooks/models.py:991`, pinned by `tests/test_playbook_models.py:1677::test_schema_file_matches_generated`. The `playbook-compiler` profile is told to "Draft a compiled JSON artifact matching the playbook JSON Schema" (`src/profiles/defaults/playbook-compiler/profile.md:16`) and to iterate against `playbook_validate` because "the framework is the source of truth for what is valid" (`:96`) — but `_cmd_playbook_validate` (`src/playbooks/validator_command.py:135`) only runs `CompiledPlaybook.from_dict` (lossy, per §1.1) plus `pb.validate()`. The published schema and the accepting loader are two independent interpretations of the same artifact.

**Package 2 replaces all five with one Pydantic model that is simultaneously the loader, the validator, the schema source, and the serializer.**

---

## 2. Live-tree reconciliation — deviations from the roadmap's file list

Roadmap §3 and §5 permit a child plan to refine filenames after inspecting the live tree, and require the deviation be documented. Every row was verified by reading the live tree at `origin/main` `1b835131`.

| Roadmap says | Live tree | Decision |
|---|---|---|
| Create `tests/playbooks/test_definition.py` | **`tests/playbooks/` does not exist.** All suites are flat `tests/test_*.py` | `tests/test_playbook_v2_definition.py` |
| Create `tests/playbooks/test_expressions.py` | as above | `tests/test_playbook_v2_expressions.py` |
| Create `tests/playbooks/test_v2_validation.py` | as above | `tests/test_playbook_v2_validation.py` |
| Create `tests/playbooks/test_v2_compiler.py` | as above | `tests/test_playbook_v2_compiler.py` |
| Create `tests/playbooks/fixtures/v2/` | `tests/fixtures/` exists and is the house location; Package 5 §10.1 already names `tests/fixtures/playbooks/v2/` | `tests/fixtures/playbooks/v2/` — **shared with Package 5**, which reads the same artifact fixture |
| Modify `src/playbook_schema.json` | That file is **V1's**, generated by `generate_json_schema()` (`src/playbooks/models.py:991`) and pinned by `tests/test_playbook_models.py:1677`. Overwriting it turns V1 red, violating the package's own verification list ("existing V1 compiler tests remain green") | **Create `src/playbook_v2_schema.json`**; leave `src/playbook_schema.json` untouched until Package 7 |
| "compiler API/command entry points" | `_cmd_playbook_validate` / `_cmd_playbook_install` (`src/playbooks/validator_command.py:135`, `:241`); `_cmd_compile_playbook` (`src/commands/playbook_commands.py:619`); tool definitions in `src/tools/definitions.py:4522` / `:4544`; category map at `src/tools/definitions.py:236-237` | Add three **new** commands (§14) beside the V1 ones; modify none of the V1 handlers |
| "compiler prompt templates and their tests" | There is no template file. The compiler prompt **is** `src/profiles/defaults/playbook-compiler/profile.md` (102 lines); its tests are `tests/test_default_agent_type_playbooks.py` and `tests/test_profiles*.py` | Modify that profile; add `tests/test_playbook_v2_compiler.py::TestCompilerProfileContract` |
| Modify `src/playbooks/compiler.py` | It is a deterministic-dispatch shell (`compile_playbook`, `PlaybookCompiler` static helpers). Putting the V2 proposal pipeline in it would couple V1 dispatch to the V2 model and to the P0/P1 registries | New module `src/playbooks/proposal.py`; `compiler.py` gains **only** a re-export `compile_v2_proposal = src.playbooks.proposal.propose` and one docstring paragraph, so the roadmap's "modify `compiler.py`" stays literally true and the coupling stays one-directional |
| Modify `src/playbooks/models.py` | V1's dataclasses. V2 shares **no field** with them | Modify only to add a module docstring pointer to `definition.py`; the V1 dataclasses are untouched and deleted in Package 7 |
| — (not in the roadmap list) | The semantic diff is required by the roadmap's proposal object, but Package 5 §5.2 owns `src/playbooks/artifact_diff.py` | **Create `src/playbooks/semantic_diff.py`** (P2, definition→definition structural diff). Package 5's `artifact_diff.py` consumes it and adds DTO/explanation projection. §17.2 |
| — (not in the roadmap list) | Shadow-compiling the live sources needs a deterministic body for the two machine-compiled kinds | **Create `src/playbooks/pipeline_lowering.py`** (§7.2). Bounded to what `pipeline_compiler.py` already emits |
| — (not in the roadmap list) | `jsonschema` is used by §8 and §10.3 but is only present transitively via the `mcp` extra (`pip show jsonschema` → `Required-by: mcp`); it is absent from `pyproject.toml` | **Modify `pyproject.toml`** — declare `jsonschema>=4.20` in `dependencies` (it is a runtime need for `output_schema` validation, not just a test need). §8 |

### 2.1 Two naming reconciliations against the design spec

The design spec's prose names are illustrative; Package 5's child plan is **checked in on `main`** and its DTOs and fixture are the operative contract. Where they differ, Package 5 wins and the spec's name is recorded here:

| Spec name | Locked name | Why |
|---|---|---|
| `ResultRef` | `BindingRef`, discriminator `"binding_ref"` | Package 5 §3.1/§4.1 `ValueKind`, and §10.1's fixture already uses `{"type": "binding_ref", "binding": ..., "path": ...}` |
| `LoopItemRef` | `LoopRef`, discriminator `"loop_ref"` | same |
| `source_ref` (rule/step key) | `source` | Package 5 §10.1's fixture uses `"source": {...}` on both rules and steps |
| rule `title` | rule `name` | Package 5 §10.1's fixture and `RuleClusterDTO.name` |
| `LiteralValue` class named `Literal` | class `LiteralValue`, discriminator `"literal"` | `Literal` collides with `typing.Literal`, which every model file imports. Package 5 consumes the **discriminator string**, not the class name |

---

## 3. What this plan assumes from Packages 0 and 1

### 3.1 Package 0 has landed — these are its verified signatures

`d0a4c905 feat: introduce capability policy and execution principal` is on `origin/main`. Everything below was read off the live module with `inspect.signature`, not copied from Package 0's plan, so a task in this package can call it on day one.

```python
# src/profiles/capabilities.py — verified at origin/main 1b835131
NAMESPACES: Final = ("harness_tools", "aq_commands", "plugin_tools")
Namespace  = Literal["harness_tools", "aq_commands", "plugin_tools"]

@dataclass(frozen=True)
class CapabilityPolicy:
    harness_tools: frozenset[str]
    aq_commands: frozenset[str]
    plugin_tools: frozenset[str]
    derived_from_legacy: bool

    def to_canonical(self) -> dict[str, list[str]]: ...
    def fingerprint(self) -> str: ...                      # "sha256:<64 hex>" — verified
    def is_subset_of(self, other: CapabilityPolicy) -> bool: ...
    def intersect(self, other: CapabilityPolicy) -> CapabilityPolicy: ...
    def allows(self, namespace: Namespace, name: str) -> bool: ...
    def allows_aq_command(self, name: str) -> bool: ...    # + allows_harness_tool / allows_plugin_tool
    @property
    def is_empty(self) -> bool: ...
    @staticmethod
    def from_namespaces(*, harness_tools=None, aq_commands=None,
                        plugin_tools=None, derived_from_legacy: bool = False) -> CapabilityPolicy: ...

DENY_ALL: Final[CapabilityPolicy]      # all three namespaces empty

def capability_policy_for(profile: Any, *,
                          plugin_command_names: frozenset[str] = frozenset()) -> CapabilityPolicy: ...
def classify_capability(name: str, *,
                        plugin_command_names: frozenset[str] = frozenset()) -> Namespace: ...
```

Three consequences for this package, each now a fact rather than an assumption:

1. **`compiled_against.profiles[pid]` is `capability_policy_for(profile).fingerprint()`**, and that string already matches this plan's `Sha256` pattern (`^sha256:[0-9a-f]{64}$`) — verified: `DENY_ALL.fingerprint()` returns `sha256:30550cd1264b0690a3c6a64ee2e515738690d5f03d3c1301c3b786cdf5219714`. §4.4 does not need a second digest shape.
2. **The §6.7 delegation check is `is_subset_of` over `intersect`** of the on-path policies. No new set algebra is written in this package.
3. **`capability_policy_for` takes a `plugin_command_names` keyword.** `ProfileLookup` (§3.3) must thread the orchestrator's plugin-command set through it, or a profile that legitimately names a plugin tool is classified into the wrong namespace and `tool_use_not_subset` fires spuriously. `ProfileLookup.policy(profile_id)` owns that argument so no call site has to remember it.

`src/commands/principal.py` (`ExecutionPrincipal`, `check_delegation`, `principal_context`, `SERVER_OWNED_ARG_KEYS`) and `src/commands/authorization.py` (`authorize_command`, `filter_tool_definitions`, `MODE_ENFORCE`/`MODE_AUDIT`/`MODE_OFF`) also landed. **Package 2 calls none of them** — it is a pure compile-time package with no dispatch path — but §6.7's `delegation_runtime_checked` (info) diagnostic is precisely the marker saying "this delegation is deferred to `check_delegation` at run time".

### 3.2 Package 1 has NOT landed — these symbols do not exist

`src/commands/contracts/` does not exist on `origin/main` `1b835131`, and `docs/superpowers/plans/2026-09-01-playbook-v2-contracts-intent.md` is not written.

| Symbol | Owner | Package 2 uses it for |
|---|---|---|
| `src/commands/contracts/registry.py::get_contract(name) -> CommandContract \| None` | P1 | resolving `CommandStep.command` |
| `CommandContract.arguments` (Pydantic model class), `.result` (Pydantic model class), `.outcomes: frozenset[str]`, `.execution_fingerprint: str`, `.required_capability: str`, `.idempotency` | P1 | argument/result typing, business-outcome closure, `compiled_against.commands` |
| `src/event_schemas.py` enriched schemas with **nested field types** | P1 | `EventRef.path` validation and static typing (§1.4, §17.1) |

Two live-tree facts make the third row concrete and non-negotiable:

- `EventSchema.types` (`src/event_schemas.py:47`) is `NotRequired[dict[str, type | tuple[type, ...]]]` — flat, one level deep, and **absent entirely** from every `task.*` schema. Every `EventRef` carrying a dotted path therefore resolves to `type_unknown` (info) until P1 lands. §6.5 makes that visible rather than silent; it is not a reason to weaken the check.
- **`spec.created` is not a registered event type.** `get_schema("spec.created")` returns `None`; the registry has `spec.approved` (`required: ["project_id", "spec_path"]`, no optionals). Package 5's golden fixture triggers its second rule on `spec.created`, so that fixture fails `unknown_event` as written. §9.1 and §17.2 resolve this — it is the first concrete example of the shared fixture doing its job.

### 3.2.1 Reconciliation checklist — run this **before commit 1**

```bash
# P0 (expected: silent — it has landed)
python - <<'RECON'
import importlib, re
WANT = {
  "src.profiles.capabilities":       ["CapabilityPolicy", "capability_policy_for", "DENY_ALL"],
  "src.commands.contracts.registry": ["get_contract"],
}
for mod, names in WANT.items():
    try:
        m = importlib.import_module(mod)
    except Exception as exc:
        print(f"MISSING MODULE {mod}: {exc}"); continue
    for n in names:
        if not hasattr(m, n):
            print(f"MISSING SYMBOL {mod}.{n}")
from src.profiles.capabilities import CapabilityPolicy, DENY_ALL
for n in ("to_canonical", "fingerprint", "is_subset_of", "intersect", "from_namespaces", "allows"):
    if not hasattr(CapabilityPolicy, n):
        print(f"MISSING CapabilityPolicy.{n}")
fp = DENY_ALL.fingerprint()
if not re.fullmatch(r"sha256:[0-9a-f]{64}", fp):
    print(f"FINGERPRINT SHAPE CHANGED: {fp!r} — amend Sha256 in §4.4")
RECON

# P1: does CommandContract expose the five members §3.2 assumes?
python -c "
from src.commands.contracts.models import CommandContract
for n in ('arguments','result','outcomes','execution_fingerprint','required_capability'):
    print(n, hasattr(CommandContract, n) or n in getattr(CommandContract,'model_fields',{}))"

# P1: the hydrated nested task object, and spec.created (§1.4, §3.2, §17.1)
python -c "
from src.event_schemas import get_schema
s = get_schema('task.completed'); print('task.completed:', s)
print('nested task declared:', 'task' in (s.get('required',[]) + s.get('optional',[])))
print('spec.created registered:', get_schema('spec.created') is not None)"

# Are the V1 surfaces still present (Package 7 has not run)?
ls src/playbooks/compiler.py src/playbooks/pipeline_compiler.py src/playbook_schema.json
```

Run on `1b835131` this prints, exactly: nothing from the P0 block; `ModuleNotFoundError: No module named 'src.commands.contracts'`; `nested task declared: False`; `spec.created registered: False`; and the three V1 paths. That is the expected pre-P1 state, and §3.3 is what makes it landable anyway.

**If any line reports a mismatch with the above, stop and amend this document in the same commit as the code that reconciles it** (roadmap §7). Record the amendment in §20.

### 3.3 The two degraded modes, and what they are allowed to do

Package 2 is drafted ahead of Package 1. Two seams keep it independently landable, and one of them is now redundant in a useful way:

- **`ContractLookup` protocol** (`src/playbooks/validation.py`) — the load-bearing seam. Validation never imports `src.commands.contracts.registry` directly; it takes a `ContractLookup` whose default implementation is a thin adapter over it. When the registry is absent (today), the adapter yields `None` and every `CommandStep` gets diagnostic `unknown_command` (error) — never a pass.
- **`ProfileLookup` protocol**, same shape, over `capability_policy_for`. Its registry **exists now** (§3.1), so the default adapter resolves for real; the protocol stays because it is what lets `tests/test_playbook_v2_validation.py` build a two-profile capability lattice without touching the vault, and because it owns the `plugin_command_names` argument (§3.1, consequence 3). Absent registry ⇒ `unknown_profile` (error).

Degradation is always toward **error**, never toward "assume fine". There is no flag that makes an unresolvable reference pass. Concretely: **on `1b835131` the golden fixture will not validate clean**, and that is correct — it reports `unknown_command` for `ensure_task`/`gate_create`/`list_tasks` and `unknown_event` for `spec.created`. §9.1 splits the fixture suite accordingly so commits 1–2 are green today and the contract-dependent assertions arrive with P1.

### 3.4 The one thing this package must not do

Package 2 compiles, validates, diffs and reports. It **never** writes an activation, never persists an artifact to the database, never changes V1 execution, and never makes a proposal live. The exit gate is written as a prohibition ("no proposal can become active as a side effect of compilation") and §15 asserts it directly.

---

## 4. The locked model — the parallelism contract

> Roadmap §7: *"Package 2 schema-generation work may proceed alongside compiler prompt work after the Pydantic model is accepted."* This section is that acceptance. It is checked in **whole** as `src/playbooks/expressions.py` and `src/playbooks/definition.py` in commit 2, before any dependent task starts. A later task may **add** an optional field with a default; it may not rename, retype or remove one without amending this section and re-running every suite in §15.

### 4.1 Conventions that hold for every model in this package

```python
# src/playbooks/definition.py (and expressions.py) — the shared base
from pydantic import BaseModel, ConfigDict

class V2Base(BaseModel):
    """Strict base. An unknown key is a compile error, not a warning."""
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,              # artifacts are immutable after load
        populate_by_name=True,
        str_strip_whitespace=False,   # source fidelity: never mutate author text
    )
```

Four invariants, each asserted by a test in `tests/test_playbook_v2_definition.py`:

1. **`extra="forbid"` everywhere.** `test_every_v2_model_forbids_extra` walks `V2Base.__subclasses__()` transitively and asserts `model_config["extra"] == "forbid"`. A new model that forgets it fails the suite.
2. **Absent ≡ null.** No V2 field may distinguish "key missing" from `"key": null`. Every optional field is `X | None = None`. `test_absent_and_null_are_the_same_model` loads each fixture twice — once with optional keys removed, once with them explicitly `null` — and asserts model equality. This is what makes `exclude_none=True` canonical serialization (§4.7) lossless.
3. **Round-trip identity.** `test_round_trip_is_identity`: `Model.model_validate(json.loads(canonical_bytes(m))) == m` for every fixture.
4. **Identifier syntax.** Rule ids, step ids and binding names match `^[a-z0-9][a-z0-9_-]{0,63}$`; command names, profile ids and event types match `^[a-z0-9][a-z0-9._-]{0,127}$`. Enforced by `Annotated[str, StringConstraints(pattern=...)]`, so the generated JSON Schema carries the pattern too.

### 4.2 `src/playbooks/expressions.py` — the typed value union

Discriminator field is `type`. **The nine discriminator strings below are the wire contract.**

```python
ValueKind = Literal[
    "literal", "event_ref", "context_ref", "binding_ref", "loop_ref",
    "list", "object", "template", "coalesce",
]

JsonScalar = str | int | float | bool | None

class LiteralValue(V2Base):
    type: Literal["literal"] = "literal"
    value: JsonScalar | list[JsonScalar] | dict[str, JsonScalar]

class EventRef(V2Base):
    """A dotted path into the triggering event's registered payload schema."""
    type: Literal["event_ref"] = "event_ref"
    path: str                      # "project_id", "task.branch_name"

class ContextRef(V2Base):
    """A dotted path into the engine context schema (§4.2.1)."""
    type: Literal["context_ref"] = "context_ref"
    path: str                      # "run_id", "rule_id", "now"

class BindingRef(V2Base):
    """A read of a binding produced by an earlier step's ``save_result_as``."""
    type: Literal["binding_ref"] = "binding_ref"
    binding: str
    path: str | None = None        # dotted path inside the bound result

class LoopRef(V2Base):
    """A read of the current item of an enclosing ForEachStep."""
    type: Literal["loop_ref"] = "loop_ref"
    binding: str                   # == the ForEachStep's item_binding
    path: str | None = None
    index: bool = False            # True -> the 0-based iteration index, not the item

class ListValue(V2Base):
    type: Literal["list"] = "list"
    items: list["Value"]

class ObjectValue(V2Base):
    type: Literal["object"] = "object"
    fields: dict[str, "Value"]

class TemplatePart(V2Base):
    """One segment of a template. Exactly one of ``value``/``literal`` is set."""
    ...  # see below

class TemplateValue(V2Base):
    """Produces a string and only a string. Parts are concatenated in order."""
    type: Literal["template"] = "template"
    parts: list["Value"]

class CoalesceValue(V2Base):
    """First non-null branch wins. The ONLY way to express optionality."""
    type: Literal["coalesce"] = "coalesce"
    options: list["Value"]         # len >= 2; the last must be total (§6.5)

Value = Annotated[
    LiteralValue | EventRef | ContextRef | BindingRef | LoopRef
    | ListValue | ObjectValue | TemplateValue | CoalesceValue,
    Field(discriminator="type"),
]
```

`TemplatePart` is **removed** — `TemplateValue.parts` is a plain `list[Value]`, matching Package 5 §10.1's fixture (`{"type": "template", "parts": [{"type":"literal","value":"Review: "}, {"type":"event_ref","path":"title"}]}`). Rendering is `"".join(render(p) for p in parts)` with a per-part `str()` coercion; there is no format-string parsing, no `{{ }}` at runtime, and no user-controlled format spec (§10.4).

#### 4.2.1 The engine context schema

`ContextRef` validates against a **closed, P2-owned** schema, exported as `ENGINE_CONTEXT_SCHEMA` in `expressions.py` so P4's executors and P5's inspector read the same list:

| Path | Type | Meaning |
|---|---|---|
| `run_id` | string | this rule run |
| `dispatch_id` | string | shared by every run started from one event |
| `playbook_id` | string | |
| `rule_id` | string | |
| `artifact_sha256` | string | the pinned artifact |
| `now` | string | ISO-8601 UTC, sampled once per step attempt |
| `attempt` | integer | 1-based attempt number of the current step |
| `iteration_index` | integer | present only inside a loop body; `unknown_context_path` outside one |

Anything else is `unknown_context_path` (error). There is no dynamic context.

### 4.3 Conditions

A **separate** union, discriminator `type`, used by rule guards and decision cases. Package 5 projects all of them to `ValueKind == "expression"`.

```python
ComparisonOp = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "contains"]
BooleanOp    = Literal["and", "or", "not"]
ExistsMode   = Literal["present", "truthy"]

class Comparison(V2Base):
    type: Literal["comparison"] = "comparison"
    op: ComparisonOp
    left: Value
    right: Value

class BooleanExpr(V2Base):
    type: Literal["bool"] = "bool"
    op: BooleanOp
    operands: list["Condition"]      # len == 1 for "not", len >= 2 otherwise

class Exists(V2Base):
    type: Literal["exists"] = "exists"
    value: Value
    mode: ExistsMode = "present"     # "truthy" reproduces V1's `truthy:`/`not_null:`

Condition = Annotated[Comparison | BooleanExpr | Exists, Field(discriminator="type")]
```

**Deliberately excluded from initial V2:** quantified collection predicates (`any`/`all` over a collection). They need an implicit iteration variable, which breaks the definite-assignment analysis in §6.4 and the finite loop state in the spec's `ForEachStep` section. `in` / `not_in` / `contains` cover the membership cases the live pipeline actually uses. Recorded in §16.

**No vacuous truth.** `BooleanExpr` with `op in ("and","or")` and fewer than two operands is a model error (`empty_boolean_operand`), closing `src/playbooks/conditions.py:47`'s documented `all: []` ⇒ `True` and `:58`'s `any: []` ⇒ `False` holes. There is **no** permissive fallback: an unrecognised shape fails Pydantic discrimination.

### 4.4 `src/playbooks/definition.py` — artifact, scope, rules, source refs

```python
SCHEMA_GENERATION: Final[int] = 2
COMPILER_BUILD: Final[str] = "playbook-v2-compiler/1"

Sha256 = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

class SourceRef(V2Base):
    """Where in the authoring Markdown this element came from.
    Field-for-field identical to Package 5 §4.1's ``SourceRefDTO``."""
    path: str                      # vault-relative, "system/playbooks/default-pipeline.md"
    start_line: int                # 1-based, inclusive
    end_line: int                  # 1-based, inclusive; >= start_line
    heading: str | None = None
    excerpt: str | None = None     # <= 400 chars, truncated with "…" (§10.5)

class SystemScope(V2Base):
    type: Literal["system"] = "system"

class ProjectScope(V2Base):
    type: Literal["project"] = "project"
    project_id: str

class AgentTypeScope(V2Base):
    type: Literal["agent_type"] = "agent_type"
    agent_type: str

Scope = Annotated[SystemScope | ProjectScope | AgentTypeScope, Field(discriminator="type")]

class Trigger(V2Base):
    """Subscription-level match. ``filter`` is a conjunction of literal
    equality (scalar) or membership (list) tests against the event schema."""
    event_type: str
    filter: dict[str, JsonScalar | list[JsonScalar]] | None = None

class Rule(V2Base):
    id: str
    name: str
    description: str | None = None
    trigger: Trigger
    guard: Condition | None = None   # full typed expression, evaluated after delivery
    entry_step: str
    source: SourceRef

class CompiledAgainst(V2Base):
    commands: dict[str, Sha256] = {}   # command name -> CommandContract.execution_fingerprint
    profiles: dict[str, Sha256] = {}   # profile id   -> CapabilityPolicy.fingerprint()

class PlaybookDefinition(V2Base):
    schema_version: Literal[2] = 2
    id: str
    version: int                       # monotonic per playbook; server-owned
    scope: Scope
    purpose: Literal["routine", "assignment_routing"] = "routine"
    source_hash: Sha256                # of the normalized Markdown (§4.7)
    compiled_at: datetime              # UTC, tz-aware
    compiler_build: str | None = None  # COMPILER_BUILD; optional so P5's fixture validates
    rules: list[Rule]                  # len >= 1
    steps: dict[str, Step]             # len >= 1
    compiled_against: CompiledAgainst = CompiledAgainst()

    def contract_fingerprint(self) -> str: ...   # §4.7
```

`Scope` is an **object**, per the spec's canonical artifact and Package 5's fixture. V1's string form (`"system"`, `"project"`, `"agent-type:supervisor"` — `src/playbooks/models.py:459` `parse_scope`) is bridged by two pure helpers in `definition.py`, `scope_from_v1(str) -> Scope` and `scope_to_v1(Scope) -> str`, used only by `pipeline_lowering.py` and the shadow-compile report.

`source_hash` is a full 64-hex digest with a `sha256:` prefix. V1 truncates to 16 hex without a prefix (`src/playbooks/compiler.py:289` `_compute_source_hash`). The **normalization** is reused verbatim from `PlaybookCompiler._normalize_content` (`src/playbooks/compiler.py:256`) so a V1 and V2 hash of the same file are derived from identical bytes; only the digest presentation differs. `definition.py` exposes `source_digest(markdown: str) -> Sha256` that calls it.

### 4.5 The seven steps

Common fields on every step: `type`, `rule`, `title`, `description?`, `source`, and — where applicable — `save_result_as?` and `transitions`.

```python
StepKind = Literal["command", "llm", "agent_task", "decision", "wait", "foreach", "terminal"]

class StepBase(V2Base):
    rule: str                       # owner rule id — every step has exactly one (§6.2)
    title: str
    description: str | None = None
    source: SourceRef

class RetryPolicy(V2Base):
    max_attempts: int = 1           # ge=1, le=10
    backoff_seconds: float | None = None
    retry_on: list[str] = []        # outcomes that retry instead of transitioning

class AiBudget(V2Base):
    """Every field required. The spec forbids an unbounded AI state."""
    max_calls: int                  # ge=1, le=50
    max_output_tokens: int          # ge=1
    max_total_tokens: int           # ge=1
    timeout_seconds: int            # ge=1, le=3600

class ToolUsePolicy(V2Base):
    enabled: bool = False
    aq_commands: list[str] = []     # must be a subset of the step profile's policy
    plugin_tools: list[str] = []
```

**`command`**

```python
class CommandStep(StepBase):
    type: Literal["command"] = "command"
    command: str
    inputs: dict[str, Value] = {}
    idempotency_key: Value | None = None   # overrides the contract default
    retry: RetryPolicy | None = None
    save_result_as: str | None = None
    transitions: dict[str, str]            # outcome -> step id
```

**`llm`**

```python
class LlmStep(StepBase):
    type: Literal["llm"] = "llm"
    profile_id: str
    prompt: Value                          # rendered to a string; normally a TemplateValue
    inputs: dict[str, Value] = {}          # named, typed prompt inputs
    output_schema: dict[str, Any]          # JSON Schema, draft 2020-12 (§10.3)
    outcome_field: str | None = None       # required when transitions carry business outcomes
    budget: AiBudget
    tool_use: ToolUsePolicy = ToolUsePolicy()
    retry: RetryPolicy | None = None
    save_result_as: str | None = None
    transitions: dict[str, str]
```

`outcome_field` names the property of `output_schema` whose `enum` is the business-outcome set. It is what makes "LLM branching must use declared structured output" checkable: §6.6 requires `output_schema.properties[outcome_field].enum` to exist, to be a list of strings, to be in `required`, and to equal the non-reserved keys of `transitions` exactly. Package 5 §10.1's fixture omits this field — §17.2 amends it.

**`agent_task`**

```python
class AgentTaskStep(StepBase):
    type: Literal["agent_task"] = "agent_task"
    profile_id: str
    objective: Value                       # rendered to a string
    inputs: dict[str, Value] = {}
    wait_for_completion: bool = True
    cancel_child: bool = False             # spec: explicit, defaults false
    timeout_seconds: int | None = None
    retry: RetryPolicy | None = None
    save_result_as: str | None = None
    transitions: dict[str, str]
```

Business outcomes: `{"dispatched"}` when `wait_for_completion is False`, otherwise `{"completed", "failed"}`. `timed_out` and `cancelled` are reserved (§4.6).

**`decision`**

```python
class DecisionCase(V2Base):
    when: Condition
    goto: str
    label: str | None = None               # presentation only (§4.8)

class DecisionStep(StepBase):
    type: Literal["decision"] = "decision"
    cases: list[DecisionCase]              # len >= 1
    default: str                           # REQUIRED — see below
```

`default` is **required**, deviating from the spec's "optional default". Rationale: the spec's stronger rule is "There are no silent defaults for missing executable behavior" and "every executable transition is displayed". An optional default means a decision step with a fall-through that is neither an edge nor an error. Exhaustiveness over a typed expression tree is not decidable here, so the author states the fall-through. A decision step has **no** `transitions` and no `save_result_as`; its edges come from `cases` and `default`.

**`wait`**

```python
WaitKind = Literal["event", "human", "task", "timer"]

class WaitStep(StepBase):
    type: Literal["wait"] = "wait"
    wait_kind: WaitKind
    awaited: Value | None = None           # event_type / gate title / task ref (§ table)
    correlation_key: Value | None = None   # computed at pause time
    outcomes: list[str] = []               # human only: the gate's resolution vocabulary
    timeout_seconds: int | None = None
    save_result_as: str | None = None
    transitions: dict[str, str]
```

Per-kind requirements, enforced by a model validator and asserted one-per-row in `tests/test_playbook_v2_definition.py::TestWaitStepShapes`:

| `wait_kind` | required | business outcomes | bound result schema (P2-owned constant) |
|---|---|---|---|
| `event` | `awaited` (a `LiteralValue` naming a registered event type), `correlation_key` | `{"matched"}` | `{event_type: str, payload: object}` |
| `human` | `awaited` (gate title), `correlation_key`, `outcomes` (len ≥ 1) | exactly `outcomes` | `{resolution: str, note: str\|null, resolved_by: str\|null}` |
| `task` | `awaited` (task reference), `correlation_key` | `{"completed","failed","cancelled"}` | `{task_id: str, status: str, outcome: str\|null}` |
| `timer` | `timeout_seconds` | `{"fired"}` | `{fired_at: str}` |

The four schemas are exported as `WAIT_RESULT_SCHEMAS: dict[WaitKind, dict]` so Package 4's `executors/wait.py` and Package 2's binding type-checker cannot drift.

**`foreach`**

```python
FailurePolicy = Literal["halt", "continue", "collect"]

class ForEachStep(StepBase):
    type: Literal["foreach"] = "foreach"
    collection: Value
    item_binding: str
    failure_policy: FailurePolicy
    body_entry: str
    continuation: str | None = None        # == transitions["completed"] when both set
    max_iterations: int = 500              # ge=1, le=10000
    save_result_as: str | None = None
    transitions: dict[str, str]            # {"completed": …, "failed": …}
```

`continuation` is retained because Package 5's `LoopNodeDetailDTO.continuation_step_id` reads it. It is redundant with `transitions["completed"]`, so §6.3 adds `continuation_mismatch` (error) when both are set and differ — redundancy that is **checked**, not trusted.

Aggregate result schema, exported as `FOREACH_RESULT_SCHEMA`:

```json
{"type": "object", "required": ["total","succeeded","failed","items"],
 "properties": {
   "total": {"type": "integer"}, "succeeded": {"type": "integer"}, "failed": {"type": "integer"},
   "items": {"type": "array", "items": {"type": "object",
     "required": ["index","outcome"],
     "properties": {"index": {"type": "integer"}, "outcome": {"type": "string"},
                    "value": {}, "error": {"type": ["string","null"]}}}}}}
```

**`terminal`**

```python
TerminalOutcome = Literal["completed", "failed", "cancelled"]

class TerminalStep(StepBase):
    type: Literal["terminal"] = "terminal"
    outcome: TerminalOutcome
    result: Value | None = None
```

A closed enum, matching every terminal in Package 5 §10.1's fixture. Terminal outcomes are engine vocabulary, so they are exempt from the identifier-inventory check (§5.3).

```python
Step = Annotated[
    CommandStep | LlmStep | AgentTaskStep | DecisionStep
    | WaitStep | ForEachStep | TerminalStep,
    Field(discriminator="type"),
]
```

### 4.6 Outcome vocabulary and transition keys

```python
RESERVED_OUTCOMES: Final[frozenset[str]] = frozenset({
    "input_resolution_failed", "unavailable", "contract_violation",
    "state_limit_exceeded", "interrupted", "timed_out", "cancelled",
})
LLM_RESERVED_OUTCOMES: Final[frozenset[str]] = frozenset({
    "invalid_output", "budget_exceeded", "provider_error",
})
RUNTIME_ERROR_KEY: Final[str] = "runtime_error"
```

`runtime_error` is a **transition key**, never an outcome. It is the single visible catch-all target for every reserved outcome the step does not map individually. The rule, enforced per step in §6.6 and rendered as labelled edges by Package 5:

- `set(transitions)` ⊆ `business_outcomes(step) | reserved_for(step) | {"runtime_error"}`;
- every business outcome is a key (`unmapped_business_outcome` otherwise);
- every reserved outcome is either a key or covered by `runtime_error` (`unmapped_reserved_outcome` otherwise).

`business_outcomes(step)` is: the contract's closed outcome set for `command`; the `outcome_field` enum for `llm`; the table above for `wait`; `{"dispatched"}` or `{"completed","failed"}` for `agent_task`; `{"completed","failed"}` for `foreach`. `decision` and `terminal` have none.

### 4.7 Canonical serialization and the four fingerprints

All four live in `definition.py` so Package 3's `ArtifactStore` and Package 5's `ArtifactRefDTO` call them rather than re-deriving:

```python
def canonical_bytes(d: PlaybookDefinition) -> bytes:
    return json.dumps(
        d.model_dump(mode="json", exclude_none=True),
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")

def artifact_sha256(d) -> Sha256:        # "sha256:" + sha256(canonical_bytes(d)).hexdigest()
def source_digest(markdown: str) -> Sha256   # over PlaybookCompiler._normalize_content output
def contract_fingerprint(d) -> Sha256    # over canonical JSON of d.compiled_against.commands
```

`exclude_none=True` is safe **only** because of invariant 2 in §4.1 (absent ≡ null). `test_canonical_bytes_are_key_order_independent` loads the fixture with shuffled object keys and asserts an identical digest; `test_canonical_bytes_are_stable_across_processes` asserts the digest against a hard-coded constant so a Pydantic upgrade that changes dump ordering fails loudly.

`compiler_build` is `COMPILER_BUILD`, a hand-bumped constant. It is **not** derived from git: two builds of the same source must hash identically. It is bumped whenever compiler output semantics change; §16 records that Package 6 must bump it when the bundled sources are rewritten.

**Constraint this imposes on Package 3:** the artifact hash is over these bytes, so `ArtifactStore.put` must write `canonical_bytes(d)` and `load` must re-verify `sha256(file_bytes)`. It must not round-trip through a database JSON/JSONB column — PostgreSQL `jsonb` reorders keys and drops duplicate keys, which would break hash verification. §11.

### 4.8 Executable vs presentation fields

Package 5 §4.5's `FieldChangeDTO.executable` and Package 1's fingerprint exclusion both need one authority. Presentation-only fields are declared **in the model**:

```python
title: str = Field(json_schema_extra={"executable": False})
```

Declared presentation-only: `Rule.name`, `Rule.description`, `StepBase.title`, `StepBase.description`, `DecisionCase.label`, and every `SourceRef` field. Everything else is executable. `definition.py` exports:

```python
def is_executable_path(pointer: str) -> bool: ...   # JSON pointer -> bool
EXECUTABLE_FIELDS: Final[frozenset[str]]            # flattened set, for the diff
```

`test_presentation_fields_are_declared_not_hardcoded` asserts `is_executable_path` is derived by walking `model_fields` and their `json_schema_extra`, so a new presentation field is classified by adding the annotation, not by editing a list.

---

## 5. Authoring input and identifier inventory — `src/playbooks/authoring.py`

### 5.1 `PlaybookSource`

```python
@dataclass(frozen=True)
class PlaybookSource:
    vault_path: str            # vault-relative; the value that lands in SourceRef.path
    raw: str
    frontmatter: dict[str, Any]
    body: str
    body_start_line: int       # 1-based line of the first body line in `raw`
    inventory: IdentifierInventory

    @classmethod
    def load(cls, path: Path, *, vault_root: Path) -> "PlaybookSource | SourceError": ...
```

Frontmatter parsing reuses `PlaybookCompiler._parse_frontmatter` (`src/playbooks/compiler.py:171`) and validation reuses `PlaybookCompiler._validate_frontmatter` (`:189`) so the two compilers cannot disagree about what a valid header is. `body_start_line` is computed by counting newlines up to the closing `---`; it is what turns a body offset into a 1-based file line for `SourceRef`.

### 5.2 `IdentifierInventory`

```python
@dataclass(frozen=True)
class IdentifierInventory:
    names: Mapping[str, tuple[SourceRef, ...]]     # identifier -> every place it appears
    def contains(self, name: str) -> bool: ...
    def refs(self, name: str) -> tuple[SourceRef, ...]: ...
```

Populated from exactly two places, so there is no third way for a name to become executable:

1. **Frontmatter**, structurally: `id`, every `triggers[].type`/`event_type`, every `triggers[].filter` key, `scope`, `profile_id`, `role`.
2. **Backticked spans in the body**: `` `name` `` on a single line. Fenced code blocks are excluded (a JSON example must not mint identifiers). The regex is `` r"`([^`\n]{1,128})`" `` applied to the body with fenced regions blanked first; the captured text is taken verbatim, with no case folding and no trimming beyond a single leading/trailing space.

Nested paths mint their **root**: `` `event.task.branch_name` `` puts `event.task.branch_name` in the inventory and the reference check (§6.1) accepts either the full path or its dotted prefixes being present. Backtick spans containing whitespace-only or fence characters are ignored.

### 5.3 What must be in the inventory

| Executable identifier | Checked | Rationale |
|---|---|---|
| `CommandStep.command` | yes | the compiler may not invent a command |
| `LlmStep.profile_id`, `AgentTaskStep.profile_id` | yes | may not invent a profile |
| `Trigger.event_type`, `WaitStep.awaited` when `wait_kind == "event"` | yes | may not invent an event |
| `Trigger.filter` keys, `EventRef.path` | yes | may not invent an event field |
| binding names (`save_result_as`, `BindingRef.binding`) | yes | operator-visible names |
| `ForEachStep.item_binding`, `LoopRef.binding` | yes | |
| `LlmStep` `outcome_field` and its enum values | yes | they become visible branch labels |
| `WaitStep.outcomes` values | yes | |
| `CommandStep.inputs` keys | yes | they are contract argument names |
| step ids, rule ids | **no** | artifact-local; spec: "Internal artifact-local step IDs may be compiler-generated" |
| `TerminalStep.outcome` | **no** | closed engine enum (§4.5) |
| `ContextRef.path` | **no** | closed engine schema (§4.2.1) |
| reserved outcomes, `runtime_error` | **no** | engine vocabulary |

A miss is `unknown_identifier` (error), carrying the offending name and the step's `SourceRef`.

---

## 6. Validation — `src/playbooks/validation.py`

```python
Severity = Literal["error", "warning", "question", "info"]   # == Package 5 §4.3

@dataclass(frozen=True)
class Diagnostic:
    severity: Severity
    code: str                      # stable machine code; the full list is §6.8
    message: str
    rule_id: str | None = None
    step_id: str | None = None
    field: str | None = None       # JSON pointer within the step
    source: SourceRef | None = None

def validate_definition(
    d: PlaybookDefinition,
    *,
    inventory: IdentifierInventory | None,
    contracts: ContractLookup,
    profiles: ProfileLookup,
    events: EventSchemaLookup,
) -> list[Diagnostic]: ...
```

Validation is **total**: it runs every pass and returns every diagnostic. It never raises on invalid input and never stops at the first error, because the compiler agent's whole loop depends on getting the full error list back in one call (`src/profiles/defaults/playbook-compiler/profile.md:20-22` — *"use the `errors` list… Fix the JSON and revalidate. Repeat up to 5 rounds"*).

Passes run in this order; each is a separate function and a separate test class.

### 6.1 Structure and identifiers

- `duplicate_rule_id`, `duplicate_step_id` (the latter is impossible in a `dict` and is asserted at the JSON-text level in §7.1's parse step, which rejects duplicate object keys).
- `step_rule_unknown` — `step.rule` names no rule.
- `orphan_step` — a step no rule owns transitively (see §6.2).
- `rule_entry_unknown` / `rule_entry_not_owned` — `Rule.entry_step` missing, or its `step.rule != rule.id`.
- `unknown_step_target` — any transition/`goto`/`default`/`body_entry`/`continuation` naming a missing step.
- `unknown_identifier` — §5.3.
- `unknown_command`, `unknown_profile`, `unknown_event`, `unknown_event_field`, `unknown_context_path`.

### 6.2 Rule ownership and closure

Each rule's subgraph is the forward closure from `entry_step` over every outgoing edge (transitions, decision cases + default, foreach `body_entry` + `continuation`).

- `cross_rule_transition` (error) when an edge leaves a step owned by rule R and lands on a step whose `rule != R`. This is the invariant Package 5's `RuleClusterDTO` depends on ("no edge ever crosses `rule_id`").
- `orphan_step` (error) for any step in `steps` not in any rule's closure.
- `unreachable_step` (error) for a step owned by R but not reachable from R's entry — the same condition, reported with the owner named, because that is the actionable message.
- `no_terminal_path` (error) for a step from which no `TerminalStep` is reachable. Computed by reverse BFS from the terminals, mirroring `src/playbooks/pipeline_compiler.py:196` `_reaches_terminal`, but as an error rather than the V1 warning.
- Shared terminals are **duplicated per rule** by the compiler, never shared across rules (spec: "the artifact duplicates shared terminal or utility steps per rule"). Enforced by `cross_rule_transition` falling out naturally.

### 6.3 Loops

- `nested_loop` (error) — a `ForEachStep` inside another loop's body.
- `loop_body_escapes` (error) — a body step transitioning to a step that is neither in the body nor the owning `ForEachStep` itself. The loop's only exits are its own `transitions`.
- `continuation_mismatch` (error) — §4.5.
- `loop_variable_shadow` (error) — `item_binding` equal to any binding name in the rule, any enclosing `item_binding`, or a reserved namespace root (`event`, `context`, `loop`, `run`, `rule`, `step`).
- `loop_ref_outside_loop` (error) — a `LoopRef{binding: v}` in a step outside `body(f)` for the `f` with `item_binding == v`.

`body(f)` is computed by BFS from `f.body_entry`, stopping at `f` itself and never traversing `f`'s own `transitions`/`continuation`.

### 6.4 Definite assignment — the exact algorithm

A must-analysis over each rule's CFG. Stated precisely because it is the one place a "roughly like" implementation silently permits an unassigned read.

1. **Uniqueness first.** Within a rule, each binding name is assigned by **exactly one** step. A second assigner is `duplicate_binding` (error). This makes bindings immutable per name and makes a loop-back a re-execution of the same assigner, not a reassignment. `binding_reassigned` is the diagnostic when a name collides with an enclosing `item_binding`.
2. **CFG node splitting.** Each `ForEachStep` `f` becomes two CFG nodes:
   - `f.enter` — predecessors are `f`'s graph predecessors; `gen = ∅`; successor is `f.body_entry`;
   - `f.exit` — predecessors are the body steps whose successor is `f`; `gen = {f.save_result_as}` when set; successors are `f.transitions` targets and `continuation`.
   This is what makes the aggregate binding available **after** the loop and invisible **inside** it.
3. **Universe.** `U` = every binding name assigned anywhere in the rule.
4. **Transfer.** `OUT[s] = IN[s] ∪ gen(s)`; `IN[entry] = ∅`; `IN[s] = ⋂_{p ∈ pred(s)} OUT[p]` for `s ≠ entry`.
5. **Initialization.** `IN[s] = U` for every `s ≠ entry` (the standard optimistic start for a must-analysis), then iterate to a fixpoint. Convergence is guaranteed: the lattice is `2^U` under `⊇` and every step is monotone. This is what makes a loop-back edge correct rather than pessimistic.
6. **Check.** A `BindingRef{binding: b}` read in step `s` is legal iff `b ∈ IN[s]`. Otherwise `binding_not_definitely_assigned` (error), whose message names the branch that omits it: the first predecessor path found by BFS from entry whose `OUT` lacks `b`.
7. **Scope.** `LoopRef` is governed by §6.3, not by this analysis.

`tests/test_playbook_v2_validation.py::TestDefiniteAssignment` covers, one test each: straight line legal; **one branch assigns, the other does not, both converge** → error naming the omitting branch; both branches assign the same name → `duplicate_binding`; loop-back re-reads its own assigner → legal; aggregate read inside the body → error; aggregate read on the continuation edge → legal.

### 6.5 Value typing

A small closed lattice, `ValueType` in `validation.py`: `string | integer | number | boolean | object | array | null | unknown`, with optional `item_type` and `properties`.

Static type of each expression:

| Expression | Type |
|---|---|
| `LiteralValue` | the JSON type of `value` |
| `EventRef` | from the registered event schema for the rule's `trigger.event_type`, walking `path` |
| `ContextRef` | from `ENGINE_CONTEXT_SCHEMA` |
| `BindingRef` | from the producing step's result schema — the contract's result model (`command`), `output_schema` (`llm`), `AGENT_TASK_RESULT_SCHEMA`, `WAIT_RESULT_SCHEMAS[kind]`, or `FOREACH_RESULT_SCHEMA` — walking `path` |
| `LoopRef` | `integer` when `index`, else the `item_type` of the loop's `collection` |
| `ListValue` | `array`, `item_type` = join of the item types |
| `ObjectValue` | `object` with known `properties` |
| `TemplateValue` | `string`, always |
| `CoalesceValue` | join of the options, with `null` removed if any option is total |

`unknown` is compatible with everything and emits `type_unknown` (**info**) so the operator can see where a check was silenced rather than passing. A concrete mismatch is `type_mismatch` (error).

`CoalesceValue` requires its **last** option to be total (a `LiteralValue` with a non-null `value`, or a reference whose type excludes `null`); otherwise `coalesce_not_total` (error). This is what makes "optional behavior uses an explicit `CoalesceValue`… it is never inferred" real: a coalesce chain that can still be null does not rescue an optional field.

`AGENT_TASK_RESULT_SCHEMA` is P2-owned and consumed by P4:

```json
{"type": "object", "required": ["task_id","status"],
 "properties": {"task_id": {"type":"string"}, "status": {"type":"string"},
                "outcome": {"type":["string","null"]}, "summary": {"type":["string","null"]}}}
```

### 6.6 Contract, argument and outcome validation

For each `CommandStep`:

- `contracts.get(step.command)` is `None` → `unknown_command` (error).
- `inputs` keys must be fields of `contract.arguments`; an extra is `argument_unknown` (error); a missing required one is `argument_missing` (error).
- Each input's static type must be compatible with the declared field type (`type_mismatch`).
- `transitions` obey §4.6 against `contract.outcomes`.
- `contract.execution_fingerprint` is recorded in `compiled_against.commands[step.command]`; a definition whose recorded fingerprint differs from the registry's is `stale_contract` (error) — the same code Package 5 §4.3 renders.

For each `LlmStep`: `output_schema` is checked in §10.3; `outcome_field` is required when `set(transitions) - RESERVED - LLM_RESERVED - {"runtime_error"}` is non-empty, and its enum must equal that set exactly (`outcome_enum_mismatch`). `llm_branch_without_schema` fires when transitions branch and `output_schema` declares no such enum — this is the check that forbids "hidden natural-language AI transitions".

For each `WaitStep` / `AgentTaskStep` / `ForEachStep` / `DecisionStep` / `TerminalStep`: outcomes per §4.6 and the §4.5 tables.

### 6.7 Profiles and capabilities

- Every `LlmStep`/`AgentTaskStep` `profile_id` resolves via `ProfileLookup` → else `unknown_profile` (error).
- The resolved `CapabilityPolicy` is recorded in `compiled_against.profiles[profile_id]` via `.fingerprint()`.
- `profile_capability_empty` (**warning**) when all three namespaces are empty — legal (empty means deny-all) but almost never intended for an AI step, so the operator sees it in the proposal.
- `tool_use_not_subset` (error) when `LlmStep.tool_use.aq_commands`/`.plugin_tools` are not subsets of the step profile's policy.
- **Static delegation check.** For each `AgentTaskStep` `a`, compute the *must*-set of AI-context profiles: the intersection, over every path from the rule entry to `a`, of the `profile_id`s of `LlmStep`s with `tool_use.enabled` on that path — the same fixpoint machinery as §6.4, over profile ids instead of bindings. When that set is non-empty, `a`'s profile policy must be a subset of the intersection of those policies; otherwise `capability_not_subset` (error). When it is empty, the run principal is a service principal and the check is deferred to Package 0's runtime enforcement — recorded as `delegation_runtime_checked` (**info**) so the deferral is visible rather than silent.

### 6.8 Diagnostic codes

The complete closed set, exported as `DIAGNOSTIC_CODES: frozenset[str]`. `test_every_diagnostic_code_is_registered` asserts every code any pass emits is a member, and `test_every_registered_code_has_a_test` asserts each appears in at least one test — so a new check cannot ship without a fixture.

`authority_field_ignored`(w) · `requires_agent_proposal`(q) · `ambiguous_prose`(q) · `unknown_identifier` · `unknown_command` · `unknown_profile` · `unknown_event` · `unknown_event_field` · `unknown_context_path` · `duplicate_rule_id` · `duplicate_step_id` · `duplicate_binding` · `step_rule_unknown` · `orphan_step` · `rule_entry_unknown` · `rule_entry_not_owned` · `unknown_step_target` · `cross_rule_transition` · `unreachable_step` · `no_terminal_path` · `nested_loop` · `loop_body_escapes` · `continuation_mismatch` · `loop_variable_shadow` · `loop_ref_outside_loop` · `binding_not_definitely_assigned` · `binding_reassigned` · `type_mismatch` · `type_unknown`(i) · `coalesce_not_total` · `empty_boolean_operand` · `argument_missing` · `argument_unknown` · `unmapped_business_outcome` · `unmapped_reserved_outcome` · `unknown_transition_outcome` · `outcome_enum_mismatch` · `llm_branch_without_schema` · `output_schema_invalid` · `output_schema_too_deep` · `profile_capability_empty`(w) · `tool_use_not_subset` · `capability_not_subset` · `delegation_runtime_checked`(i) · `stale_contract` · `source_ref_out_of_range` · `excerpt_truncated`(i)

Unmarked codes are `error`; `(w)` warning, `(q)` question, `(i)` info.

---

## 7. Proposal, semantic diff, and shadow compile

### 7.1 `src/playbooks/proposal.py`

```python
class SemanticBody(V2Base):
    """EXACTLY what the compiler agent is allowed to emit. extra='forbid'."""
    rules: list[Rule]
    steps: dict[str, Step]

AUTHORITATIVE_FIELDS: Final[frozenset[str]] = frozenset({
    "schema_version", "id", "version", "scope", "purpose", "source_hash",
    "compiled_at", "compiler_build", "compiled_against", "enabled",
    "triggers", "cooldown_seconds", "max_tokens", "llm_config",
    "transition_llm_config", "profile_id", "kind", "role",
})

@dataclass(frozen=True)
class CompileProposal:
    artifact: PlaybookDefinition | None      # None when a hard error blocked assembly
    diagnostics: list[Diagnostic]
    questions: list[Diagnostic]              # the severity=="question" subset, ordered by source
    source_digest: Sha256
    contract_fingerprint: Sha256 | None
    compiler_build: str
    semantic_diff: DefinitionDiff | None     # None when no baseline was supplied
    artifact_sha256: Sha256 | None

    @property
    def activatable(self) -> bool:           # no errors AND no questions AND artifact is not None
        ...

def propose(
    source: PlaybookSource,
    body: Mapping[str, Any],
    *,
    baseline: PlaybookDefinition | None = None,
    contracts: ContractLookup,
    profiles: ProfileLookup,
    events: EventSchemaLookup,
    version: int,
) -> CompileProposal: ...
```

The order of operations is the spec's §"Compilation and activation flow", and each numbered step is one function so the sequence is testable:

1. **Strip, then forbid.** Any key of `body` in `AUTHORITATIVE_FIELDS` is removed and one `authority_field_ignored` (warning) is emitted per key, naming it. The remainder is validated as `SemanticBody`, whose `extra="forbid"` rejects anything else outright. Strip-known-with-diagnostic plus forbid-unknown is what "the server discards any compiler-supplied identity… with a diagnostic" means operationally. **Duplicate JSON object keys are rejected** at parse time via `json.loads(..., object_pairs_hook=_reject_duplicates)` — otherwise `{"id": "x", "id": "y"}` lets a stripped key survive.
2. **Merge trusted metadata.** `id`, `scope`, `purpose`, `version`, `source_hash`, `compiled_at`, `compiler_build` come from `source.frontmatter` and the server, never from `body`. This is the V2 analogue of `PlaybookCompiler._merge_frontmatter` (`src/playbooks/compiler.py:337`), whose orphaning Package 0 §1.1 repairs on the V1 path; V2 never has an unmerged path to orphan because assembly and merge are the same function.
3. **Assemble and strictly validate** as `PlaybookDefinition`. A Pydantic `ValidationError` becomes one diagnostic per error location, with `source` resolved from the offending step when the step parsed far enough to carry one.
4. **Resolve fingerprints** — `compiled_against.commands` and `.profiles` from the registries (§6.6, §6.7).
5. **Run `validate_definition`** (§6).
6. **Diff** against `baseline` when supplied (§7.4).
7. **Hash** — `artifact_sha256(d)`, `contract_fingerprint(d)`.

`propose` **returns**; it does not write, install, enable, or persist. §15's `test_propose_touches_no_activation_state` asserts this with a `PlaybookManager` double whose every method raises.

### 7.2 `src/playbooks/pipeline_lowering.py`

A deterministic, LLM-free transcription of the two machine-compiled kinds into a `SemanticBody`. It exists so shadow compile (§7.3) gives a real answer for the playbooks that actually run in production, and so Package 6 starts from a mechanical baseline rather than a blank page.

```python
def lower_pipeline(source: PlaybookSource) -> tuple[Mapping[str, Any], list[Diagnostic]]: ...
def lower_assignment(source: PlaybookSource) -> tuple[Mapping[str, Any], list[Diagnostic]]: ...
```

Scope is exactly what `src/playbooks/pipeline_compiler.py` already emits — no more:

| V1 shape (`src/playbooks/pipeline_compiler.py`, `default-pipeline.md`) | V2 |
|---|---|
| `rules[].id` / `.on` / `.entry` | `Rule.id` / `Trigger.event_type` / `Rule.entry_step` |
| `rules[].when` (`eval_pipeline_when` shape) | `Rule.guard`: `field`+`truthy`→`Exists(mode="truthy")`; `not_null`→`Exists(mode="present")`; `is_null`→`BooleanExpr(op="not", …)`; `equals`→`Comparison(op="eq")`; `all`/`any`→`BooleanExpr` |
| node `command` + `args` | `CommandStep.command` + `inputs` |
| `"{{event.x.y}}"` (whole string) | `EventRef(path="x.y")` |
| `"{{outputs.b.f}}"` (whole string) | `BindingRef(binding="b", path="f")` |
| `"lit {{event.x}}"` (mixed) | `TemplateValue(parts=[LiteralValue, EventRef, …])` |
| `["{{outputs.dep.id}}"]` where `dep` is **not** an enclosing `for_each` binding | `ListValue(items=[BindingRef(binding="dep", path="id")])` |
| `["{{outputs.dep.id}}"]` **inside** a node whose `for_each.as == "dep"` (the real `default-pipeline.md:106-112`, `:177-183`) | `ListValue(items=[LoopRef(binding="dep", path="id")])` |
| node `output.as` | `save_result_as` |
| node `for_each: {source, as}` | a `ForEachStep` wrapping the node as a one-step body: `collection` from `source`, `item_binding` from `as`, `failure_policy="collect"`, `body_entry` = the wrapped step, `continuation` = the node's `on_success` |
| `on_success` / `on_failure` | `transitions: {"success": …, "failure": …, "runtime_error": <on_failure>}` |
| `{"terminal": true}` | `TerminalStep(outcome="completed")` |

**How `outputs.<name>` resolves, and why the list rows differ.** `outputs.` in V1 is one flat namespace: it holds both step results (`output.as`) and `for_each` item bindings (`for_each.as`). V2 splits them into two node kinds, so lowering is context-sensitive and the rule is mechanical:

> Walk the enclosing `for_each` bindings of the node being lowered, innermost first. If `<name>` equals one of their `as` values, emit `LoopRef(binding=<name>, …)`. Otherwise emit `BindingRef(binding=<name>, …)` — the ordinary case, and the default.

`LoopRef` is **only** ever correct for the item binding of an enclosing `ForEachStep`; §6.3's `loop_ref_outside_loop` rejects it anywhere else, so a lowering that reached for `LoopRef` on an ordinary step result would produce an artifact that cannot validate. `BindingRef` is then checked by §6.4's definite-assignment analysis against the step that carries `save_result_as: "<name>"`.

The two `dep` rows above are the same V1 text lowering two ways for exactly this reason: in the live `default-pipeline.md` both occurrences of `{{outputs.dep.id}}` sit inside a node whose `for_each.as` is `"dep"` (`:106`, `:177`), so they lower to `LoopRef`; the identical string on a node with no enclosing loop lowers to `BindingRef`. `{{outputs.review.task_id}}` (`:90`, `:111`, `:162`) is the ordinary case and lowers to `BindingRef(binding="review", path="task_id")` — including at `:111`, which sits inside the `dep` loop, because `review` is not that loop's binding.

Everything else — `prompt`, natural-language `transitions`, `wait_for_human`, `goto`, `llm_config` — is **not** lowered; it emits `requires_agent_proposal` (question). Rationale: those are exactly the constructs whose V2 form needs an author decision (which profile, which budget, which output schema), and guessing them is the thing the spec forbids.

Line attribution: every lowered element gets a `SourceRef` pointing at the line of its JSON key inside the fenced block, computed from `json.JSONDecoder.raw_decode` offsets plus `source.body_start_line`.

### 7.3 Shadow compile

```python
def shadow_compile(sources: Iterable[PlaybookSource], *, contracts, profiles, events) -> ShadowReport
```

`ShadowReport` is a per-source row: `playbook_id`, `vault_path`, `kind`, `lowered` (bool), `error_count`, `warning_count`, `question_count`, `diagnostics`, `artifact_sha256 | None`. It writes nothing and touches no runtime state.

**What "active sources" means, and why the test must not read a developer's vault.** The roadmap says "shadow-compile current active Markdown sources". The live tree has two disjoint populations, and conflating them makes the suite non-deterministic:

| Population | Where | Count on `1b835131` | Used by |
|---|---|---|---|
| **Bundled defaults** — checked into the repo, seeded into every vault | `src/prompts/default_playbooks/*.md` (3) + `src/prompts/default_agent_type_playbooks/claude-opus/reflection.md` (1) | **4** | the **test**, `TestShadowCompile` |
| **Installed vault sources** — site-specific, whatever the operator has written | `vault/system/playbooks/`, `vault/projects/<pid>/playbooks/`, `vault/agent-types/<id>/playbooks/` (the three globs of `_vault_playbook_dirs`, `src/commands/playbook_commands.py:1286`) | machine-dependent — 5 on this box (3 system + 1 project + 1 agent-type) | the **command**, `playbook_v2_shadow_compile` |

`shadow_compile()` itself takes an `Iterable[PlaybookSource]` and has no opinion about where they came from. `tests/test_playbook_v2_compiler.py::TestShadowCompile` feeds it the **four bundled files only**, read from `src/prompts/`, so the assertions are reproducible on any machine and in CI. `playbook_v2_shadow_compile` (§14.3) walks `_vault_playbook_dirs()` and is therefore an operator report whose row count varies by installation — which is why its own test asserts *shape*, never counts.

Of the four bundled sources, exactly two carry a machine-compiled `kind:` frontmatter and are lowerable by §7.2:

| File | `id` | `kind` | `scope` | Lowerable |
|---|---|---|---|---|
| `default_playbooks/default-pipeline.md` | `default-pipeline` | `pipeline` | `system` | yes — 5 rules, 233 lines |
| `default_playbooks/default-assignment-routing.md` | `default-assignment-routing` | `assignment-routing` | `system` | yes |
| `default_playbooks/memory-consolidation.md` | `memory-consolidation` | *(none)* | `system` | no — prose LLM playbook |
| `default_agent_type_playbooks/claude-opus/reflection.md` | `coding-reflection` | *(none)* | `agent-type:coding` | no — prose LLM playbook |

**Expected result on the live tree, and why it is the point of this task.** The report is expected to show:

- the two prose rows with `requires_agent_proposal` (question) and `lowered: false` — correct, and not a failure;
- `default-assignment-routing.md` lowering to its one-node graph;
- `default-pipeline.md` lowering to five rules, then failing `unknown_event_field` on `event.task.branch_name` (`default-pipeline.md:56`) and `event.task.pr_url` (`:111`) for the reason in §1.4 — plus `unknown_command` on every `CommandStep` until P1 lands (§3.3).

`tests/test_playbook_v2_compiler.py::TestShadowCompile::test_default_pipeline_reports_the_undeclared_task_object` **asserts that `unknown_event_field` failure** rather than working around it, and the test's docstring names §17.1. When Package 1 lands the nested `task` declaration, that test flips to asserting a clean lowering — a one-line change with the amendment as its justification. This is the honest form of "report questions without affecting runtime behavior".

---

### 7.4 `src/playbooks/semantic_diff.py`

```python
DiffChange = Literal["added", "removed", "modified", "unchanged"]   # == Package 5 §4.5

@dataclass(frozen=True)
class FieldChange:
    pointer: str                 # JSON pointer within the rule/step
    before: Any | None
    after: Any | None
    executable: bool             # from definition.is_executable_path (§4.8)

@dataclass(frozen=True)
class StepChange: step_id: str; rule_id: str | None; change: DiffChange; fields: list[FieldChange]
@dataclass(frozen=True)
class RuleChange: rule_id: str; change: DiffChange; steps_added: list[str]; steps_removed: list[str]
@dataclass(frozen=True)
class EdgeChange: edge_id: str; rule_id: str; source: str; target: str; outcome: str; change: DiffChange

@dataclass(frozen=True)
class DefinitionDiff:
    rules: list[RuleChange]; steps: list[StepChange]; edges: list[EdgeChange]
    contracts: list[tuple[str, str | None, str | None]]     # (command, before_fp, after_fp)
    executable_change: bool
    semantic_change_count: int
    presentation_change_count: int

def diff_definitions(base: PlaybookDefinition | None, target: PlaybookDefinition) -> DefinitionDiff
```

Matching: rules by `rule_id`, steps by `(rule_id, step_id)`, edges by `f"{rule_id}::{source}::{outcome}"` — byte-identical to Package 5 §5.2's `GraphEdgeDTO.id`, which is why Package 5 can consume this instead of re-deriving it (§17.2). The diff is over **models, not bytes**: reordering an unordered map is `unchanged`. `base=None` yields everything `added` and `executable_change=True`.

---

## 8. Schema generation — `scripts/generate-playbook-schema.py`

```bash
python scripts/generate-playbook-schema.py            # write src/playbook_v2_schema.json
python scripts/generate-playbook-schema.py --check    # exit 1 on drift, print a unified diff
```

```python
schema = PlaybookDefinition.model_json_schema(
    by_alias=True, ref_template="#/$defs/{model}", mode="serialization"
)
text = json.dumps(schema, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
```

Determinism comes from `sort_keys=True` (Pydantic's `$defs` insertion order is not stable across refactors), `mode="serialization"` (the schema must describe what is *written*, matching `canonical_bytes`), and a fixed `ref_template`. The script imports **only** `src.playbooks.definition`; it must run with no daemon, no database and no config, which is asserted by running it in the test with `AQ_CONFIG` unset.

Two tests:

- `tests/test_playbook_v2_definition.py::test_generated_schema_matches_checked_in_file` — the CI guard, mirroring the V1 guard at `tests/test_playbook_models.py:1677`.
- `tests/test_playbook_v2_definition.py::test_generation_is_idempotent` — runs the script twice into a temp dir and asserts byte equality, which is the roadmap's "run twice and confirm a clean diff after the first generation".

Plus `test_every_fixture_validates_against_the_published_schema`: every fixture in `tests/fixtures/playbooks/v2/` is checked with `jsonschema` against the generated file, so the published schema and the accepting loader cannot become two interpretations (§1.5). **`jsonschema` must be declared.** It is importable today (4.26.0) only as a transitive dependency of the `mcp` extra — `pip show jsonschema` reports `Required-by: mcp`, and it appears nowhere in `pyproject.toml`. Relying on that is a latent break the day `mcp` drops it. Commit 4 adds `"jsonschema>=4.20"` to the `dev` optional-dependency group (`pyproject.toml`, `[project.optional-dependencies]`), which is also what §10.3's `Draft202012Validator.check_schema` needs at **runtime** — so `jsonschema` additionally moves into the base `dependencies` list in the same commit. `test_output_schema_validation_has_its_validator` imports it directly and fails loudly rather than skipping.

`src/playbook_schema.json` is **not** touched; `generate_json_schema()` in `src/playbooks/models.py:991` keeps owning it until Package 7.

---

## 9. Fixture data

All under `tests/fixtures/playbooks/v2/`, shared with Package 5.

### 9.1 `review-pipeline.artifact.json` — the shared golden artifact

Package 5 §10.1 already specifies this file in full and this package **adopts it** as the model's acceptance fixture, with the amendments of §17.2 applied: an `llm` `prompt` and `outcome_field`, a `wait` `awaited` and `outcomes`, an `agent_task` `objective`, `"filter": null` left as-is to exercise §4.1's absent≡null invariant — and **`spec.created` replaced by `spec.approved`** (§3.2), whose registered payload is `required: ["project_id", "spec_path"]`. That last change also forces the fixture's `list-downstream` step to read `{"type": "event_ref", "path": "project_id"}` from a schema that actually declares it, which is the point.

Its load-bearing properties — two rules on two events, convergence, a loop-back, a loop whose body re-enters the loop node, three terminals in one rule, every reserved outcome mapped on the LLM node, one of every expression kind — are exactly the branching patterns §6.2–§6.6 must handle, which is why one fixture serves both packages.

**The fixture is used at two strengths, because Package 1 has not landed (§3.3).** This split is what lets commits 1–2 be green on `1b835131` without weakening a single check:

| Suite | Lookups | Asserts | Green today? |
|---|---|---|---|
| `TestGoldenArtifactModel` | none — model only | parses, round-trips, canonical digest is stable, every step carries a resolvable `SourceRef` | **yes** |
| `TestGoldenArtifactGraph` | stub `ContractLookup`/`ProfileLookup` fixtures defined in the test module | rule closure, cross-rule edges, loops, definite assignment, outcome mapping | **yes** |
| `TestGoldenArtifactContracts` | the real `src.commands.contracts.registry` | `argument_missing`/`argument_unknown`/`type_mismatch`/`stale_contract` against real contracts | **no** — `pytest.mark.skipif(get_contract is None)`, with the skip reason naming §17.1 |

The stub lookups are not a weakening: they are the same `ContractLookup`/`ProfileLookup` protocols the production adapters implement (§3.3), populated from a small literal table in the test module. `test_stub_contracts_match_registry_when_available` asserts the stub table agrees with the real registry the moment P1 lands, so the stubs cannot rot into a parallel truth.

### 9.2 `default-pipeline.lowered.json`

The `SemanticBody` produced by `lower_pipeline` from the **real** `src/prompts/default_playbooks/default-pipeline.md` (233 lines, five rules). Not hand-written: generated once by the lowering and checked in, so a change in lowering behavior shows up as a reviewable diff. It carries the two `unknown_event_field` diagnostics of §7.3 in a sibling `default-pipeline.lowered.diagnostics.json`.

Two micro-sources sit beside it in `tests/fixtures/playbooks/v2/lowering/` to drive T-18a/T-18b without depending on the 233-line real file:

- `output-ref-no-loop.pipeline.md` — one rule, two nodes: the first carries `"output": {"as": "dep"}`, the second (**no** `for_each`) carries `"args": {"waiter_task_ids": ["{{outputs.dep.id}}"]}`. Its checked-in `output-ref-no-loop.lowered.json` has `binding_ref`, and validating it yields zero diagnostics.
- `output-ref-in-loop.pipeline.md` — the same second node with `"for_each": {"source": "outputs.downstream.tasks", "as": "dep"}` added. Its `output-ref-in-loop.lowered.json` has `loop_ref` for `dep` and `binding_ref` for the `review` reference on the same node.

The pair is the regression guard for §7.2's resolution rule in both directions.

### 9.3 One fixture per rejection, in `tests/fixtures/playbooks/v2/invalid/`

One file per diagnostic code in §6.8 that is reachable from a whole artifact, named `<code>.json`, each a **minimal** two-or-three-step artifact differing from a valid twin by exactly the defect. `test_every_registered_code_has_a_test` (§6.8) walks this directory. The named ones that carry the most weight:

- `binding_not_definitely_assigned.json` — a decision with two cases; only one branch runs a command with `save_result_as: "review"`; both converge on a step reading `{"type":"binding_ref","binding":"review"}`.
- `cross_rule_transition.json` — rule `a`'s step transitions into rule `b`'s terminal.
- `nested_loop.json` — a `foreach` whose `body_entry` is another `foreach`.
- `loop_ref_outside_loop.json` — a `LoopRef` on the loop's `continuation` step.
- `llm_branch_without_schema.json` — an `llm` step with `transitions: {"low": …, "high": …}` and an `output_schema` with no matching enum.
- `capability_not_subset.json` — an `agent_task` under a tool-using `llm` step, naming a profile with a strictly wider `aq_commands` set.
- `coalesce_not_total.json` — a two-option coalesce whose last option is an `EventRef` to an optional field.

### 9.4 `hostile-body.json`

A `SemanticBody` payload as an injected compiler agent would emit it: `"id": "default-pipeline"`, `"scope": {"type":"system"}`, `"enabled": true`, `"compiled_against": {"commands": {"ensure_task": "sha256:00…"}}`, plus a `CommandStep` naming `session_kill` — a real command that appears nowhere in the source. Drives §10.1's tests. Sibling `hostile-body.duplicate-keys.json` carries `"id"` twice to drive the duplicate-key rejection in §7.1.

---

## 10. Security analysis

### 10.1 New boundary: the compiler agent's semantic body

The `playbook-compiler` profile reads arbitrary vault Markdown, which is untrusted input (Package 0 §1.1). Package 2's `propose` is the point where that agent's output becomes a candidate artifact.

| Attack | Defense | Test |
|---|---|---|
| Claim a different `id`/`scope` to install a system playbook | §7.1 step 1 strips every `AUTHORITATIVE_FIELDS` key; step 2 rebuilds from frontmatter | `test_hostile_body_cannot_set_identity` |
| Smuggle a key past the strip via duplicate JSON keys | `object_pairs_hook` rejects duplicates before the strip | `test_duplicate_json_keys_are_rejected` |
| Add an unmodelled field (`"__proto__"`, `"exec"`) | `SemanticBody` and every model are `extra="forbid"` | `test_every_v2_model_forbids_extra` |
| Invoke a command the prose never mentions (`session_kill`) | §5.3 identifier inventory ⇒ `unknown_identifier` | `test_command_absent_from_source_is_rejected` |
| Name a broader profile for a delegated agent task | §6.7 static subset check ⇒ `capability_not_subset` | `test_agent_task_cannot_widen_ai_context` |
| Grant itself tools via `tool_use` | §6.7 subset-of-profile check | `test_tool_use_cannot_exceed_profile` |
| Set `enabled: true` / activate | `enabled` is in `AUTHORITATIVE_FIELDS` and has no home in the V2 artifact at all — activation lives in `playbook_activations` (P3) | `test_propose_touches_no_activation_state` |
| Forge `compiled_against` fingerprints to look compatible | `compiled_against` is stripped and recomputed from the registries in §7.1 step 4 | `test_compiled_against_is_server_computed` |

The inventory check is the load-bearing one: it converts "the model may write any JSON" into "the model may only wire together names a human wrote in the source".

### 10.2 Control flow cannot be driven by runtime output

`transitions` keys are validated against a **closed** outcome set per step (§4.6). There is no transition target computed from a result value, no `goto` expression, and no natural-language edge. The spec's "Runtime output cannot alter control flow unless the typed step contract explicitly exposes the referenced field" holds structurally: the only runtime-driven branch is an `llm` step's `outcome_field`, whose legal values are the declared enum, checked to equal the transition keys exactly.

### 10.3 Author-supplied JSON Schema is bounded

`LlmStep.output_schema` is author data reaching a provider's structured-output API. It is validated as draft 2020-12 with `jsonschema.Draft202012Validator.check_schema` (`output_schema_invalid`) and bounded: nesting depth ≤ 5, ≤ 100 properties total, no `$ref`, no `$dynamicRef`, no `unevaluatedProperties` (`output_schema_too_deep`). `$ref` is excluded because it permits remote-schema fetches in some validators — the artifact must be self-contained.

### 10.4 Templates are not a format language

`TemplateValue.parts` is a list of typed values concatenated in order. There is no `{{ }}` parsing at runtime, no `str.format`, no f-string, no user-controllable format spec. The V1 `"{{outputs.x}}"` string form exists only as `pipeline_lowering.py` **input** and never appears in a V2 artifact.

### 10.5 Source excerpts

`SourceRef.excerpt` is author prose from the vault, echoed into the UI. It is capped at 400 characters (matching Package 5's `SourceRefDTO`), truncated on a character boundary with a trailing `…`, and emits `excerpt_truncated` (info). `start_line`/`end_line` are checked against the source's line count (`source_ref_out_of_range`) so a fabricated ref cannot make the UI read past the file. No HTML escaping is done here — that is the renderer's job, and doing it at compile time would corrupt the stored source text.

### 10.6 Denial-of-service surface

`validate_definition` is the only unbounded computation. It is bounded by construction: `steps` ≤ 500 and `rules` ≤ 50 (`Field(max_length=…)` on `PlaybookDefinition`); the §6.4 fixpoint is O(|steps| × |bindings|) per rule with a hard 1000-iteration trip-wire that raises `ValidationBudgetExceeded` (surfaced as `state_limit_exceeded`); expression trees are depth-capped at 10 by a recursive model validator. `test_pathological_artifact_is_bounded` feeds a 500-step artifact and asserts the whole validation completes under 2 seconds.

---

## 11. Storage, Alembic, SQLite and PostgreSQL

**Package 2 adds no table, no column and no migration.** `alembic revision --autogenerate` after this package must produce an empty diff; `tests/test_playbook_v2_definition.py::test_package_2_adds_no_schema_change` asserts `compare_metadata()` is empty against `head`. Persistence is Package 3's.

Two forward constraints this package's canonical form imposes on Package 3, stated here because they are decisions made by §4.7 and would be expensive to discover later:

- **The artifact must be stored as bytes, not as JSON.** `artifact_sha256` is over `canonical_bytes`. PostgreSQL `jsonb` normalizes key order, strips insignificant whitespace and collapses duplicate keys; a round-trip through it changes the bytes and breaks hash verification. Store the artifact in the content-addressed file (`compiled/artifacts/<sha256>.json`, spec §"Storage and activation") and keep only the digest, path and validation summary in the database — as `TEXT`/`VARCHAR`, identical on both backends.
- **Digest columns are fixed-width `VARCHAR(71)`** (`sha256:` + 64 hex), which behaves identically on SQLite and PostgreSQL and permits a plain B-tree index for artifact lookup on both.

No `datetime` reaches the database in this package. `compiled_at` is serialized by `model_dump(mode="json")` to an ISO-8601 string with an explicit `+00:00` offset, so the SQLite/PostgreSQL timezone divergence that bites elsewhere in this repo does not apply to the artifact bytes.

---

## 12. Observability and operator failure behavior

- **Every diagnostic is addressable.** `Diagnostic` carries `code`, `rule_id`, `step_id`, `field` (JSON pointer) and `source`. The CLI formatter prints `<severity> <code> at <path>:<line> — <message>`, which is the same tuple the dashboard renders in Package 5.
- **Questions are not errors.** `severity == "question"` means the prose left an executable decision unmade. A proposal with questions and no errors still returns its partial artifact when one could be assembled, so the reviewer sees the graph *and* the gaps. `CompileProposal.activatable` is false for both.
- **Logging.** `propose` logs one structured line at INFO: `playbook_id`, `source_digest`, `artifact_sha256`, `compiler_build`, and the three counts. Failures log at WARNING with the same fields plus the first five codes. No artifact body and no `excerpt` is logged — proposals can contain project-specific prose.
- **Operator failure behavior.** Every failure mode of this package is "the proposal is not activatable, and here is the list". There is no partial application: `propose` has no side effects at all, so there is nothing to roll back. The corresponding runtime failure (`stale_contract` on an already-active artifact) is Package 3's health computation, which calls the same `contract_fingerprint` this package exports.
- **Shadow compile is a report.** `playbook_v2_shadow_compile` writes nothing and is safe to run against production; §7.3 makes its expected non-clean output explicit so an operator does not read the two `unknown_event_field` rows as a regression.

---

## 13. Feature flag ownership

One flag: **`playbooks.v2.compiler_enabled`**, default `false`, added to `PlaybooksConfig` (`src/config.py:857`, reached as `config.playbooks` at `:1641`).

- It gates only the three new commands in §14. With it off they return `{"success": False, "error": "playbook v2 compiler is disabled"}`.
- It gates **nothing** in `definition.py`, `expressions.py`, `validation.py`, `semantic_diff.py` — importing and using the models never depends on config, so Packages 3–5 can build on them before the flag flips.
- **Removal package: 7**, in the same commit that deletes the V1 compiler modules. Recorded in the flag's config docstring as `# removed in Playbook V2 Package 7`.

---

## 14. Commands and API examples

Three new commands. Each needs an entry in `_ALL_TOOL_DEFINITIONS` (beside `playbook_validate` at `src/tools/definitions.py:4522`) and the category map (`src/tools/definitions.py:236-237`), which auto-exposes them over MCP, the generated CLI and the codegen routers — so **`openapi.json` and both generated clients must be regenerated** (§15).

Agent reachability: `playbook_v2_propose` and `playbook_v2_validate` are added to `_PLAYBOOK_COMPILER_COMMANDS` (`src/api/scope.py:146`, read at `:312`), the existing per-assignment carve-out that already admits `playbook_validate`/`playbook_install` for exactly the `playbook-compiler` profile. They are **not** added to `AGENT_COMMAND_SET` (`src/api/scope.py:13`) — Package 0 §1.5 forbids widening the global set, and `playbook_v2_shadow_compile` is operator-only and appears in neither.

### 14.1 `playbook_v2_validate`

```jsonc
// POST /api/playbook/v2-validate
{"path": "system/playbooks/default-pipeline.v2.json"}
```

```jsonc
{
  "success": false,
  "artifact_sha256": null,
  "counts": {"error": 2, "warning": 0, "question": 0, "info": 1},
  "diagnostics": [
    {"severity": "error", "code": "unknown_event_field",
     "message": "event.task.branch_name is not declared on task.completed",
     "rule_id": "per-task-review", "step_id": null, "field": "/guard/value/path",
     "source": {"path": "system/playbooks/default-pipeline.md",
                "start_line": 55, "end_line": 55, "heading": "Per-task review", "excerpt": null}},
    {"severity": "error", "code": "binding_not_definitely_assigned",
     "message": "binding 'review' is not assigned on every path to 'gate-downstream' (path: create-review -> done)",
     "rule_id": "per-task-review", "step_id": "gate-downstream",
     "field": "/inputs/await_id", "source": {"…": "…"}},
    {"severity": "info", "code": "type_unknown",
     "message": "event.task has no declared type; input type check skipped",
     "rule_id": "per-task-review", "step_id": "create-review", "field": "/inputs/description"}
  ]
}
```

### 14.2 `playbook_v2_propose`

```jsonc
// POST /api/playbook/v2-propose
{"playbook_id": "default-pipeline",
 "semantic_body_path": "system/playbooks/.proposals/default-pipeline.body.json",
 "baseline_artifact_path": null}
```

```jsonc
{
  "success": true,
  "activatable": false,
  "artifact_sha256": "sha256:9f2c…41ab",
  "source_digest": "sha256:6f1c…928f",
  "contract_fingerprint": "sha256:31c9…8f0a",
  "compiler_build": "playbook-v2-compiler/1",
  "counts": {"error": 0, "warning": 1, "question": 2, "info": 0},
  "diagnostics": [
    {"severity": "warning", "code": "authority_field_ignored",
     "message": "compiler-supplied 'scope' was discarded; scope comes from frontmatter",
     "rule_id": null, "step_id": null, "field": "/scope", "source": null},
    {"severity": "question", "code": "ambiguous_prose",
     "message": "the failure path for 'Ensure a review task' is not stated in the source",
     "rule_id": "per-task-review", "step_id": "create-review", "field": "/transitions/failure",
     "source": {"path": "system/playbooks/default-pipeline.md",
                "start_line": 21, "end_line": 24, "heading": "Per-task review",
                "excerpt": "spawns one reviewer task with a `discovered-from` edge…"}}
  ],
  "semantic_diff": null,
  "artifact": {"schema_version": 2, "id": "default-pipeline", "…": "…"}
}
```

`"activatable": false` with zero errors is the shape that matters: two unanswered questions block activation exactly as a hard error would, which is the spec's "Ambiguous prose returns source-linked questions rather than defaults".

### 14.3 `playbook_v2_shadow_compile`

```jsonc
// POST /api/playbook/v2-shadow-compile
{"scope": "system"}
```

```jsonc
{
  "success": true,
  "total": 5, "lowered": 2, "clean": 1,          // installation-dependent; see §7.3
  "rows": [
    {"playbook_id": "default-pipeline", "vault_path": "system/playbooks/default-pipeline.md",
     "kind": "pipeline", "lowered": true, "artifact_sha256": null,
     "counts": {"error": 2, "warning": 0, "question": 0, "info": 4}},
    {"playbook_id": "default-assignment-routing",
     "vault_path": "system/playbooks/default-assignment-routing.md",
     "kind": "assignment-routing", "lowered": true,
     "artifact_sha256": "sha256:c4e0…77d1",
     "counts": {"error": 0, "warning": 0, "question": 0, "info": 0}},
    {"playbook_id": "memory-consolidation", "vault_path": "system/playbooks/memory-consolidation.md",
     "kind": "", "lowered": false, "artifact_sha256": null,
     "counts": {"error": 0, "warning": 0, "question": 1, "info": 0}}
  ]
}
```

`total` is the number of sources under `_vault_playbook_dirs()` on **this** installation (5 here: 3 system + 1 project + 1 agent-type), not a constant. `tests/test_playbook_v2_commands.py::test_shadow_compile_reports_every_vault_source` points `config.data_dir` at a `tmp_path` vault it seeds itself, so the command's own test is deterministic without pretending the number is fixed.

### 14.4 The compiler profile

`src/profiles/defaults/playbook-compiler/profile.md` gains a V2 section under `## Role` and two names in `## Tools` (`playbook_v2_validate`, `playbook_v2_propose`). Its `## Rules` block gains the inventory rule, which is the one instruction that changes the model's behavior rather than its vocabulary:

> - Emit **only** `rules` and `steps`. Do not emit `id`, `version`, `scope`, `source_hash`, `compiled_at`, `enabled`, `triggers` or `compiled_against` — the server owns them and will discard yours with a diagnostic.
> - Every command name, profile id, event type, event field, binding name and outcome label you emit must appear **verbatim in backticks** in the source Markdown, or in its frontmatter. If the prose does not name it, you may not invent it: return the question instead.
> - Iterate against `playbook_v2_validate`; a `question` blocks activation exactly as an `error` does.

`tests/test_playbook_v2_compiler.py::TestCompilerProfileContract` asserts every command named in that Role section appears in `## Tools` — the same invariant the repo's existing `<!-- tools-rationale -->` convention states (`src/profiles/defaults/playbook-compiler/profile.md:79`).

---

## 15. Tasks

Five commits, matching the roadmap's sequence. Every task names its failing assertion **before** its implementation.

### 15.1 Commit 1 — `test: define strict playbook v2 model invariants`

| # | Red | Green |
|---|---|---|
| T-1 | `tests/test_playbook_v2_definition.py::test_golden_artifact_loads` — `ModuleNotFoundError: src.playbooks.definition` | — (red only; the fixture and the test land first) |
| T-2 | Check in `tests/fixtures/playbooks/v2/review-pipeline.artifact.json` per §9.1, and the nine `invalid/` fixtures of §9.3 that do not need P1 | — |
| T-3 | `test_every_v2_model_forbids_extra`, `test_absent_and_null_are_the_same_model`, `test_round_trip_is_identity`, `test_canonical_bytes_are_key_order_independent` — all fail at import | — |

Verify: `aq test tests/test_playbook_v2_definition.py` — collects, every test fails on import. Commit red deliberately (this is the roadmap's step 1, "specify the invariants").

### 15.2 Commit 2 — `feat: add typed expressions and whole-graph validation`

| # | Red | Green |
|---|---|---|
| T-4 | commit 1's suite | `src/playbooks/expressions.py` — §4.2, §4.3, `ENGINE_CONTEXT_SCHEMA` |
| T-5 | as above | `src/playbooks/definition.py` — §4.4–§4.8, the four hash functions, `WAIT_RESULT_SCHEMAS`, `FOREACH_RESULT_SCHEMA`, `AGENT_TASK_RESULT_SCHEMA` |
| T-6 | `tests/test_playbook_v2_expressions.py::TestValueTyping` — one test per row of §6.5's table, each asserting the computed `ValueType` | the typing lattice in `validation.py` |
| T-7 | `tests/test_playbook_v2_validation.py::TestDefiniteAssignment` — six tests per §6.4 | the fixpoint analysis |
| T-8 | `TestRuleClosure`, `TestLoops`, `TestOutcomeMapping`, `TestIdentifierInventory` — one per §6.1–§6.6 code | the remaining passes |
| T-9 | `test_every_registered_code_has_a_test` | `DIAGNOSTIC_CODES` and the `invalid/` fixture walk |

Verify: `aq test tests/test_playbook_v2_definition.py tests/test_playbook_v2_expressions.py tests/test_playbook_v2_validation.py` green; `ruff check src/playbooks/definition.py src/playbooks/expressions.py src/playbooks/validation.py tests/test_playbook_v2_*.py`.

### 15.3 Commit 3 — `feat: compile markdown into v2 proposals`

| # | Red | Green |
|---|---|---|
| T-10 | `tests/test_playbook_v2_compiler.py::TestAuthority` — the six §10.1 tests against `hostile-body.json` | `src/playbooks/authoring.py` (§5) and `src/playbooks/proposal.py` (§7.1) |
| T-11 | `test_propose_touches_no_activation_state` — a `PlaybookManager` double whose every method raises | — (passes once `propose` is pure; the test is the proof) |
| T-12 | `TestSemanticDiff` — added/removed/modified/unchanged, and `executable_change=False` for a title-only edit | `src/playbooks/semantic_diff.py` (§7.4) |
| T-13 | `tests/test_playbook_v2_commands.py` — the three commands' arg validation, disabled-flag behavior, and the §14 response shapes | the commands, `src/tools/definitions.py` entries, the `src/api/scope.py:146` carve-out, the `playbooks.v2.compiler_enabled` flag |
| T-14 | `TestCompilerProfileContract` | the profile edits of §14.4 |

Verify: `aq test tests/test_playbook_v2_compiler.py tests/test_playbook_v2_commands.py`; then the client regeneration in §16.

### 15.4 Commit 4 — `build: generate playbook v2 json schema`

| # | Red | Green |
|---|---|---|
| T-15 | `test_generated_schema_matches_checked_in_file` — the file does not exist | `scripts/generate-playbook-schema.py` + `src/playbook_v2_schema.json` |
| T-15b | `test_output_schema_validation_has_its_validator` — `jsonschema` is only transitively present (§8) | declare `jsonschema>=4.20` in `pyproject.toml` `dependencies` |
| T-16 | `test_generation_is_idempotent`, `test_every_fixture_validates_against_the_published_schema` | — |
| T-17 | `tests/test_playbook_models.py::test_schema_file_matches_generated` must still pass | assert V1's file is byte-unchanged (`git diff --exit-code src/playbook_schema.json`) |

Verify: `python scripts/generate-playbook-schema.py && python scripts/generate-playbook-schema.py --check` (exit 0); `aq test tests/test_playbook_v2_definition.py tests/test_playbook_models.py`.

### 15.5 Commit 5 — `test: shadow compile active playbook sources`

| # | Red | Green |
|---|---|---|
| T-18 | `TestPipelineLowering` — one test per row of §7.2's table, driven by `default-pipeline.lowered.json` | `src/playbooks/pipeline_lowering.py` |
| T-18a | `TestPipelineLowering::test_non_loop_output_reference_lowers_to_binding_ref` — lowers a one-rule pipeline whose node has **no** `for_each` and whose `args` carry `{"waiter_task_ids": ["{{outputs.dep.id}}"]}`, asserting the value is `{"type":"list","items":[{"type":"binding_ref","binding":"dep","path":"id"}]}` (**not** `loop_ref`) and that `validate_definition` on the lowered artifact returns **no** `loop_ref_outside_loop` and no `binding_not_definitely_assigned` — the assigning node carries `output.as: "dep"`. Red before §7.2's resolution rule exists, because a table-driven lowering that hard-codes `loop_ref` for list-of-output-ref emits `loop_ref` and §6.3 then rejects the artifact | the enclosing-`for_each` walk in `lower_pipeline` |
| T-18b | `TestPipelineLowering::test_loop_item_reference_lowers_to_loop_ref` — the same rule with `for_each: {"source": "outputs.downstream.tasks", "as": "dep"}` on the node asserts `loop_ref`, and `{{outputs.review.task_id}}` on that same node still asserts `binding_ref`. Guards the fix from over-correcting into "always `BindingRef`" | as above |
| T-19 | `TestShadowCompile::test_default_pipeline_reports_the_undeclared_task_object` (§7.3) | `shadow_compile` |
| T-20 | `test_shadow_compile_writes_nothing` — a read-only vault fixture; any write raises | — |
| T-21 | `test_pathological_artifact_is_bounded` (§10.6) | the caps in §10.6 |

Verify: `aq test tests/test_playbook_v2_compiler.py`; then the full §16 list.

---

## 16. Verification

### 16.1 Per-package required commands (roadmap §5, reconciled per §2)

```bash
# roadmap: pytest tests/playbooks/test_definition.py test_expressions.py test_v2_validation.py test_v2_compiler.py -q
aq test tests/test_playbook_v2_definition.py tests/test_playbook_v2_expressions.py \
        tests/test_playbook_v2_validation.py tests/test_playbook_v2_compiler.py \
        tests/test_playbook_v2_commands.py -q

# roadmap: generate twice, clean diff after the first generation
python scripts/generate-playbook-schema.py
python scripts/generate-playbook-schema.py --check      # exit 0
git diff --exit-code src/playbook_v2_schema.json        # exit 0

# roadmap: existing V1 compiler tests remain green
aq test tests/test_playbook_models.py tests/test_playbook_compiler_scope.py \
        tests/test_playbook_install_compiled.py tests/test_playbook_validate_install_commands.py \
        tests/test_compile_playbook_command.py tests/test_playbook_services.py \
        tests/test_assignment_playbook_compiler.py -q
git diff --exit-code src/playbook_schema.json           # exit 0

# roadmap: ruff
ruff check src/playbooks scripts/generate-playbook-schema.py tests/test_playbook_v2_*.py
```

### 16.2 Generated clients

Three new tool definitions change the served API surface, so both clients and the committed spec must be regenerated or `tests/test_api_client_contract.py::test_committed_openapi_json_matches_the_live_app_surface` fails:

```bash
./scripts/regenerate-api-client.sh --offline
./scripts/regenerate-ts-client.sh --offline
aq test tests/test_api_client_contract.py -q
```

### 16.3 Migration

```bash
alembic upgrade head
alembic revision --autogenerate -m "package 2 sanity"   # MUST produce an empty migration
git status --short migrations/versions/                 # then delete the empty revision
```

`test_package_2_adds_no_schema_change` (§11) automates the same assertion.

### 16.4 One area suite before closing

```bash
aq test tests/test_playbook*.py tests/test_pipeline*.py tests/test_compile_playbook_command.py \
        tests/test_assignment_playbook_compiler.py tests/test_api_playbook_graph_view.py -q
```

Not the whole repo. Package 2 touches no runtime path; the area suite plus §16.1's V1 list is the evidence.

---

## 17. Amendments required in other child plans

Roadmap §7: *"Changes to locked interfaces require updating all not-yet-completed child plans."* These are the changes this plan makes to interfaces other plans already reference. Each must be applied to the named document **in the commit that implements it**.

### 17.1 Package 1 (`docs/superpowers/plans/2026-09-01-playbook-v2-contracts-intent.md`, not yet written)

1. **Declare the hydrated `task` object.** `EVENT_SCHEMAS["task.completed"]` (and every `task.*` event that reaches a pipeline) must declare the nested object that `src/orchestrator/core.py:855` injects, with typed sub-fields for at least `branch_name`, `pr_url`, `status`, `profile_id`, `parent_task_id`. Without it the shipped default pipeline cannot be transcribed to V2 (§1.4, §7.3). This is the single hardest dependency in Package 2.
2. **`EventSchema` needs types for nested paths.** `EventRef` typing (§6.5) walks a dotted path; today `EventSchema.types` (`src/event_schemas.py:47`, on the TypedDict at `:32`) is a flat, **optional** `NotRequired[dict[str, type | tuple[type, ...]]]` — flat, one level, and absent on every `task.*` schema. Package 1's enrichment must make a nested path resolvable, or every nested `EventRef` degrades to `type_unknown` (info) and the type check is silently off.
3. **`CommandContract` must expose** `arguments` (a Pydantic model class), `result` (ditto), `outcomes: frozenset[str]` (closed), `execution_fingerprint: str` in `sha256:<64hex>` form, and `required_capability: str`. §3.2.

### 17.2 Package 5 (`docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md`, checked in)

1. **§10.1's fixture triggers on an event that does not exist.** `sweep-on-spec-created` has `"event_type": "spec.created"`, and `get_schema("spec.created")` returns `None` on `origin/main` `1b835131` — the registry has `spec.approved` (`required: ["project_id", "spec_path"]`). Under §6.1 that is `unknown_event` (error), so the shared golden artifact does not validate as written. **Change the fixture to `spec.approved`** (and rename the rule `sweep-on-spec-approved`); its `project_id` field is declared, so the fixture's `{"type":"event_ref","path":"project_id"}` keeps working. Alternatively P1 registers `spec.created` — but inventing an event to satisfy a fixture is the wrong direction. §3.2, §9.1.
2. **§10.1's fixture is incomplete against the locked model.** `classify-risk` needs `prompt` and `outcome_field: "risk"`; `escalate`'s `objective` is present but `inputs` should be shown; `await-approval` needs `awaited` and `outcomes: ["approve","revise"]`. Apply §4.5's requirements to the fixture; it stays the shared golden artifact (§9.1).
3. **`RuleClusterDTO` has no projection for `Rule.guard`.** The spec requires guards to appear in the inspector ("Both survive compilation and appear in the inspector"); §4.4 keeps `guard` in the model. Package 5 must add `guard: ExplanationValueDTO | None` (kind `"expression"`) to `RuleClusterDTO`. Filed as an emergent task.
4. **§5.2's `diff_artifacts` should build on `src/playbooks/semantic_diff.diff_definitions`** (§7.4) rather than re-deriving rule/step/edge matching. The edge id and the executable/presentation split are already identical by construction; re-deriving them is the drift risk.
5. **`ArtifactRefDTO.contract_fingerprint` and `.source_digest`** are produced by `PlaybookDefinition.contract_fingerprint()` and `source_digest()` (§4.7), not recomputed in the projector.

### 17.3 Package 3

`ArtifactStore.put`/`load` must use `canonical_bytes` and verify `sha256(file_bytes)`; the artifact must not be round-tripped through a JSON/JSONB column (§4.7, §11).

---

## 18. Mapping to the package exit gate

> **Exit gate (roadmap §5, Package 2):** *"A prose source can produce a reviewable V2 proposal whose graph is strict, source-mapped, fully validated, and fingerprinted, but no proposal can become active as a side effect of compilation."*

| Gate clause | Where it is met | The test that proves it |
|---|---|---|
| a prose source | `PlaybookSource.load` + `IdentifierInventory` (§5) | `TestIdentifierInventory` |
| produces a **reviewable** proposal | `CompileProposal` with diagnostics, questions and a semantic diff (§7.1, §7.4) | `TestSemanticDiff`, `test_questions_block_activatable` |
| whose graph is **strict** | `extra="forbid"` on every model; absent≡null; lossless round-trip (§4.1) | `test_every_v2_model_forbids_extra`, `test_round_trip_is_identity` |
| **source-mapped** | required `SourceRef` on every rule and step; line attribution in lowering (§4.4, §7.2) | `test_every_step_carries_a_resolvable_source_ref` |
| **fully validated** | the seven passes of §6.1–§6.7 and the closed 47-code set of §6.8 | `test_every_registered_code_has_a_test` |
| **fingerprinted** | `artifact_sha256`, `source_digest`, `contract_fingerprint`, `compiled_against` (§4.7) | `test_canonical_bytes_are_stable_across_processes`, `test_compiled_against_is_server_computed` |
| **no proposal can become active** | `propose` has no side effects; `enabled` has no home in the artifact; activation is P3's table (§7.1, §13) | `test_propose_touches_no_activation_state`, `test_shadow_compile_writes_nothing` |

And the roadmap's thirteen Package 2 required outcomes:

| Outcome | Section |
|---|---|
| seven step variants as a discriminated union | §4.5 |
| typed literal/event/binding/loop/template/comparison/boolean/existence expressions | §4.2, §4.3 |
| exact `on_success`/`on_failure`/decision/loop/wait/terminal edges preserved | §4.6, §7.2 |
| one owner rule per step; no cross-rule transitions | §6.2 |
| unique ids, entry nodes, reachability, terminal paths, event fields, command arguments, result references, output shapes | §6.1, §6.2, §6.5, §6.6 |
| definite-assignment analysis across branches and convergence | §6.4 |
| reject reassignment, loop shadowing, nested loops, out-of-scope loop refs | §6.3, §6.4 |
| identifier inventory; backticks and source locations preserved in diagnostics | §5.2, §5.3, §12 |
| validate every AI profile and its effective capability subset at compile time | §6.7 |
| deterministic schema generation | §8 |
| proposal object with artifact, diagnostics, questions, source digest, semantic diff, contract fingerprint | §7.1 |
| review/activation required outside the compiler | §7.1, §13 |
| shadow-compile active sources and report questions | §7.3 |
| deterministic fixtures for every step kind, expression kind, validation error, branching pattern | §9 |

---

## 19. Rollback boundary

Roadmap: *"V2 definitions and compiler outputs are additive. Removing the V2 proposal entry point leaves V1 compilation and execution intact."*

Concretely, reverting Package 2 is: delete six new modules (`definition.py`, `expressions.py`, `authoring.py`, `validation.py`, `proposal.py`, `semantic_diff.py`, `pipeline_lowering.py`), one script, one generated schema file, five test files and the fixture directory; remove three entries from `src/tools/definitions.py`, two names from `_PLAYBOOK_COMPILER_COMMANDS` (`src/api/scope.py:146`), one config flag, the `jsonschema` dependency line, and the profile edits. No table, no migration, no V1 code path. The two touched V1 files (`compiler.py`, `models.py`) receive only a re-export and a docstring, so reverting them is a two-line diff.

Set `playbooks.v2.compiler_enabled: false` (its default) to disable the surface without a revert.

---

## 20. Open items for the next child plans

1. **Quantified collection predicates** (`any`/`all` over a collection) are excluded from initial V2 (§4.3). If Package 6 finds a shipped playbook that needs one, it belongs in a follow-up that also extends §6.4 — not in a permissive escape hatch.
2. **Nested `ForEachStep`** is rejected (§6.3), per the spec. Revisit only with a durable loop-frame stack in Package 3.
3. **`DecisionStep.default` is required** here and optional in the spec (§4.5). If a reviewer disagrees, the change is one field and one validation rule, but it reintroduces an un-displayed fall-through.
4. **`COMPILER_BUILD` must be bumped by Package 6** when the bundled Markdown is rewritten as normative prose, so pre- and post-rewrite artifacts are distinguishable in the receipt trail (§4.7).
5. **Assignment-routing lowering** (§7.2) covers the current one-node graph. If `purpose: "assignment_routing"` grows a real graph before Package 4, that lowering needs revisiting.
6. **`spec.created` vs `spec.approved`** (§3.2, §17.2). The shared golden fixture is changed to `spec.approved` here. If Package 1 decides `spec.created` should be a real event, the fixture moves back and this item closes; until then, no plan may reference `spec.created`.
7. **`jsonschema` becomes a declared runtime dependency** in commit 4 (§8). If a reviewer would rather keep it test-only, §10.3's `output_schema` validation has to move behind an import guard, and an unvalidatable `output_schema` then has to be an error rather than a skip — say so explicitly if that is the call.
8. **Package 7 wants a home in `expressions.py`.** The Package 7 child plan (`2026-09-01-playbook-v2-cutover-cleanup.md` §3.6, and its D1-b row) moves `_parse_json_from_text` out of the deleted `src/playbooks/runner_context.py` into **this package's** `src/playbooks/expressions.py`, renamed `parse_json_from_text`, because `validate_assignment_response` (`src/orchestrator/assignment_routing.py:85`) survives cutover and needs it. That move belongs to Package 7's commit, not this one — Package 2 must not pre-emptively add it — but §4.2's module must stay import-light enough to accept a pure parser later: `expressions.py` may import only `pydantic`, `typing`, `json` and `re`, and nothing from `src.playbooks`. `test_expressions_module_has_no_intra_package_imports` pins that.

9. **Package 7 does not name `src/playbook_schema.json`.** §2 defers deleting V1's schema file to Package 7, and Package 7's plan does not mention it. Whoever writes Package 7's implementation must add it to the deletion list alongside `generate_json_schema()` (`src/playbooks/models.py:991`) and `tests/test_playbook_models.py:1677`, or the repo keeps a stale generated artifact with no generator.

10. **Amendments applied.** Record here, with a date, every amendment made under §3.2.1's stop-and-amend rule.

   - *2026-09-02* — rebased from `origin/main` `130be765` to `1b835131`. Package 0 landed (`d0a4c905`): §3.1 rewritten from assumption to verified signature. Re-verified again at `8335fa5d` (docs-only delta). Corrected line citations in §1.1 (`models.py:306/307/312`), §1.4 (`default-pipeline.md:56`), §1.5 (`validator_command.py:135`, `profile.md:96`), §2 (`validator_command.py:135/:241`, `definitions.py:4522/:236-237`, `profile.md` is 102 lines), §6 (`profile.md:20-22`), §6.2 (`pipeline_compiler.py:196`), §14 (`scope.py:146`), §14.4 (`profile.md:79`), §17.1 (`event_schemas.py:47`). Replaced the unsupported "fourteen shipped sources" in §7.3 with the verified 4-bundled / 5-installed split and made the shadow-compile suite read only the bundled files. Found `spec.created` unregistered (§17.2 item 1) and `jsonschema` undeclared (§8).
