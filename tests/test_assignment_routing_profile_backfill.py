"""Routing pins an unpinned task to the pool profile that serves its class."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.assignment_routing import assignment_input_hash
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.models import AgentProfile, Project, Task, TaskAssignmentRoute, TaskStatus
from src.orchestrator import Orchestrator
from src.orchestrator.assignment_routing import pool_profile_for_route
from src.sessions.harness_parser import Harness

CLASSES = {
    "standard-medium": IntelligenceClass("standard-medium", "Standard", "", {
        "anthropic": {"model": "claude-sonnet-5"},
        "codex": {"model": "gpt-5.6-sol"},
    }),
    "deep-low": IntelligenceClass("deep-low", "Deep", "", {
        "anthropic": {"model": "claude-fable-5"},
        "codex": {"model": "gpt-5.6-sol"},
    }),
}


@pytest.fixture
async def orch(tmp_path):
    db = Database(str(tmp_path / "backfill.db"))
    await db.initialize()
    for profile in (
        AgentProfile(
            id="standard-medium-claude", name="std", lifecycle="pool",
            harness="claude", default_class="standard-medium",
        ),
        AgentProfile(
            id="deep-low-codex", name="deep codex", lifecycle="pool",
            harness="codex", default_class="deep-low",
        ),
        AgentProfile(
            id="deep-low-claude", name="deep claude", lifecycle="pool",
            harness="claude", default_class="deep-low",
        ),
    ):
        await db.create_profile(profile)
    await db.create_project(
        Project(id="p", name="Project", default_profile_id="standard-medium-claude")
    )
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="test", guild_id="1"),
        workspace_dir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "unused.db"),
    )
    cfg.sessions.enabled = True
    cfg.sessions.provider = "fake"
    cfg.swarm.enabled = True
    orchestrator = Orchestrator(cfg)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    orchestrator.bus.emit = AsyncMock()
    for harness in ("claude", "codex"):
        orchestrator.harness_registry.upsert(Harness(
            id=harness, name=harness, command=harness, model_flag="--model",
        ))
    orchestrator.session_spec_builder._intelligence_classes = dict(CLASSES)
    # No playbook artifact is activated in this fixture; the coordinator
    # must still backfill profiles for routes it can resolve without one.
    orchestrator.assignment_routing._assignment_artifact = AsyncMock(
        return_value=MagicMock(artifact_sha256="sha", playbook_id="router", version=1)
    )
    yield orchestrator
    await db.close()


async def _create(db, task_id: str, **kw) -> Task:
    task = Task(
        id=task_id, project_id="p", title=task_id, description="",
        status=TaskStatus.READY, **kw,
    )
    await db.create_task(task)
    return await db.get_task(task_id)


def test_pool_profile_for_route_prefers_default_provider_then_id():
    profiles = [
        AgentProfile(id="deep-low-codex", name="", lifecycle="pool",
                     harness="codex", default_class="deep-low"),
        AgentProfile(id="deep-low-claude", name="", lifecycle="pool",
                     harness="claude", default_class="deep-low"),
        AgentProfile(id="deep-low-task", name="", lifecycle="task",
                     harness="claude", default_class="deep-low"),
    ]
    assert pool_profile_for_route("p", profiles, "deep-low", None, None,
                                  prefer_provider="anthropic") == "deep-low-claude"
    assert pool_profile_for_route("p", profiles, "deep-low", "openai", None,
                                  prefer_provider="anthropic") == "deep-low-codex"
    assert pool_profile_for_route("p", profiles, "deep-low", None, None) == "deep-low-claude"
    assert pool_profile_for_route("p", profiles, "fast-low", None, None) is None


async def test_explicit_class_gets_the_matching_pool_profile(orch):
    await _create(orch.db, "explicit", intelligence_class="deep-low")
    resolved = await orch.assignment_routing.reconcile()
    assert resolved["explicit"].intelligence_class == "deep-low"
    task = await orch.db.get_task("explicit")
    assert task.profile_id == "deep-low-claude"
    assert task.intelligence_class == "deep-low"
    demand = await orch.db.count_ready_by_profile("p")
    assert demand == {"deep-low-claude": 1}


async def test_playbook_route_is_repinned_and_restamped(orch):
    from src.orchestrator.assignment_routing import _catalog_hash

    task = await _create(orch.db, "routed")
    options, profiles = await orch.assignment_routing._catalog("p")
    # Seed the row the way ``_commit`` writes it, behind a real run row.
    from sqlalchemy import insert

    from src.database.tables import playbook_artifacts, playbook_v2_runs

    now = time.time()
    async with orch.db.immediate() as conn:
        await conn.execute(insert(playbook_artifacts).values(
            artifact_sha256="sha256:" + "1a" * 32, playbook_id="router", scope="system",
            scope_identifier="", schema_generation=2, version=1,
            source_digest="sha256:" + "2a" * 32,
            contract_fingerprint="sha256:" + "3b" * 32, profile_fingerprint="",
            compiler_build="test", path="artifacts/x.json", size_bytes=1,
            validation="{}", compiled_at=None, created_at=now,
        ))
        await conn.execute(insert(playbook_v2_runs).values(
            run_id="run-1", playbook_id="router", artifact_sha256="sha256:" + "1a" * 32,
            rule_id="route", event_type="assignment.route.requested",
            started_at=now, updated_at=now,
        ))
        await orch.db.upsert_task_assignment_routes([TaskAssignmentRoute(
            task_id="routed", project_id="p",
            input_hash=assignment_input_hash(task), task_updated_at=task.updated_at,
            options_hash=_catalog_hash("p", options, profiles),
            intelligence_class="deep-low", provider=None,
            playbook_id="router", playbook_version=1, playbook_run_id="run-1",
            reason="hard", decided_at=now,
        )], conn=conn)

    resolved = await orch.assignment_routing.reconcile()
    assert resolved["routed"].source == "playbook"

    task = await orch.db.get_task("routed")
    assert task.profile_id == "deep-low-claude"
    route = await orch.db.get_task_assignment_route("routed")
    assert route.input_hash == assignment_input_hash(task)
    assert route.task_updated_at == task.updated_at
    assert route.intelligence_class == "deep-low"
    # The re-stamped route is still fresh, so the next pass does not re-route.
    again = await orch.assignment_routing.routes_for([task])
    assert again["routed"].source == "playbook"


async def test_default_pool_task_and_pinned_task_are_left_alone(orch):
    await _create(orch.db, "default", intelligence_class="standard-medium")
    await _create(orch.db, "pinned", intelligence_class="deep-low",
                  profile_id="deep-low-codex")
    await orch.assignment_routing.reconcile()
    assert (await orch.db.get_task("default")).profile_id is None
    assert (await orch.db.get_task("pinned")).profile_id == "deep-low-codex"


async def test_unserved_class_is_left_for_the_operator(orch, caplog):
    await orch.db.create_profile(AgentProfile(
        id="fast-low-claude", name="", lifecycle="task", harness="claude",
        default_class="fast-low",
    ))
    await _create(orch.db, "orphan", intelligence_class="fast-low")
    await orch.assignment_routing.reconcile()
    assert (await orch.db.get_task("orphan")).profile_id is None
