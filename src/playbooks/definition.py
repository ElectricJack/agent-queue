"""The strict Playbook V2 artifact model.

Child plan ``docs/superpowers/plans/2026-09-01-playbook-v2-typed-model-compiler.md``
§4.4 (artifact, scope, rules, source refs), §4.5 (the seven steps), §4.6 (the
outcome vocabulary), §4.7 (canonical serialization and the four fingerprints)
and §4.8 (the executable/presentation split).

This module is the schema authority for V2.  ``src/playbook_v2_schema.json``,
Package 3's ``ArtifactStore``, Package 4's executors and Package 5's DTOs are
downstream projections of what is written here; none of them re-derives it.

V1's ``src/playbooks/models.py`` dataclasses share no field with these models and
are untouched — they are deleted in Package 7.
"""

from __future__ import annotations

import hashlib
import json
import re

import yaml
from datetime import datetime
from types import UnionType
from typing import Annotated, Any, Final, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Field, StringConstraints, model_validator

from src.playbooks.expressions import (
    Condition,
    Identifier,
    JsonScalar,
    LiteralValue,
    QualifiedName,
    V2Base,
    Value,
    check_expression_depth,
    condition_values,
    walk_value,
)

SCHEMA_GENERATION: Final[int] = 2

#: Hand-bumped, never derived from git: two builds of the same source must hash
#: identically.  Package 6 bumps it when the bundled Markdown is rewritten (§4.7).
COMPILER_BUILD: Final[str] = "playbook-v2-compiler/1"

Sha256 = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

#: §10.6 — the bounds that make ``validate_definition`` a bounded computation.
MAX_RULES: Final[int] = 50
MAX_STEPS: Final[int] = 500

#: §10.5 — ``SourceRef.excerpt`` is author prose echoed into the UI.
MAX_EXCERPT_CHARS: Final[int] = 400

_PRESENTATION: Final[dict[str, Any]] = {"executable": False}


# --------------------------------------------------------------------------
# §4.6 — outcome vocabulary and transition keys
# --------------------------------------------------------------------------

#: Engine-owned failure outcomes every step may produce.
RESERVED_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "input_resolution_failed",
        "unavailable",
        "contract_violation",
        "state_limit_exceeded",
        "interrupted",
        "timed_out",
        "cancelled",
    }
)

#: Additional reserved outcomes only an ``llm`` step can produce.
LLM_RESERVED_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"invalid_output", "budget_exceeded", "provider_error"}
)

#: A transition KEY, never an outcome: the single visible catch-all edge for
#: every reserved outcome a step does not map individually.
RUNTIME_ERROR_KEY: Final[str] = "runtime_error"


# --------------------------------------------------------------------------
# §4.4 — source references and scope
# --------------------------------------------------------------------------


class SourceRef(V2Base):
    """Where in the authoring Markdown this element came from.

    Field-for-field identical to Package 5 §4.1's ``SourceRefDTO``.  Every field
    is presentation-only (§4.8): moving a rule down the page must not change the
    artifact's executable fingerprint.
    """

    path: str = Field(json_schema_extra=_PRESENTATION)
    start_line: int = Field(ge=1, json_schema_extra=_PRESENTATION)
    end_line: int = Field(ge=1, json_schema_extra=_PRESENTATION)
    heading: str | None = Field(default=None, json_schema_extra=_PRESENTATION)
    excerpt: str | None = Field(default=None, json_schema_extra=_PRESENTATION)

    @model_validator(mode="after")
    def _ordered_and_bounded(self) -> SourceRef:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        if self.excerpt is not None and len(self.excerpt) > MAX_EXCERPT_CHARS:
            raise ValueError(f"excerpt exceeds {MAX_EXCERPT_CHARS} characters")
        return self


def truncate_excerpt(text: str) -> tuple[str, bool]:
    """§10.5 — cap an excerpt on a character boundary; report whether it was cut."""
    if len(text) <= MAX_EXCERPT_CHARS:
        return text, False
    return text[: MAX_EXCERPT_CHARS - 1] + "…", True


class SystemScope(V2Base):
    type: Literal["system"] = "system"


class ProjectScope(V2Base):
    type: Literal["project"] = "project"
    project_id: str


class AgentTypeScope(V2Base):
    type: Literal["agent_type"] = "agent_type"
    agent_type: str


Scope = Annotated[SystemScope | ProjectScope | AgentTypeScope, Field(discriminator="type")]


def scope_from_v1(raw: str) -> Scope:
    """Bridge V1's string scope (``src/playbooks/models.py`` ``parse_scope``).

    Used only by ``pipeline_lowering`` and the shadow-compile report; the V2
    artifact itself always carries the object form.
    """
    text = (raw or "").strip()
    if text in ("", "system"):
        return SystemScope()
    for prefix, build in (
        ("agent-type:", lambda rest: AgentTypeScope(agent_type=rest)),
        ("agent_type:", lambda rest: AgentTypeScope(agent_type=rest)),
        ("project:", lambda rest: ProjectScope(project_id=rest)),
    ):
        if text.startswith(prefix):
            rest = text[len(prefix) :].strip()
            if not rest:
                raise ValueError(f"scope {raw!r} names no target")
            return build(rest)
    if text == "project":
        raise ValueError("a project scope must name its project")
    raise ValueError(f"unrecognised V1 scope {raw!r}")


def scope_to_v1(scope: Any) -> str:
    """Inverse of :func:`scope_from_v1`."""
    if isinstance(scope, SystemScope):
        return "system"
    if isinstance(scope, ProjectScope):
        return f"project:{scope.project_id}"
    if isinstance(scope, AgentTypeScope):
        return f"agent-type:{scope.agent_type}"
    raise TypeError(f"not a V2 scope: {scope!r}")


# --------------------------------------------------------------------------
# §4.5 — step common shapes
# --------------------------------------------------------------------------

StepKind = Literal["command", "llm", "agent_task", "decision", "wait", "foreach", "terminal"]


class StepBase(V2Base):
    rule: Identifier  # owner rule id — every step has exactly one (§6.2)
    title: str = Field(json_schema_extra=_PRESENTATION)
    description: str | None = Field(default=None, json_schema_extra=_PRESENTATION)
    source: SourceRef


class RetryPolicy(V2Base):
    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: float | None = Field(default=None, ge=0)
    retry_on: list[str] = Field(default_factory=list)  # outcomes that retry, not transition


class AiBudget(V2Base):
    """Every field required. The spec forbids an unbounded AI state."""

    max_calls: int = Field(ge=1, le=50)
    max_output_tokens: int = Field(ge=1)
    max_total_tokens: int = Field(ge=1)
    timeout_seconds: int = Field(ge=1, le=3600)


class ToolUsePolicy(V2Base):
    enabled: bool = False
    # Both must be a subset of the step profile's policy (§6.7).
    aq_commands: list[QualifiedName] = Field(default_factory=list)
    plugin_tools: list[QualifiedName] = Field(default_factory=list)


# --------------------------------------------------------------------------
# §4.5 — the seven steps
# --------------------------------------------------------------------------


class CommandStep(StepBase):
    type: Literal["command"] = "command"
    command: QualifiedName
    inputs: dict[str, Value] = Field(default_factory=dict)
    idempotency_key: Value | None = None  # overrides the contract default
    retry: RetryPolicy | None = None
    save_result_as: Identifier | None = None
    transitions: dict[str, Identifier]


class LlmStep(StepBase):
    type: Literal["llm"] = "llm"
    profile_id: QualifiedName
    prompt: Value  # rendered to a string; normally a TemplateValue
    inputs: dict[str, Value] = Field(default_factory=dict)  # named, typed prompt inputs
    output_schema: dict[str, Any]  # JSON Schema, draft 2020-12 (§10.3)
    outcome_field: str | None = None  # required when transitions carry business outcomes
    budget: AiBudget
    tool_use: ToolUsePolicy = Field(default_factory=ToolUsePolicy)
    retry: RetryPolicy | None = None
    save_result_as: Identifier | None = None
    transitions: dict[str, Identifier]


class CapabilityNarrowing(V2Base):
    """Explicit per-step narrowing of a delegated agent task's capabilities.

    Roadmap §2: "Delegated agent-task permissions are the intersection of
    parent permissions, child profile permissions, and explicit per-step
    narrowing."  This is that third term, and it is a *narrowing* only: the
    executor intersects, never unions, so listing a name the parent or the
    child profile does not hold grants nothing.

    ``None`` in a namespace means "this step narrows nothing here" — the
    identity of intersection, not deny-all.  An explicitly empty list means
    *none*, matching :class:`~src.profiles.capabilities.CapabilityPolicy`'s
    "empty means none" rule.  The distinction matters: a step that wants a
    child with no AQ commands writes ``aq_commands: []``, and a step that
    does not care omits the key.

    Package 2 shipped ``AgentTaskStep`` without this field; Package 4's T-8
    added it, because the roadmap constraint above is not implementable
    without it.  See the child plan §2.1 and §4.5.
    """

    harness_tools: list[str] | None = None
    aq_commands: list[QualifiedName] | None = None
    plugin_tools: list[QualifiedName] | None = None


class AgentTaskStep(StepBase):
    type: Literal["agent_task"] = "agent_task"
    profile_id: QualifiedName
    objective: Value  # rendered to a string
    inputs: dict[str, Value] = Field(default_factory=dict)
    wait_for_completion: bool = True
    cancel_child: bool = False  # spec: explicit, defaults false
    #: The third intersection term of §4.5 step 1.  ``None`` narrows nothing.
    capability_narrowing: CapabilityNarrowing | None = None
    timeout_seconds: int | None = Field(default=None, ge=1)
    retry: RetryPolicy | None = None
    save_result_as: Identifier | None = None
    transitions: dict[str, Identifier]


class DecisionCase(V2Base):
    when: Condition
    goto: Identifier
    label: str | None = Field(default=None, json_schema_extra=_PRESENTATION)


class DecisionStep(StepBase):
    """``default`` is required, deviating from the spec's "optional default".

    An optional default is a fall-through that is neither a displayed edge nor
    an error, which the spec's stronger rule ("every executable transition is
    displayed", "no silent defaults") forbids.  §20 item 3 records the deviation.
    """

    type: Literal["decision"] = "decision"
    cases: Annotated[list[DecisionCase], Field(min_length=1)]
    default: Identifier


WaitKind = Literal["event", "human", "task", "timer"]


class WaitStep(StepBase):
    type: Literal["wait"] = "wait"
    wait_kind: WaitKind
    awaited: Value | None = None  # event_type / gate title / task ref
    correlation_key: Value | None = None  # computed at pause time
    outcomes: list[str] = Field(default_factory=list)  # human only: the gate vocabulary
    timeout_seconds: int | None = Field(default=None, ge=1)
    save_result_as: Identifier | None = None
    transitions: dict[str, Identifier]

    @model_validator(mode="after")
    def _per_kind_requirements(self) -> WaitStep:
        """§4.5's per-kind table, enforced in the model rather than in a pass."""
        missing: list[str] = []
        if self.wait_kind in ("event", "human", "task"):
            if self.awaited is None:
                missing.append("awaited")
            if self.correlation_key is None:
                missing.append("correlation_key")
        if self.wait_kind == "human" and not self.outcomes:
            missing.append("outcomes")
        if self.wait_kind == "timer":
            if self.timeout_seconds is None:
                missing.append("timeout_seconds")
            if self.awaited is not None or self.correlation_key is not None:
                raise ValueError("a timer wait takes neither awaited nor correlation_key")
        if self.wait_kind != "human" and self.outcomes:
            raise ValueError("only a human wait declares outcomes")
        if missing:
            raise ValueError(
                f"a {self.wait_kind!r} wait requires: {', '.join(sorted(missing))}"
            )
        return self


FailurePolicy = Literal["halt", "continue", "collect"]


class ForEachStep(StepBase):
    type: Literal["foreach"] = "foreach"
    collection: Value
    item_binding: Identifier
    failure_policy: FailurePolicy
    body_entry: Identifier
    continuation: Identifier | None = None  # == transitions["completed"] when both set
    max_iterations: int = Field(default=500, ge=1, le=10000)
    save_result_as: Identifier | None = None
    transitions: dict[str, Identifier]  # {"completed": …, "failed": …}


TerminalOutcome = Literal["completed", "failed", "cancelled"]


class TerminalStep(StepBase):
    type: Literal["terminal"] = "terminal"
    outcome: TerminalOutcome
    result: Value | None = None


Step = Annotated[
    CommandStep | LlmStep | AgentTaskStep | DecisionStep | WaitStep | ForEachStep | TerminalStep,
    Field(discriminator="type"),
]

#: Every step model, for the invariant walks and the diff.
STEP_MODELS: Final[tuple[type[StepBase], ...]] = (
    CommandStep,
    LlmStep,
    AgentTaskStep,
    DecisionStep,
    WaitStep,
    ForEachStep,
    TerminalStep,
)


# --------------------------------------------------------------------------
# §4.5/§6.5 — the P2-owned result schemas the binding type-checker walks
# --------------------------------------------------------------------------

WAIT_RESULT_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    "event": {
        "type": "object",
        "required": ["event_type", "payload"],
        "properties": {"event_type": {"type": "string"}, "payload": {"type": "object"}},
    },
    "human": {
        "type": "object",
        "required": ["resolution"],
        "properties": {
            "resolution": {"type": "string"},
            "note": {"type": ["string", "null"]},
            "resolved_by": {"type": ["string", "null"]},
        },
    },
    "task": {
        "type": "object",
        "required": ["task_id", "status"],
        "properties": {
            "task_id": {"type": "string"},
            "status": {"type": "string"},
            "outcome": {"type": ["string", "null"]},
        },
    },
    "timer": {
        "type": "object",
        "required": ["fired_at"],
        "properties": {"fired_at": {"type": "string"}},
    },
}

FOREACH_RESULT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["total", "succeeded", "failed", "items"],
    "properties": {
        "total": {"type": "integer"},
        "succeeded": {"type": "integer"},
        "failed": {"type": "integer"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "outcome"],
                "properties": {
                    "index": {"type": "integer"},
                    "outcome": {"type": "string"},
                    "value": {},
                    "error": {"type": ["string", "null"]},
                },
            },
        },
    },
}

AGENT_TASK_RESULT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["task_id", "status"],
    "properties": {
        "task_id": {"type": "string"},
        "status": {"type": "string"},
        "outcome": {"type": ["string", "null"]},
        "summary": {"type": ["string", "null"]},
    },
}

#: §4.5 — the wait kinds whose business-outcome set is the engine's, not the
#: author's.  ``human`` is the exception: its outcomes are exactly ``outcomes``.
WAIT_BUSINESS_OUTCOMES: Final[dict[str, frozenset[str]]] = {
    "event": frozenset({"matched"}),
    "task": frozenset({"completed", "failed", "cancelled"}),
    "timer": frozenset({"fired"}),
}


def reserved_outcomes_for(step: Any) -> frozenset[str]:
    """The reserved outcomes ``step`` can produce (§4.6)."""
    if isinstance(step, LlmStep):
        return RESERVED_OUTCOMES | LLM_RESERVED_OUTCOMES
    return RESERVED_OUTCOMES


def business_outcomes(step: Any, *, contract_outcomes: frozenset[str] | None = None) -> frozenset[str]:
    """The closed business-outcome set of ``step`` (§4.6).

    ``contract_outcomes`` supplies the command contract's declared outcomes; it
    is ``None`` when the contract could not be resolved, in which case the
    command's business set is empty and §6.6 has already emitted
    ``unknown_command``.
    """
    if isinstance(step, CommandStep):
        return contract_outcomes if contract_outcomes is not None else frozenset()
    if isinstance(step, LlmStep):
        # A schema-only LLM step has one deterministic business outcome. The
        # live executor already returns ``completed`` when no outcome field is
        # declared; expose that edge to validation as well.
        return frozenset(_outcome_enum(step) or ("completed",))
    if isinstance(step, AgentTaskStep):
        if not step.wait_for_completion:
            return frozenset({"dispatched"})
        return frozenset({"completed", "failed"})
    if isinstance(step, WaitStep):
        if step.wait_kind == "human":
            return frozenset(step.outcomes)
        return WAIT_BUSINESS_OUTCOMES[step.wait_kind]
    if isinstance(step, ForEachStep):
        return frozenset({"completed", "failed"})
    return frozenset()


def _outcome_enum(step: LlmStep) -> list[str] | None:
    """The declared enum of ``step.outcome_field``, or ``None``."""
    if not step.outcome_field:
        return None
    properties = step.output_schema.get("properties")
    if not isinstance(properties, dict):
        return None
    declared = properties.get(step.outcome_field)
    if not isinstance(declared, dict):
        return None
    enum = declared.get("enum")
    if not isinstance(enum, list) or not all(isinstance(item, str) for item in enum):
        return None
    return list(enum)


def result_schema_for(step: Any, *, command_schema: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The JSON-Schema-shaped result a step binds through ``save_result_as``."""
    if isinstance(step, CommandStep):
        return command_schema
    if isinstance(step, LlmStep):
        return step.output_schema
    if isinstance(step, AgentTaskStep):
        return AGENT_TASK_RESULT_SCHEMA
    if isinstance(step, WaitStep):
        return WAIT_RESULT_SCHEMAS[step.wait_kind]
    if isinstance(step, ForEachStep):
        return FOREACH_RESULT_SCHEMA
    return None


def step_targets(step: Any) -> dict[str, str]:
    """Every outgoing edge of ``step`` as ``edge label -> target step id``.

    The labels are diagnostic field pointers, not the transition keys, so a
    decision case and a transition never collide in one mapping.
    """
    targets: dict[str, str] = {}
    if isinstance(step, DecisionStep):
        for index, case in enumerate(step.cases):
            targets[f"/cases/{index}/goto"] = case.goto
        targets["/default"] = step.default
        return targets
    if isinstance(step, ForEachStep):
        targets["/body_entry"] = step.body_entry
        if step.continuation is not None:
            targets["/continuation"] = step.continuation
    for key, target in getattr(step, "transitions", {}).items():
        targets[f"/transitions/{key}"] = target
    return targets


def step_values(step: Any) -> list[Any]:
    """Every value node a step carries, for the typing and reference passes."""
    values: list[Any] = []
    for attribute in ("prompt", "objective", "collection", "correlation_key", "awaited",
                      "idempotency_key", "result"):
        node = getattr(step, attribute, None)
        if node is not None:
            values.extend(walk_value(node))
    for node in getattr(step, "inputs", {}).values():
        values.extend(walk_value(node))
    if isinstance(step, DecisionStep):
        for case in step.cases:
            values.extend(condition_values(case.when))
    return values


#: The argument name that hands a *delegated* capability profile to a command.
#: ``ensure_task``'s ``profile_id`` is the one the shipped pipeline uses: the
#: playbook never runs the reviewer itself, it creates a task the reviewer
#: profile will be assigned to.  The dependency is no weaker for being
#: indirect — a capability change there changes what the created task may do —
#: so it is recorded in ``compiled_against.profiles`` exactly like a profile an
#: ``LlmStep`` names directly.
DELEGATED_PROFILE_INPUT: Final[str] = "profile_id"


def step_profile_ids(step: Any) -> tuple[str, ...]:
    """Every capability profile one step depends on, in declaration order.

    Two positions carry one, and both are real dependencies of the artifact:

    * the *own* profile of an :class:`LlmStep` or :class:`AgentTaskStep`, which
      the step runs as; and
    * a *delegated* profile — a literal :data:`DELEGATED_PROFILE_INPUT` input,
      which is how a command step hands work to another profile.

    Only a literal counts.  A ``profile_id`` computed from an event reference
    is not knowable at compile time, so there is no fingerprint to record and
    pretending otherwise would freeze a value the run picks.
    """
    found: list[str] = []
    own = getattr(step, "profile_id", None)
    if isinstance(own, str) and own:
        found.append(own)
    delegated = getattr(step, "inputs", None) or {}
    value = delegated.get(DELEGATED_PROFILE_INPUT) if hasattr(delegated, "get") else None
    if isinstance(value, LiteralValue) and isinstance(value.value, str) and value.value:
        found.append(value.value)
    return tuple(dict.fromkeys(found))


def referenced_profile_ids(definition: PlaybookDefinition) -> tuple[str, ...]:
    """Every capability profile the artifact depends on, sorted.

    This is the set ``compiled_against.profiles`` must cover; it is defined
    here so the compiler, the validator and the release check cannot drift
    apart on what "a profile this artifact depends on" means.
    """
    found: dict[str, None] = {}
    for step in definition.steps.values():
        for profile_id in step_profile_ids(step):
            found[profile_id] = None
    return tuple(sorted(found))


# --------------------------------------------------------------------------
# §4.4 — triggers, rules and the artifact
# --------------------------------------------------------------------------


class Trigger(V2Base):
    """Subscription-level match.

    ``filter`` is a conjunction of literal equality (scalar) or membership
    (list) tests against the event schema — not an expression tree, because a
    subscription filter must be matchable without a run context.
    """

    event_type: QualifiedName
    filter: dict[str, JsonScalar | list[JsonScalar]] | None = None


class Rule(V2Base):
    id: Identifier
    name: str = Field(json_schema_extra=_PRESENTATION)
    description: str | None = Field(default=None, json_schema_extra=_PRESENTATION)
    trigger: Trigger
    guard: Condition | None = None  # full typed expression, evaluated after delivery
    entry_step: Identifier
    source: SourceRef


class CompiledAgainst(V2Base):
    """Server-computed provenance: what the artifact was compiled against.

    Never author-supplied — §7.1 strips and recomputes it from the registries.
    """

    commands: dict[QualifiedName, Sha256] = Field(default_factory=dict)
    profiles: dict[QualifiedName, Sha256] = Field(default_factory=dict)


class PlaybookDefinition(V2Base):
    schema_version: Literal[2] = 2
    id: QualifiedName
    version: int = Field(ge=1)  # monotonic per playbook; server-owned
    scope: Scope
    purpose: Literal["routine", "assignment_routing"] = "routine"
    source_hash: Sha256  # of the normalized Markdown (§4.7)
    compiled_at: datetime  # UTC, tz-aware
    compiler_build: str | None = None  # COMPILER_BUILD; optional so P5's fixture validates
    rules: Annotated[list[Rule], Field(min_length=1, max_length=MAX_RULES)]
    steps: Annotated[dict[Identifier, Step], Field(min_length=1, max_length=MAX_STEPS)]
    compiled_against: CompiledAgainst = Field(default_factory=CompiledAgainst)

    @model_validator(mode="after")
    def _bounded(self) -> PlaybookDefinition:
        """§10.6 — expression depth is capped at load, not at validation."""
        if self.compiled_at.tzinfo is None:
            raise ValueError("compiled_at must be timezone-aware")
        for rule in self.rules:
            if rule.guard is not None:
                check_expression_depth(rule.guard)
        for step in self.steps.values():
            for value in step_values(step):
                check_expression_depth(value)
            if isinstance(step, DecisionStep):
                for case in step.cases:
                    check_expression_depth(case.when)
        return self

    def contract_fingerprint(self) -> str:
        return contract_fingerprint(self)

    def artifact_sha256(self) -> str:
        return artifact_sha256(self)


# --------------------------------------------------------------------------
# §4.7 — canonical serialization and the four fingerprints
# --------------------------------------------------------------------------


def canonical_bytes(definition: PlaybookDefinition) -> bytes:
    """The bytes the artifact hash is taken over.

    ``exclude_none=True`` is lossless only because of §4.1 invariant 2 (absent
    ≡ null): no V2 field distinguishes a missing key from an explicit ``null``.
    Package 3's ``ArtifactStore`` must write exactly these bytes and must not
    round-trip them through a JSON/JSONB column — ``jsonb`` reorders keys.
    """
    return json.dumps(
        definition.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def artifact_sha256(definition: PlaybookDefinition) -> str:
    return _digest(canonical_bytes(definition))


def normalize_source(markdown: str) -> str:
    """Normalize Markdown so cosmetic changes do not alter source identity."""
    frontmatter: dict[str, Any] = {}
    body = markdown
    if markdown.startswith("---"):
        parts = markdown.split("---", 2)
        if len(parts) == 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
                body = parts[2]
            except yaml.YAMLError:
                pass
    fm = yaml.dump(frontmatter, default_flow_style=False, sort_keys=True).strip() if frontmatter else ""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    lines: list[str] = []
    blank = False
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line:
            if not blank:
                lines.append("")
            blank = True
        else:
            lines.append(line)
            blank = False
    return f"{fm}\n---\n{'\n'.join(lines).strip()}"


def source_digest(markdown: str) -> str:
    return _digest(normalize_source(markdown).encode("utf-8"))


def contract_fingerprint(definition: PlaybookDefinition) -> str:
    """Over the canonical JSON of ``compiled_against.commands`` only.

    Profiles are excluded: a capability change is an activation-health question
    (``stale_contract`` is about the command surface), and Package 5's
    ``ArtifactRefDTO.contract_fingerprint`` reads exactly this.
    """
    payload = json.dumps(
        dict(sorted(definition.compiled_against.commands.items())),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return _digest(payload)


class DuplicateJsonKey(ValueError):
    """§7.1 — a duplicate object key in artifact JSON text.

    ``json.loads`` keeps the *last* of two identical keys, which is exactly the
    smuggling primitive §10.1 defends against: a stripped ``"id"`` followed by a
    second ``"id"`` would survive the strip.  Rejecting duplicates before the
    strip is what closes it, and it is also where ``duplicate_step_id`` and
    ``duplicate_rule_id`` become visible — ``steps`` is a ``dict``, so by the
    time Pydantic sees it the collision is already gone.
    """

    def __init__(self, pointer: str, key: str) -> None:
        self.pointer = pointer
        self.key = key
        self.code = "duplicate_step_id" if pointer.endswith("/steps") else "duplicate_rule_id"
        super().__init__(f"duplicate key {key!r} at {pointer or '/'}")


def _load_no_duplicates(text: str) -> Any:
    """``json.loads`` that rejects duplicate object keys, tracking the pointer."""
    stack: list[str] = []

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise DuplicateJsonKey("/" + "/".join(stack) if stack else "", key)
            seen[key] = value
        return seen

    # ``object_pairs_hook`` fires innermost-first, so a pointer cannot be
    # reconstructed from the call order alone.  Two passes keep it simple: the
    # first finds *a* duplicate, the second locates it.
    try:
        return json.loads(text, object_pairs_hook=hook)
    except DuplicateJsonKey as exc:
        raise DuplicateJsonKey(_locate_duplicate(text, exc.key), exc.key) from None


def _locate_duplicate(text: str, key: str) -> str:
    """A best-effort JSON pointer to the object that carries a duplicate ``key``."""
    for container in ("steps", "rules"):
        probe = json.loads(text)
        section = probe.get(container) if isinstance(probe, dict) else None
        if isinstance(section, dict) and key in section:
            return f"/{container}"
        if isinstance(section, list) and any(
            isinstance(item, dict) and key in item for item in section
        ):
            return f"/{container}"
    return ""


def load_definition_json(text: str) -> PlaybookDefinition:
    """Parse artifact JSON text strictly.

    Package 3's ``ArtifactStore.load`` uses this rather than ``json.loads`` so a
    stored artifact cannot carry a duplicate key that a re-serialization would
    silently resolve one way and a hash check the other.  Activation health
    (``src/playbooks/activation.py:_load_definition``) reads stored artifacts
    through it for the same reason: every read of stored artifact text is this
    one parse, so nothing downstream can disagree with ``validate`` about what
    an artifact says.
    """
    return PlaybookDefinition.model_validate(_load_no_duplicates(text))


# --------------------------------------------------------------------------
# §4.8 — executable vs presentation fields
# --------------------------------------------------------------------------


def _unwrap(annotation: Any) -> list[Any]:
    """Every concrete type reachable from an annotation."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _unwrap(get_args(annotation)[0])
    if origin in (Union, UnionType):
        found: list[Any] = []
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            found.extend(_unwrap(arg))
        return found
    if origin in (list, set, tuple, frozenset):
        args = get_args(annotation)
        return _unwrap(args[0]) if args else []
    if origin is dict:
        args = get_args(annotation)
        return _unwrap(args[1]) if len(args) > 1 else []
    return [annotation]


def _presentation_fields() -> frozenset[tuple[str, str]]:
    """``(model name, field name)`` for every field annotated presentation-only.

    Derived by walking ``model_fields``, so a new presentation field is
    classified by adding the annotation — never by editing a list here.
    """
    found: set[tuple[str, str]] = set()
    for model in _reachable_models():
        for name, field in model.model_fields.items():
            extra = field.json_schema_extra
            if isinstance(extra, dict) and extra.get("executable") is False:
                found.add((model.__name__, name))
    return frozenset(found)


def _reachable_models() -> tuple[type[BaseModel], ...]:
    """Every V2 model reachable from :class:`PlaybookDefinition`."""
    seen: dict[str, type[BaseModel]] = {}
    stack: list[Any] = [PlaybookDefinition]
    while stack:
        model = stack.pop()
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        if model.__name__ in seen:
            continue
        seen[model.__name__] = model
        for field in model.model_fields.values():
            stack.extend(_unwrap(field.annotation))
    return tuple(seen.values())


PRESENTATION_FIELDS: Final[frozenset[tuple[str, str]]] = _presentation_fields()

#: Flattened ``"<Model>.<field>"`` names that are executable, for the diff (§4.8).
EXECUTABLE_FIELDS: Final[frozenset[str]] = frozenset(
    f"{model.__name__}.{name}"
    for model in _reachable_models()
    for name in model.model_fields
    if (model.__name__, name) not in PRESENTATION_FIELDS
)


def is_executable_path(pointer: str) -> bool:
    """Does a JSON pointer into an artifact address an executable field?

    Unresolvable pointers are executable: the diff must never call a change it
    does not understand "presentation only".
    """
    segments = [segment for segment in pointer.split("/") if segment != ""]
    cursors: list[Any] = [PlaybookDefinition]
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        next_cursors: list[Any] = []
        matched = False
        presentation_votes = 0
        for cursor in cursors:
            if not (isinstance(cursor, type) and issubclass(cursor, BaseModel)):
                continue
            field = cursor.model_fields.get(segment)
            if field is None:
                continue
            matched = True
            if (cursor.__name__, segment) in PRESENTATION_FIELDS:
                presentation_votes += 1
            next_cursors.extend(_unwrap(field.annotation))
        if not matched:
            # A container index or mapping key: stay on the element types.
            next_cursors = [
                member
                for cursor in cursors
                for member in _unwrap(cursor)
                if isinstance(member, type) and issubclass(member, BaseModel)
            ]
            if not next_cursors:
                return True
            cursors = next_cursors
            continue
        if last:
            return presentation_votes == 0
        cursors = next_cursors
    return True


__all__ = [
    "AGENT_TASK_RESULT_SCHEMA",
    "COMPILER_BUILD",
    "DELEGATED_PROFILE_INPUT",
    "EXECUTABLE_FIELDS",
    "FOREACH_RESULT_SCHEMA",
    "LLM_RESERVED_OUTCOMES",
    "MAX_EXCERPT_CHARS",
    "MAX_RULES",
    "MAX_STEPS",
    "PRESENTATION_FIELDS",
    "RESERVED_OUTCOMES",
    "RUNTIME_ERROR_KEY",
    "SCHEMA_GENERATION",
    "STEP_MODELS",
    "WAIT_BUSINESS_OUTCOMES",
    "WAIT_RESULT_SCHEMAS",
    "AgentTaskStep",
    "AgentTypeScope",
    "AiBudget",
    "CapabilityNarrowing",
    "CommandStep",
    "CompiledAgainst",
    "DecisionCase",
    "DecisionStep",
    "DuplicateJsonKey",
    "FailurePolicy",
    "ForEachStep",
    "LlmStep",
    "PlaybookDefinition",
    "ProjectScope",
    "RetryPolicy",
    "Rule",
    "Scope",
    "Sha256",
    "SourceRef",
    "Step",
    "StepBase",
    "StepKind",
    "SystemScope",
    "TerminalOutcome",
    "TerminalStep",
    "ToolUsePolicy",
    "Trigger",
    "V2Base",
    "WaitKind",
    "WaitStep",
    "artifact_sha256",
    "business_outcomes",
    "canonical_bytes",
    "contract_fingerprint",
    "is_executable_path",
    "load_definition_json",
    "normalize_source",
    "referenced_profile_ids",
    "reserved_outcomes_for",
    "result_schema_for",
    "scope_from_v1",
    "scope_to_v1",
    "source_digest",
    "step_profile_ids",
    "step_targets",
    "step_values",
    "truncate_excerpt",
]
