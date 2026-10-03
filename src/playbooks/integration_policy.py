"""Declarative, bounded integration decisions embedded in reviewed V2 artifacts.

The table uses the existing V2 condition/value language. It chooses one typed
primitive per visit; every closed outcome chooses a schedule, never another
primitive. There are no live configuration or activation reads here.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from src.integration.models import Fence, RequiredCheckSet
from src.integration.subjects import (
    PRIMITIVE_ARGS,
    PRIMITIVE_OUTCOMES,
    UNKNOWN,
    Decision,
    GateArgs,
    PolicyArtifactPin,
    Primitive,
    PrimitiveOutcome,
    Subject,
    SubjectFacts,
    SubjectKind,
    SubjectPhase,
    SubjectSchedule,
)
from src.playbooks.expressions import (
    BindingRef,
    Comparison,
    Condition,
    Identifier,
    LiteralValue,
    ResolutionScope,
    V2Base,
    Value,
    check_expression_depth,
    condition_values,
    evaluate_condition,
    resolve_value,
    walk_value,
)

if TYPE_CHECKING:
    from src.playbooks.definition import PlaybookDefinition


class IntegrationPolicyFacts(SubjectFacts):
    """Additive policy observation supplied by the root primitive adapter.

    A repository publisher is distinct from the repair task's writer lease.
    Until the observer can prove this fence, publication remains a bounded
    wait. The extra fact is included in the inherited binding and digest.
    """

    publisher_fence: Fence | None = None


class OutcomeSchedule(V2Base):
    kind: Literal["progress", "backoff", "wait", "hold", "close"]
    seconds: int | None = Field(default=None, gt=0)
    ceiling_seconds: int | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, min_length=1)
    phase: SubjectPhase | None = None

    @model_validator(mode="after")
    def _shape(self) -> OutcomeSchedule:
        if self.kind in {"wait", "backoff"}:
            if self.seconds is None:
                raise ValueError("a wait/backoff route requires a finite seconds bound")
        elif self.seconds is not None:
            raise ValueError("seconds belongs only to wait/backoff routes")
        if self.kind == "backoff":
            if self.ceiling_seconds is None or self.ceiling_seconds < self.seconds:
                raise ValueError("backoff requires an ordered finite ceiling")
        elif self.ceiling_seconds is not None:
            raise ValueError("ceiling_seconds belongs only to backoff")
        if self.kind in {"wait", "close"} and not self.reason:
            raise ValueError("wait/close routes require a reason")
        if self.kind == "close" and self.phase != SubjectPhase.DONE:
            raise ValueError("a close route must enter done")
        if self.kind != "close" and self.phase == SubjectPhase.DONE:
            raise ValueError("only a close route may enter done")
        return self


class PolicyAction(V2Base):
    primitive: Primitive
    inputs: dict[str, Value] = Field(default_factory=dict)
    outcomes: dict[str, OutcomeSchedule]
    answers: dict[str, OutcomeSchedule] | None = None
    messages: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _closed(self) -> PolicyAction:
        expected = PRIMITIVE_OUTCOMES[self.primitive] | {UNKNOWN}
        if set(self.outcomes) != expected:
            raise ValueError(
                f"{self.primitive.value} needs exactly the closed outcomes {sorted(expected)}; "
                f"missing {sorted(expected - self.outcomes.keys())}, "
                f"unknown {sorted(self.outcomes.keys() - expected)}"
            )
        model = PRIMITIVE_ARGS[self.primitive]
        fields = {name: field for name, field in model.model_fields.items() if name != "primitive"}
        extra = self.inputs.keys() - fields.keys()
        missing = {
            name for name, field in fields.items() if field.is_required()
        } - self.inputs.keys()
        if extra or missing:
            raise ValueError(
                f"{self.primitive.value} inputs: missing {sorted(missing)}, extra {sorted(extra)}"
            )
        for value in self.inputs.values():
            check_expression_depth(value)
        for name, value in self.inputs.items():
            if not any(isinstance(node, BindingRef) for node in walk_value(value)):
                TypeAdapter(fields[name].rebuild_annotation()).validate_python(
                    resolve_value(value, ResolutionScope())
                )
        # Fully literal calls are checked with the same model an adapter gets.
        if not any(
            isinstance(node, BindingRef)
            for value in self.inputs.values()
            for node in walk_value(value)
        ):
            model.model_validate({"primitive": self.primitive, **self.resolve(ResolutionScope())})
        if self.primitive == Primitive.WAIT:
            self._literal("seconds", int)
            if self._literal("seconds", int) <= 0:
                raise ValueError("wait seconds must be positive")
            self._literal("reason", str)
            waiting = self.outcomes["waiting"]
            if waiting.kind != "wait" or waiting.seconds != self._literal("seconds", int):
                raise ValueError("the waiting route must preserve the wait call's bound")
        if self.primitive == Primitive.GATE:
            # The human question, choices and default are reviewed policy, not
            # an unbounded value inferred from a runtime observation.
            config = {name: self._literal(name) for name in self.inputs}
            gate = GateArgs.model_validate(config)
            if not gate.question.strip() or any(not choice.strip() for choice in gate.choices):
                raise ValueError("a human gate must name its decision and non-empty choices")
            if any(self.outcomes[name].kind != "hold" for name in ("created", "reused")):
                raise ValueError("created/reused gates must hold until the human decision")
            if self.answers is None or set(self.answers) != set(gate.choices):
                raise ValueError("a gate needs a schedule for every declared answer")
        elif self.answers is not None:
            raise ValueError("answer schedules belong only to human gates")
        if (
            any(route.kind == "hold" for route in self.outcomes.values())
            and self.primitive != Primitive.GATE
        ):
            raise ValueError("only an explicit gate may produce a held schedule")
        if self.outcomes[UNKNOWN].kind not in {"wait", "backoff"}:
            raise ValueError("unknown(reason) must have a bounded wait/backoff route")
        return self

    def _literal(self, name: str, kind: type | None = None) -> Any:
        value = self.inputs.get(name)
        if not isinstance(value, LiteralValue):
            raise ValueError(f"{self.primitive.value}.{name} must be a reviewed literal")
        if kind is not None and type(value.value) is not kind:
            raise ValueError(f"{self.primitive.value}.{name} must be {kind.__name__}")
        return value.value

    def resolve(self, scope: ResolutionScope) -> dict[str, Any]:
        return {name: resolve_value(value, scope) for name, value in self.inputs.items()}


class PolicyCase(V2Base):
    rule: Identifier
    when: Condition
    action: Identifier


class SubjectDecisionTable(V2Base):
    cases: list[PolicyCase] = Field(min_length=1, max_length=100)
    actions: dict[Identifier, PolicyAction] = Field(min_length=1, max_length=100)
    default: Identifier
    required_checks: RequiredCheckSet | None = None

    @model_validator(mode="after")
    def _coverage(self) -> SubjectDecisionTable:
        names = [case.rule for case in self.cases]
        if len(names) != len(set(names)) or "default" in names:
            raise ValueError("decision rules must be distinct and cannot be named default")
        if not any(_is_overdue(case.when) for case in self.cases):
            raise ValueError("a table needs an explicit positive s.wait_overdue branch")
        targets = {case.action for case in self.cases} | {self.default}
        if targets != self.actions.keys():
            raise ValueError("every action must be referenced, and every target must exist")
        if self.actions[self.default].primitive != Primitive.WAIT:
            raise ValueError("the table default must be a bounded wait")
        first = self.cases[0]
        if (
            not _is_true(first.when, "held")
            or self.actions[first.action].primitive != Primitive.WAIT
        ):
            raise ValueError("the first case must wait on s.held == true (binding human holds)")
        if any(
            route.phase is not None or route.kind not in {"wait", "backoff"}
            for route in self.actions[first.action].outcomes.values()
        ):
            raise ValueError("a binding human hold must preserve the phase with a bounded wait")
        for case in self.cases:
            check_expression_depth(case.when)
            if not any(isinstance(value, BindingRef) for value in condition_values(case.when)):
                raise ValueError("a decision case must test observed facts, not a constant")
        return self


def _is_overdue(condition: Condition) -> bool:
    # A standalone true comparison is demonstrably reachable; mere mention
    # of the flag (or a false/contradictory branch) is not coverage.
    return _is_true(condition, "wait_overdue")


def _is_true(condition: Condition, path: str) -> bool:
    if not isinstance(condition, Comparison) or condition.op != "eq":
        return False
    left, right = condition.left, condition.right
    return (
        isinstance(left, BindingRef)
        and left.binding == "s"
        and left.path == path
        and isinstance(right, LiteralValue)
        and right.value is True
    )


class IntegrationPolicy(V2Base):
    version: Literal[1] = 1
    max_wait_seconds: int = Field(gt=0, le=86400)
    tables: dict[SubjectKind, SubjectDecisionTable] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def _bounded(self) -> IntegrationPolicy:
        for table in self.tables.values():
            for action in table.actions.values():
                if (
                    action.primitive == Primitive.WAIT
                    and action._literal("seconds", int) > self.max_wait_seconds
                ):
                    raise ValueError("wait call exceeds max_wait_seconds")
                for route in [*action.outcomes.values(), *(action.answers or {}).values()]:
                    if (route.ceiling_seconds or route.seconds or 0) > self.max_wait_seconds:
                        raise ValueError("outcome wait/backoff exceeds max_wait_seconds")
                values = [node for value in action.inputs.values() for node in walk_value(value)]
                _check_reads(values)
            for case in table.cases:
                _check_reads(condition_values(case.when))
        return self


def _check_reads(values: list[Any]) -> None:
    for value in values:
        if value.type.endswith("_ref") and not isinstance(value, BindingRef):
            raise ValueError("policy expressions read only s and subject bindings")
        if isinstance(value, BindingRef):
            if value.binding not in {"s", "subject"}:
                raise ValueError(f"unknown policy binding {value.binding!r}")
            # Check the typed observation/subject paths, including nullable
            # nested objects. Runtime validation still checks resolved args.
            root = IntegrationPolicyFacts if value.binding == "s" else Subject
            derived = {
                "ci_state",
                "conflict_count",
                "writer_status",
                "budget_expired",
                "budget_exhausted",
                "held",
                "merge_members",
                "tested_head",
                "next_writer_ordinal",
                "conflict_member",
            }
            if value.binding == "s" and value.path in derived:
                continue
            if value.binding == "s" and value.path and value.path.startswith("tested_head."):
                # These projections are built by the evaluator; validate via
                # their real object schemas below rather than arbitrary paths.
                from src.integration.subjects import HeadIdentity

                root = HeadIdentity
                path = value.path.split(".", 1)[1]
            else:
                path = value.path
            if path:
                schema = _model_schema(root)
                node = schema
                for part in path.split("."):
                    if "anyOf" in node:
                        node = next(
                            (item for item in node["anyOf"] if item.get("type") != "null"), {}
                        )
                    if "$ref" in node:
                        node = schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
                    node = node.get("properties", {}).get(part)
                    if node is None:
                        raise ValueError(f"unknown policy path {value.binding}.{value.path}")


@lru_cache(maxsize=4)
def _model_schema(model: type) -> dict[str, Any]:
    return model.model_json_schema()


_BLOCK = re.compile(r"^```integration-policy\s*\n(.*?)^```\s*$", re.M | re.S)


def policy_from_markdown(markdown: str) -> IntegrationPolicy | None:
    """Compile only the author's literal JSON block; reject duplicate keys."""
    blocks = _BLOCK.findall(markdown)
    openings = re.findall(r"^```integration-policy\b", markdown, re.M)
    if len(openings) != len(blocks):
        raise ValueError("unterminated integration-policy block")
    if not blocks:
        return None
    if len(blocks) != 1:
        raise ValueError("exactly one integration-policy block is allowed")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate integration policy key {key!r}")
            result[key] = value
        return result

    return IntegrationPolicy.model_validate(json.loads(blocks[0], object_pairs_hook=unique))


class CompiledIntegrationPolicy:
    """Pure evaluator for an already verified immutable V2 artifact."""

    def __init__(self, definition: PlaybookDefinition) -> None:
        if definition.integration_policy is None:
            raise ValueError("artifact has no integration decision table")
        self.policy = definition.integration_policy
        self.pin = PolicyArtifactPin(
            playbook_id=definition.id, artifact_sha256=definition.artifact_sha256()
        )

    def evaluate(self, subject: Subject, facts: SubjectFacts) -> Decision:
        if subject.policy != self.pin:
            raise ValueError("policy artifact does not match the subject's immutable pin")
        if (facts.subject_id, facts.subject_version, facts.kind, facts.phase) != (
            subject.id,
            subject.version,
            subject.kind,
            subject.phase,
        ):
            raise ValueError("observation does not match the subject identity/version/phase")
        table = self.policy.tables[subject.kind]
        binding = facts.binding()
        binding.setdefault("publisher_fence", None)
        publisher = binding["publisher_fence"]
        if publisher is not None and publisher["target"] != {
            "repository_id": subject.repository_id,
            "branch": subject.target_ref,
        }:
            raise ValueError("observed publisher fence belongs to another repository/ref")
        binding["next_writer_ordinal"] = facts.budget.ordinal + 1 if facts.budget else 1
        binding["tested_head"] = (
            facts.tested_head.model_dump(mode="json") if facts.tested_head else None
        )
        # Lists are not addressable by path; a table that ejects or scopes a
        # writer names the earliest conflicting member in manifest order.
        conflicted = {conflict.member_task_id for conflict in facts.conflicts}
        binding["conflict_member"] = next(
            (member.task_id for member in facts.members if member.task_id in conflicted), None
        )
        binding["merge_members"] = [
            {"task_id": member.task_id, "head_sha": member.head_sha, "base_sha": member.base_sha}
            for member in facts.members
            if member.head_sha is not None and not member.ejected
        ]
        scope = ResolutionScope(bindings={"s": binding, "subject": subject.model_dump(mode="json")})
        rule, action = "default", table.actions[table.default]
        for case in table.cases:
            if evaluate_condition(case.when, scope):
                rule, action = case.rule, table.actions[case.action]
                break
        request = PRIMITIVE_ARGS[action.primitive].model_validate(
            {"primitive": action.primitive, **action.resolve(scope)}
        )
        return Decision(
            subject_id=subject.id,
            subject_version=subject.version,
            policy=self.pin,
            rule=rule,
            facts_digest=facts.digest(),
            request=request,
            messages=action.messages,
        )

    def _route(
        self, subject: Subject, decision: Decision, outcome: PrimitiveOutcome
    ) -> OutcomeSchedule:
        if (
            decision.policy != self.pin
            or subject.policy != self.pin
            or (decision.subject_id, decision.subject_version) != (subject.id, subject.version)
        ):
            raise ValueError("decision does not match the subject/pinned policy")
        table = self.policy.tables[subject.kind]
        action_id = (
            table.default
            if decision.rule == "default"
            else next(case.action for case in table.cases if case.rule == decision.rule)
        )
        action = table.actions[action_id]
        if action.primitive != decision.primitive or outcome.primitive != decision.primitive:
            raise ValueError("outcome does not belong to the decided primitive")
        if action.primitive == Primitive.GATE and outcome.outcome == "answered":
            answer = outcome.detail.get("choice")
            if answer not in (action.answers or {}):
                raise ValueError("gate answered with an undeclared choice")
            return action.answers[answer]
        return action.outcomes[outcome.outcome]

    def phase(
        self, subject: Subject, decision: Decision, outcome: PrimitiveOutcome
    ) -> SubjectPhase | None:
        return self._route(subject, decision, outcome).phase

    def schedule(
        self, subject: Subject, decision: Decision, outcome: PrimitiveOutcome, *, now: float
    ) -> SubjectSchedule:
        route = self._route(subject, decision, outcome)
        bound = min(subject.schedule.max_wait_seconds, self.policy.max_wait_seconds)
        args = {"now": now, "max_wait_seconds": bound}
        if route.kind == "progress":
            if outcome.primitive == Primitive.GATE and outcome.outcome == "answered":
                gate_id = outcome.detail.get("gate_id")
                if not isinstance(gate_id, str) or not gate_id:
                    raise ValueError("an applied gate answer needs its exact gate_id")
                # Keep the answer observable for the table's next action.
                # That action clears the gate while writing its own schedule.
                return SubjectSchedule.hold(**args, gate_id=gate_id, revisit_at=now)
            return SubjectSchedule.progress(**args)
        if route.kind == "backoff":
            return SubjectSchedule.backoff(
                **args,
                refusal_streak=subject.schedule.refusal_streak,
                base_seconds=route.seconds,
                ceiling_seconds=route.ceiling_seconds,
            )
        if route.kind == "wait":
            return SubjectSchedule.wait(**args, until=now + route.seconds, reason=route.reason)
        if route.kind == "close":
            return SubjectSchedule.close(**args, reason=route.reason)
        gate_id = outcome.detail.get("gate_id")
        if not isinstance(gate_id, str) or not gate_id:
            raise ValueError("gate creation/reuse needs an exact gate_id before holding")
        request = decision.request
        assert isinstance(request, GateArgs)
        revisit_at = None
        if request.default_after_seconds is not None and outcome.outcome != "answered":
            revisit_at = outcome.detail.get("timeout_at")
            if revisit_at is None and outcome.outcome == "created":
                revisit_at = now + request.default_after_seconds
            if type(revisit_at) not in {int, float} or not math.isfinite(revisit_at):
                raise ValueError("a reused default gate needs its persisted finite timeout_at")
        return SubjectSchedule.hold(
            **args,
            gate_id=gate_id,
            revisit_at=revisit_at,
        )
