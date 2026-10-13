"""Typed value and condition expressions for the Playbook V2 artifact.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md``
§4.1 (the strict base), §4.2 (typed values), §4.2.1 (the closed
engine context schema) and §4.3 (conditions).

V1 interpolates opaque ``"{{outputs.x.y}}"`` strings and evaluates a permissive
mini-language that defaults to *true*.  Nothing here is opaque: every value is a
discriminated model, every condition shape that is not one of the three declared
kinds fails Pydantic discrimination rather than falling through to a default.

**Import discipline.** This module may import only ``pydantic``, the standard
library and ``typing`` — never anything from ``src.playbooks``.  Package 7's
child plan (§3.6) moves ``parse_json_from_text`` in here after ``runner_context``
is deleted, and that move only stays cheap while this module has no intra-package
edges.  ``test_expressions_module_has_no_intra_package_imports`` pins it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def parse_json_from_text(text: str) -> Any:
    """Recover the final JSON object from a model response."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        pass
    for block in reversed(_FENCED_JSON_RE.findall(text)):
        try:
            return json.loads(block.strip())
        except (json.JSONDecodeError, TypeError):
            continue
    decoder = json.JSONDecoder()
    last = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            last, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
    return last


# --------------------------------------------------------------------------
# §4.1 — the shared strict base
# --------------------------------------------------------------------------


class V2Base(BaseModel):
    """Strict base. An unknown key is a compile error, not a warning."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=False,  # source fidelity: never mutate author text
    )


#: §4.1 invariant 4 — artifact-local names (rule ids, step ids, binding names).
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]

#: §4.1 invariant 4 — externally-owned names (commands, profiles, event types).
QualifiedName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]

#: §10.6 — expression trees are depth-capped so validation stays bounded.
MAX_EXPRESSION_DEPTH: Final[int] = 10


# --------------------------------------------------------------------------
# §4.2 — the typed value union
# --------------------------------------------------------------------------

ValueKind = Literal[
    "literal",
    "event_ref",
    "context_ref",
    "binding_ref",
    "loop_ref",
    "list",
    "object",
    "template",
    "coalesce",
    "filter",
    "map",
    "len",
    "diff",
]

JsonScalar = str | int | float | bool | None

#: A dotted read path. Empty segments are rejected here so no downstream walker
#: has to guess what ``"a..b"`` or a trailing dot means.
DottedPath = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
]


class LiteralValue(V2Base):
    """A constant. Named ``LiteralValue`` because ``Literal`` is ``typing``'s."""

    type: Literal["literal"] = "literal"
    value: JsonScalar | list[JsonScalar] | dict[str, JsonScalar]


class EventRef(V2Base):
    """A dotted path into the triggering event's registered payload schema."""

    type: Literal["event_ref"] = "event_ref"
    path: DottedPath  # "project_id", "task.branch_name"


class ContextRef(V2Base):
    """A dotted path into the engine context schema (§4.2.1)."""

    type: Literal["context_ref"] = "context_ref"
    path: DottedPath  # "run_id", "rule_id", "now"


class BindingRef(V2Base):
    """A read of a binding produced by an earlier step's ``save_result_as``."""

    type: Literal["binding_ref"] = "binding_ref"
    binding: Identifier
    path: DottedPath | None = None  # dotted path inside the bound result


class LoopRef(V2Base):
    """The item or index of an enclosing foreach, filter predicate or map body."""

    type: Literal["loop_ref"] = "loop_ref"
    binding: Identifier  # == the ForEachStep's item_binding
    path: DottedPath | None = None
    index: bool = False  # True -> the 0-based iteration index, not the item


class ListValue(V2Base):
    type: Literal["list"] = "list"
    items: list[Value]


class ObjectValue(V2Base):
    type: Literal["object"] = "object"
    fields: dict[str, Value]


class TemplateValue(V2Base):
    """Produces a string and only a string. Parts are concatenated in order.

    §10.4: this is not a format language.  There is no ``{{ }}`` parsing, no
    ``str.format``, and no user-controlled format spec — rendering is
    ``"".join(str(render(p)) for p in parts)``.
    """

    type: Literal["template"] = "template"
    parts: list[Value]


class CoalesceValue(V2Base):
    """First non-null branch wins. The ONLY way to express optionality."""

    type: Literal["coalesce"] = "coalesce"
    options: Annotated[list[Value], Field(min_length=2)]  # last must be total (§6.5)


class FilterValue(V2Base):
    type: Literal["filter"] = "filter"
    collection: Value
    item_binding: Identifier
    predicate: Condition


class MapValue(V2Base):
    type: Literal["map"] = "map"
    collection: Value
    item_binding: Identifier
    value: Value


class LenValue(V2Base):
    type: Literal["len"] = "len"
    collection: Value


class DiffValue(V2Base):
    type: Literal["diff"] = "diff"
    left: Value
    right: Value
    key_path: DottedPath | None = None


Value = Annotated[
    LiteralValue
    | EventRef
    | ContextRef
    | BindingRef
    | LoopRef
    | ListValue
    | ObjectValue
    | TemplateValue
    | CoalesceValue
    | FilterValue
    | MapValue
    | LenValue
    | DiffValue,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# §4.2.1 — the closed engine context schema
# --------------------------------------------------------------------------

#: The complete set of ``ContextRef`` paths.  There is no dynamic context:
#: anything absent from this mapping is ``unknown_context_path`` (error).
#: Package 4's executors and Package 5's inspector read this same table.
ENGINE_CONTEXT_SCHEMA: Final[dict[str, str]] = {
    "run_id": "string",
    "dispatch_id": "string",
    "playbook_id": "string",
    "rule_id": "string",
    "artifact_sha256": "string",
    "now": "string",
    "attempt": "integer",
    "iteration_index": "integer",
}

#: ``iteration_index`` exists only inside a loop body; §6.3 rejects it elsewhere.
LOOP_ONLY_CONTEXT_PATHS: Final[frozenset[str]] = frozenset({"iteration_index"})


# --------------------------------------------------------------------------
# §4.3 — conditions
# --------------------------------------------------------------------------

ComparisonOp = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "contains"]
BooleanOp = Literal["and", "or", "not"]
ExistsMode = Literal["present", "truthy"]


class Comparison(V2Base):
    type: Literal["comparison"] = "comparison"
    op: ComparisonOp
    left: Value
    right: Value


class BooleanExpr(V2Base):
    type: Literal["bool"] = "bool"
    op: BooleanOp
    operands: list[Condition]  # len == 1 for "not", len >= 2 otherwise

    @model_validator(mode="after")
    def _arity(self) -> BooleanExpr:
        """§4.3: no vacuous truth.

        V1's ``all: []`` evaluated to ``True`` and ``any: []`` to ``False``
        (``src/playbooks/conditions.py``).  A nullary boolean is a model error
        here, so the hole cannot be reopened by an author or a compiler agent.
        """
        if self.op == "not":
            if len(self.operands) != 1:
                raise ValueError("empty_boolean_operand: 'not' takes exactly one operand")
        elif len(self.operands) < 2:
            raise ValueError(f"empty_boolean_operand: {self.op!r} takes at least two operands")
        return self


class Exists(V2Base):
    type: Literal["exists"] = "exists"
    value: Value
    mode: ExistsMode = "present"  # "truthy" reproduces V1's `truthy:`/`not_null:`


Condition = Annotated[Comparison | BooleanExpr | Exists, Field(discriminator="type")]


# --------------------------------------------------------------------------
# Traversal helpers — shared by validation, the diff and the explanation layer
# --------------------------------------------------------------------------


def expression_children(node: Any) -> list[Any]:
    """Direct children, including predicates nested inside collection values."""
    if isinstance(node, ListValue):
        return list(node.items)
    if isinstance(node, ObjectValue):
        return list(node.fields.values())
    if isinstance(node, TemplateValue):
        return list(node.parts)
    if isinstance(node, CoalesceValue):
        return list(node.options)
    if isinstance(node, FilterValue):
        return [node.collection, node.predicate]
    if isinstance(node, MapValue):
        return [node.collection, node.value]
    if isinstance(node, LenValue):
        return [node.collection]
    if isinstance(node, (Comparison, DiffValue)):
        return [node.left, node.right]
    if isinstance(node, BooleanExpr):
        return list(node.operands)
    if isinstance(node, Exists):
        return [node.value]
    return []


def walk_expression(node: Any) -> list[tuple[Any, tuple[Any, ...]]]:
    """Pre-order nodes with enclosing expression binders, outermost first.

    A map/filter collection is evaluated outside its own binding. Only its
    body gains that binding. Keeping this context on each occurrence also
    handles the same reference object reused in multiple lexical positions.
    """
    found = []
    stack = [(node, ())]
    while stack:
        current, scope = stack.pop()
        if not isinstance(current, V2Base):
            continue
        found.append((current, scope))
        children = expression_children(current)
        for index in range(len(children) - 1, -1, -1):
            local = scope
            if isinstance(current, (FilterValue, MapValue)) and index == 1:
                local = (*scope, current)
            stack.append((children[index], local))
    return found


def walk_value(value: Any, *, scoped: bool = False) -> list[Any]:
    """Every value node, optionally paired with its lexical binders."""
    return [
        (node, scope) if scoped else node
        for node, scope in walk_expression(value)
        if not isinstance(node, (Comparison, BooleanExpr, Exists))
    ]


def walk_condition(condition: Any) -> list[Any]:
    """Every condition, including predicates inside collection operands."""
    return [
        node for node, _ in walk_expression(condition)
        if isinstance(node, (Comparison, BooleanExpr, Exists))
    ]


def condition_values(condition: Any, *, scoped: bool = False) -> list[Any]:
    """Every value reachable from a condition, retaining lexical scope on request."""
    if not isinstance(condition, (Comparison, BooleanExpr, Exists)):
        return []
    return walk_value(condition, scoped=scoped)


def expression_depth(node: Any) -> int:
    """Nesting depth of a value or condition tree; a leaf is depth 1."""
    depth = 0
    stack = [(node, 1)]
    while stack:
        current, level = stack.pop()
        if not isinstance(current, V2Base):
            continue
        depth = max(depth, level)
        stack.extend((child, level + 1) for child in expression_children(current))
    return depth


def check_expression_depth(node: Any) -> None:
    """§10.6 — raise when a tree exceeds ``MAX_EXPRESSION_DEPTH``."""
    depth = expression_depth(node)
    if depth > MAX_EXPRESSION_DEPTH:
        raise ValueError(
            f"expression nesting depth {depth} exceeds the limit of {MAX_EXPRESSION_DEPTH}"
        )


# --------------------------------------------------------------------------
# Runtime resolution — Package 4 child plan §2.5 item 1
# --------------------------------------------------------------------------
#
# ``validation.py`` proves statically that every reference in an artifact
# points at something the graph declares.  What it cannot see is the *value*
# that arrives at run time, so this is where a declared-but-absent path
# becomes an error rather than an empty string.  V1 interpolated a miss as
# ``""`` and carried on; here every miss raises, and the engine maps the
# raise onto the ``input_resolution_failed`` reserved outcome **before** the
# executor runs (Package 4 §3.4 step 4).


class ValueResolutionError(ValueError):
    """A typed reference could not be resolved against a live scope."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class ExpressionBudgetExceeded(ValueResolutionError):
    """A named, non-recoverable expression limit (even inside exists/coalesce)."""

    def __init__(self, code: str, limit: int) -> None:
        self.code = code
        super().__init__("expression", f"{code}: limit {limit} exceeded")


@dataclass
class EvaluationBudget:
    """One aggregate budget for a step's inputs or a decision's cases.

    Expression nodes and inspected JSON members both consume visits. Byte
    accounting is incremental: an oversized element is rejected before it is
    appended to a constructed collection, and strings are encoded in chunks.
    """

    max_visits: int = 100_000
    max_items: int = 10_000
    max_bytes: int = 262_144
    max_depth: int = MAX_EXPRESSION_DEPTH
    visits: int = 0
    depth: int = 0

    def visit(self, count: int = 1) -> None:
        self.visits += count
        if self.visits > self.max_visits:
            raise ExpressionBudgetExceeded("expression_visit_limit", self.max_visits)

    def items(self, count: int) -> None:
        if count > self.max_items:
            raise ExpressionBudgetExceeded("expression_item_limit", self.max_items)

    def size(self, count: int) -> int:
        if count > self.max_bytes:
            raise ExpressionBudgetExceeded("expression_byte_limit", self.max_bytes)
        return count

    def enter(self) -> None:
        self.visit()
        if self.depth >= self.max_depth:
            raise ExpressionBudgetExceeded("expression_depth_limit", self.max_depth)
        self.depth += 1

    def string_size(self, value: str) -> int:
        # JSON quotes plus escaped UTF-8 bytes, with no whole-string allocation.
        size = 2
        self.size(len(value) + 2)
        for start in range(0, len(value), 4096):
            part = json.dumps(value[start:start + 4096], ensure_ascii=False)[1:-1]
            size = self.size(size + len(part.encode("utf-8")))
        return size

    def measure(self, value: Any) -> int:
        """Validate/measure JSON without constructing its serialized payload."""
        size = 0
        stack = [value]
        while stack:
            item = stack.pop()
            self.visit()
            if isinstance(item, str):
                size += self.string_size(item)
            elif item is None:
                size += 4
            elif isinstance(item, bool):
                size += 4 if item else 5
            elif isinstance(item, (int, float)):
                if isinstance(item, float) and not math.isfinite(item):
                    raise ValueResolutionError("expression", "non-finite JSON number")
                size += len(str(item))
            elif isinstance(item, (list, dict)):
                self.items(len(item))
                size += 2 + max(0, len(item) - 1)
                if isinstance(item, dict):
                    size += len(item)  # colons
                    for key in item:
                        if not isinstance(key, str):
                            raise ValueResolutionError("expression", "JSON keys must be strings")
                        size = self.size(size + self.string_size(key))
                    stack.extend(item.values())
                else:
                    stack.extend(item)
            else:
                raise ValueResolutionError("expression", "value is not JSON")
            self.size(size)
        return size


#: Sentinel distinguishing "resolved to null" from "not present at all".
#: ``CoalesceValue`` needs the difference, and so does ``Exists(mode=...)``.
class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


MISSING: Final[_Missing] = _Missing()


class ResolutionScope(BaseModel):
    """The four namespaces a value may read, kept structurally separate.

    They are four fields rather than one dict on purpose: V1 wrote loop items
    into the same mapping as step outputs, so a binding named ``task`` and a
    loop item named ``task`` silently collided.  Here a ``LoopRef`` cannot
    reach a binding and a ``BindingRef`` cannot reach a loop item, whatever
    they are called.  ``loop`` holds ``{item_binding: item}`` plus
    ``{item_binding + "#index": int}`` for ``LoopRef(index=True)``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    event: Mapping[str, Any] = Field(default_factory=dict)
    context: Mapping[str, Any] = Field(default_factory=dict)
    bindings: Mapping[str, Any] = Field(default_factory=dict)
    loop: Mapping[str, Any] = Field(default_factory=dict)

    def with_loop_item(self, name: str, item: Any, index: int) -> ResolutionScope:
        loop = dict(self.loop)
        loop[name] = item
        loop[f"{name}#index"] = index
        return self.model_copy(update={"loop": loop})

    def with_binding(self, name: str, value: Any) -> ResolutionScope:
        """Bind *name*.  Rebinding raises — bindings are immutable (§3.4 step 8)."""
        if name in self.bindings:
            raise ValueResolutionError(f"bindings.{name}", "binding is already assigned")
        bindings = dict(self.bindings)
        bindings[name] = value
        return self.model_copy(update={"bindings": bindings})


def _walk_path(root: Any, path: str | None, *, where: str) -> Any:
    """Follow a dotted path, returning :data:`MISSING` when it does not exist.

    A path *into a scalar* is not a miss — it is a type error the compiler
    could not see, because the compiler knows the shape of an event schema
    but not the shape of a handler result — so it raises immediately.
    """
    if not path:
        return root
    current = root
    walked: list[str] = []
    for segment in path.split("."):
        walked.append(segment)
        if isinstance(current, Mapping):
            if segment not in current:
                return MISSING
            current = current[segment]
            continue
        raise ValueResolutionError(
            f"{where}.{path}", f"{'.'.join(walked[:-1]) or where} is not an object"
        )
    return current


def _resolve(value: Any, scope: ResolutionScope, budget: EvaluationBudget) -> Any:
    budget.enter()
    try:
        result = _resolve_node(value, scope, budget)
        if result is not MISSING:
            budget.measure(result)
        return result
    finally:
        budget.depth -= 1


def _resolve_node(value: Any, scope: ResolutionScope, budget: EvaluationBudget) -> Any:
    """Resolve one node, returning :data:`MISSING` for an absent reference."""
    if isinstance(value, LiteralValue):
        return value.value
    if isinstance(value, EventRef):
        return _walk_path(scope.event, value.path, where="event")
    if isinstance(value, ContextRef):
        return _walk_path(scope.context, value.path, where="context")
    if isinstance(value, BindingRef):
        if value.binding not in scope.bindings:
            return MISSING
        return _walk_path(
            scope.bindings[value.binding], value.path, where=f"bindings.{value.binding}"
        )
    if isinstance(value, LoopRef):
        key = f"{value.binding}#index" if value.index else value.binding
        if key not in scope.loop:
            return MISSING
        item = scope.loop[key]
        if value.index:
            return item
        return _walk_path(item, value.path, where=f"loop.{value.binding}")
    if isinstance(value, ListValue):
        budget.items(len(value.items))
        result = []
        size = 2
        for item in value.items:
            resolved = resolve_value(item, scope, budget)
            size = budget.size(size + budget.measure(resolved) + bool(result))
            result.append(resolved)
        return result
    if isinstance(value, ObjectValue):
        budget.items(len(value.fields))
        result = {}
        size = 2
        for name, node in value.fields.items():
            resolved = resolve_value(node, scope, budget)
            size = budget.size(
                size + budget.string_size(name) + 1 + budget.measure(resolved) + bool(result)
            )
            result[name] = resolved
        return result
    if isinstance(value, TemplateValue):
        parts = []
        size = 2
        for part in value.parts:
            rendered = _render(resolve_value(part, scope, budget))
            size = budget.size(size + budget.string_size(rendered) - 2)
            parts.append(rendered)
        return "".join(parts)
    if isinstance(value, CoalesceValue):
        for option in value.options:
            resolved = _resolve(option, scope, budget)
            if resolved is not MISSING and resolved is not None:
                return resolved
        return MISSING
    if isinstance(value, (FilterValue, MapValue, LenValue)):
        collection = _collection(resolve_value(value.collection, scope, budget), value.type)
        if isinstance(value, LenValue):
            return len(collection)
        result = []
        size = 2
        for index, item in enumerate(collection):
            budget.visit()
            local = scope.with_loop_item(value.item_binding, item, index)
            if isinstance(value, FilterValue):
                if not evaluate_condition(value.predicate, local, budget):
                    continue
                resolved = item
            else:
                resolved = resolve_value(value.value, local, budget)
            budget.items(len(result) + 1)
            size = budget.size(size + budget.measure(resolved) + bool(result))
            result.append(resolved)
        return result
    if isinstance(value, DiffValue):
        left = _collection(resolve_value(value.left, scope, budget), "diff.left")
        right = _collection(resolve_value(value.right, scope, budget), "diff.right")
        seen = {_identity(item, value.key_path, budget) for item in right}
        result = []
        size = 2
        for item in left:
            identity = _identity(item, value.key_path, budget)
            if identity in seen:
                continue
            budget.items(len(result) + 1)
            size = budget.size(size + budget.measure(item) + bool(result))
            seen.add(identity)
            result.append(item)
        return result
    raise ValueResolutionError(type(value).__name__, "is not a typed value")


def _collection(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueResolutionError(where, "requires a list (coalesce null explicitly)")
    return value


def _identity(item: Any, key_path: str | None, budget: EvaluationBudget) -> tuple:
    """Flat tagged canonical JSON tokens, hashable without recursive hashing."""
    if key_path is not None:
        item = _walk_path(item, key_path, where="diff.key_path")
        if item is MISSING or isinstance(item, (list, dict)):
            raise ValueResolutionError("diff.key_path", "requires an existing scalar identity")
    tokens = []
    stack = [item]
    while stack:
        value = stack.pop()
        budget.visit()
        if value is None:
            tokens.append(("null",))
        elif isinstance(value, bool):
            tokens.append(("boolean", value))
        elif isinstance(value, (int, float)):
            tokens.append(("number", Decimal(str(value))))
        elif isinstance(value, str):
            tokens.append(("string", value))
        elif isinstance(value, list):
            tokens.append(("array", len(value)))
            stack.extend(reversed(value))
        elif isinstance(value, dict):
            tokens.append(("object", len(value)))
            for key in sorted(value, reverse=True):
                stack.extend((value[key], key))
        else:
            raise ValueResolutionError("diff", "identity is not JSON")
    return tuple(tokens)


def _render(value: Any) -> str:
    """One template part as text.  ``True`` is ``true``, so a rendered
    template reads the same as the JSON the same value would serialize to."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _describe(value: Any) -> str:
    if isinstance(value, EventRef | ContextRef):
        return f"{value.type}:{value.path}"
    if isinstance(value, BindingRef | LoopRef):
        suffix = "#index" if getattr(value, "index", False) else (f".{value.path}" if value.path else "")
        return f"{value.type}:{value.binding}{suffix}"
    return value.type if isinstance(value, V2Base) else type(value).__name__


def resolve_value(
    value: Any, scope: ResolutionScope, budget: EvaluationBudget | None = None
) -> Any:
    """Resolve *value* against *scope*, raising on any unresolvable reference."""
    resolved = _resolve(value, scope, budget if budget is not None else EvaluationBudget())
    if resolved is MISSING:
        raise ValueResolutionError(_describe(value), "is not present in this run's scope")
    return resolved


_ORDERING_OPS: Final[frozenset[str]] = frozenset({"lt", "lte", "gt", "gte"})


def evaluate_condition(
    condition: Any, scope: ResolutionScope, budget: EvaluationBudget | None = None
) -> bool:
    budget = budget if budget is not None else EvaluationBudget()
    budget.enter()
    try:
        return _evaluate_condition(condition, scope, budget)
    finally:
        budget.depth -= 1


def _evaluate_condition(
    condition: Any, scope: ResolutionScope, budget: EvaluationBudget
) -> bool:
    """Evaluate a typed condition.  Never defaults to true.

    V1's evaluator returned ``True`` for a shape it did not recognise; a
    shape that is not one of the three declared kinds raises here, and the
    caller turns that into an outcome rather than a silently-taken edge.
    """
    if isinstance(condition, Exists):
        # The one node that tolerates a miss: asking whether something is
        # present must not fail because it is absent.
        try:
            resolved = _resolve(condition.value, scope, budget)
        except ExpressionBudgetExceeded:
            raise
        except ValueResolutionError:
            return False
        if resolved is MISSING or resolved is None:
            return False
        return bool(resolved) if condition.mode == "truthy" else True

    if isinstance(condition, BooleanExpr):
        if condition.op == "not":
            return not evaluate_condition(condition.operands[0], scope, budget)
        if condition.op == "and":
            return all(evaluate_condition(node, scope, budget) for node in condition.operands)
        return any(evaluate_condition(node, scope, budget) for node in condition.operands)

    if isinstance(condition, Comparison):
        left = resolve_value(condition.left, scope, budget)
        right = resolve_value(condition.right, scope, budget)
        if condition.op in {"in", "not_in", "contains"}:
            haystack = left if condition.op == "contains" else right
            if isinstance(haystack, (list, dict)):
                budget.visit(len(haystack))
        return _compare(condition.op, left, right)

    raise ValueResolutionError(type(condition).__name__, "is not a typed condition")


def _compare(op: str, left: Any, right: Any) -> bool:
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op in ("in", "not_in", "contains"):
        haystack, needle = (left, right) if op == "contains" else (right, left)
        if isinstance(haystack, str):
            contained = isinstance(needle, str) and needle in haystack
        elif isinstance(haystack, (list, tuple, set, frozenset, Mapping)):
            contained = needle in haystack
        else:
            raise ValueResolutionError(op, f"{type(haystack).__name__} is not a collection")
        return contained if op != "not_in" else not contained
    if op in _ORDERING_OPS:
        # ``bool`` is an ``int`` in Python, so ``True < 2`` would quietly
        # succeed; an artifact that orders a flag against a number is a type
        # error the compiler could not see, and it must not become an edge.
        if isinstance(left, bool) != isinstance(right, bool) or not isinstance(
            left, (int, float, str)
        ) or not isinstance(right, (int, float, str)):
            raise ValueResolutionError(op, f"cannot order {type(left).__name__} against {type(right).__name__}")
        if isinstance(left, str) != isinstance(right, str):
            raise ValueResolutionError(op, f"cannot order {type(left).__name__} against {type(right).__name__}")
        if op == "lt":
            return left < right
        if op == "lte":
            return left <= right
        if op == "gt":
            return left > right
        return left >= right
    raise ValueResolutionError(op, "is not a comparison operator")


FilterValue.model_rebuild()
MapValue.model_rebuild()
LenValue.model_rebuild()
DiffValue.model_rebuild()
ListValue.model_rebuild()
ObjectValue.model_rebuild()
TemplateValue.model_rebuild()
CoalesceValue.model_rebuild()
Comparison.model_rebuild()
BooleanExpr.model_rebuild()
Exists.model_rebuild()

__all__ = [
    "ENGINE_CONTEXT_SCHEMA",
    "LOOP_ONLY_CONTEXT_PATHS",
    "MAX_EXPRESSION_DEPTH",
    "MISSING",
    "BindingRef",
    "BooleanExpr",
    "BooleanOp",
    "CoalesceValue",
    "FilterValue",
    "MapValue",
    "LenValue",
    "DiffValue",
    "EvaluationBudget",
    "ExpressionBudgetExceeded",
    "expression_children",
    "walk_expression",
    "Comparison",
    "ComparisonOp",
    "Condition",
    "ContextRef",
    "DottedPath",
    "EventRef",
    "Exists",
    "ExistsMode",
    "Identifier",
    "JsonScalar",
    "ListValue",
    "LiteralValue",
    "LoopRef",
    "ObjectValue",
    "QualifiedName",
    "ResolutionScope",
    "TemplateValue",
    "V2Base",
    "Value",
    "ValueKind",
    "ValueResolutionError",
    "check_expression_depth",
    "condition_values",
    "evaluate_condition",
    "expression_depth",
    "resolve_value",
    "walk_condition",
    "walk_value",
]
