from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import AgentProfile, Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.vault import ensure_default_intelligence_classes

PROJECT_ID = "p"


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "et.db"))
    await d.initialize()
    await d.create_project(Project(id=PROJECT_ID, name="P"))
    yield d
    await d.close()


@pytest.fixture
def config(tmp_path):
    data_dir = str(tmp_path / "d")
    ensure_default_intelligence_classes(data_dir)
    return AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "et.db"),
        data_dir=data_dir,
    )


@pytest.fixture
async def handler(db, config):
    o = Orchestrator(config)
    o.db = db
    o.git = MagicMock()
    return CommandHandler(o, config)


async def test_ensure_task_creates_when_missing(handler):
    res = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
    )
    assert res["success"] is True
    assert res["created"] is True
    assert res["task_id"]


async def test_ensure_task_returns_existing(handler):
    r1 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
    )
    r2 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Different title"},
    )
    assert r2["success"] is True
    assert r2["created"] is False
    assert r2["task_id"] == r1["task_id"]


async def test_ensure_task_ignores_completed_task(handler, db):
    r1 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "review-branch-feat", "title": "Review"},
    )
    # Complete r1's task and ensure a fresh one is created.
    await db.transition_task(r1["task_id"], TaskStatus.COMPLETED, force=True)
    r2 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "review-branch-feat", "title": "Review"},
    )
    assert r2["created"] is True
    assert r2["task_id"] != r1["task_id"]


async def test_ensure_task_requires_dedup_key(handler):
    res = await handler.execute(
        "ensure_task", {"project_id": PROJECT_ID, "title": "x"}
    )
    assert res.get("success") is False or "error" in res


async def test_ensure_task_does_NOT_emit_task_created(handler):
    """Control-plane invariant: ensure_task must NOT emit task.created.

    Emitting would re-fire the default pipeline against the bookkeeping task,
    attaching a routing gate resolvable only by the triage agent — the
    triage task would deadlock blocked on its own gate.  Rationale is
    documented on the suppression site (src/commands/task_commands.py).
    """
    orch = handler.orchestrator
    with patch.object(orch, "_emit_task_event", new=AsyncMock()) as spy_task, \
         patch.object(orch, "_emit_notify", new=AsyncMock()):
        res = await handler.execute(
            "ensure_task",
            {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
        )
    assert res["success"] is True
    assert res["created"] is True
    # Zero task.created (or any other task.*) emissions from the ensure path.
    called_types = [c.args[0] for c in spy_task.call_args_list]
    assert "task.created" not in called_types, called_types


async def test_create_task_DOES_emit_task_created(handler):
    """Foil to the ensure_task suppression test: normal create_task emits."""
    orch = handler.orchestrator
    with patch.object(orch, "_emit_task_event", new=AsyncMock()) as spy_task, \
         patch.object(orch, "_emit_notify", new=AsyncMock()):
        res = await handler.execute(
            "create_task",
            {"project_id": PROJECT_ID, "title": "Do a thing"},
        )
    assert "error" not in res, res
    called_types = [c.args[0] for c in spy_task.call_args_list]
    assert "task.created" in called_types, called_types


async def test_ensure_task_reuses_in_progress_task(handler, db):
    r1 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
    )
    await db.transition_task(r1["task_id"], TaskStatus.IN_PROGRESS, force=True)
    r2 = await handler.execute(
        "ensure_task",
        {"project_id": PROJECT_ID, "dedup_key": "triage-open", "title": "Triage"},
    )
    assert r2["created"] is False
    assert r2["task_id"] == r1["task_id"]


# --- explicit route intent on create (routing design §2) -------------------
# A pinned ``profile_id`` is a compatibility constraint, not a route: without
# an explicit class the task waits for the assignment playbook, and the
# launch path refuses it with "awaiting intelligence route" until then.


@pytest.fixture
async def reviewer(db):
    await db.create_profile(
        AgentProfile(id="reviewer", name="Reviewer", harness="claude", needs_workspace=False)
    )
    return "reviewer"


async def test_ensure_task_records_explicit_intelligence_class(handler, db, reviewer):
    res = await handler.execute(
        "ensure_task",
        {
            "project_id": PROJECT_ID,
            "dedup_key": "review:task:t1",
            "title": "Review",
            "profile_id": reviewer,
            "intelligence_class": "standard-low",
        },
    )
    assert res["success"] is True, res
    task = await db.get_task(res["task_id"])
    assert task.profile_id == "reviewer"
    assert task.intelligence_class == "standard-low"


async def test_ensure_task_without_class_invents_none(handler, db, reviewer):
    """A profile default must never stand in for the task's route."""
    res = await handler.execute(
        "ensure_task",
        {
            "project_id": PROJECT_ID,
            "dedup_key": "review:task:t2",
            "title": "Review",
            "profile_id": reviewer,
        },
    )
    assert res["success"] is True, res
    task = await db.get_task(res["task_id"])
    assert task.intelligence_class is None


async def test_ensure_task_rejects_unknown_intelligence_class(handler, db, reviewer):
    res = await handler.execute(
        "ensure_task",
        {
            "project_id": PROJECT_ID,
            "dedup_key": "review:task:t3",
            "title": "Review",
            "profile_id": reviewer,
            "intelligence_class": "no-such-class",
        },
    )
    assert res["success"] is False
    assert "no-such-class" in res["error"]
    assert await db.find_task_by_dedup_key(PROJECT_ID, "review:task:t3") is None


async def test_ensure_task_class_applies_only_on_create(handler, db, reviewer):
    first = await handler.execute(
        "ensure_task",
        {
            "project_id": PROJECT_ID,
            "dedup_key": "review:task:t4",
            "title": "Review",
            "profile_id": reviewer,
            "intelligence_class": "standard-low",
        },
    )
    second = await handler.execute(
        "ensure_task",
        {
            "project_id": PROJECT_ID,
            "dedup_key": "review:task:t4",
            "title": "Review",
            "profile_id": reviewer,
            "intelligence_class": "standard-high",
        },
    )
    assert second["created"] is False
    assert second["task_id"] == first["task_id"]
    task = await db.get_task(first["task_id"])
    assert task.intelligence_class == "standard-low"


async def test_ensure_task_routes_canonical_triage_task(handler, db):
    await db.create_profile(
        AgentProfile(id="triage", name="Triage", harness="claude", needs_workspace=False)
    )
    # The canonical triage task is only born when routing work is waiting.
    await db.create_task(
        Task(id="unrouted", project_id=PROJECT_ID, title="Unrouted", description="")
    )
    await db.create_gate(PROJECT_ID, "routing", "Route task", waiter_task_ids=["unrouted"])
    res = await handler.execute(
        "ensure_task",
        {
            "project_id": PROJECT_ID,
            "dedup_key": "triage-open",
            "title": "Triage",
            "profile_id": "triage",
            "intelligence_class": "standard-low",
        },
    )
    assert res["success"] is True, res
    assert res["created"] is True, res
    task = await db.get_task(res["task_id"])
    assert task.intelligence_class == "standard-low"


async def test_ensure_task_rejects_unknown_class_for_triage(handler, db):
    await db.create_profile(
        AgentProfile(id="triage", name="Triage", harness="claude", needs_workspace=False)
    )
    res = await handler.execute(
        "ensure_task",
        {
            "project_id": PROJECT_ID,
            "dedup_key": "triage-open",
            "title": "Triage",
            "profile_id": "triage",
            "intelligence_class": "no-such-class",
        },
    )
    assert res["success"] is False
    assert "no-such-class" in res["error"]
