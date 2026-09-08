"""``DatabaseActivationSource.ready_activations`` scope selection.

Timer and cron events carry no project id.  A project-scoped activation must
still receive them (once, globally) — otherwise ``pr-merge-sweep`` and
``ci-main-sentinel`` never fire — while every other project-less event keeps
reaching system playbooks only.
"""

from __future__ import annotations

import pytest

from src.database import Database
from src.models import Project, RepoConfig, RepoSourceType
from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.services import DatabaseActivationSource, is_global_event
from tests.db_fixtures import lease_dsn


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("scope.db"))
    await database.initialize()
    yield database
    await database.close()


async def _activate(db, playbook_id: str, scope: str, identifier: str, digit: str) -> str:
    ref = ArtifactRef(
        playbook_id=playbook_id,
        artifact_sha256="sha256:" + digit * 64,
        schema_generation=2,
        contract_fingerprint="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        compiler_build="test-build",
    )
    await db.upsert_playbook_artifact(
        ref, scope=scope, scope_identifier=identifier, path=f"/artifacts/{digit}.json", size_bytes=1
    )
    await db.set_playbook_activation(
        playbook_id=playbook_id,
        scope=scope,
        scope_identifier=identifier,
        artifact_sha256=ref.artifact_sha256,
        enabled=True,
        activated_by="test",
        health="ready",
        reasons="[]",
    )
    return playbook_id


@pytest.fixture
async def activations(db):
    await db.create_project(Project(id="agent-queue", name="agent queue"))
    await db.create_project(Project(id="other-project", name="other"))
    await _activate(db, "default-pipeline", "system", "", "1")
    await _activate(db, "pr-merge-sweep", "project", "agent-queue", "2")
    await _activate(db, "other-sweep", "project", "other-project", "3")
    await _activate(db, "reviewer-hook", "agent_type", "reviewer", "4")
    return DatabaseActivationSource(db)


async def test_suppressed_project_merge_sweep_stays_filtered_after_reenable(db, activations):
    async with db.immediate() as conn:
        await db.set_integration_legacy_suppression_on(
            conn,
            project_id="agent-queue",
            generation=0,
            merge_sweep_suppressed=True,
            final_review_route_suppressed=True,
            legacy_gate_creation_suppressed=True,
            policy_snapshot={"source": "test"},
            now=1.0,
        )

    event = {"tick_time": "2026-09-06T00:00:00+00:00", "interval": "30m"}
    assert await _ids(activations, "timer.30m", event) == [
        "default-pipeline",
        "other-sweep",
    ]

    row = next(
        item
        for item in await db.list_playbook_activations(enabled_only=False)
        if item["playbook_id"] == "pr-merge-sweep"
    )
    await db.set_playbook_activation(
        playbook_id="pr-merge-sweep",
        scope="project",
        scope_identifier="agent-queue",
        artifact_sha256=row["active_artifact_sha256"],
        enabled=True,
        activated_by="test-reenable",
        health="ready",
        reasons="[]",
    )
    assert "pr-merge-sweep" not in await _ids(activations, "timer.30m", event)


async def _ids(source, event_type, event=None):
    return sorted(ref.playbook_id for ref in await source.ready_activations(event_type, event))


async def _enable_system_integration_route(
    db, *, project_id: str, playbook_id: str, artifact_sha256: str
) -> None:
    repository_id = f"repo-{project_id}"
    await db.create_repo(
        RepoConfig(
            id=repository_id,
            project_id=project_id,
            source_type=RepoSourceType.CLONE,
            url=f"https://github.com/acme/{project_id}.git",
        )
    )
    artifact = await db.get_playbook_artifact(artifact_sha256)
    assert artifact is not None
    boundary = {
        "required_checks": {
            "version": "checks-v1",
            "names": ["Tests (default)"],
            "producer_id": "1234",
        },
        "repair": {
            "debug_intelligence_class": "deep",
            "debug_profile_id": "debugger",
        },
        "route": {
            "playbook_id": playbook_id,
            "scope": "system",
            "scope_identifier": "",
            "activation_id": None,
            "artifact": artifact.as_dict(),
        },
        "primary_intelligence_class": "standard",
        "primary_profile_id": "worker",
    }
    await db.update_project(
        project_id,
        integration_mode="pull_request",
        integration_repository_id=repository_id,
        hierarchical_integration_mode="hierarchy",
        hierarchical_integration_policy={
            "version": 1,
            "parent": boundary,
            "root": boundary,
            "branchless_parent": "skip",
            "on_failed_child": "block",
            "cleanup": {},
        },
    )


def test_global_event_families():
    assert is_global_event("timer.30m") and is_global_event("cron.07:00")
    assert not is_global_event("task.completed") and not is_global_event("")


async def test_timer_tick_reaches_every_project_playbook_once(activations):
    event = {"tick_time": "2026-09-05T19:53:00+00:00", "interval": "30m"}
    assert await _ids(activations, "timer.30m", event) == [
        "default-pipeline", "other-sweep", "pr-merge-sweep"
    ]


async def test_cron_tick_reaches_project_playbooks_too(activations):
    assert await _ids(activations, "cron.07:00", {"interval": "07:00"}) == [
        "default-pipeline", "other-sweep", "pr-merge-sweep"
    ]


async def test_project_event_reaches_only_its_own_project(activations):
    assert await _ids(activations, "task.completed", {"project_id": "agent-queue"}) == [
        "default-pipeline", "pr-merge-sweep"
    ]


async def test_project_less_ordinary_event_stays_system_only(activations):
    assert await _ids(activations, "task.completed", {}) == ["default-pipeline"]
    assert await _ids(activations, "task.completed") == ["default-pipeline"]


async def test_agent_type_scope_needs_a_project_and_matching_type(activations):
    assert await _ids(
        activations, "task.completed", {"project_id": "agent-queue", "agent_type": "reviewer"}
    ) == ["default-pipeline", "pr-merge-sweep", "reviewer-hook"]
    assert await _ids(activations, "timer.30m", {"agent_type": "reviewer"}) == [
        "default-pipeline", "other-sweep", "pr-merge-sweep"
    ]


async def test_disabled_project_does_not_admit_shared_hierarchical_delivery(db):
    await db.create_project(Project(id="disabled", name="disabled"))
    await _activate(db, "hierarchical-delivery", "system", "", "5")
    source = DatabaseActivationSource(db)

    assert await _ids(
        source,
        "task.completed",
        {"project_id": "disabled", "task_id": "task-1", "title": "done"},
    ) == []


async def test_enabled_project_uses_shared_route_without_activation_id(db):
    await db.create_project(Project(id="enabled", name="enabled"))
    await _activate(db, "default-pipeline", "system", "", "5")
    await _activate(db, "hierarchical-delivery", "system", "", "6")
    await _enable_system_integration_route(
        db,
        project_id="enabled",
        playbook_id="hierarchical-delivery",
        artifact_sha256="sha256:" + "6" * 64,
    )
    source = DatabaseActivationSource(db)

    assert await _ids(
        source,
        "task.completed",
        {"project_id": "enabled", "task_id": "task-1", "title": "done"},
    ) == ["default-pipeline", "hierarchical-delivery"]
