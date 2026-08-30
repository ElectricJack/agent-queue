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


def test_create_task_accepts_swarm_fields():
    props = defs()["create_task"]["input_schema"]["properties"]
    assert {"depends_on", "discovered_from", "dedup_key", "parent_id"} <= set(props)


def test_pool_commands_defined():
    d = defs()
    assert _TOOL_CATEGORIES["pool_status"] == _TOOL_CATEGORIES["pool_scale"] == "pool"
    assert {"project_id", "profile_id", "min", "max", "now"} <= set(
        d["pool_scale"]["input_schema"]["properties"]
    )


def test_read_claim_epoch_prefers_file(tmp_path, monkeypatch):
    from src.cli.agent_surface import read_claim_epoch

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

    monkeypatch.delenv("AQ_CLAIM_EPOCH", raising=False)
    (tmp_path / ".aq").mkdir()
    (tmp_path / ".aq" / "claim.json").write_text(json.dumps({"task_id": "t", "claim_epoch": 7}))
    deep = tmp_path / "src" / "pkg" / "sub"
    deep.mkdir(parents=True)
    assert read_claim_epoch(str(deep)) == 7


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
    res = await handler._cmd_pool_scale({"project_id": PROJECT_ID, "profile_id": "worker"})
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


def _override_path(tmp_path):
    return (
        tmp_path
        / "data"
        / "vault"
        / "projects"
        / PROJECT_ID
        / "agent-types"
        / "worker"
        / "profile.md"
    )


async def test_pool_scale_creates_project_override_in_vault(pool_handler, tmp_path):
    """The bounds land in the vault file, not just the agent_profiles row."""
    from src.profiles.parser import parse_profile

    path = _override_path(tmp_path)
    assert not path.exists()

    res = await pool_handler._cmd_pool_scale(
        {"project_id": PROJECT_ID, "profile_id": "worker", "min": 3, "max": 7}
    )
    assert res["success"], res
    assert (res["min_active"], res["max_active"]) == (3, 7)

    assert path.is_file(), "project-scoped override was not created"
    text = path.read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config["min_active"] == 3
    assert parsed.config["max_active"] == 7
    # The rest of the system definition carried over unchanged.
    assert parsed.config["lifecycle"] == "pool"
    assert "Do the work." in text
    assert "Be careful." in text
    # The override must own its own row, not upsert the system one.
    assert parsed.frontmatter.id == f"project:{PROJECT_ID}:worker"


async def test_pool_scale_db_row_matches_vault(pool_handler, tmp_path):
    res = await pool_handler._cmd_pool_scale(
        {"project_id": PROJECT_ID, "profile_id": "worker", "min": 2, "max": 5}
    )
    assert res["success"], res

    scoped = await pool_handler.db.get_profile(f"project:{PROJECT_ID}:worker")
    assert scoped is not None, "sync did not create the project-scoped row"
    assert (scoped.min_active, scoped.max_active) == (2, 5)


async def test_pool_scale_never_touches_the_system_row(pool_handler, tmp_path):
    """First scale with no override: the project row is created, the system
    row (shared by every other project) keeps its bounds."""
    before = await pool_handler.db.get_profile("worker")
    res = await pool_handler._cmd_pool_scale(
        {"project_id": PROJECT_ID, "profile_id": "worker", "min": 2, "max": 6}
    )
    assert res["success"], res
    system = await pool_handler.db.get_profile("worker")
    assert (system.min_active, system.max_active) == (before.min_active, before.max_active)
    scoped = await pool_handler.db.get_profile(f"project:{PROJECT_ID}:worker")
    assert (scoped.min_active, scoped.max_active, scoped.lifecycle) == (2, 6, "pool")


async def test_pool_scale_survives_a_resync(pool_handler, tmp_path):
    """Re-syncing the vault must not revert the scale (the old bug)."""
    from src.profiles.sync import sync_profile_text_to_db

    await pool_handler._cmd_pool_scale(
        {"project_id": PROJECT_ID, "profile_id": "worker", "min": 4, "max": 9}
    )
    path = _override_path(tmp_path)
    scoped_id = f"project:{PROJECT_ID}:worker"

    result = await sync_profile_text_to_db(
        path.read_text(encoding="utf-8"),
        pool_handler.db,
        source_path=str(path),
        fallback_id=scoped_id,
    )
    assert result.success, result.errors

    scoped = await pool_handler.db.get_profile(scoped_id)
    assert (scoped.min_active, scoped.max_active) == (4, 9)


async def test_pool_scale_updates_an_existing_override(pool_handler, tmp_path):
    """A second scale edits the override in place, preserving its prose."""
    from src.profiles.parser import parse_profile

    await pool_handler._cmd_pool_scale(
        {"project_id": PROJECT_ID, "profile_id": "worker", "min": 1, "max": 3}
    )
    path = _override_path(tmp_path)
    # Author-added content must survive the next write.
    path.write_text(path.read_text(encoding="utf-8") + "\n## Reflection\n\nKeep notes.\n")

    await pool_handler._cmd_pool_scale({"project_id": PROJECT_ID, "profile_id": "worker", "max": 8})

    text = path.read_text(encoding="utf-8")
    parsed = parse_profile(text)
    assert parsed.config["max_active"] == 8
    assert parsed.config["min_active"] == 1, "min must be left alone when only max is passed"
    assert "Keep notes." in text
