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
  marked ``narrow: true`` whose classification satisfies every flag.  It may
  also cap the classified risk (``max_risk``); an unknown risk does not meet
  the cap.

A task's priority is not a routing input: no lane and no ``local_models``
gate reads it, so priority never decides which model runs a task.

The optional ``risk`` map (keyed by :data:`RISK_LEVELS`) is a safety floor
for a classified risk: ``min_class`` raises the task's class (``relax`` names
a lower floor for a classification with every flag it ``requires``), and
``harnesses`` restricts every candidate to those selectors.  A policy that
uses none of these keys plans, and digests, exactly as it did before they
existed.

A harness a narrow lane matches is reachable only through a narrow lane: the
general candidates of a task never include it.  That is what keeps an
integration repair (``origins: {integration_repair: {narrow: false}}``) off
OpenCode although OpenCode may have a rung at the repair's class.

A lane names harnesses by selector: an exact harness id, or a prefix with a
single trailing ``*`` (``opencode-zen*``).  Anything else is a policy error,
so a malformed exclusion stops the router instead of silently admitting the
harness it meant to exclude.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any, ClassVar

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    model_serializer,
    model_validator,
)

#: The classification flags a narrow lane may require (§6.5).
CLASSIFICATION_FLAGS: frozenset[str] = frozenset(
    {"narrow", "test_verified", "independent_verifier"}
)

#: The risk levels a classification may answer, least to most risky.
RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high", "very_high")


def risk_rank(level: str) -> int:
    """Position on :data:`RISK_LEVELS`; a higher rank is a riskier task."""
    return RISK_LEVELS.index(level)


#: A lane's harness selector: an exact harness id, optionally ending in one ``*``.
_SELECTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\*?")


class PolicyError(ValueError):
    """The policy text is not YAML, or does not satisfy the schema."""


def selector_matches(harness: str, selector: str) -> bool:
    """Whether *harness* is the id *selector* names, or has its ``*`` prefix."""
    if selector.endswith("*"):
        return harness.startswith(selector[:-1])
    return harness == selector


#: Priority once gated the cheap lanes and the local models; it no longer
#: routes at all (rev-wise-impact, 2026-10-09).
_PRIORITY_RETIRED = (
    "a task's priority does not route it; delete the key (a lane admits work "
    "by its class, flags and risk)"
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    #: Optional keys a dump omits while they hold their default (``None`` or
    #: empty), so a policy that does not use them keeps the canonical JSON, and
    #: so the digest, it had before they existed.
    _OMIT_AT_DEFAULT: ClassVar[tuple[str, ...]] = ()
    #: Keys a policy may no longer write -> what replaced them.  Refused by
    #: name, so an old policy fails loudly instead of reading as unknown keys.
    _RETIRED: ClassVar[dict[str, str]] = {}

    @model_validator(mode="before")
    @classmethod
    def _refuse_retired(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, replacement in cls._RETIRED.items():
                if key in data:
                    raise ValueError(f"'{key}' is retired: {replacement}")
        return data

    @model_serializer(mode="wrap")
    def _omit_unused(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        for key in self._OMIT_AT_DEFAULT:
            if key in data and data[key] in (None, {}):
                del data[key]
        return data


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


def _check_selectors(selectors: Iterable[str]) -> None:
    bad = [selector for selector in selectors if not _SELECTOR.fullmatch(selector)]
    if bad:
        raise ValueError(
            f"harness selectors {bad} are not an exact harness id or one with a "
            "single trailing '*'"
        )


class Lane(_Strict):
    """``lanes.<name>``: a design lane (``class``) or a narrow lane (``classes``)."""

    class_: str | None = Field(default=None, alias="class")
    #: Harness selectors (module docstring), used for lane admission and
    #: exclusion from general candidates.
    harnesses: tuple[str, ...] = Field(min_length=1)
    #: A design lane lists the harnesses tried first; a narrow lane says
    #: ``true`` to make its candidates the preferred tier.
    prefer: tuple[str, ...] | bool = ()
    hold: bool = False
    #: Narrow lanes only: task class -> the class the lane runs it at.
    classes: dict[str, str] | None = None
    requires: tuple[str, ...] = ()
    #: Narrow lanes only: the riskiest classified risk the lane admits.  An
    #: unknown risk does not meet it, exactly like a missing flag.
    max_risk: str | None = None

    _OMIT_AT_DEFAULT: ClassVar[tuple[str, ...]] = ("max_risk",)
    _RETIRED: ClassVar[dict[str, str]] = {"below_priority": _PRIORITY_RETIRED}

    @property
    def narrow(self) -> bool:
        return self.classes is not None

    @property
    def preferred_harnesses(self) -> tuple[str, ...]:
        return self.prefer if isinstance(self.prefer, tuple) else ()

    def admits(self, harness: str) -> bool:
        return any(selector_matches(harness, selector) for selector in self.harnesses)

    @model_validator(mode="after")
    def _shape(self) -> Lane:
        _check_selectors(self.harnesses)
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
            if self.max_risk is not None:
                raise ValueError("'max_risk' belongs to a narrow lane")
            unknown = {
                harness for harness in self.preferred_harnesses
                if harness.endswith("*") or not self.admits(harness)
            }
            if unknown:
                raise ValueError(f"'prefer' names harnesses outside the lane: {sorted(unknown)}")
        unknown_flags = set(self.requires) - CLASSIFICATION_FLAGS
        if unknown_flags:
            raise ValueError(f"unknown 'requires' flags: {sorted(unknown_flags)}")
        if self.max_risk is not None and self.max_risk not in RISK_LEVELS:
            raise ValueError(
                f"max_risk '{self.max_risk}' is not a risk level {list(RISK_LEVELS)}"
            )
        return self


class RiskRelax(_Strict):
    """``risk.<level>.relax``: the lower floor for a classification with every flag."""

    class_: str = Field(alias="class")
    requires: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _flags(self) -> RiskRelax:
        unknown = set(self.requires) - CLASSIFICATION_FLAGS
        if unknown:
            raise ValueError(f"unknown 'requires' flags: {sorted(unknown)}")
        return self


class RiskRule(_Strict):
    """``risk.<level>``: the class floor and harness restriction for a classified risk."""

    #: The lowest class a task of this risk runs at; it beats a class hint and
    #: ``max_class`` (an operator who wants otherwise overrides the route).
    min_class: str
    #: Harness selectors every candidate must match; empty restricts nothing.
    harnesses: tuple[str, ...] = ()
    relax: RiskRelax | None = None

    @model_validator(mode="after")
    def _selectors(self) -> RiskRule:
        _check_selectors(self.harnesses)
        return self

    def floor(self, flags: Iterable[str]) -> str:
        """The class floor for a classification with *flags*."""
        if self.relax is not None and set(self.relax.requires) <= set(flags):
            return self.relax.class_
        return self.min_class


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
    that is not one of the ``train_kinds`` delivered through an integration
    train and that no other task waits on (unless ``allow_blocking``).  A
    policy without the block gets these defaults.
    """

    harnesses: tuple[str, ...] = ()
    train_kinds: tuple[str, ...] = ("bugfix",)
    allow_blocking: bool = False

    _RETIRED: ClassVar[dict[str, str]] = {"below_priority": _PRIORITY_RETIRED}


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
    #: Risk level -> the floor and harness restriction a classified risk gets.
    risk: dict[str, RiskRule] = Field(default_factory=dict)

    _OMIT_AT_DEFAULT: ClassVar[tuple[str, ...]] = ("risk",)

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
        for level, rule in self.risk.items():
            if level not in RISK_LEVELS:
                raise ValueError(
                    f"risk names '{level}', which is not a risk level {list(RISK_LEVELS)}"
                )
            check_class(f"risk.{level}.min_class", rule.min_class)
            if rule.relax is not None:
                check_class(f"risk.{level}.relax.class", rule.relax.class_)
                if self.rank(rule.relax.class_) > self.rank(rule.min_class):
                    raise ValueError(f"risk.{level}: relax.class ranks above min_class")
        return self

    def rank(self, class_id: str) -> int:
        """Position on ``class_order``; a higher rank is a more capable class."""
        return self.class_order.index(class_id)

    @property
    def uses_risk(self) -> bool:
        """The policy reads a classified risk: a ``risk`` rule or a lane's ``max_risk``."""
        return bool(self.risk) or any(lane.max_risk is not None for lane in self.lanes.values())

    def narrow_lanes(self) -> list[tuple[str, Lane]]:
        """Every narrow lane, in declaration order."""
        return [(name, lane) for name, lane in self.lanes.items() if lane.narrow]

    def narrow_harnesses(self) -> frozenset[str]:
        """Selectors of harnesses reachable only through a narrow lane (module docstring)."""
        return frozenset(h for _name, lane in self.narrow_lanes() for h in lane.harnesses)

    def unmatched_selectors(self, installed: Iterable[str]) -> list[str]:
        """``lane:selector`` for every lane selector matching no *installed* harness."""
        ids = set(installed)
        return [
            f"{name}:{selector}"
            for name, lane in self.lanes.items()
            for selector in lane.harnesses
            if not any(selector_matches(harness, selector) for harness in ids)
        ]

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
    "RISK_LEVELS",
    "Balance",
    "KindRule",
    "Lane",
    "LocalModels",
    "OriginRule",
    "PolicyError",
    "Reserved",
    "RiskRelax",
    "RiskRule",
    "RoutingPolicy",
    "parse_policy",
    "policy_digest",
    "risk_rank",
    "selector_matches",
]


def is_opencode_family(harness: str, provider: str = "", command: str = "") -> bool:
    """Recognize OpenCode CLI families even when installed under a custom id."""
    from pathlib import PurePath

    return any(
        value == "opencode" or value.startswith("opencode-")
        for value in (harness, provider, PurePath(command).name.removesuffix(".exe"))
    )
