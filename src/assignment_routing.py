"""Pure data and freshness rules for playbook-owned assignment routing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
from collections.abc import Sequence

from src.models import AssignmentOption, Task, TaskAssignmentRoute


@dataclass(frozen=True)
class EffectiveAssignmentRoute:
    """The one route consumed by schedulers, claims, and launch checks."""

    task_id: str
    intelligence_class: str
    provider: str | None
    source: str
    input_hash: str | None = None
    decision_id: str | None = None


def _value(value):
    return value.value if isinstance(value, Enum) else value


def assignment_input(task: Task) -> dict[str, object]:
    """Return the canonical material task snapshot shown to the router."""

    return {
        "task_id": task.id,
        "project_id": task.project_id,
        "title": task.title,
        "description": task.description,
        "priority": task.priority,
        "task_type": _value(task.task_type),
        "profile_id": task.profile_id,
        "preferred_workspace_id": task.preferred_workspace_id,
        "workspace_mode": _value(task.workspace_mode),
    }


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assignment_input_hash(task: Task) -> str:
    return _digest(assignment_input(task))


def options_hash(options: Sequence[AssignmentOption]) -> str:
    """Hash stable compatibility, excluding transient idle/busy occupancy."""

    stable = [
        {
            "intelligence_class": option.intelligence_class,
            "provider": option.provider,
            "configured_capacity": option.configured_capacity,
            "availability": option.availability,
        }
        for option in options
    ]
    stable.sort(key=lambda item: (item["intelligence_class"], item["provider"]))
    return _digest(stable)


def resolve_effective_route(
    task: Task,
    saved: TaskAssignmentRoute | None,
    current_options_hash: str,
) -> EffectiveAssignmentRoute | None:
    """Resolve explicit intent or a fresh playbook decision, never a default."""

    explicit = (task.intelligence_class or "").strip()
    if explicit:
        return EffectiveAssignmentRoute(task.id, explicit, None, "explicit")
    if saved is None:
        return None
    if saved.project_id != task.project_id:
        return None
    if saved.task_updated_at != task.updated_at:
        return None
    if saved.input_hash != assignment_input_hash(task):
        return None
    if saved.options_hash != current_options_hash:
        return None
    return EffectiveAssignmentRoute(
        task_id=task.id,
        intelligence_class=saved.intelligence_class,
        provider=saved.provider,
        source="playbook",
        input_hash=saved.input_hash,
        decision_id=saved.playbook_run_id,
    )


def assignment_option_payload(option: AssignmentOption) -> dict[str, object]:
    """Serialize the full catalog row shown to the assignment playbook."""

    return asdict(option)
