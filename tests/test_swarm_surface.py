"""Generated surface for the swarm commands — spec §14."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project
from src.orchestrator import Orchestrator
from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES

PROJECT_ID = "proj"


def defs():
    return {d["name"]: d for d in _ALL_TOOL_DEFINITIONS}


def test_task_claim_definition():
    d = defs()["task_claim"]
    assert set(d["input_schema"]["properties"]) >= {"task_id", "next", "wait"}
    assert _TOOL_CATEGORIES["task_claim"] == "task"


def test_close_and_mutators_carry_claim_epoch():
    d = defs()
    for name in ("task_close", "task_heartbeat", "task_set", "task_handoff"):
        assert "claim_epoch" in d[name]["input_schema"]["properties"], name
    assert {"claim_next", "wait"} <= set(d["task_close"]["input_schema"]["properties"])


def test_close_surface_accepts_structured_completion_details(tmp_path, monkeypatch):
    """Repeatable close flags must preserve verification detail in the request."""
    from src.cli import agent_surface
    from src.cli.app import cli

    sent = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, command, args=None):
            sent.update(command=command, args=args or {})
            return {"success": True}

    monkeypatch.setattr(agent_surface, "_get_client", lambda *a, **k: FakeClient())
    monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "task", "close", "t1", "--outcome", "pass", "--summary", "Done.",
            "--changes", "Added durable completion records.",
            "--verification", "Focused tests passed.",
            "--test", "pytest tests/test_swarm_surface.py -q",
            "--command", "ruff check src tests",
            "--command", "npm test -- task-detail",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sent == {
        "command": "task_close",
        "args": {
            "task_id": "t1",
            "outcome": "pass",
            "summary": "Done.",
            "changes": "Added durable completion records.",
            "verification": "Focused tests passed.",
            "tests": ["pytest tests/test_swarm_surface.py -q"],
            "commands": ["ruff check src tests", "npm test -- task-detail"],
        },
    }
    props = defs()["task_close"]["input_schema"]["properties"]
    assert props["tests"]["type"] == "array"
    assert props["commands"]["type"] == "array"


def test_create_task_accepts_swarm_fields():
    props = defs()["create_task"]["input_schema"]["properties"]
    assert {"depends_on", "discovered_from", "dedup_key", "parent_id"} <= set(props)


def test_pool_commands_defined():
    d = defs()
    assert _TOOL_CATEGORIES["pool_status"] == _TOOL_CATEGORIES["pool_scale"] == "pool"
    assert {"profile_id", "min", "max", "now"} <= set(
        d["pool_scale"]["input_schema"]["properties"]
    )
    # project_id survives as a deprecated no-op for one release, but is no
    # longer required: bounds live on the (global) system profile.
    assert d["pool_scale"]["input_schema"]["required"] == ["profile_id"]
    assert "Deprecated" in d["pool_scale"]["input_schema"]["properties"]["project_id"][
        "description"
    ]


def test_pool_lifecycle_command_is_part_of_the_generated_surface():
    d = defs()
    assert _TOOL_CATEGORIES["pool_set_lifecycle"] == "pool"
    assert {"profile_id", "lifecycle"} <= set(
        d["pool_set_lifecycle"]["input_schema"]["properties"]
    )
    assert d["pool_set_lifecycle"]["input_schema"]["required"] == ["profile_id", "lifecycle"]


def test_project_scoped_profile_commands_are_gone():
    """Project-scoped profiles were retired; their CRUD must be off the surface."""
    d = defs()
    for name in (
        "create_project_profile",
        "edit_project_profile",
        "delete_project_profile",
        "list_project_profiles",
    ):
        assert name not in d, f"{name} is still registered"
        assert name not in _TOOL_CATEGORIES


def test_read_claim_epoch_prefers_file(tmp_path, monkeypatch):
    from src.cli.agent_surface import read_claim_epoch

    monkeypatch.delenv("AQ_TASK_ID", raising=False)
    monkeypatch.delenv("AQ_SESSION_ID", raising=False)
    monkeypatch.setenv("AQ_CLAIM_EPOCH", "9")
    assert read_claim_epoch(str(tmp_path)) == 9
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text(json.dumps({"task_id": "t", "claim_epoch": 3}))
    assert read_claim_epoch(str(tmp_path)) == 3


def test_cli_close_sends_claim_epoch(tmp_path, monkeypatch):
    from src.cli import agent_surface
    from src.cli.app import cli

    sent = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, command, args=None):
            sent.update(command=command, args=args or {})
            return {"success": True}

    monkeypatch.setattr(agent_surface, "_get_client", lambda *a, **k: FakeClient())
    monkeypatch.delenv("AQ_TASK_ID", raising=False)
    monkeypatch.delenv("AQ_SESSION_ID", raising=False)
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text(json.dumps({"task_id": "t1", "claim_epoch": 4}))
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(
        cli, ["task", "close", "t1", "--outcome", "pass", "--summary", "s", "--claim-next"]
    )
    assert r.exit_code == 0, r.output
    assert sent["command"] == "task_close"
    assert (sent["args"]["claim_epoch"], sent["args"]["claim_next"]) == (4, True)


def test_read_claim_epoch_walks_up_from_a_subdirectory(tmp_path, monkeypatch):
    """M5: a worker that ``cd``ed into a subdirectory still resolves its epoch."""
    from src.cli.agent_surface import read_claim_epoch

    monkeypatch.delenv("AQ_TASK_ID", raising=False)
    monkeypatch.delenv("AQ_SESSION_ID", raising=False)
    monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text(json.dumps({"task_id": "t", "claim_epoch": 7}))
    deep = tmp_path / "src" / "pkg" / "sub"
    deep.mkdir(parents=True)
    assert read_claim_epoch(str(deep)) == 7


def test_read_claim_epoch_ignores_another_session_claim_file(tmp_path, monkeypatch):
    """A reused slot must fall back to the calling worker's environment epoch."""
    from src.cli.agent_surface import read_claim_epoch

    monkeypatch.setenv("AQ_TASK_ID", "active-task")
    monkeypatch.setenv("AQ_SESSION_ID", "active-session")
    monkeypatch.setenv("AQ_CLAIM_EPOCH", "2")
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text(json.dumps({
        "task_id": "reassigned-task", "session_id": "new-session", "claim_epoch": 1,
    }))

    assert read_claim_epoch(str(tmp_path)) == 2


def test_read_claim_epoch_returns_none_outside_a_workspace(tmp_path, monkeypatch):
    from src.cli.agent_surface import read_claim_epoch

    monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
    lonely = tmp_path / "nowhere"
    lonely.mkdir()
    assert read_claim_epoch(str(lonely)) is None


def test_cli_close_without_task_id_sends_none(tmp_path, monkeypatch):
    """I3: the pool bootstrap prompt's form must be accepted by the CLI."""
    from src.cli import agent_surface
    from src.cli.app import cli

    sent = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, command, args=None):
            sent.update(command=command, args=args or {})
            return {"success": True}

    monkeypatch.setattr(agent_surface, "_get_client", lambda *a, **k: FakeClient())
    monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli, ["task", "close", "--outcome", "pass", "--claim-next"])
    assert r.exit_code == 0, r.output
    assert sent["command"] == "task_close"
    assert "task_id" not in sent["args"]
    assert sent["args"]["claim_next"] is True


def test_cli_heartbeat_without_task_id_sends_none(tmp_path, monkeypatch):
    from src.cli import agent_surface
    from src.cli.app import cli

    sent = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, command, args=None):
            sent.update(command=command, args=args or {})
            return {"success": True}

    monkeypatch.setattr(agent_surface, "_get_client", lambda *a, **k: FakeClient())
    monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli, ["task", "heartbeat"])
    assert r.exit_code == 0, r.output
    assert sent["command"] == "task_heartbeat"
    assert "task_id" not in sent["args"]


def test_cli_claim_timeout_is_long():
    from src.cli.client import _COMMAND_TIMEOUTS

    assert _COMMAND_TIMEOUTS["task_claim"] >= 180 and _COMMAND_TIMEOUTS["task_close"] >= 180


@pytest.fixture
async def handler(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    await db.create_project(Project(id=PROJECT_ID, name="p"))
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "ws"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(cfg)
    orch.db = db
    orch.git = MagicMock()
    yield CommandHandler(orch, cfg)
    await db.close()


async def test_schema_enums(handler):
    res = await handler._cmd_get_schema({})
    enums = res["enums"]
    assert enums["lifecycle"] == ["task", "named", "pool"]
    assert "stale_claim" in enums["claim_result"] and enums["outcome"] == ["pass", "fail"]


async def test_pool_status_empty(handler):
    res = await handler._cmd_pool_status({})
    assert res == {"success": True, "pools": []}


async def test_pool_scale_requires_min_or_max(handler):
    res = await handler._cmd_pool_scale({"profile_id": "worker"})
    assert res == {"success": False, "error": "nothing to change: pass min and/or max"}


def test_skill_documents_worker_loop():
    text = open("src/skills/aq-tasks/SKILL.md", encoding="utf-8").read()
    assert "aq task claim --next" in text and "--claim-next" in text
    assert "--outcome pass|fail" in text and "needs_context" not in text


# ─────────────────── pool scale writes the vault (spec §14) ──────────────


POOL_PROFILE_MD = """---
id: worker
name: Worker
---

## Role

Do the work.

## Config

```json
{
  "lifecycle": "pool",
  "min_active": 1,
  "max_active": 2
}
```

## Rules

Be careful.
"""


@pytest.fixture
async def pool_handler(handler, tmp_path):
    """``handler`` plus a system ``worker`` pool profile in DB *and* vault."""
    from src.profiles.sync import sync_profile_text_to_db

    system_path = tmp_path / "data" / "vault" / "agent-types" / "worker" / "profile.md"
    system_path.parent.mkdir(parents=True, exist_ok=True)
    system_path.write_text(POOL_PROFILE_MD, encoding="utf-8")
    result = await sync_profile_text_to_db(
        POOL_PROFILE_MD, handler.db, source_path=str(system_path), fallback_id="worker"
    )
    assert result.success, result.errors
    return handler


def _system_path(tmp_path):
    return tmp_path / "data" / "vault" / "agent-types" / "worker" / "profile.md"


async def test_pool_scale_writes_the_system_profile(pool_handler, tmp_path):
    """The bounds land in the system vault file, not a project override."""
    from src.profiles.parser import parse_profile

    res = await pool_handler._cmd_pool_scale({"profile_id": "worker", "min": 3, "max": 7})
    assert res["success"], res
    assert (res["min_active"], res["max_active"]) == (3, 7)

    text = _system_path(tmp_path).read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config["min_active"] == 3
    assert parsed.config["max_active"] == 7
    # The rest of the system definition survives the surgical config rewrite.
    assert parsed.config["lifecycle"] == "pool"
    assert "Do the work." in text
    assert "Be careful." in text
    assert parsed.frontmatter.id == "worker"
    # No project override is created any more.
    assert not (tmp_path / "data" / "vault" / "projects" / PROJECT_ID / "agent-types").exists()


async def test_pool_scale_db_row_matches_vault(pool_handler, tmp_path):
    res = await pool_handler._cmd_pool_scale({"profile_id": "worker", "min": 2, "max": 5})
    assert res["success"], res

    system = await pool_handler.db.get_profile("worker")
    assert (system.min_active, system.max_active) == (2, 5)
    assert await pool_handler.db.get_profile(f"project:{PROJECT_ID}:worker") is None


async def test_pool_scale_accepts_project_id_as_a_deprecated_no_op(pool_handler, tmp_path):
    """Existing scripts keep working, and are told the argument is ignored."""
    res = await pool_handler._cmd_pool_scale(
        {"project_id": PROJECT_ID, "profile_id": "worker", "min": 2, "max": 5}
    )
    assert res["success"], res
    assert res["warnings"] and "deprecated" in res["warnings"][0]
    system = await pool_handler.db.get_profile("worker")
    assert (system.min_active, system.max_active) == (2, 5)
    assert await pool_handler.db.get_profile(f"project:{PROJECT_ID}:worker") is None


async def test_pool_scale_survives_a_resync(pool_handler, tmp_path):
    """Re-syncing the vault must not revert the scale (the old bug)."""
    from src.profiles.sync import sync_profile_text_to_db

    await pool_handler._cmd_pool_scale({"profile_id": "worker", "min": 4, "max": 9})
    path = _system_path(tmp_path)

    result = await sync_profile_text_to_db(
        path.read_text(encoding="utf-8"),
        pool_handler.db,
        source_path=str(path),
        fallback_id="worker",
    )
    assert result.success, result.errors

    system = await pool_handler.db.get_profile("worker")
    assert (system.min_active, system.max_active) == (4, 9)


async def test_pool_scale_explicit_null_clears_max_in_db_and_vault(pool_handler, tmp_path):
    """An explicit ``max: None`` removes the cap — the CLI spells it `--max null`."""
    from src.profiles.parser import parse_profile

    await pool_handler._cmd_pool_scale({"profile_id": "worker", "min": 1, "max": 5})
    res = await pool_handler._cmd_pool_scale({"profile_id": "worker", "max": None})
    assert res["success"], res
    assert res["max_active"] is None

    parsed = parse_profile(_system_path(tmp_path).read_text(encoding="utf-8"))
    assert "max_active" not in parsed.config, "the key must be removed, not set to a string"
    assert parsed.config["min_active"] == 1

    system = await pool_handler.db.get_profile("worker")
    assert system.max_active is None
    assert system.min_active == 1


async def test_pool_scale_preserves_author_prose(pool_handler, tmp_path):
    """A second scale edits the system profile in place, preserving its prose."""
    from src.profiles.parser import parse_profile

    await pool_handler._cmd_pool_scale({"profile_id": "worker", "min": 1, "max": 3})
    path = _system_path(tmp_path)
    # Author-added content must survive the next write.
    path.write_text(path.read_text(encoding="utf-8") + "\n## Reflection\n\nKeep notes.\n")

    await pool_handler._cmd_pool_scale({"profile_id": "worker", "max": 8})

    text = path.read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config["max_active"] == 8
    assert parsed.config["min_active"] == 1, "min must be left alone when only max is passed"
    assert "Keep notes." in text


async def test_pool_lifecycle_is_global_durable_and_guarded(pool_handler, tmp_path):
    """Lifecycle lives on the system profile and a later sync must not revert it."""
    import time

    from src.event_bus import EventBus
    from src.models import SessionRecord
    from src.profiles.parser import parse_profile

    pool_handler.orchestrator.bus = EventBus(env="dev")

    await pool_handler.db.create_session(
        SessionRecord(
            id="drain-on-task-lifecycle",
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="fake",
            provider="fake",
            name="p-worker--proj--drain",
            lifecycle="pool",
            work_dir="/tmp/drain-on-task-lifecycle",
            epoch="test",
            instance_token="token",
            started_at=time.time(),
            state="running",
        )
    )
    changed = await pool_handler._cmd_pool_set_lifecycle(
        {"profile_id": "worker", "lifecycle": "task"}
    )
    assert changed == {
        "success": True,
        "profile_id": "worker",
        "lifecycle": "task",
        "warnings": [],
    }
    path = _system_path(tmp_path)
    assert parse_profile(path.read_text(encoding="utf-8")).config["lifecycle"] == "task"
    from src.profiles.sync import sync_profile_text_to_db

    resynced = await sync_profile_text_to_db(
        path.read_text(encoding="utf-8"),
        pool_handler.db,
        source_path=str(path),
        fallback_id="worker",
    )
    assert resynced.success, resynced.errors
    system = await pool_handler.db.get_profile("worker")
    assert system.min_active is system.max_active is system.max_claims_per_session is None
    # Every project's pool for the profile drains, not just one project's.
    drained = await pool_handler.db.get_session("drain-on-task-lifecycle")
    assert drained.desired_state == "stopped"

    pool_handler.config.swarm.enabled = False
    refused = await pool_handler._cmd_pool_set_lifecycle(
        {"profile_id": "worker", "lifecycle": "pool"}
    )
    assert refused == {
        "success": False,
        "error": "cannot set lifecycle to pool while swarm.enabled is false",
    }


async def test_pool_lifecycle_event_includes_the_request_project(pool_handler):
    """A global lifecycle edit emits schema-valid events for every project."""
    from src.event_bus import EventBus

    await pool_handler.db.create_project(Project(id="other-project", name="other"))
    events = []
    bus = EventBus(env="dev")
    bus.subscribe("pool.lifecycle_changed", events.append)
    pool_handler.orchestrator.bus = bus

    changed = await pool_handler._cmd_pool_set_lifecycle(
        {"project_id": PROJECT_ID, "profile_id": "worker", "lifecycle": "task"}
    )

    assert changed["success"] is True
    assert {
        (event["project_id"], event["profile_id"], event["lifecycle"])
        for event in events
    } == {
        (PROJECT_ID, "worker", "task"),
        ("other-project", "worker", "task"),
    }


async def test_pool_scale_rejects_zero_max_before_writing(pool_handler, tmp_path):
    """A parser-invalid zero max must not leave DB or vault state behind."""
    before = (await pool_handler.db.get_profile("worker"))
    zero = await pool_handler._cmd_pool_scale({"profile_id": "worker", "min": 0, "max": 0})
    assert zero == {"success": False, "error": "max must be >= 1"}
    after = await pool_handler.db.get_profile("worker")
    assert (after.min_active, after.max_active) == (before.min_active, before.max_active)


async def test_pool_scale_reports_each_project_cap(pool_handler):
    """Bounds are global; each project's cap is still the runtime maximum."""

    capped = await pool_handler._cmd_pool_scale({"profile_id": "worker", "min": 0, "max": 8})
    assert capped["success"], capped
    assert capped["max_active"] == 8
    assert capped["warnings"] == []
    caps = {row["project_id"]: row for row in capped["project_caps"]}
    assert caps[PROJECT_ID]["max_concurrent_agents"] == 2
    assert caps[PROJECT_ID]["effective_max_active"] == 2


async def test_pool_status_includes_live_instance_detail(pool_handler):
    """The dashboard needs the actual worker behind each aggregate row."""
    import time

    from src.models import SessionRecord

    now = time.time()
    await pool_handler.db.create_session(
        SessionRecord(
            id="pool-1",
            project_id=PROJECT_ID,
            profile_id="worker",
            harness="fake",
            provider="fake",
            name="p-worker--proj--deadbeef",
            lifecycle="pool",
            work_dir="/tmp/pool-1",
            epoch="test",
            instance_token="token",
            started_at=now - 30,
            last_activity=now - 12,
            state="running",
        )
    )

    result = await pool_handler._cmd_pool_status({"project_id": PROJECT_ID})
    instance = result["pools"][0]["instances"][0]
    assert instance == {
        "session_id": "pool-1",
        "name": "p-worker--proj--deadbeef",
        "state": "running",
        "task_id": None,
        "task_title": None,
        "idle_seconds": pytest.approx(12, abs=2),
        "started_at": pytest.approx(now - 30, abs=2),
        "quarantine_reason": None,
    }
