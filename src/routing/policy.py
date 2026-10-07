"""The routing policy document (mandatory-routing spec §6.3).

The routing playbook carries its policy as a fenced YAML block, and every
``task_route_plan`` step passes that block's text verbatim as the ``policy``
string.  :func:`parse_policy` turns the text into a validated
:class:`RoutingPolicy` and its digest.  Nothing here reads a database or a
profile: the planner (:mod:`src.routing.planner`) applies the policy to one
capacity snapshot.

Profiles are named by ``(class, harness)``, never by rung id, so a policy
survives an install that derives different rungs.

Two kinds of lane share the ``lanes`` map:

* a **design lane** (``code-design``, ``art-design``, ``design-review``) has
  a ``class`` and is named by a kind or an origin (``lane: code-design``);
* a **narrow lane** (``narrow``, ``narrow-unverified-model``) has a
  ``classes`` map and ``requires`` flags, and is reached only through a kind
  marked ``narrow: true`` whose classification satisfies every flag.

A harness a narrow lane matches is reachable only through a narrow lane: the
general candidates of a task never include it.  That is what keeps an
integration repair (``origins: {integration_repair: {narrow: false}}``) off
OpenCode although OpenCode may have a rung at the repair's class.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

#: The classification flags a narrow lane may require (§6.5).
CLASSIFICATION_FLAGS: frozenset[str] = frozenset(
    {"narrow", "test_verified", "independent_verifier"}
)


class PolicyError(ValueError):
    """The policy text is not YAML, or does not satisfy the schema."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class KindRule(_Strict):
    """``kinds.<kind>``: the class a kind gets without a hint, and its bounds."""

    class_: str = Field(alias="class")
    max_class: str | None = None
    lane: str | None = None
    narrow: bool = False
    prefer_harnesses: tuple[str, ...] = ()


class OriginRule(_Strict):
    """``origins.<created_by_kind>``: keys merged over the kind's rule."""

    class_: str | None = Field(default=None, alias="class")
    max_class: str | None = None
    lane: str | None = None
    narrow: bool | None = None
    prefer_harnesses: tuple[str, ...] | None = None


class Lane(_Strict):
    """``lanes.<name>``: a design lane (``class``) or a narrow lane (``classes``)."""

    class_: str | None = Field(default=None, alias="class")
    #: Harness ids or shell glob patterns, used for lane admission and
    #: exclusion from general candidates.
    harnesses: tuple[str, ...] = Field(min_length=1)
    #: A design lane lists the harnesses tried first; a narrow lane says
    #: ``true`` to make its candidates the preferred tier.
    prefer: tuple[str, ...] | bool = ()
    hold: bool = False
    #: Narrow lanes only: task class -> the class the lane runs it at.
    classes: dict[str, str] | None = None
    requires: tuple[str, ...] = ()

    @property
    def narrow(self) -> bool:
        return self.classes is not None

    @property
    def preferred_harnesses(self) -> tuple[str, ...]:
        return self.prefer if isinstance(self.prefer, tuple) else ()

    @model_validator(mode="after")
    def _shape(self) -> Lane:
        if self.narrow:
            if self.class_ is not None:
                raise ValueError("a narrow lane maps classes; it takes no 'class'")
            if not self.requires:
                raise ValueError("a narrow lane needs 'requires'")
            if isinstance(self.prefer, tuple) and self.prefer:
                raise ValueError("a narrow lane's 'prefer' is true or false")
        else:
            if self.class_ is None:
                raise ValueError("a design lane needs 'class'")
            if self.requires:
                raise ValueError("'requires' belongs to a narrow lane")
            if self.prefer is True:
                raise ValueError("a design lane's 'prefer' lists harnesses")
            unknown = set(self.preferred_harnesses) - set(self.harnesses)
            if unknown:
                raise ValueError(f"'prefer' names harnesses outside the lane: {sorted(unknown)}")
        unknown_flags = set(self.requires) - CLASSIFICATION_FLAGS
        if unknown_flags:
            raise ValueError(f"unknown 'requires' flags: {sorted(unknown_flags)}")
        return self


class Reserved(_Strict):
    """``reserved[]``: a (class, harness) cell kept for the lanes listed."""

    class_: str = Field(alias="class")
    harness: str
    only_lanes: tuple[str, ...] = Field(min_length=1)


class BenchmarkArm(_Strict):
    """An explicitly allowlisted, pinned route for a named benchmark arm."""

    class_: str = Field(alias="class")
    harness: str = Field(min_length=1)
    requested_model: str = Field(min_length=1)
    observed_models: tuple[str, ...] = Field(min_length=1)


class Balance(_Strict):
    """``balance``: the weights of the load score (§6.4 step 5)."""

    harness_weights: dict[str, float] = Field(default_factory=dict)
    usage_soft_percent: float = Field(default=80.0, ge=0, lt=100)
    usage_floor_factor: float = Field(default=0.1, gt=0, le=1)
    degraded_factor: float = Field(default=0.5, gt=0, le=1)
    tie_order: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _weights(self) -> Balance:
        bad = sorted(h for h, weight in self.harness_weights.items() if weight <= 0)
        if bad:
            raise ValueError(f"harness_weights must be positive: {bad}")
        return self

    def weight(self, harness: str) -> float:
        return float(self.harness_weights.get(harness, 1.0))


class LocalModels(_Strict):
    """``local_models``: the only work a self-hosted model may take (§6.4 step 2).

    A profile is local when its harness declares a local provider
    (:data:`src.routing.planner.LOCAL_MODEL_PROVIDERS`) or its harness is
    named under ``harnesses``.  A local profile is a candidate only for a task
    whose priority is below ``below_priority``, that is not one of the
    ``train_kinds`` delivered through an integration train, and that no other
    task waits on (unless ``allow_blocking``).  A policy without the block
    gets these defaults.
    """

    harnesses: tuple[str, ...] = ()
    below_priority: int = Field(default=150, ge=1)
    train_kinds: tuple[str, ...] = ("bugfix",)
    allow_blocking: bool = False


class RoutingPolicy(_Strict):
    """The whole ``## Routing policy`` block."""

    version: int
    class_order: tuple[str, ...] = Field(min_length=1)
    default_kind: str
    kinds: dict[str, KindRule] = Field(min_length=1)
    origins: dict[str, OriginRule] = Field(default_factory=dict)
    lanes: dict[str, Lane] = Field(default_factory=dict)
    reserved: tuple[Reserved, ...] = ()
    benchmark_arms: dict[str, BenchmarkArm] = Field(default_factory=dict)
    balance: Balance = Field(default_factory=Balance)
    local_models: LocalModels = Field(default_factory=LocalModels)

    @model_validator(mode="after")
    def _references(self) -> RoutingPolicy:
        if self.version != 1:
            raise ValueError(f"unsupported policy version {self.version}; this build reads 1")
        if len(set(self.class_order)) != len(self.class_order):
            raise ValueError("class_order repeats a class")
        if self.default_kind not in self.kinds:
            raise ValueError(f"default_kind '{self.default_kind}' has no rule under kinds")
        known = set(self.class_order)

        def check_class(where: str, value: str | None) -> None:
            if value is not None and value not in known:
                raise ValueError(f"{where} names class '{value}', which is not in class_order")

        def check_lane(where: str, value: str | None) -> None:
            if value is None:
                return
            lane = self.lanes.get(value)
            if lane is None:
                raise ValueError(f"{where} names lane '{value}', which is not under lanes")
            if lane.narrow:
                raise ValueError(
                    f"{where} names narrow lane '{value}'; narrow lanes are reached "
                    "through 'narrow: true', never named"
                )

        for name, rule in self.kinds.items():
            check_class(f"kinds.{name}.class", rule.class_)
            check_class(f"kinds.{name}.max_class", rule.max_class)
            check_lane(f"kinds.{name}.lane", rule.lane)
            if rule.max_class is not None and self.rank(rule.class_) > self.rank(rule.max_class):
                raise ValueError(f"kinds.{name}: class ranks above max_class")
        for name, rule in self.origins.items():
            check_class(f"origins.{name}.class", rule.class_)
            check_class(f"origins.{name}.max_class", rule.max_class)
            check_lane(f"origins.{name}.lane", rule.lane)
        for name, lane in self.lanes.items():
            check_class(f"lanes.{name}.class", lane.class_)
            for source, target in (lane.classes or {}).items():
                check_class(f"lanes.{name}.classes", source)
                check_class(f"lanes.{name}.classes", target)
        for index, cell in enumerate(self.reserved):
            check_class(f"reserved[{index}].class", cell.class_)
            for lane in cell.only_lanes:
                if lane not in self.lanes:
                    raise ValueError(f"reserved[{index}] names lane '{lane}', which is not under lanes")
        for name, arm in self.benchmark_arms.items():
            if not name or not name.strip() or ":" in name:
                raise ValueError(f"invalid benchmark arm name {name!r}")
            check_class(f"benchmark_arms.{name}.class", arm.class_)
        return self

    def rank(self, class_id: str) -> int:
        """Position on ``class_order``; a higher rank is a more capable class."""
        return self.class_order.index(class_id)

    def narrow_lanes(self) -> list[tuple[str, Lane]]:
        """Every narrow lane, in declaration order."""
        return [(name, lane) for name, lane in self.lanes.items() if lane.narrow]

    def narrow_harnesses(self) -> frozenset[str]:
        """Harnesses reachable only through a narrow lane (module docstring)."""
        return frozenset(h for _name, lane in self.narrow_lanes() for h in lane.harnesses)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        )


def policy_digest(policy: RoutingPolicy) -> str:
    """``sha256:<hex>`` of the policy's canonical JSON (§6.3)."""
    return "sha256:" + hashlib.sha256(policy.canonical_json().encode("utf-8")).hexdigest()


def parse_policy(text: Any) -> tuple[RoutingPolicy, str]:
    """Validate *text* as a routing policy; return it with its digest.

    Raises :class:`PolicyError` naming the first problem, so a
    ``task_route_plan`` step answers ``rejected`` with a readable error
    rather than a traceback.
    """
    if not isinstance(text, str) or not text.strip():
        raise PolicyError("policy must be the non-empty YAML text of the routing policy block")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError("policy must be a YAML mapping")
    try:
        policy = RoutingPolicy.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "policy"
        raise PolicyError(f"invalid routing policy at {where}: {first.get('msg')}") from exc
    return policy, policy_digest(policy)


__all__ = [
    "CLASSIFICATION_FLAGS",
    "Balance",
    "KindRule",
    "Lane",
    "LocalModels",
    "OriginRule",
    "PolicyError",
    "Reserved",
    "RoutingPolicy",
    "parse_policy",
    "policy_digest",
]
