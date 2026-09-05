"""``DatabaseActivationSource.ready_activations`` scope selection.

Timer and cron events carry no project id.  A project-scoped activation must
still receive them (once, globally) — otherwise ``pr-merge-sweep`` and
``ci-main-sentinel`` never fire — while every other project-less event keeps
reaching system playbooks only.
"""

from __future__ import annotations

import pytest

from src.database import Database
from src.playbooks.artifact_ref import ArtifactRef
from src.playbooks.services import DatabaseActivationSource, is_global_event


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "scope.db"))
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
    await _activate(db, "default-pipeline", "system", "", "1")
    await _activate(db, "pr-merge-sweep", "project", "agent-queue", "2")
    await _activate(db, "other-sweep", "project", "other-project", "3")
    await _activate(db, "reviewer-hook", "agent_type", "reviewer", "4")
    return DatabaseActivationSource(db)


async def _ids(source, event_type, event=None):
    return sorted(ref.playbook_id for ref in await source.ready_activations(event_type, event))


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
