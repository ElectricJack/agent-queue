"""Pure data and freshness rules for playbook-owned assignment routing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
from collections.abc import Iterable, Sequence

from src.models import AssignmentOption, Task, TaskAssignmentRoute


DEFAULT_ASSIGNMENT_PLAYBOOK_ID = "default-assignment-routing"


class AssignmentPlaybookError(ValueError):
    """The effective assignment playbook is missing or unsafe to run."""


def select_assignment_playbook(manager, project):
    """Resolve the system default or a project-scoped explicit override."""

    if manager is None:
        raise AssignmentPlaybookError("assignment playbook manager is unavailable")
    playbook_id = project.assignment_playbook_id or DEFAULT_ASSIGNMENT_PLAYBOOK_ID
    playbook = manager.get_playbook(playbook_id)
    if playbook is None:
        raise AssignmentPlaybookError(f"assignment playbook '{playbook_id}' is missing")
    if not playbook.enabled:
        raise AssignmentPlaybookError(f"assignment playbook '{playbook_id}' is disabled")
    if playbook.kind != "assignment-routing" or playbook.role != "assignment-routing":
        raise AssignmentPlaybookError(
            f"playbook '{playbook_id}' is not an assignment-routing playbook"
        )
    if project.assignment_playbook_id:
        if playbook.scope != "project" or manager.get_scope_identifier(playbook_id) != project.id:
            raise AssignmentPlaybookError(
                f"assignment playbook '{playbook_id}' is not scoped to project '{project.id}'"
            )
    elif playbook.scope != "system":
        raise AssignmentPlaybookError(
            f"default assignment playbook '{playbook_id}' must be system scoped"
        )
    return playbook


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


def options_hash(
    options: Sequence[AssignmentOption],
    *,
    profile_defaults: Iterable[tuple[str, str]] = (),
) -> str:
    """Hash stable compatibility, excluding transient idle/busy occupancy.

    ``profile_defaults`` carries the fixed classes that profile-pinned tasks
    must obey.  It intentionally belongs to the project-wide catalog hash:
    pool claim queries use that one cached value when checking a saved route.
    """

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
    defaults = sorted(
        (str(profile_id), str(class_id))
        for profile_id, class_id in profile_defaults
    )
    return _digest({"options": stable, "profile_defaults": defaults})


def resolve_effective_route(
    task: Task,
    saved: TaskAssignmentRoute | None,
    current_options_hash: str,
) -> EffectiveAssignmentRoute | None:
    """Resolve explicit intent or a fresh playbook decision, never a default.

    Freshness is decided by ``input_hash`` (every material field the router
    was shown) and ``options_hash`` (the compatible class/provider catalog).
    ``saved.task_updated_at`` is deliberately *not* part of that test: it is
    the redundant revision the claim query joins on because SQL cannot hash
    the task, and it moves for reasons the router does not care about — the
    READY→ASSIGNED write itself bumps ``updated_at``.  Requiring it here
    revoked a route the instant the scheduler reserved the task, so the
    launch check then failed with "awaiting intelligence route", paused the
    task, and flipped its worker back to IDLE every cycle.  The coordinator
    re-stamps a drifted row (``AssignmentRoutingCoordinator.reconcile``) so
    the SQL-side approximation stays in step with this decision.
    """

    explicit = (task.intelligence_class or "").strip()
    if explicit:
        return EffectiveAssignmentRoute(task.id, explicit, None, "explicit")
    if saved is None:
        return None
    if saved.project_id != task.project_id:
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
