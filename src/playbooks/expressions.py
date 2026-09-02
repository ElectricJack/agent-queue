"""Typed value and condition expressions for the Playbook V2 artifact.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md``
§4.1 (the strict base), §4.2 (the nine-member value union), §4.2.1 (the closed
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

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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
    """A read of the current item of an enclosing ``ForEachStep``."""

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


Value = Annotated[
    LiteralValue
    | EventRef
    | ContextRef
    | BindingRef
    | LoopRef
    | ListValue
    | ObjectValue
    | TemplateValue
    | CoalesceValue,
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


def walk_value(value: Any) -> list[Any]:
    """Every value node in ``value``, pre-order, including ``value`` itself."""
    found: list[Any] = []
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if not isinstance(node, V2Base):
            continue
        found.append(node)
        if isinstance(node, ListValue):
            stack.extend(reversed(node.items))
        elif isinstance(node, ObjectValue):
            stack.extend(reversed(list(node.fields.values())))
        elif isinstance(node, TemplateValue):
            stack.extend(reversed(node.parts))
        elif isinstance(node, CoalesceValue):
            stack.extend(reversed(node.options))
    return found


def walk_condition(condition: Any) -> list[Any]:
    """Every condition node in ``condition``, pre-order."""
    found: list[Any] = []
    stack: list[Any] = [condition]
    while stack:
        node = stack.pop()
        if not isinstance(node, V2Base):
            continue
        found.append(node)
        if isinstance(node, BooleanExpr):
            stack.extend(reversed(node.operands))
    return found


def condition_values(condition: Any) -> list[Any]:
    """Every value node reachable from ``condition``."""
    values: list[Any] = []
    for node in walk_condition(condition):
        if isinstance(node, Comparison):
            values.extend(walk_value(node.left))
            values.extend(walk_value(node.right))
        elif isinstance(node, Exists):
            values.extend(walk_value(node.value))
    return values


def expression_depth(node: Any) -> int:
    """Nesting depth of a value or condition tree; a leaf is depth 1."""
    if not isinstance(node, V2Base):
        return 0
    children: list[Any] = []
    if isinstance(node, ListValue):
        children = list(node.items)
    elif isinstance(node, ObjectValue):
        children = list(node.fields.values())
    elif isinstance(node, TemplateValue):
        children = list(node.parts)
    elif isinstance(node, CoalesceValue):
        children = list(node.options)
    elif isinstance(node, Comparison):
        children = [node.left, node.right]
    elif isinstance(node, BooleanExpr):
        children = list(node.operands)
    elif isinstance(node, Exists):
        children = [node.value]
    if not children:
        return 1
    return 1 + max(expression_depth(child) for child in children)


def check_expression_depth(node: Any) -> None:
    """§10.6 — raise when a tree exceeds ``MAX_EXPRESSION_DEPTH``."""
    depth = expression_depth(node)
    if depth > MAX_EXPRESSION_DEPTH:
        raise ValueError(
            f"expression nesting depth {depth} exceeds the limit of {MAX_EXPRESSION_DEPTH}"
        )


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
    "BindingRef",
    "BooleanExpr",
    "BooleanOp",
    "CoalesceValue",
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
    "TemplateValue",
    "V2Base",
    "Value",
    "ValueKind",
    "check_expression_depth",
    "condition_values",
    "expression_depth",
    "walk_condition",
    "walk_value",
]
