"""Compose mandatory, static and Jev sets according to spec §4.3.

Shadow records a proposal; its caller still runs the original targets. Runnable
omission from static impact requires both promotion and complete evidence.
Mandatory modules and full fallbacks are preserved in every mode.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from src.test_selection import reasons as r
from src.test_selection.catalogue import Catalogue, Policy
from src.test_selection.mandatory import MandatoryResult
from src.test_selection.static_impact import StaticResult
from src.test_selection.typesafe import AreaAnswer, JevResult

Mode = Literal["plan_only", "shadow", "enforce"]


@dataclass(frozen=True)
class PromotionIdentity:
    model: str
    question_schema_version: int
    catalogue_digest: str
    rules_digest: str
    policy_digest: str

    def matches(self, other: PromotionIdentity) -> bool:
        """A promotion applies only to the exact model, schema and artifacts."""
        return self == other


def omit_candidate(answer: AreaAnswer, policy: Policy) -> bool:
    """Only a confidently unaffected, validated answer can be a candidate."""
    return (
        answer.choice == policy.omit_choice
        and answer.probabilities.get(policy.omit_choice, 0.0) >= policy.omit_min_probability
        and answer.confidence >= policy.omit_min_confidence
    )


@dataclass(frozen=True)
class AreaDecision:
    area_id: str
    choice: str | None
    p_affected: float | None
    p_unaffected: float | None
    confidence: float | None
    omitted: bool  # omitted from J; mandatory membership is independent
    reason: str


@dataclass(frozen=True)
class Composition:
    universe: frozenset[str]
    mandatory: frozenset[str]
    static: frozenset[str]
    jev: frozenset[str] | None
    fallback: frozenset[str]
    final: frozenset[str]
    full_required: bool
    jev_used_for_omission: bool
    evidence_complete: bool
    area_decisions: dict[str, AreaDecision]
    reasons: dict[str, tuple[str, ...]]
    ordered: tuple[str, ...]
    fallback_reason: str | None


def order_modules(
    final: Iterable[str],
    *,
    area_decisions: Mapping[str, AreaDecision],
    catalogue: Catalogue,
    durations: Mapping[str, float] | None,
) -> tuple[str, ...]:
    """Order by max affected probability, duration and path, without deselection."""
    durations = durations or {}

    def key(module: str) -> tuple[float, float, str]:
        probabilities = []
        for area in catalogue.areas_for_module(module):
            decision = area_decisions.get(area)
            probability = decision.p_affected if decision is not None else None
            probabilities.append(1.0 if probability is None else probability)
        return (-max(probabilities, default=1.0), -durations.get(module, 0.0), module)

    return tuple(sorted(set(final), key=key))


def _area_decision(area_id: str, answer: AreaAnswer | None, policy: Policy) -> AreaDecision:
    if answer is None:
        return AreaDecision(area_id, None, None, None, None, False, r.JEV_MISSING_ANSWER)
    omitted = omit_candidate(answer, policy)
    reason = (
        r.JEV_CONFIDENT_UNAFFECTED
        if omitted
        else r.JEV_UNKNOWN
        if answer.choice == "unknown"
        else r.JEV_AFFECTED
    )
    return AreaDecision(
        area_id,
        answer.choice,
        answer.probabilities.get("affected"),
        answer.probabilities.get("unaffected"),
        answer.confidence,
        omitted,
        reason,
    )


def compose(
    *,
    catalogue: Catalogue,
    mandatory: MandatoryResult,
    static: StaticResult,
    jev: JevResult | None,
    policy: Policy,
    mode: Mode,
    promoted: bool,
    evidence_complete: bool,
    durations: Mapping[str, float] | None = None,
) -> Composition:
    """Return F, J and the final proposal while asserting M ⊆ final ⊆ U.

    Inputs are reviewed artifacts and results from the mandatory, static and
    validated TypeSafe adapters. The service checks promotion identity before
    passing ``promoted``. Non-ok Jev results discard all partial answers.
    """
    if mode not in ("plan_only", "shadow", "enforce"):
        raise ValueError(f"unknown selection mode: {mode!r}")

    universe = catalogue.universe
    full = mandatory.full_required or not static.complete
    m = universe if full else mandatory.modules
    s = frozenset() if full else static.modules
    fallback = m | s
    decisions: dict[str, AreaDecision] = {}
    j: frozenset[str] | None = None
    if not full and jev is not None and jev.complete:
        decisions = {
            area: _area_decision(area, jev.answers.get(area), policy)
            for area in sorted(catalogue.areas)
        }
        j = catalogue.modules_for_areas(a for a, d in decisions.items() if not d.omitted)

    fallback_reason = None
    omission = False
    if full:
        final = universe
        if not static.complete:
            fallback_reason = static.reason or r.STATIC_UNAVAILABLE
        elif mandatory.global_reasons:
            fallback_reason = mandatory.global_reasons[0]
    elif j is None:
        final = fallback
        fallback_reason = jev.reason if jev is not None else r.JEV_DISABLED
    elif mode == "shadow":
        final = m | j
    elif not (promoted and evidence_complete):
        final = fallback | j
        fallback_reason = "not_promoted" if not promoted else "evidence_incomplete"
    else:
        final = m | j
        omission = True

    assert m <= final <= universe, "selection must preserve mandatory modules within the catalogue"

    # Keep reasons for F as well as the proposal, including static-only modules
    # omitted from a promoted proposal, so both recorded sets are explainable.
    merged = {}
    for module in sorted(fallback | (j or frozenset())):
        codes = list(mandatory.reasons.get(module, ()))
        if not static.complete:
            codes.append(static.reason or r.STATIC_UNAVAILABLE)
        if module in s:
            codes.append(r.STATIC_IMPACT)
        for area in catalogue.areas_for_module(module):
            decision = decisions.get(area)
            if decision is not None and not decision.omitted:
                codes.append(decision.reason)
        merged[module] = tuple(dict.fromkeys(codes))

    return Composition(
        universe=universe,
        mandatory=m,
        static=s,
        jev=j,
        fallback=fallback,
        final=final,
        full_required=full,
        jev_used_for_omission=omission,
        evidence_complete=evidence_complete,
        area_decisions=decisions,
        reasons=merged,
        ordered=order_modules(
            final, area_decisions=decisions, catalogue=catalogue, durations=durations
        ),
        fallback_reason=fallback_reason,
    )
