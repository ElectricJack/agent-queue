from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from src.assignment_routing import (
    EffectiveAssignmentRoute,
    assignment_input_hash,
    options_hash,
    resolve_effective_route,
)
from src.models import AssignmentOption, Task, TaskAssignmentRoute
from src.orchestrator.assignment_routing import task_assignment_options


def _task(**changes) -> Task:
    values = {
        "id": "task-1",
        "project_id": "project-1",
        "title": "Fix flaky checkout",
        "description": "Find and repair the checkout race.",
        "priority": 20,
        "updated_at": 100.0,
    }
    values.update(changes)
    return Task(**values)


def _saved(task: Task, *, catalog_hash: str = "catalog-v1") -> TaskAssignmentRoute:
    return TaskAssignmentRoute(
        task_id=task.id,
        project_id=task.project_id,
        input_hash=assignment_input_hash(task),
        task_updated_at=task.updated_at,
        options_hash=catalog_hash,
        intelligence_class="standard-medium",
        provider="openai",
        playbook_id="default-assignment-routing",
        playbook_version=3,
        playbook_run_id="run-1",
        reason="Needs ordinary code reasoning.",
        decided_at=110.0,
    )


def test_explicit_class_wins_over_saved_route() -> None:
    task = _task(intelligence_class="deep-high")

    route = resolve_effective_route(task, _saved(task), "catalog-v2")

    assert route == EffectiveAssignmentRoute(
        task_id=task.id,
        intelligence_class="deep-high",
        provider=None,
        source="explicit",
    )


def test_saved_route_requires_matching_input_and_options() -> None:
    task = _task()
    saved = _saved(task)

    assert resolve_effective_route(task, saved, "catalog-v1") is not None
    assert resolve_effective_route(task, replace(saved, input_hash="old"), "catalog-v1") is None
    assert resolve_effective_route(task, saved, "catalog-v2") is None
    assert resolve_effective_route(task, replace(saved, project_id="other"), "catalog-v1") is None


def test_saved_route_survives_a_revision_bump_that_changes_no_routed_input() -> None:
    """READY→ASSIGNED bumps ``updated_at``; the decision must outlive it."""
    task = _task()
    saved = _saved(task)

    assigned = replace(task, updated_at=task.updated_at + 1)

    route = resolve_effective_route(assigned, saved, "catalog-v1")

    assert route is not None
    assert route.intelligence_class == "standard-medium"
    assert route.provider == "openai"
    assert route.source == "playbook"


def test_assignment_hashes_are_canonical_and_include_material_changes() -> None:
    task = _task()
    option_a = AssignmentOption("standard-medium", "openai", 2, 1, 1, "available")
    option_b = AssignmentOption("fast-low", "anthropic", 1, 1, 0, "unknown")

    assert options_hash([option_a, option_b]) == options_hash([option_b, option_a])
    assert options_hash([option_a]) == options_hash([
        replace(option_a, idle_count=0, busy_count=2)
    ])
    assert assignment_input_hash(task) != assignment_input_hash(replace(task, priority=21))
    assert assignment_input_hash(task) == assignment_input_hash(replace(task, retry_count=2))


def test_pinned_profile_narrows_catalog_to_its_harness_provider() -> None:
    task = _task(profile_id="standard-high-claude")
    profiles = [
        SimpleNamespace(
            id="standard-high-claude",
            default_class="standard-high",
            harness="claude",
        )
    ]
    options = [
        AssignmentOption("standard-high", "anthropic", 2, 1, 1, "available"),
        AssignmentOption("standard-high", "openai", 2, 1, 1, "available"),
    ]
    harness_registry = SimpleNamespace(
        get=lambda harness_id, project_id=None: SimpleNamespace(
            id=harness_id,
            command=harness_id,
            provider="anthropic",
        )
    )

    constrained = task_assignment_options(task, options, profiles, harness_registry)

    assert [(option.intelligence_class, option.provider) for option in constrained] == [
        ("standard-high", "anthropic")
    ]
