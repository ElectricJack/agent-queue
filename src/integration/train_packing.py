"""Bounded deterministic packing of already-admitted train inputs.

Scores are hints, never admission. Connected dependency closures are inseparable;
only independent units need disjoint signatures. Probes see the whole cumulative
prefix and may not confer publication or check authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.integration.batches import ordered_members
from src.integration.selection_metrics import selection_count, selection_outcome


@dataclass(frozen=True)
class PackingInput:
    member: object
    signature: object
    ready_at: float
    green: bool = False
    clean: bool = False
    hot_fraction: float = 1.0
    passed: int = 0
    checks: int = 0
    profile: str | None = None


def score(candidate, policy, now):
    """v1: inverse path count, nonhot fraction, fractional hours, observed rate.

    Universal signatures receive no size/nonhot preference. Missing history is
    neutral until the policy's minimum observed terminal checks is reached.
    """
    signature = candidate.signature
    known = signature.valid and not signature.universal
    return {
        "green": policy.green_weight if candidate.green else 0,
        "merge_clean": policy.merge_clean_weight if candidate.clean else 0,
        "signature_size": policy.signature_size_weight / (1 + len(signature.paths))
        if known else 0,
        "nonhot": policy.nonhot_weight * (1 - candidate.hot_fraction) if known else 0,
        "age": min(policy.max_age_score,
                   max(0, now - candidate.ready_at) / 3600 * policy.age_per_hour),
        "pass_rate": policy.pass_rate_weight * candidate.passed / candidate.checks
        if candidate.checks >= policy.pass_rate_min_checks else policy.pass_rate_neutral,
    }


def dependency_units(candidates, dependencies):
    """Union intersecting closures, retaining no partial or missing closure."""
    by_id = {c.member.task_id: c for c in candidates}
    neighbors = {tid: set() for tid in by_id}
    missing = set()
    for tid in by_id:
        for need in dependencies.get(tid, ()):
            if need not in by_id:
                missing.add(tid)
            else:
                neighbors[tid].add(need)
                neighbors[need].add(tid)
    units = []
    while neighbors:
        remaining, unit = [min(neighbors)], set()
        while remaining:
            tid = remaining.pop()
            if tid not in unit:
                unit.add(tid)
                remaining.extend(neighbors.pop(tid, ()))
        units.append((tuple(by_id[tid] for tid in sorted(unit)), bool(unit & missing)))
    return units


async def pack(candidates, dependencies, policy, *, now, deadline, clock, probe):
    """Return an ordered prefix and evidence, or an ordinary oversized unit.

    The oldest overdue *admissible* unit gets the first opportunity. A failed
    probe refunds no admission and leaves capacity available to later units.
    Oversized connected closures use ordinary delivery by themselves.
    """
    components = {c.member.task_id: score(c, policy, now) for c in candidates}
    units = dependency_units(candidates, dependencies)

    def oldest(unit):
        return min((c.ready_at, c.member.task_id) for c in unit)

    def rank(item):
        unit, _ = item
        # A unit competes on its strongest member, never its cardinality.
        return (-max(sum(components[c.member.task_id].values()) for c in unit), *oldest(unit))

    units.sort(key=rank)
    overdue = [item for item in units if not item[1]
               and now - oldest(item[0])[0] > policy.oldest_slot_after_seconds]
    reserved = min(overdue, key=lambda item: oldest(item[0])) if overdue else None
    if reserved:
        units.remove(reserved)
        units.insert(0, reserved)
    accepted, evidence = [], []
    selection_count("packing_eligible", len(candidates))
    for index, (unit, missing) in enumerate(units):
        ids = [c.member.task_id for c in unit]
        reason = None
        if index and clock() >= deadline:
            reason = "deadline"
        elif missing:
            reason = "prerequisite_pending"
        elif accepted and len(accepted) + len(unit) > policy.max_members:
            reason = "capacity"
        elif any(c.signature.conflicts_with(a.signature) for c in unit for a in accepted):
            reason = "signature_overlap"
        proposed = ()
        if reason is None:
            selection_count("packing_evaluated", len(unit))
            proposed = ordered_members([c.member for c in (*accepted, *unit)], dependencies)
            result = await probe(proposed)
            reason = result or ("ordinary_oversized" if len(unit) > policy.max_members
                                else "accepted")
        row = {"task_ids": ids, "outcome": reason,
               "reserved_oldest": reserved is not None and unit == reserved[0],
               "scores": {tid: components[tid] for tid in ids}}
        evidence.append(row)
        selection_outcome(row)
        if reason in {"accepted", "ordinary_oversized"}:
            accepted.extend(unit)
            selection_count("packing_accepted", len(unit))
        else:
            selection_count("packing_excluded_" + reason, len(unit))
    return ordered_members([c.member for c in accepted], dependencies), evidence
