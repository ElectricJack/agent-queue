from types import SimpleNamespace

from src.orchestrator.assignment_routing import AssignmentRoutingCoordinator


def _task():
    return SimpleNamespace(
        id="task-1",
        project_id="project-1",
        title="Route me",
        description="",
        priority=50,
        task_type="code",
        profile_id=None,
        preferred_workspace_id=None,
        workspace_mode="isolated",
    )


def test_cache_key_includes_the_artifact_hash():
    project = SimpleNamespace(id="project-1")
    playbook = SimpleNamespace(id="router", version=4)
    first = AssignmentRoutingCoordinator._batch_key(
        project, playbook, [_task()], "catalog", artifact_sha256="sha256:first"
    )
    second = AssignmentRoutingCoordinator._batch_key(
        project, playbook, [_task()], "catalog", artifact_sha256="sha256:second"
    )
    assert first != second
