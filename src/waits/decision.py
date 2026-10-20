"""Measured sleep policy; inputs are server observations, never agent estimates."""

from dataclasses import asdict, dataclass, replace
import math

from sqlalchemy import func, select

from src.database.tables import continuations, wait_records
from src.waits.conditions import SleepDecision


@dataclass(frozen=True)
class SleepInputs:
    kind: str
    expected_duration_seconds: float
    resume_cost_minutes: float
    hibernate_margin: float
    slot_pressure: int
    in_place_ceiling_seconds: float
    ceiling_anchor_at: float
    now: float
    hibernation_enabled: bool
    loop_detected: bool = False


def decide_sleep(inputs: SleepInputs) -> SleepDecision:
    cost = inputs.resume_cost_minutes * 60
    mode, reason = 'in_place', 'short'
    if not inputs.hibernation_enabled:
        reason = 'flag'
    elif inputs.kind == 'human' or inputs.loop_detected:
        mode, reason = 'hibernate', 'forced'
    elif inputs.expected_duration_seconds > inputs.hibernate_margin * cost:
        mode, reason = 'hibernate', 'duration'
    elif inputs.slot_pressure > 0 and inputs.expected_duration_seconds > cost:
        mode, reason = 'hibernate', 'pressure'
    elif inputs.now + inputs.expected_duration_seconds > (
            inputs.ceiling_anchor_at + inputs.in_place_ceiling_seconds):
        mode, reason = 'hibernate', 'ceiling'
    values = asdict(inputs)
    # PostgreSQL JSONB has no Infinity; null denotes the unbounded human wait.
    if not math.isfinite(values['expected_duration_seconds']):
        values['expected_duration_seconds'] = None
    return SleepDecision(mode, reason, values)


def project_policy(policy, project_id):
    override = policy.projects.get(project_id)
    if override is None:
        return policy
    values = {name: getattr(override, name) for name in (
        'in_place_ceiling_seconds', 'resume_cost_minutes', 'hibernate_margin',
    ) if getattr(override, name) is not None}
    kinds = dict(policy.kinds)
    for kind, entry in override.kinds.items():
        kinds[kind] = replace(kinds[kind], **{
            name: getattr(entry, name) for name in (
                'escalate_after_seconds', 'expected_duration_seconds',
            ) if getattr(entry, name) is not None})
    return replace(policy, kinds=kinds, **values)


async def ceiling_anchor(conn, scope, *, now, policy):
    """Re-registration cannot reset an uninterrupted in-place interval."""
    last_ack = await conn.scalar(select(func.max(continuations.c.acknowledged_at)).where(
        continuations.c.task_id == scope.owner_id,
    )) if scope.owner_kind == 'task' else None
    prior = await conn.scalar(select(wait_records.c.ceiling_anchor_at).where(
        wait_records.c.owner_kind == scope.owner_kind, wait_records.c.owner_id == scope.owner_id,
        wait_records.c.project_id == scope.project_id,
        wait_records.c.sleep_decision == 'in_place',
        wait_records.c.created_at >= now - getattr(policy, 'loop_window_seconds', 3600),
        wait_records.c.created_at >= (last_ack or 0),
        wait_records.c.ceiling_anchor_at.is_not(None),
    ).order_by(wait_records.c.created_at.desc()).limit(1))
    return now if prior is None else prior


def measured_inputs(cond, evidence, *, policy, medians, pressure, anchor, now):
    duration = medians['durations'].get((cond.kind, cond.context['source_class']), {})
    cost = medians['cost']
    expected = (duration['median'] if duration.get('count', 0) >= policy.min_samples
                else policy.kinds[cond.kind].expected_duration_seconds or 0)
    if cond.kind == 'timer':
        expected = max(0, cond.parameters['due_at'] - now)
    if cond.kind == 'human':
        expected = math.inf
    return SleepInputs(
        cond.kind, expected, cost['median'] / 60 if cost.get('count', 0) >= policy.min_samples
        else policy.resume_cost_minutes, policy.hibernate_margin, pressure,
        policy.in_place_ceiling_seconds, anchor, now, policy.hibernation_enabled,
    )
