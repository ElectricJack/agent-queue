"""Whole-graph validation for the Playbook V2 artifact.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md``
§6 (the seven passes and the closed diagnostic set), §6.4 (definite assignment),
§6.5 (the value-typing lattice), §6.7 (profiles and capabilities) and §10.3
(bounded author-supplied JSON Schema).

Validation is **total**: it runs every pass and returns every diagnostic.  It
never raises on invalid input and never stops at the first error, because the
compiler agent's repair loop depends on getting the whole error list back in one
call (``src/profiles/defaults/playbook-compiler/profile.md``).

Degradation is always toward *error*.  When a lookup cannot resolve a name the
result is ``unknown_command`` / ``unknown_profile`` / ``unknown_event``, never
"assume fine"; there is no flag that makes an unresolvable reference pass.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Final, Literal, Protocol, runtime_checkable

from src.playbooks.definition import (
    LLM_RESERVED_OUTCOMES,
    RESERVED_OUTCOMES,
    RUNTIME_ERROR_KEY,
    AgentTaskStep,
    CommandStep,
    ForEachStep,
    LlmStep,
    PlaybookDefinition,
    Rule,
    SourceRef,
    TerminalStep,
    WaitStep,
    business_outcomes,
    reserved_outcomes_for,
    result_schema_for,
    step_profile_ids,
    step_targets,
    step_values,
)
from src.playbooks.expressions import (
    ENGINE_CONTEXT_SCHEMA,
    LOOP_ONLY_CONTEXT_PATHS,
    BindingRef,
    CoalesceValue,
    ContextRef,
    EventRef,
    ListValue,
    LiteralValue,
    LoopRef,
    ObjectValue,
    TemplateValue,
    condition_values,
)

Severity = Literal["error", "warning", "question", "info"]


@dataclass(frozen=True)
class Diagnostic:
    """One finding. ``field`` is a JSON pointer relative to the step or rule."""

    severity: Severity
    code: str
    message: str
    rule_id: str | None = None
    step_id: str | None = None
    field: str | None = None
    source: SourceRef | None = None


class ValidationBudgetExceeded(RuntimeError):
    """§10.6 — the fixpoint trip-wire; surfaced as ``state_limit_exceeded``."""


#: §10.6 — the fixpoint iteration trip-wire.
MAX_FIXPOINT_ITERATIONS: Final[int] = 1000

#: §6.3 — names a loop variable may never take.
RESERVED_BINDING_ROOTS: Final[frozenset[str]] = frozenset(
    {"event", "context", "loop", "run", "rule", "step"}
)

#: §10.3 — bounds on an author-supplied ``LlmStep.output_schema``.
MAX_OUTPUT_SCHEMA_DEPTH: Final[int] = 5
MAX_OUTPUT_SCHEMA_PROPERTIES: Final[int] = 100
FORBIDDEN_SCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"$ref", "$dynamicRef", "unevaluatedProperties"}
)


# --------------------------------------------------------------------------
# §6.8 — the closed diagnostic set
# --------------------------------------------------------------------------

#: Codes ``validate_definition`` itself can emit.
VALIDATOR_CODES: Final[frozenset[str]] = frozenset(
    {
        "unknown_identifier",
        "unknown_command",
        "unknown_profile",
        "unknown_event",
        "unknown_event_field",
        "unknown_context_path",
        "duplicate_rule_id",
        "duplicate_binding",
        "step_rule_unknown",
        "orphan_step",
        "rule_entry_unknown",
        "rule_entry_not_owned",
        "unknown_step_target",
        "cross_rule_transition",
        "unreachable_step",
        "no_terminal_path",
        "nested_loop",
        "loop_body_escapes",
        "continuation_mismatch",
        "loop_variable_shadow",
        "loop_ref_outside_loop",
        "binding_not_definitely_assigned",
        "binding_reassigned",
        "type_mismatch",
        "type_unknown",
        "coalesce_not_total",
        "argument_missing",
        "argument_unknown",
        "unmapped_business_outcome",
        "unmapped_reserved_outcome",
        "unknown_transition_outcome",
        "outcome_enum_mismatch",
        "llm_branch_without_schema",
        "output_schema_invalid",
        "output_schema_too_deep",
        "profile_capability_empty",
        "tool_use_not_subset",
        "narrowing_not_subset",
        "capability_not_subset",
        "delegation_runtime_checked",
        "stale_contract",
    }
)

#: Codes the strict models reject before ``validate_definition`` ever runs, and
#: the JSON text loader's duplicate-key rejection (``load_definition_json``).
MODEL_CODES: Final[frozenset[str]] = frozenset({"empty_boolean_operand", "duplicate_step_id"})

#: Codes only the compiler slice can raise: they need the Markdown source, which
#: ``validate_definition`` deliberately does not take.
COMPILER_ONLY_CODES: Final[frozenset[str]] = frozenset(
    {
        "authority_field_ignored",
        "requires_agent_proposal",
        "ambiguous_prose",
        "source_ref_out_of_range",
        "excerpt_truncated",
    }
)

DIAGNOSTIC_CODES: Final[frozenset[str]] = VALIDATOR_CODES | MODEL_CODES | COMPILER_ONLY_CODES

#: Every code is ``error`` unless it appears here.
DIAGNOSTIC_SEVERITY: Final[dict[str, Severity]] = {
    "authority_field_ignored": "warning",
    "profile_capability_empty": "warning",
    "requires_agent_proposal": "question",
    "ambiguous_prose": "question",
    "type_unknown": "info",
    "delegation_runtime_checked": "info",
    "excerpt_truncated": "info",
}


def severity_of(code: str) -> Severity:
    return DIAGNOSTIC_SEVERITY.get(code, "error")


# --------------------------------------------------------------------------
# §3.3 — the lookup seams
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArgumentSpec:
    """One declared command argument, normalized away from Pydantic."""

    name: str
    type: ValueType
    required: bool


@dataclass(frozen=True)
class ContractInfo:
    """What validation needs from a command contract.

    Normalized rather than taken raw so the seam does not re-shape every time
    Package 1's ``ExecutionContract`` grows a field, and so a test can build a
    contract lattice as a literal.  ``RegistryContractLookup`` adapts the live
    ``src.commands.contracts.registry.CONTRACTS``.
    """

    name: str
    arguments: Mapping[str, ArgumentSpec]
    result_schema: Mapping[str, Any]
    outcomes: frozenset[str]
    execution_fingerprint: str
    outcome_classes: Mapping[str, str] = dataclass_field(default_factory=dict)


@runtime_checkable
class ContractLookup(Protocol):
    def get(self, name: str) -> ContractInfo | None: ...


@runtime_checkable
class ProfileLookup(Protocol):
    def policy(self, profile_id: str) -> Any | None:
        """The profile's ``CapabilityPolicy``, or ``None`` when unknown."""

    def routing(self, profile_id: str) -> Any | None:
        """The profile's :class:`~src.profiles.intelligence.ProfileIntelligence`
        **for a session launch**, where the harness fixes the provider.

        ``None`` when the profile is unknown.  Validation never reads this —
        it exists because the semantic graph's AI cards must state which
        provider and model a step's profile actually resolves to, and this
        lookup is the only seam the projection has onto a profile.
        """

    def direct_routing(self, profile_id: str) -> Any | None:
        """The same, **for a headless direct-path call** (an ``LlmStep``).

        A separate method rather than an argument to :meth:`routing` because
        the two answers differ in the provider: an ``LlmStep`` runs no CLI,
        so ``llm.provider`` fixes it, not the profile's harness.  Asking the
        wrong one is exactly the divergence the AI cards used to show.
        """


@runtime_checkable
class EventSchemaLookup(Protocol):
    def get(self, event_type: str) -> Mapping[str, Any] | None: ...


@runtime_checkable
class IdentifierInventory(Protocol):
    """Structural view of ``authoring.IdentifierInventory`` (§5.2).

    Declared as a Protocol rather than imported so this module carries no edge
    to the compiler slice: validation runs on an artifact, with or without the
    Markdown it came from.
    """

    def contains(self, name: str) -> bool: ...

    def refs(self, name: str) -> tuple[SourceRef, ...]: ...


class NullContractLookup:
    """Resolves nothing. Every ``CommandStep`` becomes ``unknown_command``."""

    def get(self, name: str) -> ContractInfo | None:
        return None


class NullProfileLookup:
    def policy(self, profile_id: str) -> Any | None:
        return None

    def routing(self, profile_id: str) -> Any | None:
        return None

    def direct_routing(self, profile_id: str) -> Any | None:
        return None


class RegistryContractLookup:
    """Adapter over Package 1's ``ContractRegistry``.

    Package 1 shipped ``ContractRegistry.get(name) -> CommandRegistration`` with
    the contract at ``registration.contract`` and its execution shape at
    ``.execution`` (``args_model`` / ``result_model`` / ``outcomes``), rather
    than the ``get_contract`` / ``CommandContract.arguments`` names the child
    plan §3.2 assumed.  This class is that reconciliation; nothing else in the
    package sees either shape.
    """

    def __init__(self, registry: Any | None = None) -> None:
        if registry is None:
            from src.commands.contracts.registry import CONTRACTS

            registry = CONTRACTS
        self._registry = registry

    def get(self, name: str) -> ContractInfo | None:
        registration = self._registry.get(name)
        if registration is None:
            return None
        contract = registration.contract
        execution = contract.execution
        arguments = {
            field_name: ArgumentSpec(
                name=field_name,
                type=type_from_annotation(field.annotation),
                required=field.is_required(),
            )
            for field_name, field in execution.args_model.model_fields.items()
        }
        return ContractInfo(
            name=name,
            arguments=arguments,
            result_schema=execution.result_model.model_json_schema(),
            outcomes=frozenset(outcome.name for outcome in execution.outcomes),
            execution_fingerprint=contract.fingerprint(),
            outcome_classes={
                outcome.name: str(outcome.classification.value)
                for outcome in execution.outcomes
            },
        )


class VaultProfileLookup:
    """Adapter over Package 0's ``capability_policy_for``.

    It owns the ``plugin_command_names`` argument so no call site has to
    remember it — forgetting it classifies a legitimate plugin tool into the
    wrong namespace and fires ``tool_use_not_subset`` spuriously (§3.1).

    It also owns ``intelligence_classes``: the profile row alone names a
    class, not a model, so without the snapshot the AI cards can only report
    the class and its provider.  The snapshot is passed in (the daemon hands
    over its live registry) rather than loaded here, because every consumer
    of this lookup is a pure projection.

    ``llm_config`` is the same thing for the direct path: an ``LlmStep``
    resolves against the ``llm:`` config, not against the profile's harness,
    so without it :meth:`direct_routing` reports the class alone rather than
    the harness's provider — which would be a different provider than the
    step calls.
    """

    def __init__(
        self,
        profiles: Mapping[str, Any],
        *,
        plugin_command_names: frozenset[str] = frozenset(),
        intelligence_classes: Mapping[str, Any] | None = None,
        llm_config: Any | None = None,
    ) -> None:
        self._profiles = profiles
        self._plugin_command_names = plugin_command_names
        self._intelligence_classes = intelligence_classes
        self._llm_config = llm_config

    def policy(self, profile_id: str) -> Any | None:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return None
        from src.profiles.capabilities import capability_policy_for

        return capability_policy_for(profile, plugin_command_names=self._plugin_command_names)

    def profile(self, profile_id: str) -> Any | None:
        """The resolved profile row itself, or ``None`` when unknown."""
        return self._profiles.get(profile_id)

    def routing(self, profile_id: str) -> Any | None:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return None
        from src.profiles.intelligence import intelligence_for

        return intelligence_for(profile, self._intelligence_classes)

    def direct_routing(self, profile_id: str) -> Any | None:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return None
        from src.profiles.intelligence import direct_call_intelligence_for

        return direct_call_intelligence_for(
            profile, self._intelligence_classes, self._llm_config
        )


class RegisteredEventLookup:
    """Adapter over ``src.event_schemas``."""

    def get(self, event_type: str) -> Mapping[str, Any] | None:
        from src.event_schemas import get_schema

        return get_schema(event_type)


# --------------------------------------------------------------------------
# §6.5 — the value-typing lattice
# --------------------------------------------------------------------------

TypeKind = Literal[
    "string", "integer", "number", "boolean", "object", "array", "null", "unknown"
]


@dataclass(frozen=True)
class ValueType:
    kind: TypeKind = "unknown"
    item_type: ValueType | None = None
    properties: Mapping[str, ValueType] | None = dataclass_field(default=None)
    nullable: bool = False

    @property
    def is_unknown(self) -> bool:
        return self.kind == "unknown"

    def compatible_with(self, other: ValueType) -> bool:
        """``unknown`` is compatible with everything (and is reported as info)."""
        if self.is_unknown or other.is_unknown:
            return True
        if self.kind == "null":
            return other.nullable or other.kind == "null"
        if self.kind == "integer" and other.kind == "number":
            return True
        return self.kind == other.kind

    def with_nullable(self, nullable: bool) -> ValueType:
        return ValueType(self.kind, self.item_type, self.properties, nullable)


UNKNOWN: Final[ValueType] = ValueType("unknown")
STRING: Final[ValueType] = ValueType("string")
INTEGER: Final[ValueType] = ValueType("integer")
BOOLEAN: Final[ValueType] = ValueType("boolean")
NULL: Final[ValueType] = ValueType("null")

_JSON_TYPE_NAMES: Final[dict[str, TypeKind]] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
    "null": "null",
}


def type_from_schema(schema: Any) -> ValueType:
    """A ``ValueType`` for a JSON-Schema fragment."""
    if not isinstance(schema, Mapping):
        return UNKNOWN
    declared = schema.get("type")
    nullable = False
    if isinstance(declared, list):
        names = [name for name in declared if name != "null"]
        nullable = "null" in declared
        declared = names[0] if names else "null"
    if declared is None:
        for keyword in ("anyOf", "oneOf"):
            options = schema.get(keyword)
            if isinstance(options, list) and options:
                joined = join_types([type_from_schema(option) for option in options])
                return joined.with_nullable(joined.nullable or nullable)
        if isinstance(schema.get("enum"), list):
            values = schema["enum"]
            if values and all(isinstance(item, str) for item in values):
                return ValueType("string", nullable=nullable)
        if isinstance(schema.get("properties"), Mapping):
            declared = "object"
        else:
            return UNKNOWN
    kind = _JSON_TYPE_NAMES.get(str(declared))
    if kind is None:
        return UNKNOWN
    if kind == "array":
        return ValueType("array", item_type=type_from_schema(schema.get("items")), nullable=nullable)
    if kind == "object":
        raw = schema.get("properties")
        properties = (
            {name: type_from_schema(value) for name, value in raw.items()}
            if isinstance(raw, Mapping)
            else None
        )
        return ValueType("object", properties=properties, nullable=nullable)
    return ValueType(kind, nullable=nullable)


def type_from_annotation(annotation: Any) -> ValueType:
    """A ``ValueType`` for a Pydantic field annotation."""
    from types import UnionType
    from typing import Union, get_args, get_origin

    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        nullable = len(args) != len(get_args(annotation))
        if not args:
            return NULL
        joined = join_types([type_from_annotation(arg) for arg in args])
        return joined.with_nullable(joined.nullable or nullable)
    if origin in (list, tuple, set, frozenset):
        args = get_args(annotation)
        return ValueType("array", item_type=type_from_annotation(args[0]) if args else UNKNOWN)
    if origin is dict or annotation is dict:
        return ValueType("object")
    if annotation is bool:
        return BOOLEAN
    if annotation is int:
        return INTEGER
    if annotation is float:
        return ValueType("number")
    if annotation is str:
        return STRING
    if annotation is type(None):
        return NULL
    if isinstance(annotation, type) and hasattr(annotation, "model_json_schema"):
        return type_from_schema(annotation.model_json_schema())
    return UNKNOWN


def join_types(types: Sequence[ValueType]) -> ValueType:
    """The least upper bound of ``types`` in the small lattice."""
    concrete = [item for item in types if not item.is_unknown]
    if not concrete:
        return UNKNOWN
    nullable = any(item.nullable or item.kind == "null" for item in concrete)
    kinds = {item.kind for item in concrete if item.kind != "null"}
    if not kinds:
        return NULL
    if len(kinds) > 1:
        if kinds <= {"integer", "number"}:
            return ValueType("number", nullable=nullable)
        return UNKNOWN
    kind = kinds.pop()
    if kind == "array":
        items = [item.item_type or UNKNOWN for item in concrete if item.kind == "array"]
        return ValueType("array", item_type=join_types(items), nullable=nullable)
    if kind == "object":
        objects = [item for item in concrete if item.kind == "object"]
        properties: dict[str, ValueType] | None = None
        if all(item.properties is not None for item in objects):
            names: set[str] = set()
            for item in objects:
                names |= set(item.properties or {})
            properties = {
                name: join_types([(item.properties or {}).get(name, UNKNOWN) for item in objects])
                for name in names
            }
        return ValueType("object", properties=properties, nullable=nullable)
    return ValueType(kind, nullable=nullable)


def walk_type_path(root: ValueType, path: str | None) -> ValueType:
    """Walk a dotted path through a ``ValueType``."""
    current = root
    if not path:
        return current
    for segment in path.split("."):
        if current.kind == "object" and current.properties is not None:
            current = current.properties.get(segment, UNKNOWN)
        else:
            return UNKNOWN
    return current


def event_field_type(schema: Mapping[str, Any], path: str) -> ValueType | None:
    """Resolve a dotted ``EventRef`` path against an ``EventSchema``.

    Returns ``None`` when the path is not declared at all (``unknown_event_field``)
    and ``UNKNOWN`` when it is declared but carries no type.
    """
    fields = schema.get("fields")
    declared = set(schema.get("required", ())) | set(schema.get("optional", ()))
    segments = path.split(".")
    head = segments[0]
    if not isinstance(fields, Mapping) or head not in fields:
        return None if head not in declared else UNKNOWN
    spec: Any = fields[head]
    for segment in segments[1:]:
        nested = spec.get("fields") if isinstance(spec, Mapping) else None
        if not isinstance(nested, Mapping) or segment not in nested:
            # A declared-but-untyped nested object: the path is not provably
            # wrong, so it degrades to `type_unknown` rather than to an error.
            return UNKNOWN if isinstance(spec, Mapping) and spec.get("type") == "object" else None
        spec = nested[segment]
    if not isinstance(spec, Mapping):
        return UNKNOWN
    kind = _JSON_TYPE_NAMES.get(str(spec.get("type")))
    if kind is None:
        return UNKNOWN
    if kind == "object":
        nested = spec.get("fields")
        properties = (
            {
                name: ValueType(_JSON_TYPE_NAMES.get(str(value.get("type")), "unknown"))
                for name, value in nested.items()
                if isinstance(value, Mapping)
            }
            if isinstance(nested, Mapping)
            else None
        )
        return ValueType("object", properties=properties)
    return ValueType(kind)


def literal_type(value: Any) -> ValueType:
    if value is None:
        return NULL
    if isinstance(value, bool):
        return BOOLEAN
    if isinstance(value, int):
        return INTEGER
    if isinstance(value, float):
        return ValueType("number")
    if isinstance(value, str):
        return STRING
    if isinstance(value, list):
        return ValueType("array", item_type=join_types([literal_type(item) for item in value]))
    if isinstance(value, Mapping):
        return ValueType(
            "object", properties={name: literal_type(item) for name, item in value.items()}
        )
    return UNKNOWN


# --------------------------------------------------------------------------
# Graph helpers
# --------------------------------------------------------------------------


def _owned_steps(definition: PlaybookDefinition, rule_id: str) -> dict[str, Any]:
    return {sid: step for sid, step in definition.steps.items() if step.rule == rule_id}


def _rule_closure(definition: PlaybookDefinition, rule: Rule) -> set[str]:
    """The forward closure of ``rule`` from its entry, over owned steps only.

    A cross-rule edge is recorded by :func:`_structure_and_closure` and is not
    traversed, so one mis-wired edge cannot drag another rule's whole subgraph
    into this rule's closure.
    """
    seen: set[str] = set()
    queue: deque[str] = deque([rule.entry_step])
    while queue:
        step_id = queue.popleft()
        step = definition.steps.get(step_id)
        if step is None or step_id in seen or step.rule != rule.id:
            continue
        seen.add(step_id)
        queue.extend(step_targets(step).values())
    return seen


def _loop_exits(loop: Any) -> set[str]:
    """The loop's own declared exits — ``transitions`` plus ``continuation``."""
    exits = set(loop.transitions.values())
    if loop.continuation is not None:
        exits.add(loop.continuation)
    return exits


def _loop_body(definition: PlaybookDefinition, loop_id: str) -> set[str]:
    """§6.3 — the steps that are *inside* the loop.

    The plan states this as "BFS from ``body_entry``, stopping at ``f`` itself".
    Taken literally that is a plain forward closure, which swallows every target
    a body step has — including the loop's own failure terminal — and makes
    ``loop_body_escapes`` vacuous, because a step's target is in its own closure
    by construction.  The body is therefore the closure restricted to the steps
    that can reach ``f`` again: exactly the nodes on a path from ``body_entry``
    back to the loop node, which is what "inside the loop" means.  Recorded as
    an amendment in the child plan's §20.
    """
    loop = definition.steps[loop_id]
    forward: set[str] = set()
    queue: deque[str] = deque([loop.body_entry])
    while queue:
        step_id = queue.popleft()
        if step_id == loop_id or step_id in forward or step_id not in definition.steps:
            continue
        forward.add(step_id)
        queue.extend(step_targets(definition.steps[step_id]).values())

    inside: set[str] = set()
    changed = True
    while changed:
        changed = False
        for step_id in forward - inside:
            targets = set(step_targets(definition.steps[step_id]).values())
            if loop_id in targets or targets & inside:
                inside.add(step_id)
                changed = True
    return inside


ENTER = "enter"
EXIT = "exit"


def _cfg(definition: PlaybookDefinition, rule: Rule, closure: set[str],
         bodies: Mapping[str, set[str]]) -> tuple[dict[str, list[str]], str]:
    """§6.4 step 2 — the rule's CFG with every ``ForEachStep`` split in two.

    ``f.enter`` gens nothing and leads into the body; ``f.exit`` gens the
    aggregate binding and owns the loop's outgoing edges.  That split is what
    makes the aggregate available *after* the loop and invisible *inside* it.
    """
    successors: dict[str, list[str]] = {}

    def node_in(step_id: str, *, from_step: str | None) -> str:
        step = definition.steps.get(step_id)
        if isinstance(step, ForEachStep):
            inside = from_step is not None and from_step in bodies.get(step_id, set())
            return f"{step_id}#{EXIT}" if inside else f"{step_id}#{ENTER}"
        return step_id

    for step_id in closure:
        step = definition.steps[step_id]
        if isinstance(step, ForEachStep):
            successors[f"{step_id}#{ENTER}"] = [
                node_in(step.body_entry, from_step=step_id)
            ]
            outgoing = list(step.transitions.values())
            if step.continuation is not None:
                outgoing.append(step.continuation)
            successors[f"{step_id}#{EXIT}"] = [
                node_in(target, from_step=step_id) for target in outgoing
            ]
            continue
        successors[step_id] = [
            node_in(target, from_step=step_id) for target in step_targets(step).values()
        ]

    entry = node_in(rule.entry_step, from_step=None)
    for node in list(successors):
        for target in successors[node]:
            successors.setdefault(target, [])
    successors.setdefault(entry, [])
    return successors, entry


def _must_analysis(
    successors: Mapping[str, list[str]],
    entry: str,
    gen: Mapping[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """§6.4 steps 3-5 — the optimistic must-analysis fixpoint.

    ``IN[entry] = ∅``; every other node starts at the universe and shrinks.  The
    lattice is ``2^U`` under ``⊇`` and every transfer is monotone, so this
    converges; ``MAX_FIXPOINT_ITERATIONS`` is a trip-wire, not the termination
    argument.
    """
    universe = frozenset().union(*gen.values()) if gen else frozenset()
    predecessors: dict[str, list[str]] = {node: [] for node in successors}
    for node, targets in successors.items():
        for target in targets:
            predecessors.setdefault(target, []).append(node)

    incoming: dict[str, frozenset[str]] = {
        node: (frozenset() if node == entry else universe) for node in successors
    }
    outgoing: dict[str, frozenset[str]] = {
        node: incoming[node] | gen.get(node, frozenset()) for node in successors
    }
    for _ in range(MAX_FIXPOINT_ITERATIONS):
        changed = False
        for node in successors:
            if node == entry:
                nxt = frozenset()
            else:
                preds = [outgoing[p] for p in predecessors.get(node, []) if p in outgoing]
                nxt = frozenset.intersection(*preds) if preds else frozenset()
            if nxt != incoming[node]:
                incoming[node] = nxt
                changed = True
            out = nxt | gen.get(node, frozenset())
            if out != outgoing[node]:
                outgoing[node] = out
                changed = True
        if not changed:
            return incoming
    raise ValidationBudgetExceeded("definite-assignment fixpoint did not converge")


def _blame_path(
    successors: Mapping[str, list[str]],
    entry: str,
    target: str,
    outgoing: Mapping[str, frozenset[str]],
    name: str,
) -> list[str]:
    """The first entry→``target`` path whose last hop's ``OUT`` lacks ``name``."""
    parents: dict[str, str | None] = {entry: None}
    queue: deque[str] = deque([entry])
    while queue:
        node = queue.popleft()
        for succ in successors.get(node, []):
            if succ in parents:
                continue
            parents[succ] = node
            queue.append(succ)
    for predecessor in successors:
        if target in successors.get(predecessor, []) and name not in outgoing.get(
            predecessor, frozenset()
        ):
            path: list[str] = []
            cursor: str | None = predecessor
            while cursor is not None:
                path.append(cursor)
                cursor = parents.get(cursor)
            return list(reversed(path))
    return []


# --------------------------------------------------------------------------
# The validation context
# --------------------------------------------------------------------------


@dataclass
class _Context:
    definition: PlaybookDefinition
    contracts: ContractLookup
    profiles: ProfileLookup
    events: EventSchemaLookup
    inventory: IdentifierInventory | None
    diagnostics: list[Diagnostic] = dataclass_field(default_factory=list)
    #: rule id -> owned step ids reachable from the entry
    closures: dict[str, set[str]] = dataclass_field(default_factory=dict)
    #: foreach step id -> its body step ids
    bodies: dict[str, set[str]] = dataclass_field(default_factory=dict)
    #: binding name -> producing step id, per rule
    producers: dict[str, dict[str, str]] = dataclass_field(default_factory=dict)
    #: rule id -> CFG node -> the bindings definitely assigned on entry to it
    assigned: dict[str, dict[str, frozenset[str]]] = dataclass_field(default_factory=dict)
    #: rule id -> the typing scope built by ``_values_and_types``
    scopes: dict[str, Any] = dataclass_field(default_factory=dict)

    def emit(
        self,
        code: str,
        message: str,
        *,
        rule_id: str | None = None,
        step_id: str | None = None,
        field: str | None = None,
    ) -> None:
        step = self.definition.steps.get(step_id) if step_id else None
        source = step.source if step is not None else None
        if source is None and rule_id:
            for rule in self.definition.rules:
                if rule.id == rule_id:
                    source = rule.source
                    break
        self.diagnostics.append(
            Diagnostic(
                severity=severity_of(code),
                code=code,
                message=message,
                rule_id=rule_id,
                step_id=step_id,
                field=field,
                source=source,
            )
        )


def validate_definition(
    definition: PlaybookDefinition,
    *,
    inventory: IdentifierInventory | None = None,
    contracts: ContractLookup | None = None,
    profiles: ProfileLookup | None = None,
    events: EventSchemaLookup | None = None,
) -> list[Diagnostic]:
    """Every diagnostic for ``definition``, in pass order.

    Total by construction: no pass raises on invalid input and no pass stops at
    the first error.  A pass that cannot run because an earlier one found the
    graph incoherent is skipped for the affected rule only.
    """
    context = _Context(
        definition=definition,
        contracts=contracts if contracts is not None else NullContractLookup(),
        profiles=profiles if profiles is not None else NullProfileLookup(),
        events=events if events is not None else RegisteredEventLookup(),
        inventory=inventory,
    )
    _structure_and_closure(context)
    _loops(context)
    _definite_assignment(context)
    _values_and_types(context)
    _contracts_and_outcomes(context)
    _profiles_and_capabilities(context)
    _identifiers(context)
    return context.diagnostics


# --------------------------------------------------------------------------
# §6.1 / §6.2 — structure, identifiers, rule ownership and closure
# --------------------------------------------------------------------------


def _structure_and_closure(context: _Context) -> None:
    definition = context.definition
    rule_ids: set[str] = set()
    for rule in definition.rules:
        if rule.id in rule_ids:
            context.emit(
                "duplicate_rule_id",
                f"rule id {rule.id!r} is declared more than once",
                rule_id=rule.id,
            )
            continue
        rule_ids.add(rule.id)

    for step_id, step in sorted(definition.steps.items()):
        if step.rule not in rule_ids:
            context.emit(
                "step_rule_unknown",
                f"step {step_id!r} names rule {step.rule!r}, which is not declared",
                step_id=step_id,
                field="/rule",
            )

    for rule in definition.rules:
        entry = definition.steps.get(rule.entry_step)
        if entry is None:
            context.emit(
                "rule_entry_unknown",
                f"rule {rule.id!r} enters at {rule.entry_step!r}, which is not a step",
                rule_id=rule.id,
                field="/entry_step",
            )
        elif entry.rule != rule.id:
            context.emit(
                "rule_entry_not_owned",
                f"rule {rule.id!r} enters at {rule.entry_step!r}, owned by {entry.rule!r}",
                rule_id=rule.id,
                field="/entry_step",
            )

    for step_id, step in sorted(definition.steps.items()):
        for pointer, target in step_targets(step).items():
            destination = definition.steps.get(target)
            if destination is None:
                context.emit(
                    "unknown_step_target",
                    f"step {step_id!r} points at {target!r}, which is not a step",
                    rule_id=step.rule,
                    step_id=step_id,
                    field=pointer,
                )
            elif destination.rule != step.rule:
                context.emit(
                    "cross_rule_transition",
                    f"step {step_id!r} (rule {step.rule!r}) points at {target!r}, "
                    f"owned by rule {destination.rule!r}; a rule owns a closed subgraph",
                    rule_id=step.rule,
                    step_id=step_id,
                    field=pointer,
                )

    for rule in definition.rules:
        if rule.id in context.closures:
            continue
        entry = definition.steps.get(rule.entry_step)
        context.closures[rule.id] = (
            _rule_closure(definition, rule) if entry is not None and entry.rule == rule.id else set()
        )

    covered: set[str] = set()
    for reachable in context.closures.values():
        covered |= reachable
    for step_id, step in sorted(definition.steps.items()):
        if step_id in covered:
            continue
        if step.rule in rule_ids:
            context.emit(
                "unreachable_step",
                f"step {step_id!r} is owned by rule {step.rule!r} but is not reachable "
                f"from its entry step",
                rule_id=step.rule,
                step_id=step_id,
            )
        else:
            context.emit(
                "orphan_step",
                f"step {step_id!r} belongs to no rule's subgraph",
                step_id=step_id,
            )

    for rule in definition.rules:
        _terminal_paths(context, rule)


def _terminal_paths(context: _Context, rule: Rule) -> None:
    """§6.2 — reverse BFS from the rule's terminals.

    V1 (``pipeline_compiler._reaches_terminal``) warns here; V2 errors, because
    a step from which no terminal is reachable is a run that cannot end.
    """
    definition = context.definition
    closure = context.closures.get(rule.id, set())
    if not closure:
        return
    reverse: dict[str, list[str]] = {step_id: [] for step_id in closure}
    for step_id in closure:
        for target in step_targets(definition.steps[step_id]).values():
            if target in closure:
                reverse[target].append(step_id)
    grounded: set[str] = set()
    queue: deque[str] = deque(
        step_id for step_id in closure if isinstance(definition.steps[step_id], TerminalStep)
    )
    grounded.update(queue)
    while queue:
        step_id = queue.popleft()
        for predecessor in reverse[step_id]:
            if predecessor not in grounded:
                grounded.add(predecessor)
                queue.append(predecessor)
    for step_id in sorted(closure - grounded):
        context.emit(
            "no_terminal_path",
            f"no terminal step is reachable from {step_id!r}",
            rule_id=rule.id,
            step_id=step_id,
        )


# --------------------------------------------------------------------------
# §6.3 — loops
# --------------------------------------------------------------------------


def _loops(context: _Context) -> None:
    definition = context.definition
    loops = {
        step_id: step
        for step_id, step in definition.steps.items()
        if isinstance(step, ForEachStep)
    }
    for loop_id in loops:
        context.bodies[loop_id] = _loop_body(definition, loop_id)

    for loop_id, loop in sorted(loops.items()):
        body = context.bodies[loop_id]
        enclosing = [other for other, other_body in context.bodies.items()
                     if other != loop_id and loop_id in other_body]
        if enclosing:
            context.emit(
                "nested_loop",
                f"loop {loop_id!r} sits inside the body of {min(enclosing)!r}; "
                f"V2 rejects nested loops",
                rule_id=loop.rule,
                step_id=loop_id,
            )

        exits = _loop_exits(loop)
        for body_step_id in sorted(body):
            body_step = definition.steps[body_step_id]
            for pointer, target in step_targets(body_step).items():
                # Legal: another body step, the loop node (next iteration), or one
                # of the loop's own declared exits — "the loop's only exits are
                # its own transitions", read as a constraint on the destination.
                if target in body or target == loop_id or target in exits:
                    continue
                if target not in definition.steps:
                    continue  # already reported as unknown_step_target
                context.emit(
                    "loop_body_escapes",
                    f"step {body_step_id!r} leaves the body of {loop_id!r} for {target!r}; "
                    f"a loop's only exits are its own transitions",
                    rule_id=body_step.rule,
                    step_id=body_step_id,
                    field=pointer,
                )

        completed = loop.transitions.get("completed")
        if loop.continuation is not None and completed is not None and loop.continuation != completed:
            context.emit(
                "continuation_mismatch",
                f"loop {loop_id!r} continues to {loop.continuation!r} but transitions "
                f"'completed' to {completed!r}",
                rule_id=loop.rule,
                step_id=loop_id,
                field="/continuation",
            )

        rule_bindings = {
            step.save_result_as
            for step in definition.steps.values()
            if step.rule == loop.rule and getattr(step, "save_result_as", None)
        }
        enclosing_items = {
            definition.steps[other].item_binding
            for other in enclosing
            if isinstance(definition.steps.get(other), ForEachStep)
        }
        if (
            loop.item_binding in rule_bindings
            or loop.item_binding in enclosing_items
            or loop.item_binding in RESERVED_BINDING_ROOTS
        ):
            context.emit(
                "loop_variable_shadow",
                f"loop variable {loop.item_binding!r} shadows an existing binding, an "
                f"enclosing loop variable or a reserved namespace root",
                rule_id=loop.rule,
                step_id=loop_id,
                field="/item_binding",
            )

    by_item: dict[str, list[str]] = {}
    for loop_id, loop in loops.items():
        by_item.setdefault(loop.item_binding, []).append(loop_id)
    for step_id, step in sorted(definition.steps.items()):
        for value in step_values(step):
            if not isinstance(value, LoopRef):
                continue
            owners = by_item.get(value.binding, [])
            inside = any(
                step_id in context.bodies.get(owner, set())
                for owner in owners
                if definition.steps[owner].rule == step.rule
            )
            if not inside:
                context.emit(
                    "loop_ref_outside_loop",
                    f"step {step_id!r} reads loop variable {value.binding!r} outside the "
                    f"body of its loop",
                    rule_id=step.rule,
                    step_id=step_id,
                )


# --------------------------------------------------------------------------
# §6.4 — definite assignment
# --------------------------------------------------------------------------


def _definite_assignment(context: _Context) -> None:
    definition = context.definition
    for rule in definition.rules:
        closure = context.closures.get(rule.id, set())
        if not closure:
            continue

        producers: dict[str, str] = {}
        for step_id in sorted(closure):
            name = getattr(definition.steps[step_id], "save_result_as", None)
            if not name:
                continue
            if name in producers:
                context.emit(
                    "duplicate_binding",
                    f"binding {name!r} is assigned by both {producers[name]!r} and "
                    f"{step_id!r}; within a rule a binding has exactly one assigner",
                    rule_id=rule.id,
                    step_id=step_id,
                    field="/save_result_as",
                )
                continue
            producers[name] = step_id
        context.producers[rule.id] = producers

        for step_id in sorted(closure):
            name = getattr(definition.steps[step_id], "save_result_as", None)
            if not name:
                continue
            enclosing = {
                definition.steps[loop_id].item_binding
                for loop_id, body in context.bodies.items()
                if step_id in body
            }
            if name in enclosing:
                context.emit(
                    "binding_reassigned",
                    f"binding {name!r} collides with the enclosing loop variable of the "
                    f"same name; bindings are immutable and loop variables are scoped",
                    rule_id=rule.id,
                    step_id=step_id,
                    field="/save_result_as",
                )

        successors, entry = _cfg(definition, rule, closure, context.bodies)
        gen: dict[str, frozenset[str]] = {}
        for step_id in closure:
            step = definition.steps[step_id]
            name = getattr(step, "save_result_as", None)
            produced = frozenset({name}) if name and producers.get(name) == step_id else frozenset()
            if isinstance(step, ForEachStep):
                gen[f"{step_id}#{ENTER}"] = frozenset()
                gen[f"{step_id}#{EXIT}"] = produced
            else:
                gen[step_id] = produced
        incoming = _must_analysis(successors, entry, gen)
        outgoing = {
            node: incoming.get(node, frozenset()) | gen.get(node, frozenset())
            for node in successors
        }
        context.assigned[rule.id] = incoming

        flow = _Flow(successors=successors, entry=entry, incoming=incoming, outgoing=outgoing)
        if rule.guard is not None:
            _check_binding_reads(context, flow, condition_values(rule.guard), entry, None, rule.id)
        for step_id in sorted(closure):
            step = definition.steps[step_id]
            node = f"{step_id}#{ENTER}" if isinstance(step, ForEachStep) else step_id
            _check_binding_reads(context, flow, step_values(step), node, step_id, rule.id)


@dataclass(frozen=True)
class _Flow:
    """One rule's CFG plus the solved must-analysis, passed around explicitly."""

    successors: Mapping[str, list[str]]
    entry: str
    incoming: Mapping[str, frozenset[str]]
    outgoing: Mapping[str, frozenset[str]]


def _check_binding_reads(
    context: _Context,
    flow: _Flow,
    values: Iterable[Any],
    node: str,
    step_id: str | None,
    rule_id: str,
) -> None:
    """§6.4 step 6 — a read is legal iff the binding is in ``IN[node]``."""
    available = flow.incoming.get(node, frozenset())
    for value in values:
        if not isinstance(value, BindingRef) or value.binding in available:
            continue
        path = _blame_path(flow.successors, flow.entry, node, flow.outgoing, value.binding)
        where = " -> ".join(path) if path else "the rule entry"
        context.emit(
            "binding_not_definitely_assigned",
            f"binding {value.binding!r} is read here but is not assigned on every "
            f"path that reaches it; the path {where} does not assign it",
            rule_id=rule_id,
            step_id=step_id,
        )


# --------------------------------------------------------------------------
# §6.5 — value typing
# --------------------------------------------------------------------------


@dataclass
class _TypeScope:
    """Everything the typing lattice needs about one rule."""

    rule: Rule
    event_schema: Mapping[str, Any] | None
    bindings: Mapping[str, ValueType]
    loop_items: Mapping[str, ValueType]


def _command_result_schema(context: _Context, step: Any) -> Mapping[str, Any] | None:
    if not isinstance(step, CommandStep):
        return None
    info = context.contracts.get(step.command)
    return info.result_schema if info is not None else None


def _build_scope(context: _Context, rule: Rule) -> _TypeScope:
    definition = context.definition
    event_schema = context.events.get(rule.trigger.event_type)
    bindings: dict[str, ValueType] = {}
    for name, step_id in context.producers.get(rule.id, {}).items():
        step = definition.steps[step_id]
        schema = result_schema_for(step, command_schema=_command_result_schema(context, step))
        bindings[name] = type_from_schema(schema) if schema is not None else UNKNOWN
    scope = _TypeScope(rule=rule, event_schema=event_schema, bindings=bindings, loop_items={})
    loop_items: dict[str, ValueType] = {}
    for step_id in context.closures.get(rule.id, set()):
        step = definition.steps[step_id]
        if not isinstance(step, ForEachStep):
            continue
        collection = _value_type(context, scope, step.collection)
        loop_items[step.item_binding] = (
            collection.item_type or UNKNOWN if collection.kind == "array" else UNKNOWN
        )
    return _TypeScope(
        rule=rule, event_schema=event_schema, bindings=bindings, loop_items=loop_items
    )


def _value_type(context: _Context, scope: _TypeScope, value: Any) -> ValueType:
    """The static type of one value node (§6.5's table)."""
    if isinstance(value, LiteralValue):
        return literal_type(value.value)
    if isinstance(value, TemplateValue):
        return STRING
    if isinstance(value, EventRef):
        if scope.event_schema is None:
            return UNKNOWN
        resolved = event_field_type(scope.event_schema, value.path)
        return resolved if resolved is not None else UNKNOWN
    if isinstance(value, ContextRef):
        kind = ENGINE_CONTEXT_SCHEMA.get(value.path)
        return ValueType(_JSON_TYPE_NAMES[kind]) if kind else UNKNOWN
    if isinstance(value, BindingRef):
        return walk_type_path(scope.bindings.get(value.binding, UNKNOWN), value.path)
    if isinstance(value, LoopRef):
        if value.index:
            return INTEGER
        return walk_type_path(scope.loop_items.get(value.binding, UNKNOWN), value.path)
    if isinstance(value, ListValue):
        return ValueType(
            "array",
            item_type=join_types([_value_type(context, scope, item) for item in value.items]),
        )
    if isinstance(value, ObjectValue):
        return ValueType(
            "object",
            properties={
                name: _value_type(context, scope, item) for name, item in value.fields.items()
            },
        )
    if isinstance(value, CoalesceValue):
        options = [_value_type(context, scope, option) for option in value.options]
        joined = join_types(options)
        if _is_total(value.options[-1], options[-1]):
            return joined.with_nullable(False)
        return joined.with_nullable(True)
    return UNKNOWN


def _is_total(value: Any, value_type: ValueType) -> bool:
    """Can ``value`` never render null? (§6.5's coalesce totality rule.)"""
    if isinstance(value, LiteralValue):
        return value.value is not None
    if isinstance(value, (TemplateValue, ListValue, ObjectValue)):
        return True
    if value_type.is_unknown:
        return False
    return not value_type.nullable and value_type.kind != "null"


def _values_and_types(context: _Context) -> None:
    definition = context.definition
    scopes: dict[str, _TypeScope] = {}
    for rule in definition.rules:
        if rule.id in scopes:
            continue
        scope = _build_scope(context, rule)
        scopes[rule.id] = scope
        if scope.event_schema is None:
            context.emit(
                "unknown_event",
                f"rule {rule.id!r} triggers on {rule.trigger.event_type!r}, which is not a "
                f"registered event type",
                rule_id=rule.id,
                field="/trigger/event_type",
            )
        elif rule.trigger.filter:
            for key in sorted(rule.trigger.filter):
                if event_field_type(scope.event_schema, key) is None:
                    context.emit(
                        "unknown_event_field",
                        f"trigger filter key {key!r} is not a field of "
                        f"{rule.trigger.event_type!r}",
                        rule_id=rule.id,
                        field=f"/trigger/filter/{key}",
                    )

    for step_id, step in sorted(definition.steps.items()):
        scope = scopes.get(step.rule)
        if scope is None:
            continue
        in_loop = any(step_id in body for body in context.bodies.values())
        for value in step_values(step):
            if isinstance(value, EventRef) and scope.event_schema is not None:
                if event_field_type(scope.event_schema, value.path) is None:
                    context.emit(
                        "unknown_event_field",
                        f"{value.path!r} is not a field of {scope.rule.trigger.event_type!r}",
                        rule_id=step.rule,
                        step_id=step_id,
                    )
            elif isinstance(value, ContextRef):
                if value.path not in ENGINE_CONTEXT_SCHEMA:
                    context.emit(
                        "unknown_context_path",
                        f"{value.path!r} is not part of the engine context",
                        rule_id=step.rule,
                        step_id=step_id,
                    )
                elif value.path in LOOP_ONLY_CONTEXT_PATHS and not in_loop:
                    context.emit(
                        "unknown_context_path",
                        f"{value.path!r} exists only inside a loop body",
                        rule_id=step.rule,
                        step_id=step_id,
                    )
            elif isinstance(value, CoalesceValue):
                last = value.options[-1]
                last_type = _value_type(context, scope, last)
                if last_type.is_unknown and not isinstance(
                    last, (LiteralValue, TemplateValue, ListValue, ObjectValue)
                ):
                    context.emit(
                        "type_unknown",
                        "the last coalesce option has no statically known type, so its "
                        "totality could not be checked",
                        rule_id=step.rule,
                        step_id=step_id,
                    )
                elif not _is_total(last, last_type):
                    context.emit(
                        "coalesce_not_total",
                        "the last coalesce option can still be null, so this chain does "
                        "not make the value total",
                        rule_id=step.rule,
                        step_id=step_id,
                    )
    context.scopes.update(scopes)


# --------------------------------------------------------------------------
# §6.6 / §10.3 — contracts, arguments, outcomes and bounded output schemas
# --------------------------------------------------------------------------


def _schema_shape(schema: Any) -> tuple[int, int, set[str]]:
    """``(depth, property count, forbidden keywords)`` for a JSON Schema."""
    forbidden: set[str] = set()
    properties = 0
    depth = 0

    def walk(node: Any, level: int) -> None:
        nonlocal properties, depth
        depth = max(depth, level)
        if isinstance(node, Mapping):
            forbidden.update(FORBIDDEN_SCHEMA_KEYWORDS & set(node))
            declared = node.get("properties")
            if isinstance(declared, Mapping):
                properties += len(declared)
                for value in declared.values():
                    walk(value, level + 1)
            for key in ("items", "additionalProperties", "contains", "propertyNames"):
                if key in node:
                    walk(node[key], level + 1)
            for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
                for value in node.get(key, []) if isinstance(node.get(key), list) else []:
                    walk(value, level + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, level)

    walk(schema, 1)
    return depth, properties, forbidden


def _check_output_schema(context: _Context, step_id: str, step: LlmStep) -> None:
    """§10.3 — author data reaching a provider's structured-output API."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(step.output_schema)
    except SchemaError as exc:
        context.emit(
            "output_schema_invalid",
            f"output_schema is not a valid draft 2020-12 schema: {exc.message}",
            rule_id=step.rule,
            step_id=step_id,
            field="/output_schema",
        )
        return
    depth, properties, forbidden = _schema_shape(step.output_schema)
    problems: list[str] = []
    if depth > MAX_OUTPUT_SCHEMA_DEPTH:
        problems.append(f"nesting depth {depth} exceeds {MAX_OUTPUT_SCHEMA_DEPTH}")
    if properties > MAX_OUTPUT_SCHEMA_PROPERTIES:
        problems.append(f"{properties} properties exceed {MAX_OUTPUT_SCHEMA_PROPERTIES}")
    if forbidden:
        problems.append(
            f"uses {', '.join(sorted(forbidden))}; the artifact must be self-contained"
        )
    if problems:
        context.emit(
            "output_schema_too_deep",
            "output_schema is out of bounds: " + "; ".join(problems),
            rule_id=step.rule,
            step_id=step_id,
            field="/output_schema",
        )


def _outcome_enum_of(step: LlmStep) -> list[str] | None:
    from src.playbooks.definition import _outcome_enum

    return _outcome_enum(step)


def _contracts_and_outcomes(context: _Context) -> None:
    definition = context.definition
    for step_id, step in sorted(definition.steps.items()):
        scope = context.scopes.get(step.rule)
        info: ContractInfo | None = None
        resolved = True

        if isinstance(step, CommandStep):
            info = context.contracts.get(step.command)
            if info is None:
                resolved = False
                context.emit(
                    "unknown_command",
                    f"{step.command!r} is not a registered command contract",
                    rule_id=step.rule,
                    step_id=step_id,
                    field="/command",
                )
            else:
                _check_command_arguments(context, step_id, step, info, scope)
                recorded = definition.compiled_against.commands.get(step.command)
                if recorded != info.execution_fingerprint:
                    context.emit(
                        "stale_contract",
                        f"the artifact was compiled against {recorded or 'no'} fingerprint "
                        f"for {step.command!r}; the registry now serves "
                        f"{info.execution_fingerprint}",
                        rule_id=step.rule,
                        step_id=step_id,
                        field="/command",
                    )

        if isinstance(step, LlmStep):
            _check_output_schema(context, step_id, step)
            _check_llm_outcomes(context, step_id, step)
            for tool_name in (*step.tool_use.aq_commands, *step.tool_use.plugin_tools):
                tool_contract = context.contracts.get(tool_name)
                if tool_contract is None:
                    context.emit(
                        "unknown_command",
                        f"LLM tool {tool_name!r} is not a registered command contract",
                        rule_id=step.rule,
                        step_id=step_id,
                        field="/tool_use",
                    )
                    continue
                recorded = definition.compiled_against.commands.get(tool_name)
                if recorded != tool_contract.execution_fingerprint:
                    context.emit(
                        "stale_contract",
                        f"the artifact was compiled against {recorded or 'no'} fingerprint "
                        f"for LLM tool {tool_name!r}; the registry now serves "
                        f"{tool_contract.execution_fingerprint}",
                        rule_id=step.rule,
                        step_id=step_id,
                        field="/tool_use",
                    )

        transitions = getattr(step, "transitions", None)
        if transitions is None or not resolved:
            continue
        business = business_outcomes(
            step, contract_outcomes=info.outcomes if info is not None else None
        )
        reserved = reserved_outcomes_for(step)
        keys = set(transitions)
        allowed = business | reserved | {RUNTIME_ERROR_KEY}
        for key in sorted(keys - allowed):
            context.emit(
                "unknown_transition_outcome",
                f"{key!r} is neither a business outcome of this step nor a reserved one",
                rule_id=step.rule,
                step_id=step_id,
                field=f"/transitions/{key}",
            )
        for outcome in sorted(business - keys):
            context.emit(
                "unmapped_business_outcome",
                f"business outcome {outcome!r} has no transition; every executable "
                f"transition must be displayed",
                rule_id=step.rule,
                step_id=step_id,
                field="/transitions",
            )
        if RUNTIME_ERROR_KEY not in keys:
            for outcome in sorted(reserved - keys):
                context.emit(
                    "unmapped_reserved_outcome",
                    f"reserved outcome {outcome!r} is neither mapped nor covered by a "
                    f"{RUNTIME_ERROR_KEY!r} transition",
                    rule_id=step.rule,
                    step_id=step_id,
                    field="/transitions",
                )


def _check_command_arguments(
    context: _Context,
    step_id: str,
    step: CommandStep,
    info: ContractInfo,
    scope: Any,
) -> None:
    for name in sorted(set(step.inputs) - set(info.arguments)):
        context.emit(
            "argument_unknown",
            f"{name!r} is not an argument of {step.command!r}",
            rule_id=step.rule,
            step_id=step_id,
            field=f"/inputs/{name}",
        )
    for name, spec in sorted(info.arguments.items()):
        if spec.required and name not in step.inputs:
            context.emit(
                "argument_missing",
                f"required argument {name!r} of {step.command!r} has no input",
                rule_id=step.rule,
                step_id=step_id,
                field="/inputs",
            )
    if scope is None:
        return
    for name, value in sorted(step.inputs.items()):
        spec = info.arguments.get(name)
        if spec is None:
            continue
        actual = _value_type(context, scope, value)
        if actual.is_unknown or spec.type.is_unknown:
            context.emit(
                "type_unknown",
                f"the type of input {name!r} could not be determined statically, so the "
                f"check against {spec.type.kind} was silenced",
                rule_id=step.rule,
                step_id=step_id,
                field=f"/inputs/{name}",
            )
        elif not actual.compatible_with(spec.type):
            context.emit(
                "type_mismatch",
                f"input {name!r} is {actual.kind} but {step.command!r} declares "
                f"{spec.type.kind}",
                rule_id=step.rule,
                step_id=step_id,
                field=f"/inputs/{name}",
            )


def _check_llm_outcomes(context: _Context, step_id: str, step: LlmStep) -> None:
    """§6.6 — the check that forbids hidden natural-language AI transitions."""
    branching = set(step.transitions) - RESERVED_OUTCOMES - LLM_RESERVED_OUTCOMES
    branching.discard(RUNTIME_ERROR_KEY)
    if not step.outcome_field:
        branching.discard("completed")
    if not branching:
        return
    enum = _outcome_enum_of(step)
    if not step.outcome_field or enum is None:
        context.emit(
            "llm_branch_without_schema",
            f"this step branches on {sorted(branching)} but declares no structured "
            f"outcome_field enum; AI branching must use declared structured output",
            rule_id=step.rule,
            step_id=step_id,
            field="/outcome_field",
        )
        return
    if set(enum) != branching:
        context.emit(
            "outcome_enum_mismatch",
            f"outcome_field {step.outcome_field!r} declares {sorted(set(enum))} but the "
            f"transitions branch on {sorted(branching)}",
            rule_id=step.rule,
            step_id=step_id,
            field="/outcome_field",
        )
    required = step.output_schema.get("required")
    if not isinstance(required, list) or step.outcome_field not in required:
        context.emit(
            "outcome_enum_mismatch",
            f"outcome_field {step.outcome_field!r} is not in output_schema.required, so "
            f"the branch value is not guaranteed to be present",
            rule_id=step.rule,
            step_id=step_id,
            field="/output_schema/required",
        )


# --------------------------------------------------------------------------
# §6.7 — profiles and capabilities
# --------------------------------------------------------------------------


def _profile_snapshots(context: _Context) -> None:
    """The profile half of the ``stale_contract`` check ``_contracts_and_outcomes`` makes.

    ``compiled_against.profiles`` is server-computed provenance, so the useful
    question is not whether the artifact is self-consistent but whether it
    still agrees with the registry this process serves.  A step's *delegated*
    profile counts here exactly as its own does: ``ensure_task`` handing work
    to ``reviewer`` depends on that profile's capabilities as surely as an
    ``LlmStep`` running as it.

    Only a profile the lookup resolves is compared.  An unresolvable one is
    ``unknown_profile``'s business where the step declares it, and a
    ``NullProfileLookup`` — what a caller passes when it has no registry to
    resolve against — must not turn every artifact into a stale one.
    """
    definition = context.definition
    reported: set[str] = set()
    for step_id, step in sorted(definition.steps.items()):
        for profile_id in step_profile_ids(step):
            if profile_id in reported:
                continue
            policy = context.profiles.policy(profile_id)
            if policy is None:
                continue
            recorded = definition.compiled_against.profiles.get(profile_id)
            current = policy.fingerprint()
            if recorded == current:
                continue
            reported.add(profile_id)
            context.emit(
                "stale_contract",
                f"the artifact was compiled against {recorded or 'no'} fingerprint "
                f"for profile {profile_id!r}; the registry now serves {current}",
                rule_id=step.rule,
                step_id=step_id,
                field="/compiled_against/profiles",
            )


def _profiles_and_capabilities(context: _Context) -> None:
    definition = context.definition
    policies: dict[str, Any] = {}

    _profile_snapshots(context)

    for step_id, step in sorted(definition.steps.items()):
        if not isinstance(step, (LlmStep, AgentTaskStep)):
            continue
        policy = context.profiles.policy(step.profile_id)
        if policy is None:
            context.emit(
                "unknown_profile",
                f"{step.profile_id!r} is not a known profile",
                rule_id=step.rule,
                step_id=step_id,
                field="/profile_id",
            )
            continue
        policies[step.profile_id] = policy
        if _policy_is_empty(policy):
            context.emit(
                "profile_capability_empty",
                f"profile {step.profile_id!r} grants no capabilities in any namespace; an "
                f"AI step with a deny-all policy is legal but rarely intended",
                rule_id=step.rule,
                step_id=step_id,
                field="/profile_id",
            )
        if isinstance(step, AgentTaskStep) and step.capability_narrowing is not None:
            _check_narrowing(context, step_id, step, policy)
        if isinstance(step, LlmStep) and (
            step.tool_use.aq_commands or step.tool_use.plugin_tools
        ):
            requested = _policy_like(
                policy, aq_commands=step.tool_use.aq_commands,
                plugin_tools=step.tool_use.plugin_tools,
            )
            if requested is not None and not requested.is_subset_of(policy):
                context.emit(
                    "tool_use_not_subset",
                    f"tool_use asks for capabilities profile {step.profile_id!r} does not "
                    f"grant",
                    rule_id=step.rule,
                    step_id=step_id,
                    field="/tool_use",
                )

    for rule in definition.rules:
        closure = context.closures.get(rule.id, set())
        agent_tasks = [
            step_id
            for step_id in sorted(closure)
            if isinstance(definition.steps[step_id], AgentTaskStep)
        ]
        if not agent_tasks:
            continue
        successors, entry = _cfg(definition, rule, closure, context.bodies)
        gen: dict[str, frozenset[str]] = {}
        for step_id in closure:
            step = definition.steps[step_id]
            produced = (
                frozenset({step.profile_id})
                if isinstance(step, LlmStep) and step.tool_use.enabled
                else frozenset()
            )
            if isinstance(step, ForEachStep):
                gen[f"{step_id}#{ENTER}"] = frozenset()
                gen[f"{step_id}#{EXIT}"] = frozenset()
            else:
                gen[step_id] = produced
        must = _must_analysis(successors, entry, gen)
        for step_id in agent_tasks:
            step = definition.steps[step_id]
            upstream = sorted(must.get(step_id, frozenset()))
            child = policies.get(step.profile_id)
            if not upstream:
                context.emit(
                    "delegation_runtime_checked",
                    f"no tool-using AI step precedes {step_id!r} on every path, so the "
                    f"delegation narrowing is enforced at run time by check_delegation",
                    rule_id=rule.id,
                    step_id=step_id,
                    field="/profile_id",
                )
                continue
            available = [policies[name] for name in upstream if name in policies]
            if child is None or len(available) != len(upstream):
                continue  # unknown_profile already reported
            ceiling = available[0]
            for policy in available[1:]:
                ceiling = ceiling.intersect(policy)
            if not child.is_subset_of(ceiling):
                context.emit(
                    "capability_not_subset",
                    f"agent task {step_id!r} delegates to profile {step.profile_id!r}, "
                    f"whose capabilities exceed the intersection of the AI context "
                    f"({', '.join(upstream)}) that reaches it",
                    rule_id=rule.id,
                    step_id=step_id,
                    field="/profile_id",
                )


def _check_narrowing(
    context: _Context, step_id: str, step: AgentTaskStep, policy: Any
) -> None:
    """§6.7 — an explicit per-step narrowing may only name capabilities the
    child profile actually grants.

    The executor intersects (``src/playbooks/executors/agent_task.py``), so a
    name the child profile does not hold narrows nothing: the run behaves
    exactly as if the author had not written it.  That is the silent no-op
    this diagnostic exists to prevent — an author who narrows a delegated
    agent task to ``task_clos`` believes they restricted it, and a reviewer
    reading the card sees a restriction that never applied.

    ``None`` in a namespace is "narrows nothing here" and is skipped; an
    explicitly empty list is "none", which is always a subset.
    """
    narrowing = step.capability_narrowing
    if narrowing is None:
        return
    for namespace in ("harness_tools", "aq_commands", "plugin_tools"):
        declared = getattr(narrowing, namespace, None)
        if declared is None:
            continue
        granted = getattr(policy, namespace, None)
        if not isinstance(granted, (frozenset, set)):
            continue  # a lookup that does not expose namespaces; nothing to check
        unknown = sorted(name for name in declared if name not in granted)
        if not unknown:
            continue
        context.emit(
            "narrowing_not_subset",
            f"capability_narrowing.{namespace} names "
            f"{', '.join(repr(name) for name in unknown)}, which profile "
            f"{step.profile_id!r} does not grant; a narrowing intersects, so this "
            f"restricts nothing",
            rule_id=step.rule,
            step_id=step_id,
            field=f"/capability_narrowing/{namespace}",
        )


def _policy_is_empty(policy: Any) -> bool:
    """``CapabilityPolicy.is_empty`` — a method in the shipped Package 0 module,
    a property in that package's plan.  Tolerate both rather than pin one."""
    empty = getattr(policy, "is_empty", None)
    return bool(empty() if callable(empty) else empty)


def _policy_like(policy: Any, *, aq_commands: Sequence[str], plugin_tools: Sequence[str]) -> Any:
    """A ``CapabilityPolicy`` holding exactly the requested tool-use names."""
    builder = getattr(type(policy), "from_namespaces", None)
    if builder is None:
        return None
    return builder(aq_commands=frozenset(aq_commands), plugin_tools=frozenset(plugin_tools))


# --------------------------------------------------------------------------
# §5.3 — the identifier inventory check
# --------------------------------------------------------------------------


def _inventory_names(definition: PlaybookDefinition) -> list[tuple[str, str | None, str | None]]:
    """``(name, rule id, step id)`` for every identifier §5.3 requires in source.

    Step ids, rule ids, terminal outcomes, ``ContextRef`` paths and the reserved
    outcome vocabulary are deliberately absent: they are artifact-local or
    engine-owned, and requiring them in the prose would make the compiler
    transcribe its own bookkeeping.
    """
    names: list[tuple[str, str | None, str | None]] = []
    for rule in definition.rules:
        names.append((rule.trigger.event_type, rule.id, None))
        for key in rule.trigger.filter or {}:
            names.append((key, rule.id, None))
    for step_id, step in sorted(definition.steps.items()):
        if isinstance(step, CommandStep):
            names.append((step.command, step.rule, step_id))
            names.extend((key, step.rule, step_id) for key in step.inputs)
        if isinstance(step, (LlmStep, AgentTaskStep)):
            names.append((step.profile_id, step.rule, step_id))
        if isinstance(step, AgentTaskStep) and step.capability_narrowing is not None:
            # §5.3 applied to the third intersection term: a per-step narrowing
            # is an authored restriction, so every capability it names has to
            # come from the prose.  A compiler that invents one silently changes
            # what a delegated child may do, in either direction.
            for namespace in ("harness_tools", "aq_commands", "plugin_tools"):
                declared = getattr(step.capability_narrowing, namespace, None) or ()
                names.extend((name, step.rule, step_id) for name in declared)
        if isinstance(step, LlmStep) and step.outcome_field:
            names.append((step.outcome_field, step.rule, step_id))
            from src.playbooks.definition import _outcome_enum

            names.extend((value, step.rule, step_id) for value in _outcome_enum(step) or ())
        if isinstance(step, WaitStep):
            names.extend((value, step.rule, step_id) for value in step.outcomes)
            if (
                step.wait_kind == "event"
                and isinstance(step.awaited, LiteralValue)
                and isinstance(step.awaited.value, str)
            ):
                names.append((step.awaited.value, step.rule, step_id))
        if isinstance(step, ForEachStep):
            names.append((step.item_binding, step.rule, step_id))
        binding = getattr(step, "save_result_as", None)
        if binding:
            names.append((binding, step.rule, step_id))
        for value in step_values(step):
            if isinstance(value, (BindingRef, LoopRef)):
                names.append((value.binding, step.rule, step_id))
            elif isinstance(value, EventRef):
                names.append((value.path, step.rule, step_id))
    return names


def _identifiers(context: _Context) -> None:
    """§5.3 — the compiler may only wire together names a human wrote."""
    inventory = context.inventory
    if inventory is None:
        return
    reported: set[tuple[str, str | None]] = set()
    for name, rule_id, step_id in _inventory_names(context.definition):
        if (name, step_id) in reported or inventory.contains(name):
            continue
        # A dotted path is satisfied by any dotted prefix being present (§5.2).
        if "." in name and any(
            inventory.contains(name.rsplit(".", index)[0])
            for index in range(1, name.count(".") + 1)
        ):
            continue
        reported.add((name, step_id))
        context.emit(
            "unknown_identifier",
            f"{name!r} appears nowhere in the authoring source; the compiler may not "
            f"invent an executable name",
            rule_id=rule_id,
            step_id=step_id,
        )


__all__ = [
    "COMPILER_ONLY_CODES",
    "DIAGNOSTIC_CODES",
    "DIAGNOSTIC_SEVERITY",
    "FORBIDDEN_SCHEMA_KEYWORDS",
    "MAX_FIXPOINT_ITERATIONS",
    "MAX_OUTPUT_SCHEMA_DEPTH",
    "MAX_OUTPUT_SCHEMA_PROPERTIES",
    "MODEL_CODES",
    "RESERVED_BINDING_ROOTS",
    "VALIDATOR_CODES",
    "ArgumentSpec",
    "ContractInfo",
    "ContractLookup",
    "Diagnostic",
    "EventSchemaLookup",
    "IdentifierInventory",
    "NullContractLookup",
    "NullProfileLookup",
    "ProfileLookup",
    "RegisteredEventLookup",
    "RegistryContractLookup",
    "Severity",
    "ValidationBudgetExceeded",
    "ValueType",
    "VaultProfileLookup",
    "event_field_type",
    "join_types",
    "literal_type",
    "severity_of",
    "type_from_annotation",
    "type_from_schema",
    "validate_definition",
    "walk_type_path",
]
