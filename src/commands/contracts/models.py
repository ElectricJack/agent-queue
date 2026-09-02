"""Typed, fingerprinted command contracts for Playbook V2."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Final, Generic, Literal, Mapping, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.profiles.capabilities import WILDCARD_CHARS

SCHEMA_GENERATION: Final[int] = 1
REDACTED: Final[str] = "[redacted]"
RESERVED_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"contract_violation", "unauthorized", "runtime_error"}
)


class CommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CommandValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


A = TypeVar("A", bound=CommandArgs)
R = TypeVar("R", bound=CommandValue)


class SideEffectClass(StrEnum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    LINK = "link"
    RESOLVE = "resolve"
    COMPOSITE = "composite"


class OutcomeClass(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class OutcomeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    classification: OutcomeClass


class IdempotencySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["none", "natural", "keyed"]
    key_field: str | None = None

    @model_validator(mode="after")
    def _key_shape(self) -> "IdempotencySpec":
        if (self.mode == "keyed") != (self.key_field is not None):
            raise ValueError("key_field is required iff idempotency mode is keyed")
        return self


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
    model_config = ConfigDict(extra="forbid", frozen=True)
    arg_present: str | None = None
    arg_equals: tuple[str, Any] | None = None


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
    fields_arg: str | None = None


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
    CreateClause | ReuseClause | CreateOrReuseClause | UpdateClause | LinkClause | ResolveClause | ReadClause,
    Field(discriminator="kind"),
]
EFFECT_CLAUSE_TYPES: Final[tuple[type[_Clause], ...]] = (
    CreateClause, ReuseClause, CreateOrReuseClause, UpdateClause, LinkClause, ResolveClause, ReadClause,
)


class ExecutionContract(BaseModel, Generic[A, R]):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)
    name: str
    args_model: type[A]
    result_model: type[R]
    outcomes: tuple[OutcomeSpec, ...]
    capability: str
    side_effect: SideEffectClass
    idempotency: IdempotencySpec
    retry_safe: bool
    timeout_seconds: int | None = None
    effects: tuple[EffectClause, ...] = ()
    sensitive_args: frozenset[str] = frozenset()
    sensitive_result_fields: frozenset[str] = frozenset()
    receipt_projection: tuple[str, ...] = ()
    supports_preview: bool = False

    @model_validator(mode="after")
    def _validate_contract(self) -> "ExecutionContract[A, R]":
        names = [outcome.name for outcome in self.outcomes]
        if not names or not any(o.classification is OutcomeClass.SUCCESS for o in self.outcomes):
            raise ValueError("contracts require at least one successful outcome")
        if len(names) != len(set(names)) or any(name in RESERVED_OUTCOMES for name in names):
            raise ValueError("outcomes must be unique and may not be reserved")
        if not self.capability or any(char in self.capability for char in WILDCARD_CHARS):
            raise ValueError("capability must be non-empty and contain no wildcard")
        args = self.args_model.model_fields
        values = self.result_model.model_fields
        if self.idempotency.key_field and self.idempotency.key_field not in args:
            raise ValueError("idempotency key_field is not an argument")
        for name in self.sensitive_args:
            if name not in args:
                raise ValueError(f"sensitive argument {name!r} is not an argument")
        for name in set(self.sensitive_result_fields) | set(self.receipt_projection):
            if name not in values:
                raise ValueError(f"result field {name!r} is not declared")
        for clause in self.effects:
            predicate_names = [clause.when.arg_present]
            if clause.when.arg_equals:
                predicate_names.append(clause.when.arg_equals[0])
            predicate_names.extend(
                getattr(clause, attr, None)
                for attr in ("key_arg", "fields_arg", "from_arg", "to_arg", "relation_arg", "target_arg")
            )
            if any(name is not None and name not in args for name in predicate_names):
                raise ValueError("effect clause references an unknown argument")
        return self


class CommandPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str
    summary: str
    arg_labels: dict[str, str] = {}
    outcome_labels: dict[str, str] = {}
    result_labels: dict[str, str] = {}
    subject_labels: dict[str, str] = {}
    help_url: str | None = None


class CommandContract(BaseModel, Generic[A, R]):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)
    execution: ExecutionContract[A, R]
    presentation: CommandPresentation

    @property
    def name(self) -> str:
        return self.execution.name

    def fingerprint(self) -> str:
        return execution_fingerprint(self.execution)


class UnknownOutcome(ValueError):
    pass


class CommandResult(BaseModel, Generic[R]):
    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: str
    value: R
    summary: str

    def classification(self, contract: CommandContract[Any, R]) -> OutcomeClass:
        if self.outcome in RESERVED_OUTCOMES:
            return OutcomeClass.FAILURE
        for spec in contract.execution.outcomes:
            if spec.name == self.outcome:
                return spec.classification
        raise UnknownOutcome(self.outcome)


_PRESENTATION_SCHEMA_KEYS: Final[frozenset[str]] = frozenset(
    {"title", "description", "examples", "deprecated", "$comment", "readOnly", "writeOnly"}
)


def _strip(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in _PRESENTATION_SCHEMA_KEYS:
            continue
        result[key] = sorted(item) if key == "required" and isinstance(item, list) else _strip(item)
    return result


def canonical_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    return _strip(model.model_json_schema(mode="validation", ref_template="#/$defs/{model}"))


def canonical_execution_document(ec: ExecutionContract[Any, Any]) -> dict[str, Any]:
    return {
        "schema_generation": SCHEMA_GENERATION, "name": ec.name,
        "args_schema": canonical_json_schema(ec.args_model), "result_schema": canonical_json_schema(ec.result_model),
        "outcomes": [{"name": o.name, "classification": o.classification.value} for o in sorted(ec.outcomes, key=lambda o: o.name)],
        "capability": ec.capability, "side_effect": ec.side_effect.value,
        "idempotency": {"mode": ec.idempotency.mode, "key_field": ec.idempotency.key_field},
        "retry_safe": ec.retry_safe, "timeout_seconds": ec.timeout_seconds,
        "effects": [clause.canonical() for clause in ec.effects],
        "sensitive_args": sorted(ec.sensitive_args), "sensitive_result_fields": sorted(ec.sensitive_result_fields),
        "receipt_projection": list(ec.receipt_projection), "supports_preview": ec.supports_preview,
    }


def execution_fingerprint(ec: ExecutionContract[Any, Any]) -> str:
    blob = json.dumps(canonical_execution_document(ec), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def redact_args(contract: CommandContract[Any, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    return {key: REDACTED if key in contract.execution.sensitive_args else value for key, value in args.items()}


def redact_result(contract: CommandContract[Any, Any], value: CommandValue) -> dict[str, Any]:
    raw = value.model_dump(mode="json")
    return {key: REDACTED if key in contract.execution.sensitive_result_fields else item for key, item in raw.items()}
